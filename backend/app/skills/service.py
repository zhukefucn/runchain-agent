from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
from uuid import uuid4
from weakref import WeakValueDictionary

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Principal
from app.db.models import Role, SkillRow
from app.repositories.audit import AuditRepository
from app.repositories.skill import SkillRepository
from app.skills.package import canonical_content_sha256, validate_skill_zip


UNCOMMITTED_MARKER = ".runchain-uncommitted"
logger = logging.getLogger(__name__)


class SkillPermissionError(PermissionError):
    pass


class SkillConflictError(RuntimeError):
    pass


class SkillCleanupError(RuntimeError):
    pass


class SkillNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class EffectiveSkill:
    id: str
    name: str
    version: str
    type: str
    description: str
    entrypoint: str
    install_path: str
    content_sha256: str
    skill_md_sha256: str
    parameters_json: str
    warnings: tuple[str, ...]


def _metadata(row: SkillRow) -> EffectiveSkill:
    return EffectiveSkill(
        id=row.id,
        name=row.name,
        version=row.version,
        type=row.type,
        description=row.description,
        entrypoint=row.entrypoint,
        install_path=row.install_path,
        content_sha256=row.content_sha256,
        skill_md_sha256=row.skill_md_sha256,
        parameters_json=json.dumps(
            row.manifest.get("parameters", {}), sort_keys=True, separators=(",", ":")
        ),
        warnings=tuple(row.validation_warnings),
    )


class SkillRuntimeCache:
    """Single-process cache guarded by a generation against stale refills."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._generation = 0
        self._values: dict[str, tuple[int, tuple[EffectiveSkill, ...]]] = {}

    async def snapshot(
        self, user_id: str
    ) -> tuple[int, tuple[EffectiveSkill, ...] | None]:
        async with self._lock:
            cached = self._values.get(user_id)
            if cached is not None and cached[0] == self._generation:
                return self._generation, cached[1]
            return self._generation, None

    async def put_if_current(
        self,
        user_id: str,
        generation: int,
        skills: tuple[EffectiveSkill, ...],
    ) -> bool:
        async with self._lock:
            if generation != self._generation:
                return False
            self._values[user_id] = (generation, skills)
            return True

    async def invalidate(self, user_id: str | None = None) -> None:
        async with self._lock:
            self._generation += 1
            if user_id is None:
                self._values.clear()
            else:
                self._values.pop(user_id, None)


runtime_skill_cache = SkillRuntimeCache()
_install_locks: WeakValueDictionary[tuple[str, str, str], asyncio.Lock] = (
    WeakValueDictionary()
)
_publish_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    WeakValueDictionary()
)


def _weak_lock(registry, key) -> asyncio.Lock:
    lock = registry.get(key)
    if lock is None:
        lock = asyncio.Lock()
        registry[key] = lock
    return lock


def _fsync_directory(path: Path) -> None:
    # Windows does not provide a portable Python directory fsync. Atomic rename
    # plus the on-disk marker is the durability/integrity boundary for Phase 1.
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path)
    if path.exists():
        raise OSError(f"cleanup did not remove {path}")


def _write_marker(path: Path, upload_sha256: str) -> None:
    with path.open("x", encoding="ascii") as marker:
        marker.write(upload_sha256)
        marker.flush()
        os.fsync(marker.fileno())


def _remove_empty_parent(path: Path, install_root: Path) -> None:
    if path == install_root or not path.exists():
        return
    try:
        path.rmdir()
    except OSError:
        if path.exists() and any(path.iterdir()):
            return
        raise


class SkillService:
    def __init__(self, db: AsyncSession, install_root: Path) -> None:
        self._db = db
        self._repository = SkillRepository(db)
        self._install_root = Path(install_root).resolve()

    @staticmethod
    def _require_admin(actor: Principal) -> None:
        if actor.role not in (Role.BUSINESS_ADMIN, Role.SYSTEM_ADMIN):
            raise SkillPermissionError("Skill governance requires a global admin")

    def _destination(self, name: str, version: str) -> Path:
        destination = (self._install_root / name / version).resolve()
        if self._install_root not in destination.parents:
            raise SkillConflictError("derived Skill path escapes install root")
        return destination

    def _audit_pending(
        self,
        actor: Principal,
        action: str,
        skill_id: str,
        operation: str,
        status: str,
    ) -> None:
        AuditRepository(self._db).add_pending(
            actor_user_id=actor.user_id,
            action=action,
            resource_type="skill",
            resource_id=skill_id,
            result="success",
            request_id=None,
            details={"operation": operation, "status": status},
        )

    def _verify_integrity(self, row: SkillRow) -> Path:
        expected = self._destination(row.name, row.version)
        if Path(row.install_path) != expected:
            raise SkillConflictError("stored Skill path is not the canonical install path")
        if not expected.is_dir() or expected.is_symlink():
            raise SkillConflictError("Skill integrity check failed: directory missing")
        if (expected / UNCOMMITTED_MARKER).exists():
            raise SkillConflictError("Skill is uncommitted and cannot be used")
        files: dict[str, bytes] = {}
        for child in expected.rglob("*"):
            if child.is_symlink():
                raise SkillConflictError("Skill integrity check failed: symlink found")
            if child.is_file():
                relative = child.relative_to(expected).as_posix()
                files[relative] = child.read_bytes()
            elif not child.is_dir():
                raise SkillConflictError("Skill integrity check failed: non-regular entry")
        if set(files) != set(row.file_sha256):
            raise SkillConflictError("Skill integrity check failed: file set changed")
        for name, expected_hash in row.file_sha256.items():
            if hashlib.sha256(files[name]).hexdigest() != expected_hash:
                raise SkillConflictError("Skill integrity check failed: file content changed")
        if canonical_content_sha256(files) != row.content_sha256:
            raise SkillConflictError("Skill integrity check failed: canonical hash changed")
        if hashlib.sha256(files["SKILL.md"]).hexdigest() != row.skill_md_sha256:
            raise SkillConflictError("Skill integrity check failed: SKILL.md changed")
        if row.entrypoint not in files:
            raise SkillConflictError("Skill integrity check failed: entrypoint missing")
        return expected

    async def install(self, actor: Principal, upload) -> SkillRow:
        self._require_admin(actor)
        package = validate_skill_zip(upload)
        manifest = package.manifest
        lock = _weak_lock(
            _install_locks,
            (str(self._install_root), manifest.name, manifest.version),
        )
        async with lock:
            existing = await self._repository.get_version(manifest.name, manifest.version)
            if existing is not None:
                if existing.upload_sha256 != package.upload_sha256:
                    raise SkillConflictError("Skill name and version already exists")
                self._verify_integrity(existing)
                return existing

            self._install_root.mkdir(parents=True, exist_ok=True)
            destination = self._destination(manifest.name, manifest.version)
            version_parent = destination.parent
            staging = self._install_root / f".install-{uuid4().hex}"
            moved = False
            committed = False
            row: SkillRow | None = None
            try:
                staging.mkdir()
                for relative, content in package.files.items():
                    target = staging.joinpath(*relative.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                _write_marker(staging / UNCOMMITTED_MARKER, package.upload_sha256)
                if destination.exists():
                    raise SkillConflictError("Skill destination already exists")
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
                    skill_md_sha256=package.skill_md_sha256,
                    file_sha256=package.file_sha256,
                    validation_warnings=list(package.warnings),
                    install_path=str(destination),
                )
                await self._repository.add(row)
                self._audit_pending(actor, "skill.install", row.id, "install", "success")
                await self._db.flush()
                if destination.exists():
                    raise SkillConflictError("Skill destination already exists")
                os.replace(staging, destination)
                moved = True
                await self._db.commit()
                committed = True
                _fsync_directory(version_parent)
                (destination / UNCOMMITTED_MARKER).unlink()
                _fsync_directory(destination)
                return row
            except IntegrityError as exc:
                await self._db.rollback()
                original: BaseException = SkillConflictError(
                    "Skill name and version already exists"
                )
                original.__cause__ = exc
            except BaseException as exc:
                await self._db.rollback()
                original = exc

            if committed:
                marker = destination / UNCOMMITTED_MARKER
                if not marker.exists():
                    try:
                        _write_marker(marker, package.upload_sha256)
                    except BaseException:
                        logger.exception("Failed to restore Skill quarantine marker at %s", marker)
                raise SkillCleanupError(
                    "Skill finalize failed; committed package remains quarantined"
                ) from original

            cleanup_target = destination if moved else staging
            try:
                _remove_tree(cleanup_target)
                _remove_empty_parent(version_parent, self._install_root)
            except BaseException as cleanup_error:
                logger.exception("Skill install cleanup failed for %s", cleanup_target)
                raise SkillCleanupError(
                    f"Skill install cleanup failed: {cleanup_target}"
                ) from cleanup_error
            raise original

    async def publish(self, actor: Principal, skill_id: str) -> SkillRow:
        self._require_admin(actor)
        row = await self._repository.get(skill_id)
        if row is None:
            raise SkillNotFoundError(skill_id)
        lock = _weak_lock(_publish_locks, (str(self._install_root), row.name))
        async with lock:
            await self._db.refresh(row)
            self._verify_integrity(row)
            try:
                await self._repository.retire_published_versions(row.name, row.id)
                row.status = "published"
                self._audit_pending(actor, "skill.publish", row.id, "update", "enabled")
                await self._db.commit()
            except IntegrityError as exc:
                await self._db.rollback()
                raise SkillConflictError("another version was published concurrently") from exc
            except BaseException:
                await self._db.rollback()
                raise
        await runtime_skill_cache.invalidate()
        return row

    async def disable(self, actor: Principal, skill_id: str) -> SkillRow:
        self._require_admin(actor)
        row = await self._repository.get(skill_id)
        if row is None:
            raise SkillNotFoundError(skill_id)
        try:
            row.status = "disabled"
            await self._repository.revoke_all(row.id)
            self._audit_pending(actor, "skill.disable", row.id, "update", "disabled")
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        await runtime_skill_cache.invalidate()
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
            self._audit_pending(actor, "skill.authorize", skill_id, "authorize", "allowed")
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        await runtime_skill_cache.invalidate(manager_user_id)
        return authorization

    async def revoke(
        self, actor: Principal, skill_id: str, manager_user_id: str
    ) -> bool:
        self._require_admin(actor)
        try:
            revoked = await self._repository.revoke(skill_id, manager_user_id)
            if revoked:
                self._audit_pending(actor, "skill.revoke", skill_id, "revoke", "success")
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        await runtime_skill_cache.invalidate(manager_user_id)
        return revoked

    async def effective_skills(self, user_id: str) -> list[EffectiveSkill]:
        while True:
            generation, cached = await runtime_skill_cache.snapshot(user_id)
            if cached is not None:
                return list(cached)
            rows = await self._repository.effective(user_id)
            skills = tuple(_metadata(row) for row in rows)
            if await runtime_skill_cache.put_if_current(user_id, generation, skills):
                return list(skills)
