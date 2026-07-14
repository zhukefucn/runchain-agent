from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Protocol

from agentscope.app.workspace_manager import IsolationPolicy, WorkspaceManagerBase
from agentscope.workspace import LocalWorkspace


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z", re.ASCII)
logger = logging.getLogger(__name__)


class _ManagerLocalWorkspace(LocalWorkspace):
    """Local AgentScope workspace with an explicit no-helper glob contract."""

    @property
    def _glob_helper_path(self) -> None:
        return None


class SessionIdentity(Protocol):
    owner_user_id: str
    agent_id: str
    session_id: str


class SessionOwnerResolver(Protocol):
    async def resolve_session(
        self, owner_user_id: str, session_id: str
    ) -> SessionIdentity | None: ...


def _reject_windows_alias(
    value: str, *, field: str, require_lowercase_ascii: bool
) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if unicodedata.normalize("NFKC", value) != value:
        raise ValueError(f"{field} must already be NFKC-normalized")
    if require_lowercase_ascii and value.casefold() != value:
        raise ValueError(f"{field} must use canonical lowercase ASCII")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} contains a control character")
    if any(character in _WINDOWS_FORBIDDEN_CHARS for character in value):
        raise ValueError(f"{field} contains a Windows path metacharacter")
    if value in {".", ".."} or value.endswith((".", " ")):
        raise ValueError(f"{field} is not a safe path segment")

    windows_path = PureWindowsPath(value)
    if windows_path.drive or windows_path.root or len(windows_path.parts) != 1:
        raise ValueError(f"{field} must be one relative path segment")
    device_stem = value.split(".", 1)[0].upper()
    if device_stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{field} is a reserved Windows device name")


def _validate_segment(value: str, *, field: str) -> str:
    """Validate a canonical identifier used as exactly one path segment."""
    _reject_windows_alias(value, field=field, require_lowercase_ascii=True)
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be canonical lowercase ASCII")
    return value


def _validate_path_segment(value: str, *, field: str) -> str:
    """Validate a stable normal filename/directory segment."""
    _reject_windows_alias(value, field=field, require_lowercase_ascii=False)
    if len(value) > 255:
        raise ValueError(f"{field} exceeds the filesystem segment limit")
    return value


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _canonicalize_root(root: str | os.PathLike[str]) -> Path:
    """Reject reparse components before resolving the configured root."""
    expanded = os.path.expanduser(os.fspath(root))
    unresolved = Path(os.path.abspath(expanded))
    current = Path(unresolved.anchor)
    if _is_reparse_point(current):
        raise ValueError("workspace root path crosses a reparse point")
    for part in unresolved.parts[1:]:
        current = current / part
        if _is_reparse_point(current):
            raise ValueError("workspace root path crosses a reparse point")
    return unresolved.resolve(strict=False)


def _reject_existing_reparse_points(root: Path, candidate: Path) -> None:
    """Reject links/junctions in an existing portion of a candidate path."""
    current = root
    if _is_reparse_point(current):
        raise ValueError("workspace root cannot be a symlink or reparse point")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes workspace root") from exc
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            raise ValueError("workspace path crosses a symlink or reparse point")


def _is_contained(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((str(root), str(candidate)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


class ManagerLocalWorkspaceManager(WorkspaceManagerBase):
    """AgentScope local workspace manager with manager-owner isolation.

    AgentScope's native local workspace is per-agent. Accordingly, sessions
    for one manager/agent share the agent workspace while this demo manager
    provisions their individual context below ``sessions/<session_id>``. The owner id is
    included in both the cache key and filesystem path.

    Filesystem checks reduce link/junction escape risk, but local filesystem
    operations cannot eliminate a privileged actor's check/use race. The demo
    workspace root must therefore remain writable only by the service account.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        session_resolver: SessionOwnerResolver,
        *,
        default_mcps: list | None = None,
        skill_paths: list[str] | None = None,
    ) -> None:
        super().__init__(isolation=IsolationPolicy.PER_AGENT)
        self.root = _canonicalize_root(root)
        self._session_resolver = session_resolver
        self._default_mcps = list(default_mcps or [])
        self._skill_paths = list(skill_paths or [])
        self._cache: dict[tuple[str, str, str], LocalWorkspace] = {}
        self._lock = asyncio.Lock()

    def resolve_manager_path(
        self, user_id: str, relative_path: str | os.PathLike[str]
    ) -> Path:
        """Resolve an untrusted relative path below one manager's root."""
        safe_user_id = _validate_segment(user_id, field="user_id")
        raw = os.fspath(relative_path)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("relative_path must be a non-empty string")

        windows_path = PureWindowsPath(raw)
        if (
            windows_path.is_absolute()
            or windows_path.drive
            or windows_path.root
            or windows_path.anchor
            or Path(raw).is_absolute()
        ):
            raise ValueError("absolute, rooted, UNC, and drive paths are forbidden")
        parts = windows_path.parts
        if not parts:
            raise ValueError("relative_path must name a path")
        safe_parts = [
            _validate_path_segment(part, field="relative_path segment")
            for part in parts
        ]

        user_root = (self.root / safe_user_id).resolve(strict=False)
        raw_candidate = user_root.joinpath(*safe_parts)
        _reject_existing_reparse_points(self.root, raw_candidate)
        resolved = raw_candidate.resolve(strict=False)
        if not _is_contained(user_root, resolved):
            raise ValueError("path escapes manager workspace")
        return resolved

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> LocalWorkspace:
        safe_user_id = _validate_segment(user_id, field="user_id")
        safe_agent_id = _validate_segment(agent_id, field="agent_id")
        safe_session_id = _validate_segment(session_id, field="session_id")
        binding = await self._session_resolver.resolve_session(
            safe_user_id, safe_session_id
        )
        if binding is not None:
            try:
                bound_owner = _validate_segment(
                    binding.owner_user_id, field="resolved owner_user_id"
                )
                bound_agent = _validate_segment(
                    binding.agent_id, field="resolved agent_id"
                )
                bound_session = _validate_segment(
                    binding.session_id, field="resolved session_id"
                )
                raw_bound_workspace = getattr(binding, "workspace_id", None)
                bound_workspace = (
                    _validate_segment(
                        raw_bound_workspace,
                        field="resolved workspace_id",
                    )
                    if raw_bound_workspace is not None
                    else None
                )
            except (AttributeError, ValueError) as exc:
                raise PermissionError(
                    "session resolver returned an unsafe binding"
                ) from exc
        else:
            bound_owner = bound_agent = bound_session = bound_workspace = None
        if (
            binding is None
            or bound_owner != safe_user_id
            or bound_agent != safe_agent_id
            or bound_session != safe_session_id
        ):
            raise PermissionError("session does not belong to this manager and agent")

        expected_workspace_id = bound_workspace or self.assign_workspace_id(
            user_id=safe_user_id,
            agent_id=safe_agent_id,
            session_id=safe_session_id,
        )
        if workspace_id is not None:
            supplied_workspace_id = _validate_segment(
                workspace_id, field="workspace_id"
            )
            if supplied_workspace_id != expected_workspace_id:
                raise PermissionError(
                    "workspace binding does not match manager and agent"
                )
        workspace_id = expected_workspace_id
        safe_workspace_id = _validate_segment(workspace_id, field="workspace_id")
        cache_key = (safe_user_id, safe_agent_id, safe_workspace_id)
        session_dir = self.resolve_manager_path(
            safe_user_id,
            Path("sessions") / safe_session_id,
        )

        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                session_dir.mkdir(parents=True, exist_ok=True)
                return cached

            workdir = self.resolve_manager_path(
                safe_user_id, Path("agents") / safe_agent_id
            )
            workspace = _ManagerLocalWorkspace(
                workdir=str(workdir),
                workspace_id=safe_workspace_id,
                default_mcps=self._default_mcps,
                skill_paths=self._skill_paths,
            )
            await workspace.initialize()
            session_dir.mkdir(parents=True, exist_ok=True)
            self._cache[cache_key] = workspace
            return workspace

    async def close(self, workspace_id: str) -> None:
        safe_workspace_id = _validate_segment(workspace_id, field="workspace_id")
        async with self._lock:
            matching = [
                (key, workspace)
                for key, workspace in self._cache.items()
                if key[2] == safe_workspace_id
            ]
            for key, _ in matching:
                self._cache.pop(key)
        await asyncio.gather(
            *(self._safe_close(workspace) for _, workspace in matching)
        )

    async def close_all(self) -> None:
        async with self._lock:
            workspaces = list(self._cache.values())
            self._cache.clear()
        await asyncio.gather(*(self._safe_close(workspace) for workspace in workspaces))

    @staticmethod
    async def _safe_close(workspace: LocalWorkspace) -> None:
        try:
            await workspace.close()
        except Exception:
            logger.warning(
                "Failed to close manager workspace %s",
                workspace.workspace_id,
                exc_info=True,
            )


__all__ = ["ManagerLocalWorkspaceManager", "SessionOwnerResolver"]
