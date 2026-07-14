from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentscope.agent import Agent
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse

from app.config import Settings
from app.db.models import (
    McpAuthorizationRow,
    McpServerRow,
    Role,
    SessionRecordRow,
    User,
)
from app.db.session import build_async_engine, create_schema
from app.db.seed import seed_demo_data


def _settings(tmp_path: Path, **changes) -> Settings:
    values = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "workspace_root": tmp_path / "workspace",
        "skill_root": tmp_path / "skills",
        "runner_root": tmp_path / "runner",
        "jwt_secret_key": "test-jwt-secret-with-enough-entropy",
        "model_api_key": "",
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


async def _seed_session(settings: Settings, username: str, *, agent_id="agent-a", session_id="session-a"):
    engine = build_async_engine(settings.database_url)
    await create_schema(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await seed_demo_data(db)
        user = await db.scalar(select(User).where(User.username == username))
        assert user is not None
        db.add(SessionRecordRow(id=session_id, owner_user_id=user.id, agent_id=agent_id))
        await db.commit()
        return engine, sessions, user


def test_fake_model_builds_real_agentscope_agent_and_round_trips(tmp_path):
    from app.agents.factory import build_agent, build_model

    model = build_model(_settings(tmp_path))
    agent = build_agent(model=model, tools=[])
    assert isinstance(agent, Agent)
    reply = asyncio.run(agent.reply(UserMsg("user", "hello")))
    assert "hello" in "".join(
        block.text for block in reply.content if isinstance(block, TextBlock)
    )


def test_real_model_factory_passes_secret_once_without_leaking(monkeypatch, tmp_path, caplog):
    from app.agents import factory

    captured = {}

    class StubCredential:
        def __init__(self, **kwargs):
            captured["credential"] = kwargs

    class StubModel:
        def __init__(self, **kwargs):
            captured["model"] = kwargs

    monkeypatch.setattr(factory, "OpenAICredential", StubCredential)
    monkeypatch.setattr(factory, "OpenAIChatModel", StubModel)
    secret = "top-secret-model-key"
    settings = _settings(
        tmp_path,
        app_env="real",
        model_api_key=secret,
        model_base_url="https://example.invalid/v1",
    )
    model = factory.build_model(settings)
    assert isinstance(model, StubModel)
    assert captured["credential"]["api_key"] == secret
    assert captured["credential"]["base_url"] == "https://example.invalid/v1"
    assert captured["model"]["model"] == "step-3.7-flash"
    assert secret not in caplog.text
    assert secret not in repr(model)


def test_authorized_tools_are_fresh_isolated_and_capture_trusted_identity(tmp_path):
    from app.skills.tool_adapter import AuthorizedToolService

    async def scenario():
        settings = _settings(tmp_path)
        engine, sessions, manager1 = await _seed_session(settings, "manager0001")
        async with sessions() as db:
            manager2 = await db.scalar(select(User).where(User.username == "manager0002"))
            assert manager2 is not None
            db.add(SessionRecordRow(id="session-b", owner_user_id=manager2.id, agent_id="agent-a"))
            await db.commit()

        class Skills:
            async def effective_skills(self, user_id):
                if user_id != manager1.id:
                    return ()
                return (SimpleNamespace(
                    id="skill-1", name="private-demo", version="1", type="python",
                    description="private", parameters_json="{}", install_path="",
                ),)

            async def resolve_execution_skill(self, user_id, skill_id, version=None):
                if user_id != manager1.id or skill_id != "skill-1":
                    raise LookupError("denied")
                return SimpleNamespace(id=skill_id, version="1")

        calls = []

        class Executor:
            async def execute(self, request):
                calls.append(request)
                return SimpleNamespace(status="success", output={"ok": True}, stderr_summary="", duration_ms=1, exit_code=0)

        service = AuthorizedToolService(
            session_factory=sessions,
            skill_service=Skills(),
            skill_executor=Executor(),
            mcp_service=None,
            mcp_registry=None,
        )
        first = await service.authorized_tools(manager1.id, "agent-a", "session-a")
        second = await service.authorized_tools(manager1.id, "agent-a", "session-a")
        other = await service.authorized_tools(manager2.id, "agent-a", "session-b")
        assert [tool.name for tool in first] == ["private_demo"]
        assert first[0] is not second[0]
        assert [tool.name for tool in other] == []
        chunk = await first[0](input_data={"value": 1})
        assert json.loads(chunk.content[0].text)["output"] == {"ok": True}
        assert calls[0].user_id == manager1.id
        assert calls[0].skill_id == "skill-1"
        assert not hasattr(calls[0], "owner_user_id")
        await engine.dispose()

    asyncio.run(scenario())


def test_authorized_tools_reject_admin_and_session_owner_mismatch(tmp_path):
    from app.skills.tool_adapter import AuthorizedToolService

    async def scenario():
        settings = _settings(tmp_path)
        engine, sessions, manager = await _seed_session(settings, "manager0001")
        async with sessions() as db:
            admin = await db.scalar(select(User).where(User.role == Role.SYSTEM_ADMIN))
            assert admin is not None
        service = AuthorizedToolService(sessions, SimpleNamespace(), SimpleNamespace(), None, None)
        for user_id in (admin.id, manager.id):
            try:
                await service.authorized_tools(user_id, "wrong-agent", "session-a")
            except PermissionError:
                pass
            else:
                raise AssertionError("untrusted agent/session binding was accepted")
        await engine.dispose()

    asyncio.run(scenario())


def test_mcp_tool_closure_reauthorizes_with_trusted_manager(tmp_path):
    from app.skills.tool_adapter import AuthorizedToolService

    async def scenario():
        settings = _settings(tmp_path)
        engine, sessions, manager = await _seed_session(settings, "manager0001")
        async with sessions() as db:
            admin = await db.scalar(select(User).where(User.role == Role.BUSINESS_ADMIN))
            assert admin is not None
            server = McpServerRow(
                created_by_user_id=admin.id,
                name="pickup",
                configuration={},
                executable_sha256="0" * 64,
                script_sha256="1" * 64,
                status="running",
            )
            db.add(server)
            await db.flush()
            db.add(McpAuthorizationRow(
                server_id=server.id,
                user_id=manager.id,
                granted_by_user_id=admin.id,
            ))
            await db.commit()

        advertised = SimpleNamespace(
            name="plan-pickup",
            description="mock pickup",
            inputSchema={
                "type": "object",
                "properties": {"station": {"type": "string"}},
                "required": ["station"],
            },
        )
        runtime = SimpleNamespace(
            session=object(), task=SimpleNamespace(done=lambda: False),
            tools={advertised.name: advertised},
        )

        class Registry:
            async def current(self, server_id):
                return runtime

        calls = []

        class Mcp:
            async def call_tool(self, principal, server_id, tool_name, arguments):
                calls.append((principal, server_id, tool_name, arguments))
                return {"pickup": arguments["station"]}

        class Skills:
            async def effective_skills(self, user_id):
                return ()

        service = AuthorizedToolService(sessions, Skills(), object(), Mcp(), Registry())
        tools = await service.authorized_tools(manager.id, "agent-a", "session-a")
        assert [tool.name for tool in tools] == ["plan_pickup"]
        chunk = await tools[0](station="West")
        assert json.loads(chunk.content[0].text) == {"pickup": "West"}
        principal, _, _, arguments = calls[0]
        assert principal.user_id == manager.id
        assert principal.role == Role.MANAGER
        assert arguments == {"station": "West"}
        await engine.dispose()

    asyncio.run(scenario())


def test_tool_name_collision_fails_closed(tmp_path):
    from app.skills.tool_adapter import AuthorizedToolService

    async def scenario():
        settings = _settings(tmp_path)
        engine, sessions, manager = await _seed_session(settings, "manager0001")

        class Skills:
            async def effective_skills(self, _user_id):
                base = dict(
                    version="1", type="python", description="x",
                    parameters_json='{"type":"object","properties":{}}',
                )
                return (
                    SimpleNamespace(id="1", name="same-name", **base),
                    SimpleNamespace(id="2", name="same_name", **base),
                )

        service = AuthorizedToolService(sessions, Skills(), object(), None, None)
        try:
            await service.authorized_tools(manager.id, "agent-a", "session-a")
        except PermissionError as error:
            assert "collision" in str(error)
        else:
            raise AssertionError("canonical collision was accepted")
        await engine.dispose()

    asyncio.run(scenario())


def test_root_app_liveness_readiness_identity_bridge_and_clean_lifespan(tmp_path):
    from app.auth.models import Principal
    from app.main import create_root_app

    async def scenario():
        settings = _settings(tmp_path)
        app = create_root_app(settings)
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                await check(client, app, settings)
        assert app.state.mcp_registry.closed is True

    async def check(client, app, settings):
        from agentscope.agent import ContextConfig, ReActConfig
        from agentscope.app._router._session import stream_session_events
        from agentscope.app.storage import (
            AgentData,
            AgentRecord,
            ChatModelConfig,
            SessionConfig,
        )
        from agentscope.credential import OpenAICredential

        assert (await client.get("/api/health")).json() == {"status": "ok"}
        settings.model_api_key = SimpleNamespace(
            get_secret_value=lambda: (_ for _ in ()).throw(
                AssertionError("readiness must not unseal model credentials")
            )
        )
        ready = await client.get("/api/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.json()["components"]["database"]["detail"] == (
            "schema, migration head, and rollback-only write verified"
        )
        assert all("\\" not in json.dumps(item) for item in ready.json()["components"].values())

        login = await client.post(
            "/api/auth/login",
            json={"username": "manager0001", "password": "12345678"},
        )
        token = login.json()["access_token"]
        me = (await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )).json()
        principal = Principal(me["user_id"], Role(me["role"]), me["tenant_id"])
        response = await client.get(
            "/internal/agentscope/agent/",
            headers={"Authorization": f"Bearer {token}", "X-User-ID": "forged"},
        )
        assert response.status_code == 200
        assert (await client.get("/internal/agentscope/agent/", headers={"X-User-ID": principal.user_id})).status_code == 401

        # Exercise AgentScope's native ChatService, not only our model factory.
        agent_id = "native-fake-agent"
        session_id = "native-fake-session"
        credential_id = await app.state.storage.upsert_credential(
            principal.user_id,
            OpenAICredential(
                name="placeholder-only",
                api_key="unused",
                base_url="https://example.invalid/v1",
            ),
        )
        await app.state.storage.upsert_agent(
            principal.user_id,
            AgentRecord(
                id=agent_id,
                user_id=principal.user_id,
                data=AgentData(
                    name="Native fake",
                    context_config=ContextConfig(),
                    react_config=ReActConfig(),
                ),
            ),
        )
        await app.state.storage.upsert_session(
            principal.user_id,
            agent_id,
            SessionConfig(
                workspace_id=app.state.workspace_manager.assign_workspace_id(
                    user_id=principal.user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                ),
                chat_model_config=ChatModelConfig(
                    type="openai_credential",
                    credential_id=credential_id,
                    model="must-not-be-called",
                    parameters={},
                ),
            ),
            session_id=session_id,
        )

        # Native workspace management is deliberately not part of the demo's
        # manager surface. Skills and MCP servers are installed/authorized only
        # through the governed business-admin APIs.
        workspace_query = f"?agent_id={agent_id}&session_id={session_id}"
        workspace_requests = (
            ("GET", "/internal/agentscope/workspace"),
            ("GET", "/internal/agentscope/workspace/"),
            ("GET", f"/internal/agentscope/workspace/mcp{workspace_query}"),
            ("POST", f"/internal/agentscope/workspace/mcp{workspace_query}"),
            (
                "DELETE",
                f"/internal/agentscope/workspace/mcp/native{workspace_query}",
            ),
            ("GET", f"/internal/agentscope/workspace/skill{workspace_query}"),
            ("POST", f"/internal/agentscope/workspace/skill{workspace_query}"),
            (
                "DELETE",
                f"/internal/agentscope/workspace/skill/native{workspace_query}",
            ),
        )
        for method, path in workspace_requests:
            denied = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                json={} if method == "POST" else None,
            )
            assert denied.status_code == 404, (method, path, denied.text)

        # Subscribe through the actual SSE endpoint before the run; AgentScope
        # deliberately trims a completed run's replay buffer.
        stream = await stream_session_events(
            session_id,
            agent_id=agent_id,
            user_id=principal.user_id,
            storage=app.state.storage,
            message_bus=app.state.message_bus,
        )

        async def receive_fake_event():
            async for event in stream.body_iterator:
                if "fake: hello native" in event:
                    return event

        sse_event = asyncio.create_task(receive_fake_event())
        await asyncio.sleep(0.02)
        started = await client.post(
            "/internal/agentscope/chat/"
            "?user_id=forged&owner_user_id=forged&role=system_admin",
            headers={
                "Authorization": f"Bearer {token}",
                "X-User-ID": "forged",
                "X-Owner-User-ID": "forged",
                "X-Role": "system_admin",
            },
            json={
                "agent_id": agent_id,
                "session_id": session_id,
                "input": UserMsg("user", "hello native").model_dump(mode="json"),
            },
        )
        assert started.status_code == 200, started.text
        messages = []
        for _ in range(100):
            messages = await app.state.storage.list_messages(
                principal.user_id, session_id
            )
            if "fake: hello native" in json.dumps(
                [item.model_dump(mode="json") for item in messages]
            ):
                break
            await asyncio.sleep(0.02)
        serialized = json.dumps([item.model_dump(mode="json") for item in messages])
        assert "fake: hello native" in serialized
        assert "fake: hello native" in await asyncio.wait_for(sse_event, 2)
        await stream.body_iterator.aclose()

        manager2_token = (await client.post(
            "/api/auth/login",
            json={"username": "manager0002", "password": "12345678"},
        )).json()["access_token"]
        manager2_me = (await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {manager2_token}"}
        )).json()
        async with app.state.session_factory() as db:
            db.add(SessionRecordRow(
                id="manager-two-session",
                owner_user_id=manager2_me["user_id"],
                agent_id="agent-a",
            ))
            await db.commit()
        assert (await client.delete(
            "/internal/agentscope/sessions/manager-two-session?agent_id=agent-a",
            headers={
                "Authorization": f"Bearer {token}",
                "X-User-ID": manager2_me["user_id"],
            },
        )).status_code == 404
        assert (await client.delete(
            "/internal/agentscope/sessions/manager-two-session?agent_id=agent-a",
            headers={"Authorization": f"Bearer {manager2_token}"},
        )).status_code == 204
        admin_token = (await client.post(
            "/api/auth/login",
            json={"username": "system_admin01", "password": "12345678"},
        )).json()["access_token"]
        assert (await client.get(
            "/internal/agentscope/agents",
            headers={"Authorization": f"Bearer {admin_token}"},
        )).status_code == 403

        original_workspace = settings.workspace_root
        unavailable = tmp_path / "not-a-directory"
        unavailable.write_text("x", encoding="utf-8")
        settings.workspace_root = unavailable
        failed = await client.get("/api/ready")
        assert failed.status_code == 503
        assert failed.json()["code"] == "NOT_READY"
        assert failed.json()["message"]
        assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
        assert failed.json()["components"]["workspace"]["status"] == "failed"
        assert str(tmp_path) not in failed.text
        settings.workspace_root = original_workspace

        app.state.runner_capabilities_verified = False
        failed = await client.get("/api/ready")
        assert failed.status_code == 503
        assert failed.json()["components"]["runner_capability"]["status"] == "failed"

        # Readiness is schema-aware and its write probe never leaves a row.
        app.state.runner_capabilities_verified = True
        async with app.state.session_factory() as db:
            before = await db.scalar(select(func.count()).select_from(User))
        assert (await client.get("/api/ready")).status_code == 200
        async with app.state.session_factory() as db:
            after = await db.scalar(select(func.count()).select_from(User))
            await db.execute(text("DELETE FROM alembic_version"))
            await db.commit()
        assert before == after
        stale_schema = await client.get("/api/ready")
        assert stale_schema.status_code == 503
        assert stale_schema.json()["components"]["database"] == {
            "status": "failed",
            "detail": "schema verification failed",
        }

    asyncio.run(scenario())


def test_root_app_disposes_engine_when_startup_fails(monkeypatch, tmp_path):
    from app.main import create_root_app

    app = create_root_app(_settings(tmp_path))
    dispose = AsyncMock()
    monkeypatch.setattr(type(app.state.engine), "dispose", dispose)

    async def fail_schema(_engine):
        raise RuntimeError("startup failed")

    monkeypatch.setattr("app.main.create_schema", fail_schema)

    async def scenario():
        with pytest.raises(RuntimeError, match="startup failed"):
            async with app.router.lifespan_context(app):
                pass

    asyncio.run(scenario())
    dispose.assert_awaited_once()


def test_root_app_passes_subagent_templates_and_sanitizes_identity_scope(tmp_path):
    from app.main import _sanitize_identity_scope, create_root_app

    template = SimpleNamespace(type="pickup")
    app = create_root_app(
        _settings(tmp_path), custom_subagent_templates=[template]
    )
    assert app.state.agentscope_app.state.custom_subagent_templates == {
        "pickup": template
    }

    for scope_type in ("http", "websocket"):
        scope = {
            "type": scope_type,
            "path": "/internal/agentscope/chat/",
            "headers": [
                (b"x-user-id", b"forged"),
                (b"x-owner-user-id", b"forged"),
                (b"x-role", b"system_admin"),
                (b"authorization", b"Bearer trusted"),
            ],
            "query_string": (
                b"user_id=forged&owner_user_id=forged&role=system_admin&agent_id=a"
            ),
        }
        _sanitize_identity_scope(scope)
        assert scope["headers"] == [(b"authorization", b"Bearer trusted")]
        assert scope["query_string"] == b"agent_id=a"


def test_uncancellable_cleanup_finishes_after_repeated_cancellation():
    from app.main import _await_uncancellable

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        events = []

        async def cleanup():
            started.set()
            await release.wait()
            events.append("registry-terminal")
            finished.set()
            events.append("engine-disposed")

        task = asyncio.create_task(_await_uncancellable(cleanup()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert events == ["registry-terminal", "engine-disposed"]

    asyncio.run(scenario())


def test_root_lifespan_keeps_agentscope_exit_and_root_cleanup_uncancellable(
    monkeypatch, tmp_path
):
    """AgentScope resources close before root resources despite repeated cancel."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from app.main import create_root_app

    async def scenario():
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        events: list[str] = []

        @asynccontextmanager
        async def fake_agentscope_lifespan(_app):
            try:
                yield
            finally:
                close_started.set()
                await allow_close.wait()
                events.append("agentscope-closed")

        agentscope_app = FastAPI(lifespan=fake_agentscope_lifespan)
        app = create_root_app(
            _settings(tmp_path), overrides={"agentscope_app": agentscope_app}
        )
        engine_type = type(app.state.engine)
        original_dispose = engine_type.dispose

        async def recording_dispose(engine):
            events.append("engine-disposed")
            await original_dispose(engine)

        monkeypatch.setattr(engine_type, "dispose", recording_dispose)

        async def run_lifespan():
            async with app.router.lifespan_context(app):
                pass

        task = asyncio.create_task(run_lifespan())
        await close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not app.state.mcp_registry.closed
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app.state.mcp_registry.closed
        assert events == ["agentscope-closed", "engine-disposed"]

    asyncio.run(scenario())
