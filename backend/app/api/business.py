from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_session, require_role
from app.auth.models import Principal
from app.db.models import McpServerRow, Role, SkillInvocationRow, SkillRow
from app.errors import ApiError
from app.mcp.service import (
    McpConflictError,
    McpNotFoundError,
    McpPermissionError,
    McpUnavailableError,
    McpValidationError,
)
from app.repositories.audit import AuditRepository
from app.skills.package import MAX_UPLOAD_BYTES, SkillPackageError
from app.skills.service import (
    SkillCleanupError,
    SkillConflictError,
    SkillNotFoundError,
    SkillPermissionError,
)


router = APIRouter(prefix="/api/business", tags=["business"])
BusinessPrincipal = Annotated[
    Principal, Depends(require_role(Role.BUSINESS_ADMIN))
]


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manager_user_id: str = Field(min_length=1, max_length=100)


class McpCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=1000)
    args: list[str]
    env: dict[str, str] = Field(default_factory=dict)


def _skill(row: SkillRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "type": row.type,
        "status": row.status,
        "description": row.description,
        "entrypoint": row.entrypoint,
        "validation_warnings": row.validation_warnings,
        "created_at": row.created_at,
    }


def _mcp(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "status": row.status,
        "last_error": row.last_error,
        "created_at": row.created_at,
    }


def _skill_error(error: Exception) -> ApiError:
    if isinstance(error, SkillNotFoundError):
        return ApiError(404, "NOT_FOUND", "资源不存在")
    if isinstance(error, SkillPermissionError):
        return ApiError(403, "ROLE_FORBIDDEN", "当前角色无权访问")
    if isinstance(error, (SkillPackageError, SkillConflictError, SkillCleanupError)):
        return ApiError(409, "SKILL_INVALID", "Skill 包无效或状态冲突")
    return ApiError(500, "INTERNAL_ERROR", "服务器内部错误")


async def _audit_failure(
    request: Request,
    actor: Principal,
    action: str,
    resource_type: str,
    resource_id: str | None,
    operation: str,
    status_code: int,
) -> None:
    async with request.app.state.session_factory() as db:
        await AuditRepository(db).record(
            actor_user_id=actor.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result="failure",
            request_id=request.state.request_id,
            details={
                "operation": operation,
                "status": "failure",
                "status_code": status_code,
            },
        )


@router.get("/skills")
async def list_skills(
    _principal: BusinessPrincipal,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows = list(
        await db.scalars(
            select(SkillRow)
            .order_by(SkillRow.created_at, SkillRow.id)
            .offset(offset)
            .limit(limit)
        )
    )
    return {"items": [_skill(row) for row in rows]}


@router.post("/skills/upload", status_code=status.HTTP_201_CREATED)
async def upload_skill(request: Request, principal: BusinessPrincipal):
    if request.headers.get("content-type", "").split(";", 1)[0] not in {
        "application/zip",
        "application/octet-stream",
    }:
        await _audit_failure(
            request, principal, "skill.install", "skill", None, "install", 415
        )
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "仅接受本地 ZIP 文件")
    try:
        declared_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared_length = 0
    if declared_length > MAX_UPLOAD_BYTES:
        await _audit_failure(
            request, principal, "skill.install", "skill", None, "install", 413
        )
        raise ApiError(413, "UPLOAD_TOO_LARGE", "Skill ZIP 超过大小限制")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            await _audit_failure(
                request, principal, "skill.install", "skill", None, "install", 413
            )
            raise ApiError(413, "UPLOAD_TOO_LARGE", "Skill ZIP 超过大小限制")
        chunks.append(chunk)
    upload = b"".join(chunks)
    try:
        row = await request.app.state.skill_service.install(
            principal, upload, request_id=request.state.request_id
        )
    except Exception as error:
        api_error = _skill_error(error)
        await _audit_failure(
            request, principal, "skill.install", "skill", None, "install", api_error.status_code
        )
        raise api_error from error
    return _skill(row)


@router.post("/skills/{skill_id}/publish")
async def publish_skill(
    skill_id: str, request: Request, principal: BusinessPrincipal
):
    try:
        row = await request.app.state.skill_service.publish(
            principal, skill_id, request_id=request.state.request_id
        )
    except Exception as error:
        api_error = _skill_error(error)
        await _audit_failure(
            request, principal, "skill.publish", "skill", skill_id, "update", api_error.status_code
        )
        raise api_error from error
    return _skill(row)


@router.post("/skills/{skill_id}/disable")
async def disable_skill(
    skill_id: str, request: Request, principal: BusinessPrincipal
):
    try:
        row = await request.app.state.skill_service.disable(
            principal, skill_id, request_id=request.state.request_id
        )
    except Exception as error:
        api_error = _skill_error(error)
        await _audit_failure(
            request, principal, "skill.disable", "skill", skill_id, "update", api_error.status_code
        )
        raise api_error from error
    return _skill(row)


@router.post("/skills/{skill_id}/authorizations")
async def authorize_skill(
    skill_id: str,
    payload: AuthorizationRequest,
    request: Request,
    principal: BusinessPrincipal,
):
    try:
        row = await request.app.state.skill_service.authorize(
            principal,
            skill_id,
            payload.manager_user_id,
            request_id=request.state.request_id,
        )
    except Exception as error:
        api_error = _skill_error(error)
        await _audit_failure(
            request, principal, "skill.authorize", "skill", skill_id, "authorize", api_error.status_code
        )
        raise api_error from error
    return {"id": row.id, "skill_id": row.skill_id, "user_id": row.user_id}


@router.get("/skill-invocations")
async def list_skill_invocations(
    _principal: BusinessPrincipal,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows = list(
        await db.scalars(
            select(SkillInvocationRow)
            .order_by(SkillInvocationRow.created_at.desc(), SkillInvocationRow.id)
            .offset(offset)
            .limit(limit)
        )
    )
    return {
        "items": [
            {
                "id": row.id,
                "skill_id": row.skill_id,
                "owner_user_id": row.owner_user_id,
                "session_id": row.session_id,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@router.get("/mcp-servers")
async def list_mcp_servers(
    _principal: BusinessPrincipal,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows = list(
        await db.scalars(
            select(McpServerRow)
            .order_by(McpServerRow.created_at, McpServerRow.id)
            .offset(offset)
            .limit(limit)
        )
    )
    return {"items": [_mcp(row) for row in rows]}


def _mcp_error(error: Exception) -> ApiError:
    if isinstance(error, McpNotFoundError):
        return ApiError(404, "NOT_FOUND", "资源不存在")
    if isinstance(error, McpPermissionError):
        return ApiError(403, "ROLE_FORBIDDEN", "当前角色无权访问")
    if isinstance(error, McpValidationError):
        return ApiError(422, "MCP_INVALID", "MCP 配置无效")
    if isinstance(error, McpConflictError):
        return ApiError(409, "MCP_CONFLICT", "MCP 配置冲突")
    if isinstance(error, McpUnavailableError):
        return ApiError(503, "MCP_UNAVAILABLE", "MCP Server 不可用")
    return ApiError(500, "INTERNAL_ERROR", "服务器内部错误")


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    payload: McpCreateRequest,
    request: Request,
    principal: BusinessPrincipal,
):
    try:
        row = await request.app.state.mcp_service.register_local(
            principal,
            name=payload.name,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            request_id=request.state.request_id,
        )
    except Exception as error:
        api_error = _mcp_error(error)
        await _audit_failure(
            request, principal, "mcp.register", "mcp_server", None, "create", api_error.status_code
        )
        raise api_error from error
    return _mcp(row)


@router.post("/mcp-servers/{server_id}/test")
async def test_mcp_server(
    server_id: str,
    request: Request,
    principal: BusinessPrincipal,
):
    service = request.app.state.mcp_service
    try:
        await service.start(principal, server_id, request_id=request.state.request_id)
        healthy = await service.health(
            principal, server_id, request_id=request.state.request_id
        )
        tools = (
            await service.list_tools(
                principal, server_id, request_id=request.state.request_id
            )
            if healthy
            else []
        )
    except Exception as error:
        api_error = _mcp_error(error)
        if isinstance(error, McpNotFoundError):
            await _audit_failure(
                request, principal, "mcp.test", "mcp_server", server_id, "connect", api_error.status_code
            )
        raise api_error from error
    return {"server_id": server_id, "healthy": healthy, "tools": tools}


__all__ = ["router"]
