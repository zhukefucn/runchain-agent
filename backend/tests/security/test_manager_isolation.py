import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    AuditRecordRow,
    MessageRow,
    Role,
    TeamRunRow,
    User,
    WorkspaceFileRow,
)
from app.db.session import build_async_engine, create_schema
from app.repositories.audit import AuditRepository
from app.repositories.manager import ManagerRepository


OWNERS = ("manager0001", "manager0002")


async def _with_repositories(tmp_path, check: Callable[..., Awaitable[None]]) -> None:
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'isolation.db'}")
    try:
        await create_schema(engine)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            db.add_all(
                User(
                    id=owner,
                    username=owner,
                    password_hash="not-used-in-this-test",
                    role=Role.MANAGER,
                )
                for owner in OWNERS
            )
            db.add(
                User(
                    id="system_admin01",
                    username="system_admin01",
                    password_hash="not-used-in-this-test",
                    role=Role.SYSTEM_ADMIN,
                )
            )
            await db.commit()
            await check(db, ManagerRepository(db), AuditRepository(db))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("owner,intruder", [OWNERS, OWNERS[::-1]])
@pytest.mark.parametrize(
    "operation",
    ["get", "update", "delete", "list_messages", "create_message"],
)
def test_session_access_is_failure_safe_in_both_directions(
    tmp_path, owner, intruder, operation
):
    async def check(_db, repo, _audit):
        session = await repo.create_session(owner, agent_id="default", title="private")
        if operation == "get":
            result = await repo.get_session(intruder, session.id)
            assert result is None
        elif operation == "update":
            result = await repo.update_session(intruder, session.id, title="stolen")
            assert result is False
        elif operation == "delete":
            result = await repo.delete_session(intruder, session.id)
            assert result is False
        elif operation == "list_messages":
            await repo.create_message(owner, session.id, role="user", content="secret")
            assert await repo.list_messages(intruder, session.id) == []
        else:
            assert (
                await repo.create_message(
                    intruder, session.id, role="user", content="injected"
                )
                is None
            )

        visible = await repo.get_session(owner, session.id)
        assert visible is not None
        assert visible.title == "private"

    asyncio.run(_with_repositories(tmp_path, check))


@pytest.mark.parametrize("owner,intruder", [OWNERS, OWNERS[::-1]])
@pytest.mark.parametrize("kind", ["message", "file", "run"])
def test_child_resources_cannot_be_read_written_updated_or_deleted_cross_owner(
    tmp_path, owner, intruder, kind
):
    async def check(_db, repo, _audit):
        session = await repo.create_session(owner, agent_id="default", title="private")
        if kind == "message":
            child = await repo.create_message(
                owner, session.id, role="user", content="confidential body"
            )
            assert child is not None
            assert await repo.get_message(intruder, child.id) is None
            assert await repo.update_message(intruder, child.id, content="tampered") is False
            assert await repo.delete_message(intruder, child.id) is False
            assert await repo.list_messages(intruder, session.id) == []
            assert await repo.get_message(owner, child.id) is not None
        elif kind == "file":
            child = await repo.create_workspace_file(owner, session.id, "outputs/a.txt")
            assert child is not None
            assert await repo.get_workspace_file(intruder, child.id) is None
            assert (
                await repo.update_workspace_file(
                    intruder, child.id, relative_path="outputs/tampered.txt"
                )
                is False
            )
            assert await repo.delete_workspace_file(intruder, child.id) is False
            assert await repo.list_workspace_files(intruder, session.id) == []
            assert await repo.get_workspace_file(owner, child.id) is not None
        else:
            child = await repo.create_team_run(owner, session.id, status="pending")
            assert child is not None
            assert await repo.get_team_run(intruder, child.id) is None
            assert await repo.update_team_run(intruder, child.id, status="completed") is False
            assert await repo.delete_team_run(intruder, child.id) is False
            assert await repo.list_team_runs(intruder, session.id) == []
            assert await repo.get_team_run(owner, child.id) is not None

    asyncio.run(_with_repositories(tmp_path, check))


@pytest.mark.parametrize("owner,intruder", [OWNERS, OWNERS[::-1]])
@pytest.mark.parametrize("kind", ["message", "file", "run"])
def test_known_foreign_session_id_cannot_be_used_to_create_child(
    tmp_path, owner, intruder, kind
):
    async def check(_db, repo, _audit):
        session = await repo.create_session(owner, agent_id="default", title="private")
        if kind == "message":
            result = await repo.create_message(
                intruder, session.id, role="user", content="injected"
            )
        elif kind == "file":
            result = await repo.create_workspace_file(
                intruder, session.id, "outputs/injected.txt"
            )
        else:
            result = await repo.create_team_run(intruder, session.id, status="pending")
        assert result is None

    asyncio.run(_with_repositories(tmp_path, check))


@pytest.mark.parametrize("kind", ["message", "file", "run"])
def test_inconsistent_parent_and_child_owner_is_never_exposed(tmp_path, kind):
    async def check(db, repo, _audit):
        session = await repo.create_session(
            "manager0001", agent_id="default", title="private"
        )
        if kind == "message":
            child = MessageRow(
                session_id=session.id,
                owner_user_id="manager0002",
                role="user",
                content="corrupt",
            )
        elif kind == "file":
            child = WorkspaceFileRow(
                session_id=session.id,
                owner_user_id="manager0002",
                relative_path="outputs/corrupt.txt",
            )
        else:
            child = TeamRunRow(
                session_id=session.id,
                owner_user_id="manager0002",
                status="pending",
            )
        db.add(child)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    asyncio.run(_with_repositories(tmp_path, check))


def test_lists_show_only_owner_resources_and_session_keeps_agent_id(tmp_path):
    async def check(_db, repo, _audit):
        first = await repo.create_session(
            "manager0001", agent_id="agent-a", title="manager one"
        )
        await repo.create_session("manager0002", agent_id="agent-b", title="manager two")
        rows = await repo.list_sessions("manager0001")
        assert [row.id for row in rows] == [first.id]
        assert rows[0].agent_id == "agent-a"

    asyncio.run(_with_repositories(tmp_path, check))


def test_nonexistent_and_foreign_ids_have_identical_failure_shapes(tmp_path):
    async def check(_db, repo, _audit):
        session = await repo.create_session(
            "manager0001", agent_id="default", title="private"
        )
        for resource_id in (session.id, "missing-id"):
            assert await repo.get_session("manager0002", resource_id) is None
            assert (
                await repo.update_session(
                    "manager0002", resource_id, title="not allowed"
                )
                is False
            )
            assert await repo.delete_session("manager0002", resource_id) is False

    asyncio.run(_with_repositories(tmp_path, check))


def test_global_admin_cannot_become_manager_business_data_owner(tmp_path):
    async def check(_db, repo, _audit):
        with pytest.raises(PermissionError):
            await repo.create_session(
                "system_admin01", agent_id="default", title="not manager data"
            )
        assert await repo.list_sessions("system_admin01") == []

    asyncio.run(_with_repositories(tmp_path, check))


def test_audit_details_are_recursively_sanitized_and_metadata_only(tmp_path):
    class UnsafeObject:
        def __str__(self):
            return "custom object do not store"

    async def check(db, _repo, audit):
        record = await audit.record(
            actor_user_id="system_admin01",
            action="skill.invoke.denied",
            resource_type="skill",
            resource_id="skill-1",
            result="denied",
            request_id="request-1",
            details={
                "operation": "invoke",
                "status": "denied",
                "status_code": 403,
                "duration_ms": 12.5,
                "count": 2,
                "retryable": False,
                "enabled": True,
                "role": "manager",
                "transport": "stdio",
                "metrics": {
                    "count": 2,
                    "duration_ms": 12.5,
                    "status": UnsafeObject(),
                    "prompt": "nested do not store",
                    "中文正文作为键": 99,
                },
                "operations": [
                    {
                        "operation": "execute",
                        "status": "failed",
                        "output": "nested do not store",
                    },
                    {
                        "operation": "execute",
                        "status": "account_6222020202020202020",
                        "error_code": "客户密码是八个八",
                    },
                    {"set value do not store"},
                ],
                "error_code": "客户密码是八个八",
                "正文：客户密码是八个八": "value",
                "prompt": "plain prompt do not store",
                "attachment": "plain attachment do not store",
                "input_data": {"status": "must redact whole unknown container"},
                "output": "plain output do not store",
                "unknown_set": {"set value do not store"},
                "unknown_object": UnsafeObject(),
                "message_body": "do not store",
                "nested": {
                    "apiKey": "do not store",
                    "items": [
                        {
                            "password": "do not store",
                            "authorization": "Bearer do not store",
                            "status": "failed",
                        }
                    ],
                },
                "file_content": b"do not store",
                "binary_attachment": b"do not store",
                "note": "Authorization: Bearer do not store",
            },
        )
        assert record.result == "denied"
        assert record.request_id == "request-1"
        assert record.created_at is not None
        assert record.details == {
            "operation": "invoke",
            "status": "denied",
            "status_code": 403,
            "duration_ms": 12.5,
            "count": 2,
            "retryable": False,
            "enabled": True,
            "role": "manager",
            "transport": "stdio",
            "metrics": {
                "count": 2,
                "duration_ms": 12.5,
            },
            "operations": [
                {
                    "operation": "execute",
                    "status": "failed",
                },
                {"operation": "execute"},
            ],
        }
        stored = await db.scalar(select(AuditRecordRow).where(AuditRecordRow.id == record.id))
        serialized = str(stored.details)
        assert "do not store" not in serialized
        assert "custom object" not in serialized
        assert "must redact" not in serialized
        assert "account_622" not in serialized
        assert "客户密码" not in serialized
        assert "中文正文作为键" not in serialized
        assert "prompt" not in record.details
        assert "error_code" not in record.details

    asyncio.run(_with_repositories(tmp_path, check))
