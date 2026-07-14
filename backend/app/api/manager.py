from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.hitl import HitlConflict, HitlNotFound
from app.agents.sse import StableEvent, encode_sse
from app.auth.deps import get_session, require_role
from app.auth.models import Principal
from app.db.models import Role, WorkspaceFileRow
from app.errors import ApiError
from app.repositories.audit import AuditRepository
from app.repositories.manager import ManagerRepository


router = APIRouter(prefix="/api/manager", tags=["manager"])
ManagerPrincipal = Annotated[Principal, Depends(require_role(Role.MANAGER))]


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    agent_id: str = Field(default="reception-leader", min_length=1, max_length=100)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=10_000)


class HitlDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    modifications: dict[str, Any] | None = None


def _session(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "title": row.title,
        "status": row.status,
        "created_at": row.created_at,
    }


def _message(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "role": row.role,
        "content": row.content,
        "ordinal": row.ordinal,
        "created_at": row.created_at,
    }


def _manager_repository(request: Request, db: AsyncSession) -> ManagerRepository:
    return ManagerRepository(
        db,
        session_storage=request.app.state.storage,
        write_session_factory=request.app.state.session_factory,
    )


async def _audit(
    request: Request,
    actor: Principal,
    action: str,
    resource_type: str,
    resource_id: str | None,
    operation: str,
    *,
    result: str = "success",
    status_code: int | None = None,
) -> None:
    details: dict[str, Any] = {
        "operation": operation,
        "role": Role.MANAGER.value,
        "status": result,
    }
    if status_code is not None:
        details["status_code"] = status_code
    async with request.app.state.session_factory() as db:
        await AuditRepository(db).record(
            actor_user_id=actor.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            request_id=getattr(request.state, "request_id", None),
            details=details,
        )


@router.get("/sessions")
async def list_sessions(
    _request: Request,
    principal: ManagerPrincipal,
    db: AsyncSession = Depends(get_session),
):
    rows = await ManagerRepository(db).list_sessions(principal.user_id)
    return {"items": [_session(row) for row in rows]}


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate,
    request: Request,
    principal: ManagerPrincipal,
    db: AsyncSession = Depends(get_session),
):
    row = await _manager_repository(request, db).create_session(
        principal.user_id, payload.agent_id, payload.title
    )
    await _audit(
        request, principal, "manager.session.create", "session", row.id, "create"
    )
    return _session(row)


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    request: Request,
    principal: ManagerPrincipal,
    db: AsyncSession = Depends(get_session),
):
    repository = _manager_repository(request, db)
    if await repository.get_session(principal.user_id, session_id) is None:
        raise ApiError(404, "NOT_FOUND", "资源不存在")
    rows = await repository.list_messages(principal.user_id, session_id)
    return {"items": [_message(row) for row in rows]}


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    payload: ChatRequest,
    request: Request,
    principal: ManagerPrincipal,
    db: AsyncSession = Depends(get_session),
):
    repository = _manager_repository(request, db)
    if await repository.get_session(principal.user_id, session_id) is None:
        await _audit(
            request,
            principal,
            "manager.chat.start",
            "session",
            session_id,
            "invoke",
            result="failure",
            status_code=404,
        )
        raise ApiError(404, "NOT_FOUND", "资源不存在")
    if (
        await repository.create_message(
            principal.user_id, session_id, role="user", content=payload.prompt
        )
        is None
    ):
        await _audit(
            request,
            principal,
            "manager.chat.start",
            "session",
            session_id,
            "invoke",
            result="failure",
            status_code=404,
        )
        raise ApiError(404, "NOT_FOUND", "资源不存在")
    request_id = request.state.request_id
    await _audit(
        request, principal, "manager.chat.start", "session", session_id, "invoke"
    )

    async def stream():
        run_id = "unavailable"
        nested = request.app.state.reception_runtime.chat(
            principal.user_id,
            session_id,
            payload.prompt,
            request_id=request_id,
        )
        closer = aclosing(nested)
        await closer.__aenter__()
        try:
            try:
                async for event in nested:
                    run_id = event.run_id
                    if event.type in {"hitl_pending", "complete", "error"}:
                        try:
                            await _persist_terminal_message(
                                request, principal.user_id, session_id, event
                            )
                        except Exception:
                            error_event = StableEvent(
                                type="error",
                                request_id=request_id,
                                session_id=session_id,
                                run_id=run_id,
                                data={
                                    "code": "TERMINAL_PERSIST_FAILED",
                                    "message": "Terminal state persistence failed",
                                },
                            )
                            yield encode_sse(error_event)
                            return
                    yield encode_sse(event)
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:
                error_event = StableEvent(
                    type="error",
                    request_id=request_id,
                    session_id=session_id,
                    run_id=run_id,
                    data={
                        "code": "AGENT_RUNTIME_FAILED",
                        "message": "Agent runtime failed",
                    },
                )
                try:
                    await _persist_terminal_message(
                        request, principal.user_id, session_id, error_event
                    )
                except Exception:
                    pass
                yield encode_sse(error_event)
        finally:
            await _await_uncancellable(closer.__aexit__(None, None, None))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/hitl/{request_id}/decision")
async def decide_hitl(
    request_id: str,
    payload: HitlDecisionRequest,
    request: Request,
    principal: ManagerPrincipal,
):
    try:
        result = await request.app.state.hitl_service.decide(
            principal.user_id,
            request_id,
            payload.decision,
            payload.modifications,
        )
    except HitlNotFound as error:
        await _audit(
            request, principal, "manager.hitl.decision", "hitl_request", request_id,
            "decide", result="failure", status_code=404,
        )
        raise ApiError(404, "NOT_FOUND", "资源不存在") from error
    except HitlConflict as error:
        await _audit(
            request, principal, "manager.hitl.decision", "hitl_request", request_id,
            "decide", result="failure", status_code=409,
        )
        raise ApiError(409, "HITL_CONFLICT", "该审批请求已处理") from error
    except ValueError as error:
        await _audit(
            request, principal, "manager.hitl.decision", "hitl_request", request_id,
            "decide", result="failure", status_code=422,
        )
        raise ApiError(422, "VALIDATION_ERROR", "HITL 决策参数无效") from error
    await _audit(
        request,
        principal,
        "manager.hitl.decision",
        "hitl_request",
        request_id,
        "decide",
    )
    return {
        "request_id": result.request_id,
        "status": result.status,
        "events": [event.model_dump(mode="json") for event in result.events],
    }


@router.get("/skills")
async def list_authorized_skills(
    request: Request,
    principal: ManagerPrincipal,
):
    rows = await request.app.state.skill_service.effective_skills(principal.user_id)
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "version": row.version,
                "type": row.type,
                "description": row.description,
                "warnings": list(row.warnings),
            }
            for row in rows
        ]
    }


@router.get("/files")
async def list_files(
    request: Request,
    principal: ManagerPrincipal,
    db: AsyncSession = Depends(get_session),
    session_id: str | None = Query(default=None, max_length=100),
):
    statement = select(WorkspaceFileRow).where(
        WorkspaceFileRow.owner_user_id == principal.user_id
    )
    if session_id is not None:
        if (
            await _manager_repository(request, db).get_session(
                principal.user_id, session_id
            )
            is None
        ):
            raise ApiError(404, "NOT_FOUND", "资源不存在")
        statement = statement.where(WorkspaceFileRow.session_id == session_id)
    rows = list(
        await db.scalars(
            statement.order_by(WorkspaceFileRow.created_at, WorkspaceFileRow.id)
        )
    )
    return {
        "items": [
            {
                "id": row.id,
                "session_id": row.session_id,
                "relative_path": row.relative_path,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


__all__ = ["router"]
async def _await_uncancellable(awaitable) -> None:
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _persist_terminal_message(
    request: Request,
    owner_user_id: str,
    session_id: str,
    terminal: StableEvent,
) -> None:
    async with request.app.state.session_factory() as writer:
        await ManagerRepository(
            writer,
            write_session_factory=request.app.state.session_factory,
        ).create_message(
            owner_user_id,
            session_id,
            role="assistant",
            content=json.dumps(
                terminal.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
