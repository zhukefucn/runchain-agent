from __future__ import annotations

import asyncio
import ctypes
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from app.runner.controlled_process import RunnerLimits, SkillExecutor
from app.runner.protocol import (
    ResolvedSkill,
    SkillExecutionRequest,
    SkillResolutionError,
)
from app.skills.package import validate_skill_zip


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


class StaticResolver:
    def __init__(self, skill: ResolvedSkill | None = None, error: Exception | None = None):
        self.skill = skill
        self.error = error
        self.calls = 0

    async def resolve(self, request: SkillExecutionRequest) -> ResolvedSkill:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.skill is not None
        return self.skill


def _script_skill(tmp_path: Path, source: str, *, skill_type: str = "python") -> ResolvedSkill:
    root = tmp_path / "installed" / "test-skill" / "1.0.0"
    root.mkdir(parents=True)
    entrypoint = root / "scripts" / "main.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text(source, encoding="utf-8")
    return ResolvedSkill(
        skill_id="skill-1",
        version="1.0.0",
        type=skill_type,
        install_root=root,
        entrypoint=entrypoint,
    )


def _request(input_data: dict | None = None) -> SkillExecutionRequest:
    return SkillExecutionRequest(
        user_id="manager-user-1",
        skill_id="skill-1",
        version="1.0.0",
        input_data=input_data or {},
        request_id="request-1",
    )


def _executor(tmp_path: Path, resolver: StaticResolver, **limit_overrides) -> SkillExecutor:
    values = {
        "timeout_seconds": 2.0,
        "max_input_bytes": 32_768,
        "max_stdout_bytes": 32_768,
        "max_stderr_bytes": 4_096,
        "max_processes": 4,
        "max_memory_bytes": 256 * 1024 * 1024,
    }
    values.update(limit_overrides)
    limits = RunnerLimits(**values)
    return SkillExecutor(resolver=resolver, runner_root=tmp_path / "runner", limits=limits)


@async_test
async def test_runner_executes_single_json_request_and_response(tmp_path):
    skill = _script_skill(
        tmp_path,
        "import json, sys\n"
        "data = json.loads(sys.stdin.buffer.read())\n"
        "print(json.dumps({'answer': data['value'] * 2}))\n",
    )
    resolver = StaticResolver(skill)

    result = await _executor(tmp_path, resolver).execute(_request({"value": 21}))

    assert result.status == "success"
    assert result.output == {"answer": 42}
    assert result.exit_code == 0
    assert result.stderr_summary == ""
    assert resolver.calls == 1


@async_test
@pytest.mark.parametrize(
    ("source", "expected_status"),
    [
        ("print('not-json')\n", "invalid_output"),
        ("print('{\"value\":NaN}')\n", "invalid_output"),
        ("import sys\nsys.exit(7)\n", "failed"),
    ],
)
async def test_runner_rejects_invalid_json_and_nonzero_exit(tmp_path, source, expected_status):
    runner = _executor(tmp_path, StaticResolver(_script_skill(tmp_path, source)))

    result = await runner.execute(_request())

    assert result.status == expected_status
    assert result.output is None
    assert "Traceback" not in result.stderr_summary


@async_test
async def test_runner_times_out_and_removes_call_directory(tmp_path):
    skill = _script_skill(tmp_path, "import time\ntime.sleep(30)\n")
    runner = _executor(
        tmp_path,
        StaticResolver(skill),
        timeout_seconds=0.25,
    )

    result = await runner.execute(_request())

    assert result.status == "timeout"
    assert not list((tmp_path / "runner").glob("call-*"))


@async_test
async def test_timeout_also_bounds_child_that_never_reads_large_stdin(tmp_path):
    skill = _script_skill(tmp_path, "import time\ntime.sleep(2)\n")
    runner = _executor(
        tmp_path,
        StaticResolver(skill),
        timeout_seconds=0.2,
        max_input_bytes=256 * 1024,
    )
    started = time.monotonic()

    result = await runner.execute(_request({"payload": "x" * 200_000}))

    assert result.status == "timeout"
    assert time.monotonic() - started < 1.0


@async_test
@pytest.mark.parametrize(
    ("stream", "expected_status"),
    [("stdout", "output_limit"), ("stderr", "stderr_limit")],
)
async def test_runner_terminates_on_bounded_stream_overflow(
    tmp_path, stream, expected_status
):
    target = "sys.stdout" if stream == "stdout" else "sys.stderr"
    source = f"import sys, time\n{target}.write('x' * 200000)\n{target}.flush()\ntime.sleep(30)\n"
    skill = _script_skill(tmp_path, source)
    runner = _executor(
        tmp_path,
        StaticResolver(skill),
        max_stdout_bytes=512,
        max_stderr_bytes=512,
    )

    result = await runner.execute(_request())

    assert result.status == expected_status
    assert len(result.stderr_summary.encode("utf-8")) <= 512


@async_test
async def test_runner_does_not_inherit_sensitive_or_proxy_environment(tmp_path, monkeypatch):
    secrets = {
        "MODEL_API_KEY": "sentinel-model-secret",
        "JWT_SECRET_KEY": "sentinel-jwt-secret",
        "AWS_SECRET_ACCESS_KEY": "sentinel-cloud-secret",
        "HTTPS_PROXY": "http://sentinel-proxy",
        "CUSTOM_PASSWORD": "sentinel-password",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    source = (
        "import json, os\n"
        f"keys = {list(secrets)!r}\n"
        "print(json.dumps({'present': [key for key in keys if key in os.environ]}))\n"
    )
    runner = _executor(tmp_path, StaticResolver(_script_skill(tmp_path, source)))

    result = await runner.execute(_request())

    assert result.status == "success"
    assert result.output == {"present": []}
    serialized = json.dumps(result.output) + result.stderr_summary
    assert not any(value in serialized for value in secrets.values())


@async_test
async def test_audit_callback_receives_metadata_only(tmp_path):
    skill = _script_skill(
        tmp_path,
        "import json\nprint(json.dumps({'ok': True}))\n",
    )
    events = []
    secret = "business-input-must-not-be-audited"
    runner = SkillExecutor(
        resolver=StaticResolver(skill),
        runner_root=tmp_path / "runner",
        audit_sink=events.append,
    )

    result = await runner.execute(_request({"notes": secret}))

    assert result.status == "success"
    assert len(events) == 1
    assert secret not in repr(events[0])
    assert not hasattr(events[0], "input_data")
    assert not hasattr(events[0], "output")


@async_test
async def test_runner_uses_absolute_python_isolated_argv_without_shell(
    tmp_path, monkeypatch
):
    skill = _script_skill(
        tmp_path / "path with & shell chars",
        "import json\nprint(json.dumps({'ok': True}))\n",
    )
    calls: list[tuple[object, dict]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        calls.append((args[0], dict(kwargs)))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("app.runner.controlled_process.subprocess.Popen", recording_popen)

    result = await _executor(tmp_path, StaticResolver(skill)).execute(_request())

    assert result.status == "success"
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]).resolve() == Path(sys.executable).resolve()
    assert argv[1] == "-I"
    assert kwargs.get("shell") is False


@async_test
async def test_runner_rejects_unauthorized_tampered_and_non_python_skills(tmp_path):
    for error in (
        SkillResolutionError("skill is not authorized"),
        SkillResolutionError("skill integrity check failed"),
    ):
        result = await _executor(tmp_path, StaticResolver(error=error)).execute(_request())
        assert result.status == "not_authorized"
        assert result.output is None
        assert str(tmp_path) not in result.stderr_summary

    prompt_skill = _script_skill(tmp_path / "prompt", "", skill_type="prompt")
    result = await _executor(tmp_path, StaticResolver(prompt_skill)).execute(_request())
    assert result.status == "unsupported_skill_type"
    assert result.output is None


@async_test
async def test_runner_rejects_entrypoint_outside_install_root(tmp_path):
    skill = _script_skill(tmp_path, "")
    outside = tmp_path / "outside.py"
    outside.write_text("print('{}')", encoding="utf-8")
    escaped = ResolvedSkill(
        skill_id=skill.skill_id,
        version=skill.version,
        type="python",
        install_root=skill.install_root,
        entrypoint=outside,
    )

    result = await _executor(tmp_path, StaticResolver(escaped)).execute(_request())

    assert result.status == "not_authorized"
    assert str(outside) not in result.stderr_summary


def _pid_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)


@async_test
@pytest.mark.skipif(os.name != "nt", reason="Phase 1 Job Object verification is Windows-only")
async def test_windows_job_terminates_grandchild_on_timeout(tmp_path):
    marker = tmp_path / "grandchild.pid"
    skill = _script_skill(
        tmp_path,
        "import json, subprocess, sys, time\n"
        "data = json.loads(sys.stdin.buffer.read())\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "open(data['marker'], 'w', encoding='ascii').write(str(child.pid))\n"
        "time.sleep(30)\n",
    )
    runner = _executor(
        tmp_path,
        StaticResolver(skill),
        timeout_seconds=0.5,
    )

    result = await runner.execute(_request({"marker": str(marker)}))

    assert result.status == "timeout"
    pid = int(marker.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while _pid_is_running(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _pid_is_running(pid)


@async_test
async def test_cancellation_terminates_process_tree_and_cleans_temp(tmp_path):
    marker = tmp_path / "child.pid"
    skill = _script_skill(
        tmp_path,
        "import json, subprocess, sys, time\n"
        "data = json.loads(sys.stdin.buffer.read())\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "open(data['marker'], 'w', encoding='ascii').write(str(child.pid))\n"
        "time.sleep(30)\n",
    )
    events = []
    runner = SkillExecutor(
        resolver=StaticResolver(skill),
        runner_root=tmp_path / "runner",
        limits=RunnerLimits(timeout_seconds=10),
        audit_sink=events.append,
    )
    task = asyncio.create_task(runner.execute(_request({"marker": str(marker)})))
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pid = int(marker.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while _pid_is_running(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _pid_is_running(pid)
    assert not list((tmp_path / "runner").glob("call-*"))
    assert len(events) == 1
    assert events[0].status == "failed"


def test_request_rejects_oversized_or_non_object_input():
    with pytest.raises((TypeError, ValueError)):
        SkillExecutionRequest(
            user_id="manager-user-1",
            skill_id="skill-1",
            input_data=["not", "an", "object"],  # type: ignore[arg-type]
            request_id="request-1",
        )
    with pytest.raises(TypeError):
        SkillExecutionRequest(
            user_id="manager-user-1",
            skill_id="skill-1",
            input_data={},
            request_id="request-1",
            command="python evil.py",  # type: ignore[call-arg]
        )


@async_test
async def test_temp_creation_failure_is_generic_and_audited(tmp_path, monkeypatch):
    skill = _script_skill(tmp_path, "print('{}')\n")
    events = []
    runner = SkillExecutor(
        resolver=StaticResolver(skill),
        runner_root=tmp_path / "runner",
        audit_sink=events.append,
    )
    monkeypatch.setattr(
        "app.runner.controlled_process.tempfile.mkdtemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("C:\\secret\\runner-root")),
    )

    result = await runner.execute(_request())

    assert result.status == "failed"
    assert "secret" not in result.stderr_summary
    assert len(events) == 1


@async_test
async def test_temp_acl_setup_failure_removes_partial_call_directory(tmp_path, monkeypatch):
    skill = _script_skill(tmp_path, "print('{}')\n")
    runner = _executor(tmp_path, StaticResolver(skill))
    monkeypatch.setattr(
        "app.runner.controlled_process.os.chmod",
        lambda *_args: (_ for _ in ()).throw(OSError("ACL denied")),
    )

    result = await runner.execute(_request())

    assert result.status == "failed"
    assert not list((tmp_path / "runner").glob("call-*"))


@async_test
async def test_cleanup_failure_cannot_return_success(tmp_path, monkeypatch):
    skill = _script_skill(
        tmp_path,
        "import json\nprint(json.dumps({'ok': True}))\n",
    )
    runner = _executor(tmp_path, StaticResolver(skill))
    monkeypatch.setattr(
        "app.runner.controlled_process.shutil.rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup denied")),
    )

    result = await runner.execute(_request())

    assert result.status == "failed"
    assert result.output is None


@pytest.mark.parametrize("name", ["reception-dining", "private-demo"])
def test_demo_skill_is_a_valid_installable_package(name, tmp_path):
    source = Path(__file__).parents[3] / "demo-skills" / name
    archive = tmp_path / f"{name}.zip"
    import zipfile

    with zipfile.ZipFile(archive, "w") as output:
        for path in source.rglob("*"):
            if path.is_file():
                output.write(path, f"{name}/{path.relative_to(source).as_posix()}")

    package = validate_skill_zip(archive.read_bytes())

    assert package.manifest.type == "python"
    assert package.manifest.entrypoint == "scripts/main.py"


@async_test
@pytest.mark.parametrize(
    ("name", "input_data", "expected_key"),
    [
        (
            "reception-dining",
            {
                "guest_count": 4,
                "preferences": ["清淡"],
                "allergies": ["花生"],
                "budget": 800,
            },
            "recommendations",
        ),
        ("private-demo", {"message": "hello"}, "capability"),
    ],
)
async def test_demo_skills_execute_as_deterministic_json(
    name, input_data, expected_key, tmp_path
):
    root = Path(__file__).parents[3] / "demo-skills" / name
    skill = ResolvedSkill(
        skill_id=name,
        version="1.0.0",
        type="python",
        install_root=root,
        entrypoint=root / "scripts" / "main.py",
    )
    request = SkillExecutionRequest(
        user_id="manager-user-1",
        skill_id=name,
        version="1.0.0",
        input_data=input_data,
        request_id=f"request-{name}",
    )

    first = await _executor(tmp_path, StaticResolver(skill)).execute(request)
    second = await _executor(tmp_path, StaticResolver(skill)).execute(request)

    assert first.status == second.status == "success"
    assert first.output == second.output
    assert first.output["mock"] is True
    assert expected_key in first.output
