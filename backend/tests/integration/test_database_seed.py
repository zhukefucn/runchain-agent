import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from argon2 import PasswordHasher
from sqlalchemy import UniqueConstraint, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.models import (
    AuditRecordRow,
    HitlRequestRow,
    McpServerRow,
    MessageRow,
    SessionRecordRow,
    SkillAuthorizationRow,
    SkillInvocationRow,
    SkillRow,
    TeamRunRow,
    User,
    WorkspaceFileRow,
)
from app.db.seed import seed_demo_data
from app.db.session import async_session_factory, build_async_engine, create_schema


EXPECTED_TABLES = {
    "agentscope_storage_records",
    "audit_records",
    "hitl_requests",
    "mcp_servers",
    "messages",
    "sessions",
    "skill_authorizations",
    "skill_invocations",
    "skills",
    "team_runs",
    "users",
    "workspace_files",
}
OWNER_MODELS = (
    SessionRecordRow,
    MessageRow,
    TeamRunRow,
    HitlRequestRow,
    WorkspaceFileRow,
    SkillInvocationRow,
)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def _with_database(tmp_path, check):
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    try:
        await create_schema(engine)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await check(engine, session)
    finally:
        await engine.dispose()


def test_schema_has_required_tables_foreign_keys_owner_indexes_and_unique_auth(tmp_path):
    async def check(engine, _session):
        async with engine.connect() as connection:
            table_details = await connection.run_sync(
                lambda sync_connection: {
                    table: inspect(sync_connection).get_foreign_keys(table)
                    for table in EXPECTED_TABLES
                }
            )

        assert set(Base.metadata.tables) == EXPECTED_TABLES
        assert async_session_factory.kw["expire_on_commit"] is False
        assert all(table_details[table] for table in EXPECTED_TABLES - {"users"})
        assert all(model.__table__.c.owner_user_id.index for model in OWNER_MODELS)
        assert any(
            isinstance(constraint, UniqueConstraint)
            and {column.name for column in constraint.columns} == {"skill_id", "user_id"}
            for constraint in SkillAuthorizationRow.__table__.constraints
        )
        assert not SkillRow.__table__.c.name.unique
        assert any(
            isinstance(constraint, UniqueConstraint)
            and {column.name for column in constraint.columns} == {"name", "version"}
            for constraint in SkillRow.__table__.constraints
        )

    asyncio.run(_with_database(tmp_path, check))


def test_sqlite_connections_enable_foreign_keys_and_wal(tmp_path):
    async def check(engine, _session):
        async with engine.connect() as connection:
            foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
            journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        assert foreign_keys == 1
        assert journal_mode == "wal"

    asyncio.run(_with_database(tmp_path, check))


def test_seed_creates_four_hashed_users_and_is_idempotent(tmp_path):
    async def check(_engine, session):
        await seed_demo_data(session)
        await seed_demo_data(session)
        users = (await session.scalars(select(User).order_by(User.username))).all()

        assert [user.username for user in users] == [
            "business_admin01",
            "manager0001",
            "manager0002",
            "system_admin01",
        ]
        assert all(user.password_hash != "12345678" for user in users)
        assert all(
            PasswordHasher().verify(user.password_hash, "12345678") for user in users
        )

    asyncio.run(_with_database(tmp_path, check))


def test_create_schema_registers_models_without_caller_imports(tmp_path):
    database_path = tmp_path / "clean-import.db"
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
            "JWT_SECRET_KEY": "subprocess-test-only",
            "MODEL_API_KEY": "subprocess-test-only",
            "PYTHONPATH": str(PROJECT_ROOT / "backend"),
        }
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from app.db.session import create_schema; "
            "asyncio.run(create_schema())",
        ],
        check=True,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert tables == EXPECTED_TABLES
