from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import Principal
from app.db.models import AuditRecordRow, McpAuthorizationRow, McpServerRow, Role, User
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema
from app.mcp.service import (
    McpConflictError,
    McpPermissionError,
    McpRuntimeRegistry,
    McpService,
    McpValidationError,
    McpUnavailableError,
)
from app.repositories.audit import AuditRepository


def _principal(user: User) -> Principal:
    return Principal(user_id=user.id, role=user.role, tenant_id="bank_demo")


async def _scenario(tmp_path: Path, check, **service_options) -> None:
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}")
    try:
        await create_schema(engine)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await seed_demo_data(db)
            users = {user.username: user for user in (await db.scalars(select(User))).all()}
            options = {
                "start_timeout": 8,
                "call_timeout": 3,
                "max_output_bytes": 8_192,
                **service_options,
            }
            service = McpService(
                db,
                server_root=Path(__file__).parents[2] / "app" / "mcp",
                python_executable=Path(sys.executable),
                session_factory=async_sessionmaker(db.bind, expire_on_commit=False),
                **options,
            )
            try:
                await check(service, users, db)
            finally:
                await service.aclose()
    finally:
        await engine.dispose()


async def _register(service: McpService, users: dict[str, User]) -> McpServerRow:
    return await service.register_local(
        _principal(users["business_admin01"]),
        name="pickup-planner",
        command=str(Path(sys.executable).resolve()),
        args=["mock_pickup_server.py"],
        env={},
    )


def test_local_mcp_round_trip_discovery_health_and_restart(tmp_path):
    async def check(service, users, db):
        server = await _register(service, users)
        await service.authorize(
            _principal(users["business_admin01"]), server.id, users["manager0001"].id
        )
        await asyncio.gather(
            service.start(_principal(users["business_admin01"]), server.id),
            service.start(_principal(users["system_admin01"]), server.id),
        )
        assert await service.health(_principal(users["business_admin01"]), server.id)
        tools = await service.list_tools(_principal(users["business_admin01"]), server.id)
        assert [tool["name"] for tool in tools] == ["plan_pickup"]

        result = await service.call_tool(
            _principal(users["manager0001"]),
            server.id,
            "plan_pickup",
            {
                "arrival_time": "2026-07-15T14:00:00+08:00",
                "station": "南京南站",
                "guest_count": 3,
            },
        )
        assert result["mock"] is True
        assert result["guest_count"] == 3
        assert result["station"] == "南京南站"
        assert result["vehicle"] and result["driver"] and result["timeline"]

        await service.stop(_principal(users["business_admin01"]), server.id)
        assert not await service.health(_principal(users["business_admin01"]), server.id)
        await service.start(_principal(users["business_admin01"]), server.id)
        assert await service.health(_principal(users["business_admin01"]), server.id)
        actions = {row.action for row in await db.scalars(select(AuditRecordRow))}
        assert {"mcp.health", "mcp.list_tools", "mcp.call"}.issubset(actions)

    asyncio.run(_scenario(tmp_path, check))


def test_registration_is_local_structured_hashed_and_audited_atomically(tmp_path):
    async def check(service, users, db):
        server = await _register(service, users)
        script = Path(__file__).parents[2] / "app" / "mcp" / "mock_pickup_server.py"
        assert server.created_by_user_id == users["business_admin01"].id
        assert server.executable_sha256 == hashlib.sha256(
            Path(sys.executable).read_bytes()
        ).hexdigest()
        assert server.script_sha256 == hashlib.sha256(script.read_bytes()).hexdigest()
        assert server.configuration["args"] == [str(script.resolve())]
        audits = list(await db.scalars(select(AuditRecordRow)))
        assert [row.action for row in audits] == ["mcp.register"]
        assert audits[0].details == {"operation": "create", "transport": "stdio"}
        assert "command" not in audits[0].details and "env" not in audits[0].details

    asyncio.run(_scenario(tmp_path, check))


def test_governance_roles_paths_commands_and_environment_are_fail_closed(tmp_path):
    async def check(service, users, _db):
        manager = _principal(users["manager0001"])
        with pytest.raises(McpPermissionError):
            await service.register_local(
                manager, name="bad", command=sys.executable,
                args=["mock_pickup_server.py"], env={}
            )
        invalid = [
            ("python", ["mock_pickup_server.py"], {}),
            (sys.executable, ["../mcp/mock_pickup_server.py"], {}),
            (sys.executable, ["mock_pickup_server.py", "--extra"], {}),
            (sys.executable, ["mock_pickup_server.py"], {"API_KEY": "secret"}),
            (sys.executable, ["mock_pickup_server.py"], {"SAFE": "value"}),
        ]
        for command, args, env in invalid:
            with pytest.raises(McpValidationError):
                await service.register_local(
                    _principal(users["business_admin01"]),
                    name="bad", command=command, args=args, env=env,
                )
        with pytest.raises(McpValidationError, match="allowlisted"):
            await service.register_local(
                _principal(users["business_admin01"]),
                name="not-a-server", command=sys.executable,
                args=["service.py"], env={},
            )

    asyncio.run(_scenario(tmp_path, check))


def test_manager_authorization_and_input_schema_are_enforced_without_payload_audit(tmp_path):
    async def check(service, users, db):
        server = await _register(service, users)
        await service.authorize(
            _principal(users["business_admin01"]), server.id, users["manager0001"].id
        )
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(McpPermissionError):
            await service.call_tool(
                _principal(users["manager0002"]), server.id, "plan_pickup",
                {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 2},
            )
        with pytest.raises(McpValidationError):
            await service.call_tool(
                _principal(users["manager0001"]), server.id, "unknown", {}
            )
        for arguments in (
            {"arrival_time": "2026-07-15T14:00:00", "station": "南京南站", "guest_count": 2},
            {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "未知站", "guest_count": 2},
            {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 0},
            {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 2, "secret": "x"},
        ):
            with pytest.raises(McpValidationError):
                await service.call_tool(
                    _principal(users["manager0001"]), server.id, "plan_pickup", arguments
                )
        audits = list(await db.scalars(select(AuditRecordRow)))
        assert all("arguments" not in row.details and "secret" not in row.details for row in audits)
        failed_calls = [row for row in audits if row.action == "mcp.call" and row.result == "failure"]
        assert len(failed_calls) >= 5

    asyncio.run(_scenario(tmp_path, check))


def test_tamper_disabled_target_and_duplicate_authorization_are_rejected(tmp_path):
    async def check(service, users, db):
        server = await _register(service, users)
        authorization = await service.authorize(
            _principal(users["business_admin01"]), server.id, users["manager0001"].id
        )
        same = await service.authorize(
            _principal(users["system_admin01"]), server.id, users["manager0001"].id
        )
        assert same.id == authorization.id
        users["manager0002"].is_active = False
        await db.commit()
        with pytest.raises(McpPermissionError):
            await service.authorize(
                _principal(users["business_admin01"]), server.id, users["manager0002"].id
            )
        script = Path(server.configuration["args"][0])
        original = script.read_bytes()
        try:
            script.write_bytes(original + b"\n# tamper")
            with pytest.raises(McpConflictError):
                await service.start(_principal(users["business_admin01"]), server.id)
            failures = list(
                await db.scalars(
                    select(AuditRecordRow).where(
                        AuditRecordRow.action == "mcp.start",
                        AuditRecordRow.result == "failure",
                    )
                )
            )
            assert len(failures) == 1
            assert failures[0].details == {
                "operation": "connect", "status": "failure"
            }
        finally:
            script.write_bytes(original)

    asyncio.run(_scenario(tmp_path, check))


def test_timeout_retires_process_and_allows_clean_restart(tmp_path):
    async def check(service, users, _db):
        server = await _register(service, users)
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(McpUnavailableError, match="timed out"):
            await service.call_tool(
                _principal(users["business_admin01"]), server.id, "plan_pickup",
                {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "测试超时站", "guest_count": 1},
            )
        assert not await service.health(_principal(users["business_admin01"]), server.id)
        await service.start(_principal(users["business_admin01"]), server.id)
        assert await service.health(_principal(users["business_admin01"]), server.id)

    asyncio.run(_scenario(tmp_path, check, call_timeout=0.05))


def test_crash_is_detected_and_server_can_restart(tmp_path):
    async def check(service, users, _db):
        server = await _register(service, users)
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(BaseException):
            await service.call_tool(
                _principal(users["business_admin01"]), server.id, "plan_pickup",
                {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "测试崩溃站", "guest_count": 1},
            )
        await asyncio.sleep(0.1)
        assert not await service.health(_principal(users["business_admin01"]), server.id)
        await service.start(_principal(users["business_admin01"]), server.id)
        assert await service.health(_principal(users["business_admin01"]), server.id)

    asyncio.run(_scenario(tmp_path, check))


def test_output_limit_and_manager_governance_are_enforced(tmp_path):
    async def check(service, users, _db):
        server = await _register(service, users)
        manager = _principal(users["manager0001"])
        for operation in (
            lambda: service.start(manager, server.id),
            lambda: service.stop(manager, server.id),
            lambda: service.health(manager, server.id),
            lambda: service.list_tools(manager, server.id),
        ):
            with pytest.raises(McpPermissionError):
                await operation()
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(McpUnavailableError, match="output"):
            await service.call_tool(
                _principal(users["business_admin01"]), server.id, "plan_pickup",
                {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 1},
            )

    asyncio.run(_scenario(tmp_path, check, max_output_bytes=32))


def test_input_limit_rejects_payload_before_protocol_call(tmp_path):
    async def check(service, users, _db):
        server = await _register(service, users)
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(McpValidationError, match="input"):
            await service.call_tool(
                _principal(users["business_admin01"]), server.id, "plan_pickup",
                {
                    "arrival_time": "2026-07-15T14:00:00+08:00",
                    "station": "南京南站",
                    "guest_count": 1,
                },
            )

    asyncio.run(_scenario(tmp_path, check, max_input_bytes=32))


def test_concurrent_calls_and_shutdown_finish_every_runtime_task(tmp_path):
    async def check(service, users, _db):
        server = await _register(service, users)
        admin = _principal(users["business_admin01"])
        await service.start(admin, server.id)
        arguments = {
            "arrival_time": "2026-07-15T14:00:00+08:00",
            "station": "南京南站",
            "guest_count": 2,
        }
        results = await asyncio.gather(
            *(service.call_tool(admin, server.id, "plan_pickup", arguments) for _ in range(8))
        )
        assert all(result["mock"] is True for result in results)
        runtime_tasks = [runtime.task for runtime in service._runtimes.values()]
        await service.aclose()
        assert all(task is not None and task.done() for task in runtime_tasks)

    asyncio.run(_scenario(tmp_path, check, max_concurrent_calls=2))


def test_register_rolls_back_server_when_audit_write_fails(tmp_path, monkeypatch):
    async def check(service, users, db):
        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(AuditRepository, "add_pending", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await _register(service, users)
        assert await db.scalar(select(func.count()).select_from(McpServerRow)) == 0

    asyncio.run(_scenario(tmp_path, check))


def test_two_services_share_one_runtime_for_concurrent_start(tmp_path):
    async def run():
        engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        registry = McpRuntimeRegistry()
        root = Path(__file__).parents[2] / "app" / "mcp"
        try:
            await create_schema(engine)
            async with sessions() as setup:
                await seed_demo_data(setup)
                users = {user.username: user for user in await setup.scalars(select(User))}
                first = McpService(
                    setup, server_root=root, python_executable=Path(sys.executable),
                    session_factory=sessions, runtime_registry=registry,
                )
                server = await _register(first, users)
            async with sessions() as second_db:
                second = McpService(
                    second_db, server_root=root, python_executable=Path(sys.executable),
                    session_factory=sessions, runtime_registry=registry,
                )
                admin = _principal(users["business_admin01"])
                await asyncio.gather(first.start(admin, server.id), second.start(admin, server.id))
                assert len(registry.runtimes) == 1
                assert first._runtimes[server.id] is second._runtimes[server.id]
                await asyncio.gather(
                    first.call_tool(
                        admin, server.id, "plan_pickup",
                        {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京站", "guest_count": 1},
                    ),
                    second.call_tool(
                        admin, server.id, "plan_pickup",
                        {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 1},
                    ),
                )
                await first.aclose()
        finally:
            await engine.dispose()

    asyncio.run(run())
