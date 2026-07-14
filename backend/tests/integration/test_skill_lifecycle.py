from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import io
import json
import shutil
import zipfile

import pytest
from unittest.mock import AsyncMock
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import Principal
from app.db.models import AuditRecordRow, Role, SkillRow, User
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema
from app.repositories.audit import AuditRepository
from app.repositories.skill import SkillRepository
from app.runner.protocol import SkillExecutionRequest, SkillResolutionError
from app.runner.resolver import SkillServiceResolver
from app.skills.service import (
    UNCOMMITTED_MARKER,
    EffectiveSkill,
    SkillCleanupError,
    SkillConflictError,
    SkillPermissionError,
    SkillNotFoundError,
    SkillService as GovernedSkillService,
)
from app.skills.package import MAX_FILE_BYTES, MAX_ZIP_MEMBERS


def SkillService(db, install_root):
    return GovernedSkillService(
        db,
        install_root,
        read_session_factory=async_sessionmaker(db.bind, expire_on_commit=False),
    )


def package(
    version: str = "1.0.0",
    name: str = "private-demo",
    main_source: str = "print('ok')",
) -> bytes:
    output = io.BytesIO()
    manifest = {
        "id": name, "name": name, "version": version, "type": "python",
        "entrypoint": "main.py", "description": "private demo",
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}/SKILL.md", "# demo")
        archive.writestr(f"{name}/skill.json", json.dumps(manifest))
        archive.writestr(f"{name}/main.py", main_source)
    return output.getvalue()


def boundary_package() -> bytes:
    output = io.BytesIO()
    manifest = {
        "id": "boundary-demo", "name": "boundary-demo", "version": "1.0.0",
        "type": "python", "entrypoint": "d0/main.py",
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("boundary-demo/SKILL.md", "# Boundary")
        archive.writestr("boundary-demo/skill.json", json.dumps(manifest))
        for index in range(MAX_ZIP_MEMBERS - 2):
            archive.writestr(f"boundary-demo/d{index}/main.py", "pass")
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
        assert await service.effective_skills(users["manager0001"].id) == ()
        await service.authorize(admin, first.id, users["manager0001"].id)
        effective = await service.effective_skills(users["manager0001"].id)
        assert [s.name for s in effective] == ["private-demo"]
        assert isinstance(effective[0], EffectiveSkill)
        with pytest.raises(FrozenInstanceError):
            effective[0].name = "mutated"
        assert await service.effective_skills(users["manager0002"].id) == ()

        # Prime cache, then prove a state change is visible without restart.
        await service.authorize(admin, first.id, users["manager0002"].id)
        assert [s.name for s in await service.effective_skills(users["manager0002"].id)] == ["private-demo"]
        await service.disable(admin, first.id)
        assert await service.effective_skills(users["manager0001"].id) == ()
        assert await service.effective_skills(users["manager0002"].id) == ()
        audits = (await db.scalars(select(AuditRecordRow))).all()
        assert {row.action for row in audits} == {
            "skill.install", "skill.publish", "skill.authorize", "skill.disable"
        }
        assert all("manifest" not in row.details and "content" not in row.details for row in audits)

    asyncio.run(scenario(tmp_path, check))


def test_execution_resolution_rechecks_authorization_version_and_disk_integrity(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        skill = await service.install(admin, package())
        await service.publish(admin, skill.id)
        await service.authorize(admin, skill.id, users["manager0001"].id)

        resolved = await service.resolve_execution_skill(
            users["manager0001"].id, skill.id, "1.0.0"
        )
        assert resolved.id == skill.id
        assert resolved.type == "python"
        request = SkillExecutionRequest(
            user_id=users["manager0001"].id,
            skill_id=skill.id,
            version="1.0.0",
            input_data={},
            request_id="resolve-test",
        )
        runner_skill = await SkillServiceResolver(service).resolve(request)
        assert runner_skill.entrypoint == (
            tmp_path / "installed" / "private-demo" / "1.0.0" / "main.py"
        ).resolve()

        with pytest.raises(SkillNotFoundError):
            await service.resolve_execution_skill(
                users["manager0002"].id, skill.id, "1.0.0"
            )
        with pytest.raises(SkillNotFoundError):
            await service.resolve_execution_skill(
                users["manager0001"].id, skill.id, "2.0.0"
            )

        (tmp_path / "installed" / "private-demo" / "1.0.0" / "main.py").write_text(
            "print('tampered')", encoding="utf-8"
        )
        with pytest.raises(SkillConflictError, match="integrity"):
            await service.resolve_execution_skill(
                users["manager0001"].id, skill.id, "1.0.0"
            )
        with pytest.raises(SkillResolutionError):
            await SkillServiceResolver(service).resolve(request)

    asyncio.run(scenario(tmp_path, check))


def test_near_limit_package_install_idempotent_and_publish_share_budget(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        upload = boundary_package()
        installed = await service.install(admin, upload)
        assert (await service.install(admin, upload)).id == installed.id
        assert (await service.publish(admin, installed.id)).status == "published"

    asyncio.run(scenario(tmp_path, check))


def test_policy_lock_serializes_independent_read_snapshot_and_disable(
    tmp_path, monkeypatch
):
    async def check(db, users):
        root = tmp_path / "installed"
        first_service = SkillService(db, root)
        admin = principal(users["business_admin01"])
        skill = await first_service.install(admin, package())
        await first_service.publish(admin, skill.id)
        await first_service.authorize(admin, skill.id, users["manager0001"].id)
        started = asyncio.Event()
        release = asyncio.Event()
        original = SkillRepository.effective

        async def blocked_read(repository, user_id):
            rows = await original(repository, user_id)
            started.set()
            await release.wait()
            return rows

        monkeypatch.setattr(SkillRepository, "effective", blocked_read)
        snapshot_task = asyncio.create_task(
            first_service.effective_skills(users["manager0001"].id)
        )
        await started.wait()
        async with AsyncSession(db.bind, expire_on_commit=False) as second_db:
            second_service = SkillService(second_db, root)
            disable_task = asyncio.create_task(second_service.disable(admin, skill.id))
            await asyncio.sleep(0)
            assert not disable_task.done()
            release.set()
            old_snapshot = await snapshot_task
            assert [item.name for item in old_snapshot] == ["private-demo"]
            await disable_task
            monkeypatch.setattr(SkillRepository, "effective", original)
            assert await first_service.effective_skills(users["manager0001"].id) == ()
            assert [item.name for item in old_snapshot] == ["private-demo"]

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
        upload_one = package("1.0.0")
        one = await service.install(admin, upload_one)
        assert (await service.install(admin, upload_one)).id == one.id
        assert (await service.install(admin, upload_one + b"different")).id == one.id
        with pytest.raises(SkillConflictError):
            await service.install(
                admin,
                package("1.0.0", main_source="print('changed')"),
            )
        two = await service.install(admin, package("2.0.0"))
        await service.publish(admin, one.id)
        await service.authorize(admin, one.id, users["manager0001"].id)
        await service.publish(admin, two.id)
        await db.refresh(one)
        assert one.status == "draft"
        assert two.status == "published"
        assert await service.effective_skills(users["manager0001"].id) == ()
        await service.publish(admin, one.id)
        assert await service.effective_skills(users["manager0001"].id) == ()
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


def test_audit_failure_rolls_back_install_and_files(tmp_path, monkeypatch):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(AuditRepository, "add_pending", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await service.install(principal(users["business_admin01"]), package())
        assert await db.scalar(select(func.count()).select_from(SkillRow)) == 0
        assert not (tmp_path / "installed" / "private-demo" / "1.0.0").exists()

    asyncio.run(scenario(tmp_path, check))


def test_cleanup_failure_leaves_uncommitted_marker_and_is_explicit(tmp_path, monkeypatch):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        real_commit = db.commit
        db.commit = AsyncMock(side_effect=RuntimeError("commit failed"))

        def locked_tree(_path):
            raise PermissionError("file is occupied")

        monkeypatch.setattr("app.skills.service.shutil.rmtree", locked_tree)
        try:
            with pytest.raises(SkillCleanupError, match="cleanup"):
                await service.install(principal(users["business_admin01"]), package())
        finally:
            db.commit = real_commit
        destination = tmp_path / "installed" / "private-demo" / "1.0.0"
        assert (destination / UNCOMMITTED_MARKER).is_file()
        assert await db.scalar(select(func.count()).select_from(SkillRow)) == 0

    asyncio.run(scenario(tmp_path, check))


def test_postcommit_marker_removal_failure_keeps_committed_row_quarantined(
    tmp_path, monkeypatch
):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        original_unlink = Path.unlink
        upload = package()

        def locked_marker(path, *args, **kwargs):
            if path.name == UNCOMMITTED_MARKER:
                raise PermissionError("marker occupied")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked_marker)
        with pytest.raises(SkillCleanupError, match="finalize"):
            await service.install(principal(users["business_admin01"]), upload)
        row = await db.scalar(select(SkillRow))
        assert row is not None
        assert (Path(row.install_path) / UNCOMMITTED_MARKER).is_file()
        assert await service.effective_skills(users["manager0001"].id) == ()
        with pytest.raises(SkillConflictError, match="uncommitted"):
            await service.publish(principal(users["business_admin01"]), row.id)
        monkeypatch.setattr(Path, "unlink", original_unlink)
        recovered = await service.install(
            principal(users["business_admin01"]), upload
        )
        assert recovered.id == row.id
        assert not (Path(row.install_path) / UNCOMMITTED_MARKER).exists()
        await service.publish(principal(users["business_admin01"]), row.id)

    from pathlib import Path
    asyncio.run(scenario(tmp_path, check))


def test_publish_and_idempotent_install_verify_canonical_disk_state(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        skill = await service.install(admin, package())
        destination = tmp_path / "installed" / "private-demo" / "1.0.0"
        (destination / "main.py").write_text("tampered")
        with pytest.raises(SkillConflictError, match="integrity"):
            await service.install(admin, package())
        with pytest.raises(SkillConflictError, match="integrity"):
            await service.publish(admin, skill.id)

        (destination / "main.py").write_text("print('ok')")
        skill.install_path = str(tmp_path / "outside")
        await db.commit()
        with pytest.raises(SkillConflictError, match="path"):
            await service.publish(admin, skill.id)

    asyncio.run(scenario(tmp_path, check))


def test_publish_rejects_database_name_that_escapes_install_root(tmp_path):
    async def check(db, users):
        root = tmp_path / "installed"
        service = SkillService(db, root)
        admin = principal(users["business_admin01"])
        skill = await service.install(admin, package())
        outside = tmp_path / "1.0.0"
        shutil.copytree(Path(skill.install_path), outside)
        skill.name = ".."
        skill.install_path = str(root / ".." / "1.0.0")
        await db.commit()
        with pytest.raises(SkillConflictError, match="escapes"):
            await service.publish(admin, skill.id)

    from pathlib import Path
    asyncio.run(scenario(tmp_path, check))


def test_publish_rejects_uncommitted_marker(tmp_path):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        skill = await service.install(admin, package())
        Path(skill.install_path, UNCOMMITTED_MARKER).write_text("pending")
        with pytest.raises(SkillConflictError, match="uncommitted"):
            await service.publish(admin, skill.id)

    from pathlib import Path
    asyncio.run(scenario(tmp_path, check))


def test_disk_rescan_rejects_oversize_and_excess_entries_without_read_bytes(
    tmp_path, monkeypatch
):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        skill = await service.install(admin, package())
        destination = Path(skill.install_path)
        (destination / "main.py").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda _path: (_ for _ in ()).throw(AssertionError("unbounded read")),
        )
        with pytest.raises(SkillConflictError, match="limit"):
            await service.publish(admin, skill.id)

        (destination / "main.py").write_text("print('ok')")
        (destination / "unexpected-empty").mkdir()
        with pytest.raises(SkillConflictError, match="file set"):
            await service.install(admin, package())
        (destination / "unexpected-empty").rmdir()
        monkeypatch.setattr("app.skills.service.MAX_MATERIALIZED_ENTRIES", 2)
        with pytest.raises(SkillConflictError, match="materialized"):
            await service.install(admin, package())

    from pathlib import Path
    asyncio.run(scenario(tmp_path, check))


def test_disk_rescan_rejects_reparse_points_and_resolved_nested_escape(
    tmp_path, monkeypatch
):
    async def check(db, users):
        service = SkillService(db, tmp_path / "installed")
        admin = principal(users["business_admin01"])
        skill = await service.install(admin, package())

        monkeypatch.setattr("app.skills.service._is_reparse_point", lambda _stat: True)
        with pytest.raises(SkillConflictError, match="reparse"):
            await service.publish(admin, skill.id)

        monkeypatch.setattr("app.skills.service._is_reparse_point", lambda _stat: False)
        original_resolve = Path.resolve
        outside = tmp_path / "escaped-main.py"
        outside.write_text("print('ok')")

        def escaped_nested(path, *args, **kwargs):
            if path.name == "main.py":
                return outside
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", escaped_nested)
        with pytest.raises(SkillConflictError, match="escapes"):
            await service.install(admin, package())

    from pathlib import Path
    asyncio.run(scenario(tmp_path, check))


def test_schema_has_partial_unique_published_name_index(tmp_path):
    async def check(db, _users):
        connection = await db.connection()
        indexes = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_indexes("skills")
        )
        index = next(item for item in indexes if item["name"] == "uq_skills_one_published_name")
        assert index["unique"] == 1
        predicate = index["dialect_options"]["sqlite_where"]
        assert "published" in predicate.text

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


def test_concurrent_publish_across_services_leaves_one_published_version(tmp_path):
    async def run():
        engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publish.db'}")
        root = tmp_path / "installed"
        try:
            await create_schema(engine)
            async with AsyncSession(engine, expire_on_commit=False) as setup_db:
                await seed_demo_data(setup_db)
                admin = await setup_db.scalar(
                    select(User).where(User.username == "business_admin01")
                )
                actor = principal(admin)
                service = SkillService(setup_db, root)
                one = await service.install(actor, package("1.0.0"))
                two = await service.install(actor, package("2.0.0"))
                one_id, two_id = one.id, two.id
            async with (
                AsyncSession(engine, expire_on_commit=False) as first_db,
                AsyncSession(engine, expire_on_commit=False) as second_db,
            ):
                await asyncio.gather(
                    SkillService(first_db, root).publish(actor, one_id),
                    SkillService(second_db, root).publish(actor, two_id),
                )
            async with AsyncSession(engine) as verify_db:
                published = list(
                    await verify_db.scalars(
                        select(SkillRow).where(SkillRow.status == "published")
                    )
                )
                assert len(published) == 1
                assert published[0].id in {one_id, two_id}
        finally:
            await engine.dispose()

    asyncio.run(run())
