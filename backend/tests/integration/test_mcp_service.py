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
            sessions = async_sessionmaker(db.bind, expire_on_commit=False)
            max_calls = options.pop("max_concurrent_calls", 4)
            max_running = options.pop("max_running_servers", 2)
            root = Path(__file__).parents[2] / "app" / "mcp"
            registry = McpRuntimeRegistry(
                application_namespace=f"test-{tmp_path.name}",
                server_root=root,
                python_executable=Path(sys.executable),
                session_factory=sessions,
                max_calls=max_calls,
                max_running_servers=max_running,
            )
            service = McpService(
                db,
                runtime_registry=registry,
                application_namespace=registry.application_namespace,
                server_root=root,
                python_executable=Path(sys.executable),
                session_factory=sessions,
                **options,
            )
            try:
                await check(service, users, db)
            finally:
                await service.aclose()
                await registry.shutdown_all()
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


async def _fault_scenario(
    tmp_path: Path, check, *, call_timeout: float = 0.1,
    max_running: int = 2, max_calls: int = 4,
):
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fault.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    root = Path(__file__).parents[1] / "fixtures"
    allowlist = frozenset({"fault_mcp_server.py", "idle_crash_mcp_server.py"})
    registry = McpRuntimeRegistry(
        f"fault-{tmp_path.name}", root, Path(sys.executable), sessions,
        max_calls=max_calls, max_running_servers=max_running,
        allowed_server_scripts=allowlist,
    )
    try:
        await create_schema(engine)
        async with sessions() as db:
            await seed_demo_data(db)
            users = {user.username: user for user in await db.scalars(select(User))}
            service = McpService(
                db, runtime_registry=registry,
                application_namespace=registry.application_namespace,
                server_root=root, python_executable=Path(sys.executable),
                session_factory=sessions, allowed_server_scripts=allowlist,
                call_timeout=call_timeout,
            )
            await check(service, registry, users, db)
    finally:
        await registry.shutdown_all()
        await engine.dispose()


async def _register_fault(service, users, name="fault-server", script="fault_mcp_server.py"):
    return await service.register_local(
        _principal(users["business_admin01"]), name=name,
        command=str(Path(sys.executable).resolve()), args=[script], env={},
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
        station_schema = tools[0]["input_schema"]["properties"]["station"]
        assert "测试超时站" not in station_schema.get("enum", [])
        assert "测试崩溃站" not in station_schema.get("enum", [])

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


def test_mock_server_rejects_invalid_calendar_timestamp(tmp_path):
    async def check(service, users, _db):
        server = await _register(service, users)
        await service.authorize(
            _principal(users["business_admin01"]), server.id, users["manager0001"].id
        )
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(McpValidationError):
            await service.call_tool(
                _principal(users["manager0001"]), server.id, "plan_pickup",
                {"arrival_time": "2026-02-31T14:00:00+08:00", "station": "南京南站", "guest_count": 1},
            )

    asyncio.run(_scenario(tmp_path, check))


def test_global_admin_cannot_call_business_tool_and_is_audited_once(tmp_path):
    async def check(service, users, db):
        server = await _register(service, users)
        admin = _principal(users["business_admin01"])
        await service.start(admin, server.id)
        with pytest.raises(McpPermissionError):
            await service.call_tool(
                admin, server.id, "plan_pickup",
                {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 1},
            )
        calls = list(
            await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.call"))
        )
        assert len(calls) == 1
        assert calls[0].result == "denied"
        assert calls[0].details == {"operation": "invoke", "status": "denied"}

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
        await service.authorize(
            _principal(users["business_admin01"]), server.id, users["manager0001"].id
        )
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(McpUnavailableError, match="output"):
            await service.call_tool(
                manager, server.id, "plan_pickup",
                {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 1},
            )

    asyncio.run(_scenario(tmp_path, check, max_output_bytes=32))


def test_input_limit_rejects_payload_before_protocol_call(tmp_path):
    async def check(service, users, _db):
        server = await _register(service, users)
        await service.authorize(
            _principal(users["business_admin01"]), server.id, users["manager0001"].id
        )
        await service.start(_principal(users["business_admin01"]), server.id)
        with pytest.raises(McpValidationError, match="input"):
            await service.call_tool(
                _principal(users["manager0001"]), server.id, "plan_pickup",
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
        manager = _principal(users["manager0001"])
        await service.authorize(admin, server.id, users["manager0001"].id)
        await service.start(admin, server.id)
        arguments = {
            "arrival_time": "2026-07-15T14:00:00+08:00",
            "station": "南京南站",
            "guest_count": 2,
        }
        results = await asyncio.gather(
            *(service.call_tool(manager, server.id, "plan_pickup", arguments) for _ in range(8))
        )
        assert all(result["mock"] is True for result in results)
        runtime_tasks = [runtime.task for runtime in service._runtimes.values()]
        await service.aclose()
        assert all(task is not None and not task.done() for task in runtime_tasks)

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
        root = Path(__file__).parents[2] / "app" / "mcp"
        registry = McpRuntimeRegistry(
            "shared-test", root, Path(sys.executable), sessions,
        )
        try:
            await create_schema(engine)
            async with sessions() as setup:
                await seed_demo_data(setup)
                users = {user.username: user for user in await setup.scalars(select(User))}
                first = McpService(
                    setup, runtime_registry=registry, application_namespace="shared-test",
                    server_root=root, python_executable=Path(sys.executable),
                    session_factory=sessions,
                )
                server = await _register(first, users)
            async with sessions() as second_db:
                second = McpService(
                    second_db, runtime_registry=registry, application_namespace="shared-test",
                    server_root=root, python_executable=Path(sys.executable),
                    session_factory=sessions,
                )
                admin = _principal(users["business_admin01"])
                manager = _principal(users["manager0001"])
                await first.authorize(admin, server.id, users["manager0001"].id)
                await asyncio.gather(first.start(admin, server.id), second.start(admin, server.id))
                assert len(registry.runtimes) == 1
                assert next(iter(first._runtimes.values())) is next(iter(second._runtimes.values()))
                await first.aclose()
                assert await second.health(admin, server.id)
                await asyncio.gather(
                    first.call_tool(
                        manager, server.id, "plan_pickup",
                        {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京站", "guest_count": 1},
                    ),
                    second.call_tool(
                        manager, server.id, "plan_pickup",
                        {"arrival_time": "2026-07-15T14:00:00+08:00", "station": "南京南站", "guest_count": 1},
                    ),
                )
                tasks = [runtime.task for runtime in registry.runtimes.values()]
                await registry.shutdown_all()
                assert all(task is not None and task.done() for task in tasks)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_fault_timeout_retires_once_audits_once_and_restarts(tmp_path):
    async def check(service, registry, users, db):
        server = await _register_fault(service, users)
        admin = _principal(users["business_admin01"])
        manager = _principal(users["manager0001"])
        await service.authorize(admin, server.id, users["manager0001"].id)
        await service.start(admin, server.id)
        with pytest.raises(McpUnavailableError, match="timed out"):
            await service.call_tool(manager, server.id, "fault", {"mode": "timeout"})
        assert await registry.current(server.id) is None
        calls = list(await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.call")))
        assert len(calls) == 1 and calls[0].result == "failure"
        await service.start(admin, server.id)
        assert await service.health(admin, server.id)

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=0.05))


def test_stale_generation_cannot_overwrite_restarted_runtime_state(tmp_path):
    async def check(service, registry, users, db):
        admin = _principal(users["business_admin01"])
        server = await _register_fault(service, users)
        await service.start(admin, server.id)
        stale = await registry.current(server.id)
        await service.stop(admin, server.id)
        await service.start(admin, server.id)
        current = await registry.current(server.id)
        assert current is not None and current is not stale
        await service._retire_failed_runtime(admin, server.id, stale, "STALE_FAILURE")
        assert await registry.current(server.id) is current
        async with service._sessions() as verify:
            row = await verify.get(McpServerRow, server.id)
            assert row.status == "running" and row.last_error is None
        assert not list(
            await db.scalars(
                select(AuditRecordRow).where(
                    AuditRecordRow.action == "mcp.lifecycle",
                    AuditRecordRow.result == "failure",
                )
            )
        )

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))


def test_stop_racing_old_call_failure_cannot_overwrite_stopped_or_restart(tmp_path):
    async def check(service, registry, users, db):
        admin = _principal(users["business_admin01"])
        manager = _principal(users["manager0001"])
        server = await _register_fault(service, users)
        await service.authorize(admin, server.id, users["manager0001"].id)
        await service.start(admin, server.id)
        old_call = asyncio.create_task(
            service.call_tool(manager, server.id, "fault", {"mode": "timeout"})
        )
        await asyncio.sleep(0.05)
        await service.stop(admin, server.id)
        with pytest.raises(BaseException):
            await old_call
        async with service._sessions() as verify:
            stopped = await verify.get(McpServerRow, server.id)
            assert stopped.status == "stopped" and stopped.last_error is None
        await service.start(admin, server.id)
        restarted = await registry.current(server.id)
        assert restarted is not None
        await asyncio.sleep(0.1)
        assert await registry.current(server.id) is restarted
        calls = list(await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.call")))
        assert len(calls) == 1 and calls[0].result == "failure"

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))


def test_service_registry_configuration_mismatch_fails_fast(tmp_path):
    async def run():
        engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mismatch.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = Path(__file__).parents[2] / "app" / "mcp"
        registry = McpRuntimeRegistry("expected", root, Path(sys.executable), sessions)
        try:
            await create_schema(engine)
            async with sessions() as db:
                with pytest.raises(ValueError, match="does not match"):
                    McpService(
                        db, runtime_registry=registry, application_namespace="wrong",
                        server_root=root, python_executable=Path(sys.executable),
                        session_factory=sessions,
                    )
        finally:
            await registry.shutdown_all()
            await engine.dispose()

    asyncio.run(run())


def test_fault_crash_and_unstructured_retire_current_with_single_call_audit(tmp_path):
    async def check(service, registry, users, db):
        admin = _principal(users["business_admin01"])
        manager = _principal(users["manager0001"])
        server = await _register_fault(service, users)
        await service.authorize(admin, server.id, users["manager0001"].id)
        await service.start(admin, server.id)
        with pytest.raises(BaseException):
            await service.call_tool(manager, server.id, "fault", {"mode": "crash"})
        assert await registry.current(server.id) is None
        await service.start(admin, server.id)
        current = await registry.current(server.id)
        with pytest.raises(McpValidationError):
            await service.call_tool(
                manager, server.id, "fault", {"mode": "business_error"}
            )
        assert await registry.current(server.id) is current
        with pytest.raises(McpUnavailableError, match="structured"):
            await service.call_tool(manager, server.id, "unstructured", {})
        assert await registry.current(server.id) is None
        calls = list(await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.call")))
        assert len(calls) == 3 and all(row.result == "failure" for row in calls)

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))


def test_idle_process_exit_updates_db_and_lifecycle_without_request_service(tmp_path):
    async def check(service, registry, users, db):
        server = await _register_fault(
            service, users, name="idle-crash", script="idle_crash_mcp_server.py"
        )
        await service.start(_principal(users["business_admin01"]), server.id)
        await service.aclose()
        for _ in range(40):
            if await registry.current(server.id) is None:
                break
            await asyncio.sleep(0.05)
        assert await registry.current(server.id) is None
        persisted = await db.get(McpServerRow, server.id)
        assert persisted.status == "failed" and persisted.last_error == "PROCESS_EXITED"
        lifecycle = list(
            await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.lifecycle"))
        )
        assert len(lifecycle) == 1 and lifecycle[0].result == "failure"

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))


def test_registry_running_limit_reserves_before_spawn(tmp_path):
    async def check(service, registry, users, db):
        admin = _principal(users["business_admin01"])
        first = await _register_fault(service, users, name="fault-one")
        second = await _register_fault(service, users, name="fault-two")
        results = await asyncio.gather(
            service.start(admin, first.id), service.start(admin, second.id),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, McpUnavailableError) for result in results) == 1
        assert len(registry.runtimes) == 1
        currents = await asyncio.gather(
            registry.current(first.id), registry.current(second.id)
        )
        assert sum(current is not None for current in currents) == 1
        failures = list(
            await db.scalars(
                select(AuditRecordRow).where(
                    AuditRecordRow.action == "mcp.start",
                    AuditRecordRow.result == "failure",
                )
            )
        )
        assert len(failures) == 1

    asyncio.run(_fault_scenario(tmp_path, check, max_running=1))


def test_cancelled_call_releases_slot_audits_once_retires_and_restarts(tmp_path):
    async def check(service, registry, users, db):
        admin = _principal(users["business_admin01"])
        manager = _principal(users["manager0001"])
        server = await _register_fault(service, users)
        await service.authorize(admin, server.id, users["manager0001"].id)
        await service.start(admin, server.id)
        task = asyncio.create_task(
            service.call_tool(manager, server.id, "fault", {"mode": "timeout"})
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await registry.current(server.id) is None
        calls = list(await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.call")))
        assert len(calls) == 1
        assert calls[0].result == "cancelled"
        assert calls[0].details == {"operation": "invoke", "status": "cancelled"}
        await service.start(admin, server.id)
        result = await service.call_tool(manager, server.id, "fault", {"mode": "ok"})
        assert result["mock"] is True

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))


def test_registry_close_wins_race_before_reserve_and_is_terminal(tmp_path, monkeypatch):
    async def check(service, registry, users, _db):
        server = await _register_fault(service, users)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = McpRuntimeRegistry.reserve

        async def paused_reserve(self, runtime):
            entered.set()
            await release.wait()
            await original(self, runtime)

        monkeypatch.setattr(McpRuntimeRegistry, "reserve", paused_reserve)
        start = asyncio.create_task(
            service.start(_principal(users["business_admin01"]), server.id)
        )
        await entered.wait()
        await asyncio.gather(registry.shutdown_all(), registry.aclose())
        release.set()
        with pytest.raises(McpUnavailableError, match="closed"):
            await start
        assert registry.closed is True
        assert registry.runtimes == {}
        with pytest.raises(McpUnavailableError, match="closed"):
            await service.start(_principal(users["business_admin01"]), server.id)

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))


def test_registry_close_takes_over_reserved_runtime_and_waits_cleanup(tmp_path):
    async def check(service, registry, users, _db):
        server = await _register_fault(service, users)
        await service.start(_principal(users["business_admin01"]), server.id)
        runtime = await registry.current(server.id)
        assert runtime is not None and runtime.task is not None
        await asyncio.gather(registry.shutdown_all(), registry.aclose(), registry.shutdown_all())
        assert registry.closed is True
        assert registry.runtimes == {}
        assert runtime.task.done()

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))


def test_cancel_while_waiting_call_slot_does_not_retire_shared_runtime(tmp_path):
    async def check(service, registry, users, db):
        admin = _principal(users["business_admin01"])
        manager = _principal(users["manager0001"])
        server = await _register_fault(service, users)
        await service.authorize(admin, server.id, users["manager0001"].id)
        await service.start(admin, server.id)
        runtime = await registry.current(server.id)
        occupying = asyncio.create_task(
            service.call_tool(manager, server.id, "fault", {"mode": "timeout"})
        )
        await asyncio.sleep(0.05)
        queued = asyncio.create_task(
            service.call_tool(manager, server.id, "fault", {"mode": "ok"})
        )
        await asyncio.sleep(0.05)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert await registry.current(server.id) is runtime
        assert (await occupying)["mock"] is True
        calls = list(await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.call")))
        assert sorted(row.result for row in calls) == ["cancelled", "success"]

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2, max_running=2, max_calls=1))


def test_cancel_during_authorization_does_not_retire_and_double_cancel_audits_once(
    tmp_path, monkeypatch
):
    async def check(service, registry, users, db):
        admin = _principal(users["business_admin01"])
        manager = _principal(users["manager0001"])
        server = await _register_fault(service, users)
        await service.authorize(admin, server.id, users["manager0001"].id)
        await service.start(admin, server.id)
        runtime = await registry.current(server.id)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = service._can_call

        async def blocked(db_session, actor, server_id):
            entered.set()
            await release.wait()
            return await original(db_session, actor, server_id)

        monkeypatch.setattr(service, "_can_call", blocked)
        task = asyncio.create_task(service.call_tool(manager, server.id, "fault", {"mode": "ok"}))
        await entered.wait()
        task.cancel()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await registry.current(server.id) is runtime
        calls = list(await db.scalars(select(AuditRecordRow).where(AuditRecordRow.action == "mcp.call")))
        assert len(calls) == 1 and calls[0].result == "cancelled"

    asyncio.run(_fault_scenario(tmp_path, check, call_timeout=2))
