from __future__ import annotations

import asyncio
from dataclasses import asdict
import os
from pathlib import Path
import shutil
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Principal
from app.db.models import Role, SkillRow
from app.repositories.audit import AuditRepository
from app.repositories.skill import SkillRepository
from app.skills.package import validate_skill_zip


class SkillPermissionError(PermissionError):
    pass


class SkillConflictError(RuntimeError):
    pass


class SkillNotFoundError(LookupError):
    pass


class SkillRuntimeCache:
    def __init__(self) -> None:
        self._values: dict[str, list[SkillRow]] = {}

    def get(self, user_id: str) -> list[SkillRow] | None:
        value = self._values.get(user_id)
        return None if value is None else list(value)

    def put(self, user_id: str, skills: list[SkillRow]) -> None:
        self._values[user_id] = list(skills)

    def invalidate(self, user_id: str | None = None) -> None:
        if user_id is None:
            self._values.clear()
        else:
            self._values.pop(user_id, None)


class SkillService:
    _locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def __init__(
        self,
        db: AsyncSession,
        install_root: Path,
        *,
        cache: SkillRuntimeCache | None = None,
    ) -> None:
        self._db = db
        self._repository = SkillRepository(db)
        self._install_root = Path(install_root)
        self._cache = cache or SkillRuntimeCache()

    @staticmethod
    def _require_admin(actor: Principal) -> None:
        if actor.role not in (Role.BUSINESS_ADMIN, Role.SYSTEM_ADMIN):
            raise SkillPermissionError("Skill governance requires a global admin")

    async def _audit(
        self,
        actor: Principal,
        action: str,
        skill_id: str,
        operation: str,
        status: str,
    ) -> None:
        await AuditRepository(self._db).record(
            actor_user_id=actor.user_id,
            action=action,
            resource_type="skill",
            resource_id=skill_id,
            result="success",
            request_id=None,
            details={"operation": operation, "status": status},
        )

    async def install(self, actor: Principal, upload) -> SkillRow:
        self._require_admin(actor)
        package = validate_skill_zip(upload)
        manifest = package.manifest
        root_key = str(self._install_root.resolve())
        lock = self._locks.setdefault((root_key, manifest.name, manifest.version), asyncio.Lock())
        async with lock:
            existing = await self._repository.get_version(manifest.name, manifest.version)
            if existing is not None:
                if existing.upload_sha256 == package.upload_sha256:
                    return existing
                raise SkillConflictError("Skill name and version already exists")

            self._install_root.mkdir(parents=True, exist_ok=True)
            install_root = self._install_root.resolve(strict=True)
            version_parent = install_root / manifest.name
            destination = version_parent / manifest.version
            staging = install_root / f".install-{uuid4().hex}"
            if destination.exists():
                raise SkillConflictError("Skill destination already exists")

            moved = False
            committed = False
            try:
                staging.mkdir()
                for relative, content in package.files.items():
                    target = staging.joinpath(*relative.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                version_parent.mkdir(parents=True, exist_ok=True)
                row = SkillRow(
                    created_by_user_id=actor.user_id,
                    name=manifest.name,
                    version=manifest.version,
                    type=manifest.type,
                    status="draft",
                    description=manifest.description,
                    entrypoint=manifest.entrypoint,
                    manifest=asdict(manifest),
                    upload_sha256=package.upload_sha256,
                    content_sha256=package.content_sha256,
                    install_path=str(destination),
                )
                await self._repository.add(row)
                if destination.exists():
                    raise SkillConflictError("Skill destination already exists")
                os.replace(staging, destination)
                moved = True
                await self._db.commit()
                committed = True
            except IntegrityError as exc:
                await self._db.rollback()
                raise SkillConflictError("Skill name and version already exists") from exc
            except BaseException:
                await self._db.rollback()
                raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                # A failed/rolled-back transaction must not leave runnable files.
                if moved and not committed:
                    shutil.rmtree(destination, ignore_errors=True)
            self._cache.invalidate()
            await self._audit(actor, "skill.install", row.id, "install", "success")
            return row

    async def publish(self, actor: Principal, skill_id: str) -> SkillRow:
        self._require_admin(actor)
        row = await self._repository.get(skill_id)
        if row is None:
            raise SkillNotFoundError(skill_id)
        if not Path(row.install_path).is_dir():
            raise SkillConflictError("installed Skill files are missing")
        try:
            await self._repository.retire_published_versions(row.name, row.id)
            row.status = "published"
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        self._cache.invalidate()
        await self._audit(actor, "skill.publish", row.id, "update", "enabled")
        return row

    async def disable(self, actor: Principal, skill_id: str) -> SkillRow:
        self._require_admin(actor)
        row = await self._repository.get(skill_id)
        if row is None:
            raise SkillNotFoundError(skill_id)
        row.status = "disabled"
        try:
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        self._cache.invalidate()
        await self._audit(actor, "skill.disable", row.id, "update", "disabled")
        return row

    async def authorize(
        self, actor: Principal, skill_id: str, manager_user_id: str
    ):
        self._require_admin(actor)
        if await self._repository.get(skill_id) is None:
            raise SkillNotFoundError(skill_id)
        if not await self._repository.manager_is_active(manager_user_id):
            raise SkillPermissionError("authorization target must be an active manager")
        try:
            authorization = await self._repository.authorize(
                skill_id, manager_user_id, actor.user_id
            )
            await self._db.commit()
        except IntegrityError:
            await self._db.rollback()
            authorization = await self._repository.authorize(
                skill_id, manager_user_id, actor.user_id
            )
        except BaseException:
            await self._db.rollback()
            raise
        self._cache.invalidate(manager_user_id)
        await self._audit(actor, "skill.authorize", skill_id, "authorize", "allowed")
        return authorization

    async def revoke(
        self, actor: Principal, skill_id: str, manager_user_id: str
    ) -> bool:
        self._require_admin(actor)
        try:
            revoked = await self._repository.revoke(skill_id, manager_user_id)
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        self._cache.invalidate(manager_user_id)
        if revoked:
            await self._audit(actor, "skill.revoke", skill_id, "revoke", "success")
        return revoked

    async def effective_skills(self, user_id: str) -> list[SkillRow]:
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        skills = await self._repository.effective(user_id)
        self._cache.put(user_id, skills)
        return list(skills)
