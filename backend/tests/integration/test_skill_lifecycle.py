from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest
from unittest.mock import AsyncMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Principal
from app.db.models import AuditRecordRow, Role, SkillRow, User
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema
from app.skills.service import SkillConflictError, SkillPermissionError, SkillService


def package(version: str = "1.0.0", name: str = "private-demo") -> bytes:
    output = io.BytesIO()
    manifest = {
        "id": name, "name": name, "version": version, "type": "python",
        "entrypoint": "main.py", "description": "private demo",
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}/SKILL.md", "# demo")
        archive.writestr(f"{name}/skill.json", json.dumps(manifest))
        archive.writestr(f"{name}/main.py", "print('ok')")
    return output.getvalue()


async def scenario(tmp_path, check):
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'skills.db'}")
    try:
        await create_schema(engine)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await seed_demo_data(db)
            users = {u.username: u for u in (await db.scalars(select(User))).all()}
            await check(db, users)
    finally:
        await engine.dispose()


def principal(user: User) -> Principal:
    return Principal(user_id=user.id, role=user.role, tenant_id="bank_demo")


def test_install_publish_authorize_isolate_and_invalidate_cache(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        first = await service.install(admin, package())
        assert first.status == "draft"
        assert (tmp_path / "installed" / "private-demo" / "1.0.0" / "SKILL.md").is_file()

        await service.publish(admin, first.id)
        assert await service.effective_skills(users["manager0001"].id) == []
        await service.authorize(admin, first.id, users["manager0001"].id)
        assert [s.name for s in await service.effective_skills(users["manager0001"].id)] == ["private-demo"]
        assert await service.effective_skills(users["manager0002"].id) == []

        # Prime cache, then prove a state change is visible without restart.
        await service.authorize(admin, first.id, users["manager0002"].id)
        assert [s.name for s in await service.effective_skills(users["manager0002"].id)] == ["private-demo"]
        await service.disable(admin, first.id)
        assert await service.effective_skills(users["manager0001"].id) == []
        assert await service.effective_skills(users["manager0002"].id) == []
        audits = (await db.scalars(select(AuditRecordRow))).all()
        assert {row.action for row in audits} == {
            "skill.install", "skill.publish", "skill.authorize", "skill.disable"
        }
        assert all("manifest" not in row.details and "content" not in row.details for row in audits)

    asyncio.run(scenario(tmp_path, check))


def test_only_global_admins_govern_and_authorization_target_is_active_manager(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        manager = principal(users["manager0001"])
        with pytest.raises(SkillPermissionError):
            await service.install(manager, package())

        system = principal(users["system_admin01"])
        skill = await service.install(system, package())
        await service.publish(system, skill.id)
        with pytest.raises(SkillPermissionError):
            await service.authorize(system, skill.id, users["business_admin01"].id)
        users["manager0002"].is_active = False
        await db.commit()
        with pytest.raises(SkillPermissionError):
            await service.authorize(system, skill.id, users["manager0002"].id)

    asyncio.run(scenario(tmp_path, check))


def test_versions_are_distinct_duplicate_is_idempotent_and_publish_switches_active_version(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        one = await service.install(admin, package("1.0.0"))
        assert (await service.install(admin, package("1.0.0"))).id == one.id
        with pytest.raises(SkillConflictError):
            await service.install(admin, package("1.0.0") + b"different")
        two = await service.install(admin, package("2.0.0"))
        await service.publish(admin, one.id)
        await service.publish(admin, two.id)
        await db.refresh(one)
        assert one.status == "draft"
        assert two.status == "published"
        assert len((await db.scalars(select(SkillRow))).all()) == 2

    asyncio.run(scenario(tmp_path, check))


def test_failed_install_leaves_no_row_or_directory(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        with pytest.raises(Exception):
            await service.install(principal(users["business_admin01"]), b"not zip")
        assert (await db.scalars(select(SkillRow))).all() == []
        assert not (tmp_path / "installed").exists() or not any((tmp_path / "installed").iterdir())

    asyncio.run(scenario(tmp_path, check))


def test_commit_failure_removes_database_row_and_atomic_destination(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        real_commit = db.commit
        db.commit = AsyncMock(side_effect=RuntimeError("disk full"))
        try:
            with pytest.raises(RuntimeError, match="disk full"):
                await service.install(principal(users["business_admin01"]), package())
        finally:
            db.commit = real_commit
        assert (await db.scalars(select(SkillRow))).all() == []
        assert not (tmp_path / "installed" / "private-demo" / "1.0.0").exists()

    asyncio.run(scenario(tmp_path, check))


def test_concurrent_same_package_install_is_single_row_and_single_directory(tmp_path):
    async def run():
        engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
        try:
            await create_schema(engine)
            async with AsyncSession(engine, expire_on_commit=False) as seed_db:
                await seed_demo_data(seed_db)
                admin = await seed_db.scalar(
                    select(User).where(User.username == "business_admin01")
                )
                actor = principal(admin)
            async with (
                AsyncSession(engine, expire_on_commit=False) as first_db,
                AsyncSession(engine, expire_on_commit=False) as second_db,
            ):
                first, second = await asyncio.gather(
                    SkillService(first_db, tmp_path / "installed").install(actor, package()),
                    SkillService(second_db, tmp_path / "installed").install(actor, package()),
                )
                assert first.id == second.id
            async with AsyncSession(engine) as verify_db:
                assert len((await verify_db.scalars(select(SkillRow))).all()) == 1
            assert len(list((tmp_path / "installed" / "private-demo").iterdir())) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())
