from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


def _make_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _source(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["start.ps1", "stop.ps1", "reset-demo.ps1", "test.ps1"])
def test_lifecycle_script_is_valid_powershell(name: str) -> None:
    script = SCRIPTS / name
    command = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}', "
        "[ref]$null, [ref]$errors); "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_start_builds_one_server_and_probes_the_full_demo_surface() -> None:
    source = _source("start.ps1").lower()
    assert "docker" not in source
    assert "pnpm" in source and "build" in source
    assert "frontend\\dist" in source
    assert "uvicorn" in source
    assert "--port 8000" in source
    assert "5173" not in source
    assert "/api/health" in source
    assert "/api/ready" in source
    assert "http://127.0.0.1:8000/" in source
    assert "processstarttimeutc" in source
    assert "executablepath" in source
    assert "runid" in source
    # Startup failures must reach the owned-process cleanup block even though
    # ErrorActionPreference is Stop.
    assert 'write-error "demo startup failed' not in source


def test_stop_verifies_recorded_process_identity_before_stopping() -> None:
    source = _source("stop.ps1").lower()
    for required in (
        "get-ciminstance",
        "win32_process",
        "processstarttimeutc",
        "executablepath",
        "projectroot",
        "runid",
        "commandline",
        "stop-process -id",
    ):
        assert required in source
    assert "taskkill" not in source
    assert "get-process -name" not in source
    assert "stop-process -name" not in source


def test_stop_statically_guards_run_directory_and_stale_records() -> None:
    source = _source("stop.ps1").lower()
    assert "test-containedpath" in source
    assert "fileattributes" in source and "reparsepoint" in source
    assert source.index("assert-trustedrecord") < source.index("get-ciminstance")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction verification")
def test_stop_rejects_a_run_directory_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = ROOT / ".run" / f"junction-test-{os.getpid()}"
    link.parent.mkdir(exist_ok=True)
    _make_junction(link, target)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPTS / "stop.ps1"),
                "-RunDirectory",
                str(link),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0
        assert "reparse" in (completed.stdout + completed.stderr).lower()
    finally:
        os.rmdir(link)


def test_stop_refuses_a_forged_pid_record(tmp_path: Path) -> None:
    run_dir = ROOT / ".run" / f"forged-test-{os.getpid()}"
    run_dir.mkdir()
    # PID 4 is a stable Windows process, but this record deliberately claims
    # it is the demo's venv Python.  The stop script must reject it before any
    # Stop-Process call.
    record = {
        "schemaVersion": 1,
        "pid": 4,
        "projectRoot": str(ROOT),
        "runId": "11111111-1111-1111-1111-111111111111",
        "processStartTimeUtc": "2000-01-01T00:00:00.0000000Z",
        "executablePath": str(ROOT / ".venv" / "Scripts" / "python.exe"),
        "launcherPath": str(run_dir / "launcher.py"),
    }
    (run_dir / "server.json").write_text(json.dumps(record), encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPTS / "stop.ps1"),
                "-RunDirectory",
                str(run_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0
        assert "identity" in (completed.stdout + completed.stderr).lower()
    finally:
        shutil.rmtree(run_dir)


def test_stop_rejects_untrusted_stale_record_before_deleting_it() -> None:
    run_dir = ROOT / ".run" / f"stale-test-{os.getpid()}"
    run_dir.mkdir()
    record = {
        "schemaVersion": 1,
        "pid": 2_000_000_000,
        "projectRoot": str(ROOT.parent),
        "runId": "11111111-1111-1111-1111-111111111111",
        "processStartTimeUtc": "2000-01-01T00:00:00.0000000Z",
        "executablePath": str(ROOT / ".venv" / "Scripts" / "python.exe"),
        "launcherPath": str(run_dir / "launcher.py"),
    }
    record_path = run_dir / "server.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPTS / "stop.ps1"),
                "-RunDirectory",
                str(run_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0
        assert record_path.exists()
        assert "identity" in (completed.stdout + completed.stderr).lower()
    finally:
        shutil.rmtree(run_dir)


def test_reset_is_project_scoped_and_preserves_configuration_and_fixtures() -> None:
    source = _source("reset-demo.ps1").lower()
    assert "stop.ps1" in source
    assert "getfullpath" in source and "test-containedpath" in source
    for target in ("data\\demo.db", "workspace", "skills", "runner", ".run\\e2e"):
        assert target in source
    assert "demo-skills" in source
    assert "remove-item -literalpath" in source
    assert "remove-item .env" not in source
    assert "get-childitem" not in source


def test_reset_uses_only_fixed_canonical_targets_and_rejects_reparse_points() -> None:
    source = _source("reset-demo.ps1").lower()
    assert "relativepath" not in source
    assert "approvedtargets" in source
    assert "fileattributes" in source and "reparsepoint" in source


@pytest.mark.skipif(os.name != "nt", reason="Windows junction verification")
def test_reset_rejects_a_runtime_junction_before_removing_anything(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "reset-demo.ps1", scripts / "reset-demo.ps1")
    (scripts / "stop.ps1").write_text(
        "throw 'stop must not run before target validation'\n", encoding="utf-8"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("preserve", encoding="utf-8")
    link = project / "workspace"
    _make_junction(link, outside)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "reset-demo.ps1"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0
        assert "reparse" in (completed.stdout + completed.stderr).lower()
        assert marker.read_text(encoding="utf-8") == "preserve"
    finally:
        if link.exists():
            os.rmdir(link)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction verification")
def test_e2e_server_rejects_a_run_root_junction(tmp_path: Path) -> None:
    from backend.tests.e2e_server import _assert_safe_e2e_root

    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    run_root = root / ".run"
    _make_junction(run_root, outside)
    try:
        with pytest.raises(RuntimeError, match="reparse"):
            _assert_safe_e2e_root(root, run_root / "e2e")
    finally:
        os.rmdir(run_root)


def test_test_script_runs_every_offline_gate_and_keeps_real_model_opt_in() -> None:
    source = _source("test.ps1").lower()
    assert "pytest" in source and "backend/tests" in source
    assert "not real_model" in source
    assert "vitest" in source or "test --run" in source
    assert "--exclude" in source and "tests/e2e/**" in source
    assert "typecheck" in source
    assert "build" in source
    assert "playwright" in source
    assert "run_real_model_tests" in source
    assert "-m real_model" in source
    assert "$previousrealmodeltestsenvironment" in source
    assert '$env:run_real_model_tests = "1"' in source
    assert "remove-item env:run_real_model_tests" in source
