from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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
        assert (await client.get("/api/health")).json() == {"status": "ok"}
        ready = await client.get("/api/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
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
        assert failed.json()["components"]["workspace"]["status"] == "failed"
        assert str(tmp_path) not in failed.text
        settings.workspace_root = original_workspace

        app.state.runner_capabilities_verified = False
        failed = await client.get("/api/ready")
        assert failed.status_code == 503
        assert failed.json()["components"]["runner_capability"]["status"] == "failed"

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
