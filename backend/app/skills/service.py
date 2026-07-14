from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4
from weakref import WeakValueDictionary

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import Principal
from app.db.models import Role, SkillRow
from app.repositories.audit import AuditRepository
from app.repositories.skill import SkillRepository
from app.skills.package import (
    MAX_MATERIALIZED_ENTRIES,
    MAX_PATH_DEPTH,
    MAX_FILES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    SkillPackageError,
    _safe_member_parts,
    validate_skill_zip,
)


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


_policy_loop: asyncio.AbstractEventLoop | None = None
_policy_lock: asyncio.Lock | None = None
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


def _policy_lock_for_running_loop() -> asyncio.Lock:
    """Return the one policy lock for the process's active event loop.

    Tests create sequential loops with ``asyncio.run``; replacing an unlocked
    lock after the prior loop closes preserves the single-live-loop boundary.
    """
    global _policy_loop, _policy_lock
    loop = asyncio.get_running_loop()
    if _policy_loop is not loop:
        if _policy_lock is not None and _policy_lock.locked():
            raise RuntimeError("Skill policy lock cannot span event loops")
        _policy_loop = loop
        _policy_lock = asyncio.Lock()
    assert _policy_lock is not None
    return _policy_lock


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


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _scan_installed_skill(
    root: Path,
    *,
    expected_hashes: dict[str, str],
    expected_directories: tuple[str, ...],
    expected_canonical_hash: str,
    expected_skill_md_hash: str,
    expected_entrypoint: str,
    expected_upload_hash: str,
    allow_marker: bool,
) -> bool:
    """Synchronously rescan a package with upload-equivalent bounds."""
    derived_directories = {
        "/".join(parts[:index])
        for name in expected_hashes
        for parts in (name.split("/"),)
        for index in range(1, len(parts))
    }
    if set(expected_directories) != derived_directories or len(
        expected_directories
    ) != len(derived_directories):
        raise SkillConflictError("Skill integrity check failed: directory contract")
    if len(expected_hashes) + len(derived_directories) > MAX_MATERIALIZED_ENTRIES:
        raise SkillConflictError("Skill integrity check failed: materialized budget")
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise SkillConflictError("Skill integrity check failed: directory missing") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise SkillConflictError("Skill integrity check failed: unsafe root")
    if _is_reparse_point(root_stat):
        raise SkillConflictError("Skill integrity check failed: reparse point")
    root_resolved = root.resolve(strict=True)
    materialized_entries = 0
    files = 0
    total = 0
    marker_present = False
    records: dict[str, tuple[Path, int]] = {}
    expected_directory_set = set(expected_directories)
    aliases: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = os.scandir(directory)
        except OSError as exc:
            raise SkillConflictError("Skill integrity scan failed") from exc
        with children:
            for child in children:
                path = Path(child.path)
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise SkillConflictError("Skill integrity scan failed") from exc
                if stat.S_ISLNK(child_stat.st_mode) or _is_reparse_point(child_stat):
                    raise SkillConflictError("Skill integrity check failed: reparse point")
                resolved = path.resolve(strict=True)
                if os.path.commonpath((str(root_resolved), str(resolved))) != str(root_resolved):
                    raise SkillConflictError("Skill integrity check failed: path escapes root")
                relative = path.relative_to(root).as_posix()
                if relative == UNCOMMITTED_MARKER:
                    if not allow_marker or not stat.S_ISREG(child_stat.st_mode):
                        raise SkillConflictError("Skill is uncommitted and cannot be used")
                    if child_stat.st_size > 128:
                        raise SkillConflictError("Skill integrity check failed: invalid marker")
                    if path.read_text(encoding="ascii") != expected_upload_hash:
                        raise SkillConflictError("Skill integrity check failed: invalid marker")
                    marker_present = True
                    continue
                materialized_entries += 1
                if materialized_entries > MAX_MATERIALIZED_ENTRIES:
                    raise SkillConflictError(
                        "Skill integrity check failed: too many materialized entries"
                    )
                try:
                    parts = _safe_member_parts(relative)
                except SkillPackageError as exc:
                    raise SkillConflictError("Skill integrity check failed: unsafe name") from exc
                if len(parts) > MAX_PATH_DEPTH:
                    raise SkillConflictError("Skill integrity check failed: path depth")
                alias = "/".join(part.casefold() for part in parts)
                if alias in aliases:
                    raise SkillConflictError("Skill integrity check failed: path alias")
                aliases.add(alias)
                if stat.S_ISDIR(child_stat.st_mode):
                    if relative not in expected_directory_set:
                        raise SkillConflictError(
                            "Skill integrity check failed: file set changed"
                        )
                    stack.append(path)
                    continue
                if not stat.S_ISREG(child_stat.st_mode):
                    raise SkillConflictError("Skill integrity check failed: non-regular entry")
                files += 1
                total += child_stat.st_size
                if files > MAX_FILES or child_stat.st_size > MAX_FILE_BYTES:
                    raise SkillConflictError("Skill integrity check failed: file limit exceeded")
                if total > MAX_TOTAL_BYTES:
                    raise SkillConflictError("Skill integrity check failed: total size exceeded")
                records[relative] = (path, child_stat.st_size)
    if set(records) != set(expected_hashes):
        raise SkillConflictError("Skill integrity check failed: file set changed")
    canonical = hashlib.sha256()
    observed_hashes: dict[str, str] = {}
    observed_total = 0
    for name in sorted(records):
        path, size = records[name]
        encoded = name.encode("utf-8")
        canonical.update(len(encoded).to_bytes(8, "big"))
        canonical.update(encoded)
        canonical.update(size.to_bytes(8, "big"))
        file_hash = hashlib.sha256()
        observed_size = 0
        with path.open("rb") as input_file:
            while chunk := input_file.read(64 * 1024):
                observed_size += len(chunk)
                observed_total += len(chunk)
                if observed_size > MAX_FILE_BYTES or observed_total > MAX_TOTAL_BYTES:
                    raise SkillConflictError("Skill integrity check failed: streaming size limit")
                file_hash.update(chunk)
                canonical.update(chunk)
        if observed_size != size:
            raise SkillConflictError("Skill integrity check failed: file size changed")
        observed_hashes[name] = file_hash.hexdigest()
    if observed_hashes != expected_hashes:
        raise SkillConflictError("Skill integrity check failed: file content changed")
    if canonical.hexdigest() != expected_canonical_hash:
        raise SkillConflictError("Skill integrity check failed: canonical hash changed")
    if observed_hashes.get("SKILL.md") != expected_skill_md_hash:
        raise SkillConflictError("Skill integrity check failed: SKILL.md changed")
    if expected_entrypoint not in records:
        raise SkillConflictError("Skill integrity check failed: entrypoint missing")
    return marker_present


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
    def __init__(
        self,
        db: AsyncSession,
        install_root: Path,
        *,
        read_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._db = db
        self._repository = SkillRepository(db)
        self._install_root = Path(install_root).resolve()
        self._read_session_factory = read_session_factory

    @staticmethod
    def _require_admin(actor: Principal) -> None:
        if actor.role not in (Role.BUSINESS_ADMIN, Role.SYSTEM_ADMIN):
            raise SkillPermissionError("Skill governance requires a global admin")

    def _destination(self, name: str, version: str) -> Path:
        try:
            _safe_member_parts(f"{name}/{version}")
        except SkillPackageError as exc:
            raise SkillConflictError(
                "derived Skill path contains unsafe metadata or escapes install root"
            ) from exc
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
        request_id: str | None = None,
        result: str = "success",
    ) -> None:
        AuditRepository(self._db).add_pending(
            actor_user_id=actor.user_id,
            action=action,
            resource_type="skill",
            resource_id=skill_id,
            result=result,
            request_id=request_id,
            details={"operation": operation, "status": status},
        )

    async def _verify_integrity(
        self, row: SkillRow, *, allow_marker: bool = False
    ) -> tuple[Path, bool]:
        expected = self._destination(row.name, row.version)
        if Path(row.install_path) != expected:
            raise SkillConflictError("stored Skill path is not the canonical install path")
        marker = await asyncio.to_thread(
            _scan_installed_skill,
            expected,
            expected_hashes=dict(row.file_sha256),
            expected_directories=tuple(row.expected_directories),
            expected_canonical_hash=row.content_sha256,
            expected_skill_md_hash=row.skill_md_sha256,
            expected_entrypoint=row.entrypoint,
            expected_upload_hash=row.upload_sha256,
            allow_marker=allow_marker,
        )
        return expected, marker

    async def install(
        self, actor: Principal, upload, request_id: str | None = None
    ) -> SkillRow:
        async with _policy_lock_for_running_loop():
            return await self._install_locked(actor, upload, request_id)

    async def _install_locked(
        self, actor: Principal, upload, request_id: str | None
    ) -> SkillRow:
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
                # ZIP container metadata (notably DOS timestamps) may differ
                # while the validated canonical files are byte-identical.
                # Idempotency is therefore content-based, while the original
                # upload digest remains retained for provenance.
                if existing.content_sha256 != package.content_sha256:
                    raise SkillConflictError("Skill name and version already exists")
                destination, marker = await self._verify_integrity(
                    existing, allow_marker=True
                )
                if marker:
                    await asyncio.to_thread(_fsync_directory, destination.parent)
                    try:
                        (destination / UNCOMMITTED_MARKER).unlink()
                    except OSError as exc:
                        raise SkillCleanupError(
                            "Skill finalize failed; package remains quarantined"
                        ) from exc
                self._audit_pending(
                    actor,
                    "skill.install",
                    existing.id,
                    "install",
                    "idempotent",
                    request_id,
                )
                await self._db.commit()
                return existing

            self._install_root.mkdir(parents=True, exist_ok=True)
            destination = self._destination(manifest.name, manifest.version)
            version_parent = destination.parent
            staging = self._install_root / f".install-{uuid4().hex}"
            moved = False
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
                    expected_directories=list(package.expected_directories),
                    validation_warnings=list(package.warnings),
                    install_path=str(destination),
                )
                await self._repository.add(row)
                self._audit_pending(
                    actor, "skill.install", row.id, "install", "success", request_id
                )
                await self._db.flush()
                if destination.exists():
                    raise SkillConflictError("Skill destination already exists")
                os.replace(staging, destination)
                moved = True
                await asyncio.to_thread(_fsync_directory, version_parent)
                await asyncio.to_thread(_fsync_directory, destination)
                await self._db.commit()
            except IntegrityError as exc:
                await self._db.rollback()
                original: BaseException = SkillConflictError(
                    "Skill name and version already exists"
                )
                original.__cause__ = exc
            except BaseException as exc:
                await self._db.rollback()
                original = exc

            else:
                try:
                    (destination / UNCOMMITTED_MARKER).unlink()
                except OSError as exc:
                    raise SkillCleanupError(
                        "Skill finalize failed; committed package remains quarantined"
                    ) from exc
                return row

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

    async def publish(
        self, actor: Principal, skill_id: str, request_id: str | None = None
    ) -> SkillRow:
        async with _policy_lock_for_running_loop():
            return await self._publish_locked(actor, skill_id, request_id)

    async def _publish_locked(
        self, actor: Principal, skill_id: str, request_id: str | None
    ) -> SkillRow:
        self._require_admin(actor)
        row = await self._repository.get(skill_id)
        if row is None:
            raise SkillNotFoundError(skill_id)
        lock = _weak_lock(_publish_locks, (str(self._install_root), row.name))
        async with lock:
            await self._db.refresh(row)
            await self._verify_integrity(row)
            try:
                await self._repository.retire_published_versions(row.name, row.id)
                row.status = "published"
                self._audit_pending(
                    actor, "skill.publish", row.id, "update", "enabled", request_id
                )
                await self._db.commit()
            except IntegrityError as exc:
                await self._db.rollback()
                raise SkillConflictError("another version was published concurrently") from exc
            except BaseException:
                await self._db.rollback()
                raise
        return row

    async def disable(
        self, actor: Principal, skill_id: str, request_id: str | None = None
    ) -> SkillRow:
        async with _policy_lock_for_running_loop():
            return await self._disable_locked(actor, skill_id, request_id)

    async def _disable_locked(
        self, actor: Principal, skill_id: str, request_id: str | None
    ) -> SkillRow:
        self._require_admin(actor)
        row = await self._repository.get(skill_id)
        if row is None:
            raise SkillNotFoundError(skill_id)
        try:
            row.status = "disabled"
            await self._repository.revoke_all(row.id)
            self._audit_pending(
                actor, "skill.disable", row.id, "update", "disabled", request_id
            )
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        return row

    async def authorize(
        self,
        actor: Principal,
        skill_id: str,
        manager_user_id: str,
        request_id: str | None = None,
    ):
        async with _policy_lock_for_running_loop():
            return await self._authorize_locked(
                actor, skill_id, manager_user_id, request_id
            )

    async def _authorize_locked(
        self,
        actor: Principal,
        skill_id: str,
        manager_user_id: str,
        request_id: str | None,
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
            self._audit_pending(
                actor,
                "skill.authorize",
                skill_id,
                "authorize",
                "allowed",
                request_id,
            )
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        return authorization

    async def revoke(
        self,
        actor: Principal,
        skill_id: str,
        manager_user_id: str,
        request_id: str | None = None,
    ) -> bool:
        async with _policy_lock_for_running_loop():
            return await self._revoke_locked(actor, skill_id, manager_user_id, request_id)

    async def _revoke_locked(
        self,
        actor: Principal,
        skill_id: str,
        manager_user_id: str,
        request_id: str | None,
    ) -> bool:
        self._require_admin(actor)
        try:
            revoked = await self._repository.revoke(skill_id, manager_user_id)
            if revoked:
                self._audit_pending(
                    actor, "skill.revoke", skill_id, "revoke", "success", request_id
                )
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        return revoked

    async def effective_skills(self, user_id: str) -> tuple[EffectiveSkill, ...]:
        async with _policy_lock_for_running_loop():
            async with self._read_session_factory() as read_session:
                rows = await SkillRepository(read_session).effective(user_id)
                return tuple(_metadata(row) for row in rows)

    async def resolve_execution_skill(
        self, user_id: str, skill_id: str, version: str | None = None
    ) -> EffectiveSkill:
        """Resolve one effective Skill and recheck its bounded disk integrity.

        Absence, disabled/unpublished state, wrong manager authorization, and a
        version mismatch intentionally share one not-found result so callers do
        not gain an authorization oracle. The policy lock makes governance state
        changes atomic with this snapshot; the controlled runner documents the
        remaining same-account filesystem TOCTOU boundary after this scan.
        """
        async with _policy_lock_for_running_loop():
            async with self._read_session_factory() as read_session:
                rows = await SkillRepository(read_session).effective(user_id)
                row = next(
                    (
                        candidate
                        for candidate in rows
                        if candidate.id == skill_id
                        and (version is None or candidate.version == version)
                    ),
                    None,
                )
                if row is None:
                    raise SkillNotFoundError(skill_id)
                await self._verify_integrity(row)
                return _metadata(row)
