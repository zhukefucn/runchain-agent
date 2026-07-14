from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import urlsplit, urlunsplit

from app.auth.deps import get_session, require_role
from app.auth.models import Principal
from app.db.models import AuditRecordRow, Role, User
from app.errors import ApiError
from app.repositories.audit import AuditRepository, sanitize_audit_details


router = APIRouter(prefix="/api/system", tags=["system"])
SystemPrincipal = Annotated[Principal, Depends(require_role(Role.SYSTEM_ADMIN))]


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    role: Role
    is_active: bool = True


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def require_change(self):
        if self.role is None and self.is_active is None and self.password is None:
            raise ValueError("at least one change is required")
        return self


def _user(row: User) -> dict[str, Any]:
    return {
        "id": row.id,
        "username": row.username,
        "role": row.role.value,
        "is_active": row.is_active,
        "created_at": row.created_at,
    }


async def _audit_user_failure(
    request: Request,
    principal: Principal,
    action: str,
    resource_id: str | None,
    operation: str,
    status_code: int,
) -> None:
    async with request.app.state.session_factory() as audit_db:
        await AuditRepository(audit_db).record(
            actor_user_id=principal.user_id,
            action=action,
            resource_type="user",
            resource_id=resource_id,
            result="failure",
            request_id=request.state.request_id,
            details={
                "operation": operation,
                "status": "failure",
                "status_code": status_code,
            },
        )


def _safe_model_url(value: Any) -> str:
    parsed = urlsplit(str(value))
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


@router.get("/users")
async def list_users(
    _principal: SystemPrincipal,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows = list(
        await db.scalars(
            select(User).order_by(User.created_at, User.id).offset(offset).limit(limit)
        )
    )
    return {"items": [_user(row) for row in rows]}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    request: Request,
    principal: SystemPrincipal,
    db: AsyncSession = Depends(get_session),
):
    row = User(
        username=payload.username,
        password_hash=request.app.state.password_hasher.hash(payload.password),
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(row)
    try:
        await db.flush()
        AuditRepository(db).add_pending(
            actor_user_id=principal.user_id,
            action="system.user.create",
            resource_type="user",
            resource_id=row.id,
            result="success",
            request_id=request.state.request_id,
            details={"operation": "create", "role": payload.role.value},
        )
        await db.commit()
        await db.refresh(row)
    except IntegrityError as error:
        await db.rollback()
        await _audit_user_failure(
            request,
            principal,
            "system.user.create",
            row.id,
            "create",
            409,
        )
        raise ApiError(409, "USER_CONFLICT", "用户名已存在") from error
    return _user(row)


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str,
    payload: UserPatch,
    request: Request,
    principal: SystemPrincipal,
    db: AsyncSession = Depends(get_session),
):
    row = await db.get(User, user_id)
    if row is None:
        await _audit_user_failure(
            request,
            principal,
            "system.user.patch",
            user_id,
            "update",
            404,
        )
        raise ApiError(404, "NOT_FOUND", "资源不存在")
    if payload.role is not None:
        row.role = payload.role
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.password is not None:
        row.password_hash = request.app.state.password_hasher.hash(payload.password)
    AuditRepository(db).add_pending(
        actor_user_id=principal.user_id,
        action="system.user.patch",
        resource_type="user",
        resource_id=row.id,
        result="success",
        request_id=request.state.request_id,
        details={
            "operation": "update",
            "role": row.role.value,
            "status": "enabled" if row.is_active else "disabled",
        },
    )
    await db.commit()
    await db.refresh(row)
    return _user(row)


@router.get("/model/status")
async def model_status(request: Request, _principal: SystemPrincipal):
    settings = request.app.state.settings
    observed_connectivity = getattr(request.app.state, "model_connectivity", None)
    if observed_connectivity not in {"reachable", "unreachable"}:
        observed_connectivity = (
            "not_checked"
            if bool(request.app.state.model_configured)
            else "not_configured"
        )
    return {
        "configured": bool(request.app.state.model_configured),
        "model": settings.model_name,
        "base_url": _safe_model_url(settings.model_base_url),
        "connectivity": observed_connectivity,
    }


@router.get("/audit-logs")
async def audit_logs(
    _principal: SystemPrincipal,
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows = list(
        await db.scalars(
            select(AuditRecordRow)
            .order_by(AuditRecordRow.created_at.desc(), AuditRecordRow.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    return {
        "items": [
            {
                "id": row.id,
                "actor_user_id": row.actor_user_id,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "result": row.result,
                "request_id": row.request_id,
                "details": sanitize_audit_details(row.details),
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


__all__ = ["router"]
