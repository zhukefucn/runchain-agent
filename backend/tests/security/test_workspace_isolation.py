from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import app.agentscope_ext.workspace_manager as workspace_manager_module
from app.agentscope_ext.workspace_manager import ManagerLocalWorkspaceManager


@dataclass(frozen=True)
class SessionIdentity:
    owner_user_id: str
    agent_id: str
    session_id: str
    workspace_id: str | None = None


class FakeSessionResolver:
    def __init__(self, sessions: dict[tuple[str, str], SessionIdentity]) -> None:
        self._sessions = sessions

    async def resolve_session(
        self, owner_user_id: str, session_id: str
    ) -> SessionIdentity | None:
        return self._sessions.get((owner_user_id, session_id))


@pytest.fixture
def manager(tmp_path: Path) -> ManagerLocalWorkspaceManager:
    resolver = FakeSessionResolver(
        {
            ("manager0001", "s1"): SessionIdentity("manager0001", "default", "s1"),
            ("manager0001", "s2"): SessionIdentity("manager0001", "default", "s2"),
            ("manager0002", "s2"): SessionIdentity("manager0002", "default", "s2"),
        }
    )
    return ManagerLocalWorkspaceManager(tmp_path / "workspace", resolver)


def test_same_agent_id_for_two_users_has_distinct_workdirs(manager):
    one = asyncio.run(manager.get_workspace("manager0001", "default", "s1"))
    two = asyncio.run(manager.get_workspace("manager0002", "default", "s2"))

    assert one.workdir != two.workdir
    assert Path(one.workdir).parts[-3:] == ("manager0001", "agents", "default")
    assert Path(two.workdir).parts[-3:] == ("manager0002", "agents", "default")


def test_same_manager_agent_sessions_share_agentscope_per_agent_workspace(manager):
    one = asyncio.run(manager.get_workspace("manager0001", "default", "s1"))
    two = asyncio.run(manager.get_workspace("manager0001", "default", "s2"))

    assert one is two
    assert one.workspace_id == two.workspace_id
    user_root = manager.root / "manager0001"
    assert Path(one.workdir) == user_root / "agents" / "default"
    assert (user_root / "sessions" / "s1").is_dir()
    assert (user_root / "sessions" / "s2").is_dir()
    assert not Path(one.workdir, "sessions", "s1").exists()


@pytest.mark.parametrize(
    ("user_id", "agent_id", "session_id"),
    [
        ("../manager0001", "default", "s1"),
        ("manager0001", "..\\default", "s1"),
        ("manager0001", "C:default", "s1"),
        ("manager0001", "default", "\\\\server\\share"),
        ("manager0001", "default", "NUL"),
        ("manager0001", "default", "name. "),
        ("manager0001\x00", "default", "s1"),
        ("Manager0001", "default", "s1"),
        ("manager0001", "Default", "s1"),
        ("manager0001", "default", "S1"),
        ("managerK0001", "default", "s1"),
    ],
)
def test_identifiers_must_be_safe_single_segments(
    manager, user_id, agent_id, session_id
):
    with pytest.raises(ValueError):
        asyncio.run(manager.get_workspace(user_id, agent_id, session_id))


def test_foreign_nonexistent_and_wrong_agent_sessions_fail_before_mkdir(tmp_path):
    root = tmp_path / "workspace"
    resolver = FakeSessionResolver(
        {
            ("manager0001", "s1"): SessionIdentity(
                "manager0001", "other-agent", "s1"
            )
        }
    )
    manager = ManagerLocalWorkspaceManager(root, resolver)

    with pytest.raises(PermissionError):
        asyncio.run(manager.get_workspace("manager0001", "default", "s1"))
    with pytest.raises(PermissionError):
        asyncio.run(manager.get_workspace("manager0002", "default", "s1"))
    with pytest.raises(PermissionError):
        asyncio.run(manager.get_workspace("manager0001", "default", "missing"))

    assert not root.exists()


@pytest.mark.parametrize(
    "identity",
    [
        SessionIdentity("Manager0001", "default", "s1"),
        SessionIdentity("manager0001", "Default", "s1"),
        SessionIdentity("manager0001", "default", "S1"),
    ],
)
def test_case_aliased_authoritative_binding_is_rejected_without_side_effects(
    tmp_path, identity
):
    root = tmp_path / "workspace"
    resolver = FakeSessionResolver({("manager0001", "s1"): identity})
    manager = ManagerLocalWorkspaceManager(root, resolver)

    with pytest.raises(PermissionError):
        asyncio.run(manager.get_workspace("manager0001", "default", "s1"))

    assert not root.exists()


def test_explicit_workspace_binding_cannot_be_reused_across_managers(manager):
    other_workspace_id = manager.assign_workspace_id(
        user_id="manager0002", agent_id="default", session_id="s2"
    )

    with pytest.raises(PermissionError):
        asyncio.run(
            manager.get_workspace(
                "manager0001", "default", "s1", workspace_id=other_workspace_id
            )
        )


def test_team_worker_uses_its_persisted_path_safe_leader_workspace(tmp_path):
    shared_workspace = "0123456789abcdef"
    resolver = FakeSessionResolver(
        {
            ("manager0001", "worker-session"): SessionIdentity(
                "manager0001",
                "worker-agent",
                "worker-session",
                shared_workspace,
            )
        }
    )
    manager = ManagerLocalWorkspaceManager(tmp_path / "workspace", resolver)

    workspace = asyncio.run(
        manager.get_workspace(
            "manager0001",
            "worker-agent",
            "worker-session",
            workspace_id=shared_workspace,
        )
    )

    assert workspace.workspace_id == shared_workspace
    assert Path(workspace.workdir).parts[-3:] == (
        "manager0001",
        "agents",
        "worker-agent",
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../manager0002/secret.txt",
        "a/../../b",
        "a\\..\\..\\b",
        "C:\\secret.txt",
        "C:secret.txt",
        "\\secret.txt",
        "\\\\server\\share\\secret.txt",
        "//server/share/secret.txt",
        "safe/file.txt:stream",
        "safe/NUL.txt",
        "safe/conin$.txt",
        "safe/conout$",
        "safe/com¹.txt",
        "safe/lpt²",
        "safe/COM1.txt",
    ],
)
def test_resolve_manager_path_rejects_windows_and_mixed_traversal(
    manager, relative_path
):
    with pytest.raises(ValueError):
        manager.resolve_manager_path("manager0001", relative_path)


def test_resolve_manager_path_accepts_contained_path(manager):
    resolved = manager.resolve_manager_path("manager0001", "uploads/report.txt")

    expected = (manager.root / "manager0001" / "uploads" / "report.txt").resolve()
    assert resolved == expected


@pytest.mark.parametrize("relative_path", ["Report.txt", "报告/结果.txt"])
def test_resolve_manager_path_accepts_normal_case_and_unicode_filenames(
    manager, relative_path
):
    resolved = manager.resolve_manager_path("manager0001", relative_path)

    assert os.path.commonpath((manager.root, resolved)) == str(manager.root)


@pytest.mark.parametrize(
    "device_name",
    [
        "con",
        "con.txt",
        "conin$",
        "conout$.txt",
        "com1",
        "lpt9.txt",
        "com¹",
        "lpt³.txt",
        "nul. ",
    ],
)
def test_windows_device_aliases_are_rejected_as_identifiers(tmp_path, device_name):
    manager = ManagerLocalWorkspaceManager(
        tmp_path / "workspace", FakeSessionResolver({})
    )

    with pytest.raises(ValueError):
        asyncio.run(manager.get_workspace(device_name, "default", "s1"))


def test_constructor_checks_configured_root_itself_before_resolving(
    monkeypatch, tmp_path
):
    root = (tmp_path / "configured-link").absolute()
    checked: list[Path] = []

    def fake_is_reparse(path: Path) -> bool:
        checked.append(path)
        return path == root

    monkeypatch.setattr(workspace_manager_module, "_is_reparse_point", fake_is_reparse)
    with pytest.raises(ValueError):
        ManagerLocalWorkspaceManager(root, FakeSessionResolver({}))
    assert root in checked


def test_constructor_checks_existing_parent_junction_before_resolving(
    monkeypatch, tmp_path
):
    parent = (tmp_path / "parent-junction").absolute()
    parent.mkdir()
    root = parent / "workspace"

    monkeypatch.setattr(
        workspace_manager_module,
        "_is_reparse_point",
        lambda path: path == parent,
    )
    with pytest.raises(ValueError):
        ManagerLocalWorkspaceManager(root, FakeSessionResolver({}))


def test_close_logs_workspace_shutdown_failures(manager, monkeypatch, caplog):
    workspace = asyncio.run(manager.get_workspace("manager0001", "default", "s1"))

    async def fail_close():
        raise RuntimeError("close boom")

    monkeypatch.setattr(workspace, "close", fail_close)
    with caplog.at_level("WARNING"):
        asyncio.run(manager.close(workspace.workspace_id))
    assert "Failed to close manager workspace" in caplog.text


def test_close_all_logs_workspace_shutdown_failures(manager, monkeypatch, caplog):
    workspace = asyncio.run(manager.get_workspace("manager0001", "default", "s1"))

    async def fail_close():
        raise RuntimeError("close-all boom")

    monkeypatch.setattr(workspace, "close", fail_close)
    with caplog.at_level("WARNING"):
        asyncio.run(manager.close_all())
    assert "Failed to close manager workspace" in caplog.text


def test_resolve_manager_path_rejects_symlink_or_junction_escape(manager, tmp_path):
    user_root = manager.root / "manager0001"
    user_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = user_root / "escape"
    try:
        if os.name == "nt":
            os.symlink(outside, link, target_is_directory=True)
        else:
            link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this test account")

    with pytest.raises(ValueError):
        manager.resolve_manager_path("manager0001", "escape/secret.txt")
