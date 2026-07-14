from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.config import Settings
from app.db.models import McpServerRow, User
from app.main import create_root_app


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        workspace_root=tmp_path / "workspace",
        skill_root=tmp_path / "skills",
        runner_root=tmp_path / "runner",
        jwt_secret_key="test-jwt-secret-with-enough-entropy",
        model_api_key="",
    )


def test_mcp_running_status_is_reconciled_on_shutdown_and_next_startup(tmp_path):
    async def scenario():
        settings = _settings(tmp_path)
        first = create_root_app(settings)
        async with first.router.lifespan_context(first):
            async with first.state.session_factory() as db:
                admin = await db.scalar(
                    select(User).where(User.username == "business_admin01")
                )
                row = McpServerRow(
                    created_by_user_id=admin.id,
                    name="persisted-running",
                    transport="stdio",
                    configuration={},
                    executable_sha256="0" * 64,
                    script_sha256="0" * 64,
                    status="running",
                )
                db.add(row)
                await db.commit()
                server_id = row.id
        async with first.state.session_factory() as db:
            assert (await db.get(McpServerRow, server_id)).status == "stopped"

        async with first.state.session_factory() as db:
            persisted = await db.get(McpServerRow, server_id)
            persisted.status = "running"
            await db.commit()
        second = create_root_app(settings)
        async with second.router.lifespan_context(second):
            async with second.state.session_factory() as db:
                assert (await db.get(McpServerRow, server_id)).status == "stopped"

    asyncio.run(scenario())
