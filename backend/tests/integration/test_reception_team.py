from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path
import re

import pytest
from sqlalchemy import select
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentscope.app import SubAgentTemplate

from app.config import Settings
from app.db.models import (
    HitlRequestRow,
    SessionRecordRow,
    TeamNodeRunRow,
    TeamRunRow,
    User,
)
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema
from app.agentscope_ext.sqlite_storage import SQLiteStorage


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


async def _runtime_db(tmp_path: Path):
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reception.db'}")
    await create_schema(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await seed_demo_data(db)
        users = {
            row.username: row
            for row in await db.scalars(
                select(User).where(User.username.in_(("manager0001", "manager0002")))
            )
        }
        for index, username in enumerate(("manager0001", "manager0002"), 1):
            db.add(
                SessionRecordRow(
                    id=f"reception-session-{index}",
                    owner_user_id=users[username].id,
                    agent_id="reception-leader",
                )
            )
        await db.commit()
    return engine, sessions, users


@_async_test
async def test_reception_team_uses_real_templates_and_three_mock_tool_paths(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime, reception_subagent_templates

    engine, sessions, users = await _runtime_db(tmp_path)
    runtime = ReceptionTeamRuntime(sessions)
    manager = users["manager0001"]

    events = [
        event
        async for event in runtime.chat(
            manager.id,
            "reception-session-1",
            "接待 4 位远方客人，明天 18:00 到站，偏好清淡餐饮",
            request_id="request-manager-1",
        )
    ]

    templates = reception_subagent_templates()
    assert all(isinstance(template, SubAgentTemplate) for template in templates)
    assert {template.type for template in templates} == {"pickup", "lodging", "dining"}
    assert runtime.tool_path_calls == [
        ("pickup", "mock_mcp"),
        ("lodging", "in_process_mock_tool"),
        ("dining", "authorized_python_skill"),
    ]
    assert hasattr(runtime.tool_paths["pickup"], "call_tool")
    assert hasattr(runtime.tool_paths["dining"], "execute")
    assert events[0].type == "run_started"
    assert {event.agent_type for event in events if event.type == "agent_completed"} == {
        "pickup",
        "lodging",
        "dining",
    }
    assert events[-1].type == "hitl_pending"
    assert all(
        event.request_id == "request-manager-1"
        and event.session_id == "reception-session-1"
        and event.run_id
        and event.timestamp
        for event in events
    )

    async with sessions() as db:
        run = await db.scalar(select(TeamRunRow).where(TeamRunRow.owner_user_id == manager.id))
        nodes = list(
            await db.scalars(
                select(TeamNodeRunRow)
                .where(TeamNodeRunRow.team_run_id == run.id)
                .order_by(TeamNodeRunRow.agent_type)
            )
        )
        hitl = await db.scalar(
            select(HitlRequestRow).where(HitlRequestRow.team_run_id == run.id)
        )
    assert run.status == "hitl_pending"
    assert run.started_at is not None and run.completed_at is None
    assert {node.agent_type for node in nodes} == {"pickup", "lodging", "dining"}
    assert all(
        node.status == "completed"
        and node.started_at is not None
        and node.completed_at is not None
        and node.error_code is None
        for node in nodes
    )
    assert hitl is not None and hitl.status == "pending"

    storage = SQLiteStorage(sessions)
    leader_session = await storage.get_session(
        manager.id, "reception-leader", "reception-session-1"
    )
    assert leader_session.team_id is not None
    team = await storage.get_team(manager.id, leader_session.team_id)
    assert team is not None
    assert len(team.data.members) == 3
    worker_sessions = [
        await storage.get_session(manager.id, member.agent_id, member.session_id)
        for member in team.data.members
    ]
    assert re.fullmatch(r"[0-9a-f]{16}", leader_session.config.workspace_id)
    assert leader_session.config.chat_model_config is not None
    assert (
        leader_session.config.chat_model_config.credential_id
        == "runchain-runtime-placeholder"
    )
    assert all(
        worker is not None
        and worker.config.workspace_id == leader_session.config.workspace_id
        for worker in worker_sessions
    )
    worker_agents = [
        await storage.get_agent(manager.id, member.agent_id)
        for member in team.data.members
    ]
    assert all(agent is not None and agent.source == "team" for agent in worker_agents)
    assert {
        marker
        for marker in ("pickup", "lodging", "dining")
        if any(
            f"[{marker}-template]" in agent.data.system_prompt
            for agent in worker_agents
            if agent is not None
        )
    } == {"pickup", "lodging", "dining"}
    await engine.dispose()


@_async_test
async def test_both_managers_run_independently_and_sse_encoding_is_stable(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime
    from app.agents.sse import encode_sse

    engine, sessions, users = await _runtime_db(tmp_path)
    runtime = ReceptionTeamRuntime(sessions)

    async def execute(username: str, session_id: str):
        return [
            event
            async for event in runtime.chat(
                users[username].id,
                session_id,
                f"{username} 的接待安排",
                request_id=f"request-{username}",
            )
        ]

    manager1_events, manager2_events = await asyncio.gather(
        execute("manager0001", "reception-session-1"),
        execute("manager0002", "reception-session-2"),
    )
    assert {event.run_id for event in manager1_events}.isdisjoint(
        {event.run_id for event in manager2_events}
    )
    wire = encode_sse(manager1_events[-1])
    assert wire.startswith("event: hitl_pending\ndata: ")
    assert wire.endswith("\n\n")
    assert '"request_id":"request-manager0001"' in wire

    async with sessions() as db:
        manager1_runs = list(
            await db.scalars(
                select(TeamRunRow).where(TeamRunRow.owner_user_id == users["manager0001"].id)
            )
        )
        manager2_runs = list(
            await db.scalars(
                select(TeamRunRow).where(TeamRunRow.owner_user_id == users["manager0002"].id)
            )
        )
    assert len(manager1_runs) == len(manager2_runs) == 1
    await engine.dispose()


@_async_test
async def test_cancelled_reception_run_persists_terminal_node_state(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_pickup(_owner_user_id, _prompt):
        entered.set()
        await release.wait()
        return {"vehicle": "mock-van"}

    runtime = ReceptionTeamRuntime(sessions, pickup_tool=slow_pickup)

    async def consume():
        return [
            event
            async for event in runtime.chat(
                users["manager0001"].id,
                "reception-session-1",
                "cancel this reception",
                request_id="cancel-request",
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == "cancel-request")
        )
        nodes = list(
            await db.scalars(
                select(TeamNodeRunRow).where(TeamNodeRunRow.team_run_id == run.id)
            )
        )
    assert run.status == "cancelled" and run.completed_at is not None
    assert nodes and all(node.status != "running" for node in nodes)
    await engine.dispose()


@_async_test
async def test_repeated_cancel_during_finalize_still_persists_terminal_state(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)
    tool_entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def slow_pickup(_owner, _prompt):
        tool_entered.set()
        await asyncio.Event().wait()

    async def before_finalize():
        cleanup_entered.set()
        await cleanup_release.wait()

    runtime = ReceptionTeamRuntime(
        sessions,
        pickup_tool=slow_pickup,
        before_cancel_finalize=before_finalize,
    )
    task = asyncio.create_task(
        _collect(
            runtime.chat(
                users["manager0001"].id,
                "reception-session-1",
                "repeat cancel",
                request_id="repeat-cancel",
            )
        )
    )
    await asyncio.wait_for(tool_entered.wait(), timeout=3)
    task.cancel()
    await asyncio.wait_for(cleanup_entered.wait(), timeout=3)
    task.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == "repeat-cancel")
        )
    assert run.status == "cancelled" and run.completed_at is not None
    await engine.dispose()


@_async_test
async def test_reception_subagents_enter_tools_concurrently(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)
    entered: set[str] = set()
    all_entered = asyncio.Event()
    release = asyncio.Event()

    def tool(agent_type):
        async def run(_owner, _prompt):
            entered.add(agent_type)
            if len(entered) == 3:
                all_entered.set()
            await release.wait()
            return {"agent_type": agent_type}

        return run

    runtime = ReceptionTeamRuntime(
        sessions,
        pickup_tool=tool("pickup"),
        lodging_tool=tool("lodging"),
        dining_skill=tool("dining"),
    )
    consume = asyncio.create_task(
        _collect(
            runtime.chat(
                users["manager0001"].id,
                "reception-session-1",
                "parallel",
                request_id="parallel-request",
            )
        )
    )
    await asyncio.wait_for(all_entered.wait(), timeout=2)
    assert entered == {"pickup", "lodging", "dining"}
    release.set()
    assert (await consume)[-1].type == "hitl_pending"
    await engine.dispose()


async def _collect(events):
    return [event async for event in events]


@_async_test
async def test_closing_stream_persists_cancelled_run(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)
    stream = ReceptionTeamRuntime(sessions).chat(
        users["manager0001"].id,
        "reception-session-1",
        "disconnect",
        request_id="disconnect-request",
    )
    for _ in range(8):
        await anext(stream)
    await stream.aclose()
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == "disconnect-request")
        )
    assert run.status == "cancelled" and run.completed_at is not None
    await engine.dispose()


@_async_test
async def test_disconnect_after_hitl_event_keeps_persisted_pending_state(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)
    stream = ReceptionTeamRuntime(sessions).chat(
        users["manager0001"].id,
        "reception-session-1",
        "keep pending",
        request_id="pending-disconnect",
    )
    while (event := await anext(stream)).type != "hitl_pending":
        pass
    await stream.aclose()
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == "pending-disconnect")
        )
        hitl = await db.scalar(
            select(HitlRequestRow).where(HitlRequestRow.team_run_id == run.id)
        )
    assert run.status == "hitl_pending" and run.completed_at is None
    assert hitl.status == "pending"
    await engine.dispose()


@_async_test
async def test_cancel_at_hitl_commit_boundary_preserves_pending_state(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)
    committed = asyncio.Event()
    release = asyncio.Event()

    async def after_commit():
        committed.set()
        await release.wait()

    runtime = ReceptionTeamRuntime(sessions, after_hitl_commit=after_commit)
    task = asyncio.create_task(
        _collect(
            runtime.chat(
                users["manager0001"].id,
                "reception-session-1",
                "boundary cancellation",
                request_id="hitl-boundary",
            )
        )
    )
    await asyncio.wait_for(committed.wait(), timeout=3)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == "hitl-boundary")
        )
        hitl = await db.scalar(
            select(HitlRequestRow).where(HitlRequestRow.team_run_id == run.id)
        )
    assert run.status == "hitl_pending" and run.completed_at is None
    assert hitl.status == "pending"
    await engine.dispose()


@_async_test
async def test_cross_manager_cannot_run_reception_in_foreign_session(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)
    with pytest.raises(PermissionError):
        await _collect(
            ReceptionTeamRuntime(sessions).chat(
                users["manager0002"].id,
                "reception-session-1",
                "foreign session",
                request_id="foreign-run",
            )
        )
    async with sessions() as db:
        assert await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == "foreign-run")
        ) is None
    storage = SQLiteStorage(sessions)
    assert await storage.list_teams(users["manager0002"].id) == []
    await engine.dispose()


@_async_test
async def test_failed_team_node_emits_sanitized_error_and_persists_timestamp(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    engine, sessions, users = await _runtime_db(tmp_path)

    async def broken_lodging(_owner_user_id, _prompt):
        raise RuntimeError("private traceback and secret")

    events = [
        event
        async for event in ReceptionTeamRuntime(
            sessions, lodging_tool=broken_lodging
        ).chat(
            users["manager0001"].id,
            "reception-session-1",
            "failure path",
            request_id="failed-request",
        )
    ]
    assert events[-1].type == "error"
    assert "secret" not in events[-1].model_dump_json()
    async with sessions() as db:
        node = await db.scalar(
            select(TeamNodeRunRow).where(
                TeamNodeRunRow.agent_type == "lodging",
                TeamNodeRunRow.owner_user_id == users["manager0001"].id,
            )
        )
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == "failed-request")
        )
    assert node.status == "error" and node.completed_at is not None
    assert node.error_code == "MOCK_TOOL_FAILED"
    assert run.status == "error" and run.completed_at is not None
    await engine.dispose()


async def _assert_setup_failure_is_sanitized_and_terminal(
    sessions, owner_user_id: str, request_id: str, events
):
    assert [event.type for event in events] == ["run_started", "error"]
    assert events[-1].data["code"] == "TEAM_SETUP_FAILED"
    assert "secret" not in events[-1].model_dump_json().lower()
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.request_id == request_id)
        )
        nodes = list(
            await db.scalars(
                select(TeamNodeRunRow).where(
                    TeamNodeRunRow.team_run_id == run.id,
                    TeamNodeRunRow.owner_user_id == owner_user_id,
                )
            )
        )
    assert run.status == "error" and run.completed_at is not None
    assert run.result_data == {
        "error_code": "TEAM_SETUP_FAILED",
        "agentscope_artifacts": "retained_for_audit",
    }
    assert len(nodes) == 3
    assert all(
        node.status == "error"
        and node.completed_at is not None
        and node.error_code == "TEAM_SETUP_FAILED"
        for node in nodes
    )


@_async_test
async def test_team_create_failure_is_sanitized_and_terminal(tmp_path):
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolChunk
    from app.agents.reception import ReceptionTeamRuntime

    class FailingTeamCreate:
        def __init__(self, **_kwargs):
            pass

        async def __call__(self, **_kwargs):
            return ToolChunk(
                content=[TextBlock(text="secret=team-create-key")],
                state=ToolResultState.ERROR,
            )

    engine, sessions, users = await _runtime_db(tmp_path)
    owner = users["manager0001"].id
    events = await _collect(
        ReceptionTeamRuntime(
            sessions, team_create_factory=FailingTeamCreate
        ).chat(
            owner,
            "reception-session-1",
            "team creation failure",
            request_id="failed-team-create",
        )
    )
    await _assert_setup_failure_is_sanitized_and_terminal(
        sessions, owner, "failed-team-create", events
    )
    await engine.dispose()


@_async_test
async def test_partial_agent_create_failure_is_sanitized_and_terminal(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime
    from app.agentscope_ext.team_tools import AgentCreate

    class PartiallyFailingAgentCreate:
        def __init__(self, **kwargs):
            self._delegate = AgentCreate(**kwargs)
            self._calls = 0

        async def __call__(self, **kwargs):
            self._calls += 1
            if self._calls == 2:
                raise RuntimeError("secret=partial-agent-key")
            return await self._delegate(**kwargs)

    engine, sessions, users = await _runtime_db(tmp_path)
    owner = users["manager0001"].id
    events = await _collect(
        ReceptionTeamRuntime(
            sessions, agent_create_factory=PartiallyFailingAgentCreate
        ).chat(
            owner,
            "reception-session-1",
            "partial agent failure",
            request_id="failed-partial-agent-create",
        )
    )
    await _assert_setup_failure_is_sanitized_and_terminal(
        sessions, owner, "failed-partial-agent-create", events
    )
    storage = SQLiteStorage(sessions)
    leader = await storage.get_session(
        owner, "reception-leader", "reception-session-1"
    )
    team = await storage.get_team(owner, leader.team_id)
    assert len(team.data.members) == 1
    await engine.dispose()


@_async_test
async def test_agent_create_error_chunk_after_roster_write_is_terminal(tmp_path):
    from agentscope.app.message_bus import InMemoryMessageBus
    from app.agents.reception import ReceptionTeamRuntime

    class FailingRunTriggerBus(InMemoryMessageBus):
        def __init__(self):
            super().__init__()
            self.queue_push_calls = 0

        async def queue_push(self, key, payload, *, ttl_secs=None):
            self.queue_push_calls += 1
            if self.queue_push_calls == 2:
                raise RuntimeError("secret=run-trigger-key")
            return await super().queue_push(
                key, payload, ttl_secs=ttl_secs
            )

    engine, sessions, users = await _runtime_db(tmp_path)
    owner = users["manager0001"].id
    events = await _collect(
        ReceptionTeamRuntime(
            sessions, message_bus=FailingRunTriggerBus()
        ).chat(
            owner,
            "reception-session-1",
            "message bus failure",
            request_id="failed-agent-error-chunk",
        )
    )
    await _assert_setup_failure_is_sanitized_and_terminal(
        sessions, owner, "failed-agent-error-chunk", events
    )
    assert all(event.type != "hitl_pending" for event in events)
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(
                TeamRunRow.request_id == "failed-agent-error-chunk"
            )
        )
        assert await db.scalar(
            select(HitlRequestRow).where(HitlRequestRow.team_run_id == run.id)
        ) is None
    storage = SQLiteStorage(sessions)
    leader = await storage.get_session(
        owner, "reception-leader", "reception-session-1"
    )
    team = await storage.get_team(owner, leader.team_id)
    assert len(team.data.members) == 1
    await engine.dispose()


@_async_test
async def test_repeated_cancel_during_setup_failure_finalize_stays_terminal(tmp_path):
    from app.agents.reception import ReceptionTeamRuntime

    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    class FailingTeamCreate:
        def __init__(self, **_kwargs):
            pass

        async def __call__(self, **_kwargs):
            raise RuntimeError("secret=cancel-during-failure")

    async def before_finalize():
        cleanup_entered.set()
        await cleanup_release.wait()

    engine, sessions, users = await _runtime_db(tmp_path)
    owner = users["manager0001"].id
    task = asyncio.create_task(
        _collect(
            ReceptionTeamRuntime(
                sessions,
                team_create_factory=FailingTeamCreate,
                before_setup_failure_finalize=before_finalize,
            ).chat(
                owner,
                "reception-session-1",
                "cancel failure finalize",
                request_id="cancel-failed-setup",
            )
        )
    )
    await asyncio.wait_for(cleanup_entered.wait(), timeout=3)
    task.cancel()
    task.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(
                TeamRunRow.request_id == "cancel-failed-setup"
            )
        )
        nodes = list(
            await db.scalars(
                select(TeamNodeRunRow).where(
                    TeamNodeRunRow.team_run_id == run.id
                )
            )
        )
    assert run.status == "error" and run.completed_at is not None
    assert all(node.status == "error" for node in nodes)
    await engine.dispose()


def test_root_app_injects_reception_templates_by_default(tmp_path, monkeypatch):
    from app.main import create_root_app

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        workspace_root=tmp_path / "workspace",
        skill_root=tmp_path / "skills",
        runner_root=tmp_path / "runner",
        jwt_secret_key="test-jwt-secret-with-enough-entropy",
        model_api_key="",
    )
    app = create_root_app(settings)
    assert {template.type for template in app.state.custom_subagent_templates} == {
        "pickup",
        "lodging",
        "dining",
    }
    assert app.state.reception_runtime.templates == app.state.custom_subagent_templates
    assert set(app.state.agentscope_app.state.custom_subagent_templates) >= {
        "pickup",
        "lodging",
        "dining",
    }
    assert app.state.hitl_service is not None


def test_alembic_upgrades_task10_schema_with_node_and_hitl_lifecycle_columns(tmp_path):
    from app.db.migrations import downgrade_database_url, upgrade_database_url

    database = tmp_path / "upgrade.db"
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE sessions (id VARCHAR(100), owner_user_id VARCHAR(36), "
                "PRIMARY KEY (owner_user_id, id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE team_runs (id VARCHAR(36) PRIMARY KEY, session_id VARCHAR(100), "
                "owner_user_id VARCHAR(36), status VARCHAR(32), created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE hitl_requests (id VARCHAR(36) PRIMARY KEY, team_run_id VARCHAR(36), "
                "owner_user_id VARCHAR(36), prompt TEXT, status VARCHAR(32), created_at DATETIME)"
            )
        )
    upgrade_database_url(url)

    schema = inspect(engine)
    assert "team_node_runs" in schema.get_table_names()
    assert {column["name"] for column in schema.get_columns("team_runs")} >= {
        "request_id",
        "started_at",
        "completed_at",
        "result_data",
    }
    assert {column["name"] for column in schema.get_columns("hitl_requests")} >= {
        "decision",
        "modifications",
        "result_data",
        "decided_at",
    }
    assert "alembic_version" in schema.get_table_names()
    downgrade_database_url(url)
    schema = inspect(engine)
    assert "team_node_runs" not in schema.get_table_names()
    assert "request_id" not in {
        column["name"] for column in schema.get_columns("team_runs")
    }
    assert "decision" not in {
        column["name"] for column in schema.get_columns("hitl_requests")
    }
    engine.dispose()


def test_alembic_upgrade_then_create_schema_on_fresh_empty_database(tmp_path):
    from app.db.migrations import upgrade_database_url

    database = tmp_path / "fresh-empty.db"
    sync_url = f"sqlite:///{database}"
    upgrade_database_url(sync_url)
    engine = create_engine(sync_url)
    assert "alembic_version" in inspect(engine).get_table_names()
    engine.dispose()

    async def create_current_schema():
        async_engine = build_async_engine(f"sqlite+aiosqlite:///{database}")
        await create_schema(async_engine)
        await async_engine.dispose()

    asyncio.run(create_current_schema())
    schema = inspect(create_engine(sync_url))
    assert {"team_runs", "team_node_runs", "hitl_requests"} <= set(
        schema.get_table_names()
    )
