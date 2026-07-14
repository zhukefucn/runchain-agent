from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, AsyncIterator, Mapping
from urllib.parse import parse_qsl, urlencode
from uuid import uuid4

from fastapi import FastAPI, Request
from argon2 import PasswordHasher
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentscope.app import create_app as create_agentscope_app
from agentscope.app import deps as agentscope_deps
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.credential import OpenAICredential

from app.agents.factory import (
    build_model_runtime,
    build_runtime_agent_class,
)
from app.agents.hitl import HitlService
from app.agents.reception import (
    AgentScopeSubagentExecutor,
    ReceptionTeamRuntime,
    reception_subagent_templates,
)
from app.agentscope_ext.sqlite_storage import (
    RUNTIME_PLACEHOLDER_CREDENTIAL_ID,
    SQLiteStorage,
)
from app.agentscope_ext.workspace_manager import ManagerLocalWorkspaceManager
from app.api.auth import router as auth_router
from app.api.business import router as business_router
from app.api.health import router as health_router
from app.api.manager import router as manager_router
from app.api.system import router as system_router
from app.auth.security import decode_access_token
from app.auth.models import Principal
from app.auth.security import BANK_DEMO_TENANT_ID
from app.auth.deps import get_session, get_settings as auth_get_settings
from app.config import Settings
from app.db.models import McpAuthorizationRow, McpServerRow, Role, SessionRecordRow, SkillAuthorizationRow, User
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
            payload = row.storage_payload or {}
            config = payload.get("config") if isinstance(payload, dict) else None
            persisted_workspace_id = (
                config.get("workspace_id") if isinstance(config, dict) else None
            )
            return SimpleNamespace(
                owner_user_id=row.owner_user_id,
                agent_id=row.agent_id,
                session_id=row.id,
                workspace_id=(
                    persisted_workspace_id
                    or SQLiteStorage.workspace_id_for(
                        row.owner_user_id,
                        row.agent_id,
                    )
                ),
            )


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    request_id = str(uuid4())
    return JSONResponse(
        {"code": code, "message": message, "request_id": request_id},
        status_code=status_code,
        headers={"X-Request-ID": request_id},
    )


_IDENTITY_HEADERS = {b"x-user-id", b"x-owner-user-id", b"x-role"}
_IDENTITY_QUERY_KEYS = {"user_id", "owner_user_id", "role"}


def _sanitize_identity_scope(scope: dict[str, Any]) -> None:
    """Delete every client-controlled identity hint before dispatch."""
    if scope.get("type") not in {"http", "websocket"} or not scope.get(
        "path", ""
    ).startswith("/internal/agentscope"):
        return
    scope["headers"] = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() not in _IDENTITY_HEADERS
    ]
    query = parse_qsl(
        scope.get("query_string", b"").decode("utf-8"),
        keep_blank_values=True,
    )
    scope["query_string"] = urlencode(
        [
            (key, value)
            for key, value in query
            if key.lower() not in _IDENTITY_QUERY_KEYS
        ]
    ).encode("utf-8")


class _IdentitySanitizerMiddleware:
    """ASGI-level sanitizer so HTTP and any future websocket share policy."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        _sanitize_identity_scope(scope)
        await self.app(scope, receive, send)


async def _await_uncancellable(awaitable):
    """Finish cleanup despite repeated cancellation, then propagate it."""
    cleanup_task = asyncio.create_task(awaitable)
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    result = cleanup_task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def create_root_app(
    settings: Settings,
    overrides: Mapping[str, Any] | None = None,
    *,
    custom_subagent_templates: list[Any] | None = None,
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

    async def governed_reception_tools(
        owner_user_id: str, session_id: str, prompt: str, request_id: str
    ):
        async with sessions() as db:
            session = await db.get(SessionRecordRow, (owner_user_id, session_id))
            server = await db.scalar(
                select(McpServerRow)
                .join(
                    McpAuthorizationRow,
                    McpAuthorizationRow.server_id == McpServerRow.id,
                )
                .where(
                    McpAuthorizationRow.user_id == owner_user_id,
                    McpServerRow.status == "running",
                )
                .order_by(McpServerRow.created_at, McpServerRow.id)
            )
        if session is None:
            return {}
        paths: dict[str, Any] = {}
        python_skills = [
            skill
            for skill in await runtime["skill_service"].effective_skills(owner_user_id)
            if skill.type == "python"
        ]
        if python_skills:
            async with sessions() as db:
                authorization_rows = (
                    await db.execute(
                        select(
                            SkillAuthorizationRow.skill_id,
                            func.count(SkillAuthorizationRow.id),
                        )
                        .where(
                            SkillAuthorizationRow.skill_id.in_(
                                [item.id for item in python_skills]
                            )
                        )
                        .group_by(SkillAuthorizationRow.skill_id)
                    )
                ).all()
                authorization_counts = dict(authorization_rows)
            skill = max(
                python_skills,
                key=lambda item: (authorization_counts.get(item.id, 0), item.name),
            )

            async def dining(_owner: str, current_prompt: str):
                return await runtime["authorized_tool_service"].invoke_python_skill(
                    owner_user_id,
                    session.agent_id,
                    session_id,
                    skill.id,
                    {"prompt": current_prompt},
                    request_id=request_id,
                )

            paths["dining"] = dining
        if server is not None:
            principal = Principal(owner_user_id, Role.MANAGER, BANK_DEMO_TENANT_ID)

            async def pickup(_owner: str, _current_prompt: str):
                return await runtime["mcp_service"].call_tool(
                    principal,
                    server.id,
                    "plan_pickup",
                    {
                        "arrival_time": "2026-07-15T18:00:00+08:00",
                        "station": "南京南站",
                        "guest_count": 4,
                    },
                    request_id=request_id,
                )

            paths["pickup"] = pickup
        return paths

    async def extra_tools(user_id: str, agent_id: str, session_id: str):
        return await runtime["authorized_tool_service"].authorized_tools(
            user_id, agent_id, session_id
        )

    runtime_agent_cls = supplied.get("custom_agent_cls") or build_runtime_agent_class(
        lambda: runtime["model"]
    )
    templates = supplied.get("custom_subagent_templates", custom_subagent_templates)
    if templates is None:
        templates = reception_subagent_templates()
    agentscope_app = supplied.get("agentscope_app") or create_agentscope_app(
        storage=storage,
        message_bus=bus,
        workspace_manager=workspace,
        enable_index_worker=False,
        extra_agent_tools=extra_tools,
        custom_agent_cls=runtime_agent_cls,
        custom_subagent_templates=templates,
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
        async def close_resource(name: str, closer) -> None:
            try:
                await closer()
            except BaseException:
                logger.warning("Failed to close %s", name, exc_info=True)

        async def cleanup() -> None:
            # Registry completion is terminal before the database is disposed.
            while not registry.closed:
                await close_resource("mcp", registry.aclose)
            if skill_db is not None:
                await close_resource("skill database", skill_db.close)
            await close_resource("database engine", engine.dispose)

        agentscope_lifespan = None
        try:
            settings.workspace_root.mkdir(parents=True, exist_ok=True)
            settings.skill_root.mkdir(parents=True, exist_ok=True)
            app.state.runner_capabilities_verified = _verify_runner_capabilities(
                settings
            )
            await create_schema(engine)
            async with sessions() as db:
                await seed_demo_data(db)
                manager_ids = list(
                    await db.scalars(
                        select(User.id).where(User.role == Role.MANAGER)
                    )
                )
            for manager_id in manager_ids:
                await storage.upsert_credential(
                    manager_id,
                    OpenAICredential(
                        id=RUNTIME_PLACEHOLDER_CREDENTIAL_ID,
                        name="RunChain runtime placeholder",
                        api_key="non-secret-placeholder-never-called",
                        base_url="http://127.0.0.1:9/v1",
                    ),
                )

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
            runtime["skill_service"] = skill_service
            runtime["mcp_service"] = mcp_service
            app.state.skill_service = skill_service
            app.state.skill_executor = executor
            app.state.mcp_service = mcp_service
            app.state.authorized_tool_service = authorized
            model_runtime = build_model_runtime(settings)
            app.state.model = model_runtime.model
            app.state.model_configured = model_runtime.configured
            runtime["model"] = model_runtime.model

            agentscope_lifespan = agentscope_app.router.lifespan_context(
                agentscope_app
            )
            await agentscope_lifespan.__aenter__()
        except BaseException:
            await _await_uncancellable(cleanup())
            raise

        async def shutdown(exc_type=None, exc=None, traceback=None):
            # The mounted runtime owns resources that can still use the root
            # database.  Its whole exit therefore runs before root teardown,
            # in the same cancellation-resistant transaction.
            try:
                return await agentscope_lifespan.__aexit__(
                    exc_type, exc, traceback
                )
            finally:
                await cleanup()

        try:
            yield
        except BaseException as exc:
            suppressed = await _await_uncancellable(
                shutdown(type(exc), exc, exc.__traceback__)
            )
            if not suppressed:
                raise
        else:
            await _await_uncancellable(shutdown())

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
    app.state.model_configured = False
    app.state.password_hasher = supplied.get("password_hasher") or PasswordHasher()
    app.state.custom_subagent_templates = list(templates)
    app.state.reception_runtime = ReceptionTeamRuntime(
        sessions,
        templates=list(templates),
        storage=storage,
        message_bus=bus,
        workspace_manager=workspace,
        subagent_executor=AgentScopeSubagentExecutor(
            bus,
            chat_service_provider=lambda: agentscope_app.state.chat_service,
        ),
        governed_tool_provider=governed_reception_tools,
    )
    app.state.hitl_service = HitlService(sessions)
    app.add_middleware(_IdentitySanitizerMiddleware)
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(health_router)
    app.include_router(manager_router)
    app.include_router(business_router)
    app.include_router(system_router)

    async def local_session():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = local_session
    app.dependency_overrides[auth_get_settings] = lambda: settings

    @app.middleware("http")
    async def protect_agentscope(request: Request, call_next):
        if not request.url.path.startswith("/internal/agentscope"):
            return await call_next(request)
        native_manager_routes = (
            "/internal/agentscope/chat",
            "/internal/agentscope/agent",
            "/internal/agentscope/agents",
            "/internal/agentscope/sessions",
        )
        if not any(
            request.url.path == route
            or request.url.path.startswith(route + "/")
            for route in native_manager_routes
        ):
            return _error(404, "NOT_FOUND", "Endpoint not found")
        _sanitize_identity_scope(request.scope)
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

    @app.get("/{frontend_path:path}", include_in_schema=False)
    async def frontend(frontend_path: str):
        """Serve the production SPA without turning API misses into HTML."""
        if frontend_path == "api" or frontend_path.startswith("api/"):
            return _error(404, "NOT_FOUND", "接口不存在")
        if frontend_path == "internal" or frontend_path.startswith("internal/"):
            return _error(404, "NOT_FOUND", "接口不存在")

        dist = settings.frontend_dist.resolve()
        index = dist / "index.html"
        if not index.is_file():
            return _error(503, "FRONTEND_NOT_BUILT", "前端尚未构建")

        requested = (dist / frontend_path).resolve() if frontend_path else index
        try:
            requested.relative_to(dist)
        except ValueError:
            return _error(404, "NOT_FOUND", "页面不存在")
        if frontend_path and requested.is_file():
            return FileResponse(requested)
        if Path(frontend_path).suffix:
            return _error(404, "NOT_FOUND", "静态资源不存在")
        return FileResponse(index)

    return app


__all__ = ["create_root_app"]
