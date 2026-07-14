from __future__ import annotations

import logging
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from typing import Any, AsyncIterator, Mapping
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentscope.app import create_app as create_agentscope_app
from agentscope.app import deps as agentscope_deps
from agentscope.app.message_bus import InMemoryMessageBus

from app.agents.factory import build_model
from app.agentscope_ext.sqlite_storage import SQLiteStorage
from app.agentscope_ext.workspace_manager import ManagerLocalWorkspaceManager
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.auth.security import decode_access_token
from app.auth.deps import get_session, get_settings as auth_get_settings
from app.config import Settings
from app.db.models import Role, SessionRecordRow, User
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema
from app.errors import install_error_handlers
from app.mcp.service import McpRuntimeRegistry, McpService
from app.runner.controlled_process import RunnerLimits, SkillExecutor, _WindowsJob
from app.runner.resolver import SkillServiceResolver
from app.runner.windows_acl import secure_runner_root
from app.skills.service import SkillService
from app.skills.tool_adapter import AuthorizedToolService


logger = logging.getLogger(__name__)


def _verify_runner_capabilities(settings: Settings) -> bool:
    """Verify the Phase 1 ACL and Windows Job Object boundary at startup."""
    secure_runner_root(settings.runner_root)
    if os.name != "nt":
        return settings.app_env == "test"
    job = _WindowsJob(RunnerLimits())
    if not job.close():
        raise OSError("Windows Job Object handle could not be closed")
    return True


class _SessionResolver:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def resolve_session(self, owner_user_id: str, session_id: str):
        async with self._sessions() as db:
            row = await db.get(SessionRecordRow, (owner_user_id, session_id))
            if row is None:
                return None
            return row


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    request_id = str(uuid4())
    return JSONResponse(
        {"code": code, "message": message, "request_id": request_id},
        status_code=status_code,
        headers={"X-Request-ID": request_id},
    )


def create_root_app(
    settings: Settings,
    overrides: Mapping[str, Any] | None = None,
) -> FastAPI:
    """Create one isolated application runtime; no mutable runtime is global."""
    supplied = dict(overrides or {})
    engine = supplied.get("engine") or build_async_engine(settings.database_url)
    sessions = supplied.get("session_factory") or async_sessionmaker(
        engine, expire_on_commit=False
    )
    storage = supplied.get("storage") or SQLiteStorage(sessions)
    bus = supplied.get("message_bus") or InMemoryMessageBus()
    workspace = supplied.get("workspace_manager") or ManagerLocalWorkspaceManager(
        settings.workspace_root, _SessionResolver(sessions)
    )
    mcp_root = Path(settings.mcp_root).resolve()
    registry = supplied.get("mcp_registry") or McpRuntimeRegistry(
        application_namespace=f"runchain-{uuid4()}",
        server_root=mcp_root,
        python_executable=Path(sys.executable),
        session_factory=sessions,
    )

    runtime: dict[str, Any] = {}

    async def extra_tools(user_id: str, agent_id: str, session_id: str):
        return await runtime["authorized_tool_service"].authorized_tools(
            user_id, agent_id, session_id
        )

    agentscope_app = supplied.get("agentscope_app") or create_agentscope_app(
        storage=storage,
        message_bus=bus,
        workspace_manager=workspace,
        enable_index_worker=False,
        extra_agent_tools=extra_tools,
        title="RunChain AgentScope Internal",
    )

    async def verified_agentscope_identity(request: Request) -> str:
        user_id = getattr(request.state, "verified_user_id", None)
        if not user_id:
            raise PermissionError("verified AgentScope identity is missing")
        return user_id

    agentscope_app.dependency_overrides[
        agentscope_deps.get_current_user_id
    ] = verified_agentscope_identity

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        skill_db: AsyncSession | None = None
        registry_closed = False
        skill_db_closed = False

        async def close_resource(name: str, closer) -> bool:
            try:
                await closer()
                return True
            except Exception:
                logger.warning("Failed to close %s", name, exc_info=True)
                return False

        try:
            settings.workspace_root.mkdir(parents=True, exist_ok=True)
            settings.skill_root.mkdir(parents=True, exist_ok=True)
            app.state.runner_capabilities_verified = _verify_runner_capabilities(
                settings
            )
            await create_schema(engine)
            async with sessions() as db:
                await seed_demo_data(db)

            skill_db = sessions()
            skill_service = supplied.get("skill_service") or SkillService(
                skill_db,
                settings.skill_root,
                read_session_factory=sessions,
            )
            executor = supplied.get("skill_executor") or SkillExecutor(
                resolver=SkillServiceResolver(skill_service),
                runner_root=settings.runner_root,
            )
            mcp_service = supplied.get("mcp_service") or McpService(
                skill_db,
                runtime_registry=registry,
                application_namespace=registry.application_namespace,
                server_root=mcp_root,
                python_executable=Path(sys.executable),
                session_factory=sessions,
            )
            authorized = supplied.get("authorized_tool_service") or AuthorizedToolService(
                sessions, skill_service, executor, mcp_service, registry
            )
            runtime["authorized_tool_service"] = authorized
            app.state.skill_service = skill_service
            app.state.skill_executor = executor
            app.state.mcp_service = mcp_service
            app.state.authorized_tool_service = authorized
            app.state.model = build_model(settings)

            async with agentscope_app.router.lifespan_context(agentscope_app):
                try:
                    yield
                finally:
                    # Tool runtimes depend on database/workspace resources and
                    # therefore terminate while those resources are still live.
                    registry_closed = await close_resource("mcp", registry.aclose)
                    skill_db_closed = await close_resource(
                        "skill database", skill_db.close
                    )
        finally:
            if not registry_closed:
                await close_resource("mcp", registry.aclose)
            if skill_db is not None and not skill_db_closed:
                await close_resource("skill database", skill_db.close)
            await close_resource("database engine", engine.dispose)

    app = FastAPI(title="RunChain Multi-tenant Agent", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessions
    app.state.storage = storage
    app.state.message_bus = bus
    app.state.workspace_manager = workspace
    app.state.mcp_registry = registry
    app.state.agentscope_app = agentscope_app
    app.state.runner_capabilities_verified = False
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(health_router)

    async def local_session():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = local_session
    app.dependency_overrides[auth_get_settings] = lambda: settings

    @app.middleware("http")
    async def protect_agentscope(request: Request, call_next):
        if not request.url.path.startswith("/internal/agentscope"):
            return await call_next(request)
        # Client-provided identity headers are always deleted before dispatch.
        request.scope["headers"] = [
            (key, value)
            for key, value in request.scope.get("headers", [])
            if key.lower() != b"x-user-id"
        ]
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return _error(401, "INVALID_TOKEN", "登录凭证无效")
        try:
            principal = decode_access_token(
                authorization.removeprefix("Bearer ").strip(), settings=settings
            )
            async with sessions() as db:
                user = await db.scalar(
                    select(User).where(
                        or_(User.id == principal.user_id, User.username == principal.user_id)
                    )
                )
            if user is None or not user.is_active or user.role != principal.role:
                raise PermissionError
        except Exception:
            return _error(401, "INVALID_TOKEN", "登录凭证无效")
        if user.role != Role.MANAGER:
            return _error(403, "ROLE_FORBIDDEN", "当前角色无权运行 Agent")
        request.state.verified_user_id = user.id
        return await call_next(request)

    app.mount("/internal/agentscope", agentscope_app)
    return app


__all__ = ["create_root_app"]
