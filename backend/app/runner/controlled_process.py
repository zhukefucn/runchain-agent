from __future__ import annotations

import asyncio
import ctypes
from dataclasses import dataclass
import inspect
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from app.runner.protocol import (
    ResolvedSkill,
    RunnerAuditEvent,
    SkillExecutionRequest,
    SkillExecutionResult,
    SkillResolutionError,
    TrustedSkillResolver,
)
from app.runner.windows_acl import (
    secure_runner_root,
    verify_inherited_call_directory,
)


logger = logging.getLogger(__name__)


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


@dataclass(frozen=True, slots=True)
class RunnerLimits:
    timeout_seconds: float = 15.0
    max_input_bytes: int = 64 * 1024
    max_stdout_bytes: int = 256 * 1024
    max_stderr_bytes: int = 32 * 1024
    max_processes: int = 4
    max_memory_bytes: int = 256 * 1024 * 1024
    max_concurrent_executions: int = 4
    queue_timeout_seconds: float = 5.0
    max_json_depth: int = 64
    max_json_nodes: int = 10_000

    def __post_init__(self) -> None:
        bounds = (
            ("timeout_seconds", self.timeout_seconds, 0.05, 60.0),
            ("max_input_bytes", self.max_input_bytes, 2, 256 * 1024),
            ("max_stdout_bytes", self.max_stdout_bytes, 2, 1024 * 1024),
            ("max_stderr_bytes", self.max_stderr_bytes, 2, 256 * 1024),
            ("max_processes", self.max_processes, 1, 8),
            ("max_memory_bytes", self.max_memory_bytes, 64 * 1024 * 1024, 1024**3),
            ("max_concurrent_executions", self.max_concurrent_executions, 1, 16),
            ("queue_timeout_seconds", self.queue_timeout_seconds, 0.01, 60.0),
            ("max_json_depth", self.max_json_depth, 1, 128),
            ("max_json_nodes", self.max_json_nodes, 1, 100_000),
        )
        for name, value, minimum, maximum in bounds:
            if isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside the safe range")


@dataclass(slots=True)
class _ProcessOutcome:
    status: str
    stdout: bytes
    stderr_size: int
    duration_ms: int
    exit_code: int | None
    cleanup_ok: bool


_gate_loop: asyncio.AbstractEventLoop | None = None
_shared_gates: dict[str, tuple[int, asyncio.BoundedSemaphore]] = {}


def _shared_gate(root: Path, limit: int) -> asyncio.BoundedSemaphore:
    """Return the process-wide, single-live-loop gate for one runner root."""
    global _gate_loop, _shared_gates
    loop = asyncio.get_running_loop()
    if _gate_loop is not loop:
        _gate_loop = loop
        _shared_gates = {}
    key = os.path.normcase(str(root))
    existing = _shared_gates.get(key)
    if existing is not None:
        existing_limit, gate = existing
        if existing_limit != limit:
            raise RuntimeError("runner root has conflicting concurrency limits")
        return gate
    gate = asyncio.BoundedSemaphore(limit)
    _shared_gates[key] = (limit, gate)
    return gate


async def _wait_uncancellable(task: asyncio.Task):
    """Wait for a cleanup task while deferring any repeated cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            continue


class _BoundedReader:
    def __init__(self, stream, limit: int, overflow: threading.Event) -> None:
        self._stream = stream
        self._limit = limit
        self._overflow = overflow
        self.data = bytearray()
        self.size = 0
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(8192)
                if not chunk:
                    return
                self.size += len(chunk)
                remaining = self._limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if self.size > self._limit:
                    self._overflow.set()
        except BaseException as exc:  # surfaced as a fail-closed runner result
            self.error = exc


class _InputWriter:
    def __init__(self, stream, payload: bytes) -> None:
        self._stream = stream
        self._payload = payload
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self._stream.write(self._payload)
            self._stream.flush()
        except (BrokenPipeError, OSError) as exc:
            self.error = exc
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


if os.name == "nt":
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]


class _WindowsJob:
    """Job Object wrapper that assigns a still-suspended child, closing races."""

    _KILL_ON_CLOSE = 0x00002000
    _ACTIVE_PROCESS = 0x00000008
    _PROCESS_TIME = 0x00000002
    _JOB_TIME = 0x00000004
    _PROCESS_MEMORY = 0x00000100
    _JOB_MEMORY = 0x00000200
    _EXTENDED_LIMIT_CLASS = 9

    def __init__(self, limits: RunnerLimits) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.restype = ctypes.c_long
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            self._KILL_ON_CLOSE
            | self._ACTIVE_PROCESS
            | self._PROCESS_TIME
            | self._JOB_TIME
            | self._PROCESS_MEMORY
            | self._JOB_MEMORY
        )
        info.BasicLimitInformation.ActiveProcessLimit = limits.max_processes
        info.BasicLimitInformation.PerProcessUserTimeLimit = int(
            limits.timeout_seconds * 10_000_000
        )
        info.BasicLimitInformation.PerJobUserTimeLimit = int(
            limits.timeout_seconds * 10_000_000
        )
        info.ProcessMemoryLimit = limits.max_memory_bytes
        info.JobMemoryLimit = limits.max_memory_bytes
        ok = self._kernel32.SetInformationJobObject(
            self._handle,
            self._EXTENDED_LIMIT_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            self._raise_configuration_error(error)

    def _raise_configuration_error(self, error: int) -> None:
        if not self.close():
            logger.error(
                "Job configuration failed with WinError %d and close also failed",
                error,
            )
        raise ctypes.WinError(error)

    def assign_and_resume(self, process: subprocess.Popen) -> None:
        process_handle = int(process._handle)  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        status = self._ntdll.NtResumeProcess(process_handle)
        if status != 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")

    def terminate(self) -> bool:
        if not self._handle:
            return True
        return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def active_processes(self) -> int | None:
        if not self._handle:
            return 0
        info = _BASIC_ACCOUNTING_INFORMATION()
        returned = wintypes.DWORD()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            1,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        ):
            return None
        return int(info.ActiveProcesses)

    def terminate_remaining(self, timeout: float = 5.0) -> bool:
        active = self.active_processes()
        if active is None:
            return False
        if active and not self.terminate():
            return False
        deadline = time.monotonic() + timeout
        while active:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
            active = self.active_processes()
            if active is None:
                return False
        return True

    def close(self) -> bool:
        if self._handle:
            if not self._kernel32.CloseHandle(self._handle):
                error = ctypes.get_last_error()
                logger.error("CloseHandle(Job Object) failed with WinError %d", error)
                return False
            self._handle = None
        return True


class SkillExecutor:
    """Execute trusted Python Skills under bounded Windows process controls.

    This is intentionally *not* an OS security sandbox. Skill code retains the
    current Windows account's file and network permissions. The Phase 1 boundary
    is authorization plus bounded disk-integrity verification in the resolver,
    canonical entrypoint checks, a minimal environment, private per-call cwd, and
    Job Object resource/process-tree controls. Inherited Windows ACLs and the
    unavoidable interval between hash verification and process image open leave
    a local same-account ACL/TOCTOU boundary; Ubuntu Phase 2 replaces this backend
    with an OS-isolated container implementation of the same protocol.
    """

    def __init__(
        self,
        *,
        resolver: TrustedSkillResolver,
        runner_root: Path,
        limits: RunnerLimits | None = None,
        audit_sink: Callable[[RunnerAuditEvent], Any] | None = None,
    ) -> None:
        self._resolver = resolver
        self._runner_root = Path(runner_root).absolute()
        self._limits = limits or RunnerLimits()
        self._audit_sink = audit_sink

    async def execute(self, request: SkillExecutionRequest) -> SkillExecutionResult:
        started = time.monotonic()
        try:
            skill = await self._resolver.resolve(request)
        except (SkillResolutionError, PermissionError, LookupError):
            result = self._result("not_authorized", started, "Skill is unavailable.")
            await self._audit_uncancellable(request, result)
            return result

        if skill.type != "python":
            result = self._result(
                "unsupported_skill_type", started, "Skill type is not executable."
            )
            await self._audit_uncancellable(request, result)
            return result

        entrypoint = self._canonical_entrypoint(skill)
        if entrypoint is None:
            result = self._result("not_authorized", started, "Skill is unavailable.")
            await self._audit_uncancellable(request, result)
            return result

        try:
            payload = json.dumps(
                request.input_data,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            result = self._result("invalid_input", started, "Skill input is invalid.")
            await self._audit_uncancellable(request, result)
            return result
        if len(payload) > self._limits.max_input_bytes:
            result = self._result("invalid_input", started, "Skill input is too large.")
            await self._audit_uncancellable(request, result)
            return result

        gate = _shared_gate(
            self._runner_root, self._limits.max_concurrent_executions
        )
        try:
            await asyncio.wait_for(
                gate.acquire(), timeout=self._limits.queue_timeout_seconds
            )
        except asyncio.TimeoutError:
            result = self._result(
                "queue_timeout", started, "Skill execution queue timed out."
            )
            await self._audit_uncancellable(request, result)
            return result

        cancel_event = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(self._run_sync, entrypoint, payload, cancel_event)
        )
        try:
            try:
                outcome = await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel_event.set()
                try:
                    try:
                        outcome = await _wait_uncancellable(worker)
                        cancelled_result = self._convert_outcome(outcome)
                    except Exception:
                        cancelled_result = self._result(
                            "failed", started, "Skill process failed."
                        )
                    await self._audit_uncancellable(request, cancelled_result)
                finally:
                    raise
            except Exception:
                result = self._result("failed", started, "Skill process failed.")
                await self._audit_uncancellable(request, result)
                return result

            try:
                result = self._convert_outcome(outcome)
            except Exception:
                result = self._result("failed", started, "Skill process failed.")
            await self._audit_uncancellable(request, result)
            return result
        finally:
            gate.release()

    def _canonical_entrypoint(self, skill: ResolvedSkill) -> Path | None:
        root = Path(skill.install_root).resolve()
        entrypoint = Path(skill.entrypoint).resolve()
        if root not in entrypoint.parents or not entrypoint.is_file():
            return None
        return entrypoint

    def _minimal_environment(self, call_dir: Path) -> dict[str, str]:
        env: dict[str, str] = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "TEMP": str(call_dir),
            "TMP": str(call_dir),
        }
        if os.name == "nt":
            for key in ("SYSTEMROOT", "WINDIR"):
                value = os.environ.get(key)
                if value:
                    env[key] = value
        return env

    def _spawn(self, argv: list[str], call_dir: Path, env: dict[str, str]):
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(call_dir),
            "env": env,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(argv, **kwargs)

    def _run_sync(
        self, entrypoint: Path, payload: bytes, cancel_event: threading.Event
    ) -> _ProcessOutcome:
        started = time.monotonic()
        call_dir: Path | None = None
        process: subprocess.Popen | None = None
        job: _WindowsJob | None = None
        cleanup_ok = True
        status = "failed"
        stdout = b""
        stderr_size = 0
        exit_code: int | None = None
        try:
            secure_runner_root(self._runner_root)
            if os.name == "nt":
                call_dir = self._runner_root / f"call-{uuid4().hex}"
                call_dir.mkdir()
            else:
                call_dir = Path(
                    tempfile.mkdtemp(prefix="call-", dir=self._runner_root)
                )
            verify_inherited_call_directory(call_dir)
            # ``-I`` implies ``-E``, so PYTHONUTF8/PYTHONIOENCODING are ignored.
            # Force UTF-8 with an interpreter option to keep JSON bytes portable.
            argv = [
                str(Path(sys.executable).resolve()),
                "-I",
                "-X",
                "utf8",
                str(entrypoint),
            ]
            process = self._spawn(argv, call_dir, self._minimal_environment(call_dir))
            if os.name == "nt":
                try:
                    job = _WindowsJob(self._limits)
                    job.assign_and_resume(process)
                except BaseException:
                    self._fail_closed_process(process, job)
                    process.wait(timeout=5)
                    raise RuntimeError("Windows Job Object setup failed")

            assert process.stdin and process.stdout and process.stderr
            stdout_overflow = threading.Event()
            stderr_overflow = threading.Event()
            input_writer = _InputWriter(process.stdin, payload)
            stdout_reader = _BoundedReader(
                process.stdout, self._limits.max_stdout_bytes, stdout_overflow
            )
            stderr_reader = _BoundedReader(
                process.stderr, self._limits.max_stderr_bytes, stderr_overflow
            )
            threads = [
                threading.Thread(target=input_writer.run, daemon=True),
                threading.Thread(target=stdout_reader.run, daemon=True),
                threading.Thread(target=stderr_reader.run, daemon=True),
            ]
            for thread in threads:
                thread.start()

            deadline = started + self._limits.timeout_seconds
            while process.poll() is None:
                if cancel_event.is_set():
                    status = "failed"
                    self._terminate_tree(process, job)
                    break
                if stdout_overflow.is_set():
                    status = "output_limit"
                    self._terminate_tree(process, job)
                    break
                if stderr_overflow.is_set():
                    status = "stderr_limit"
                    self._terminate_tree(process, job)
                    break
                if time.monotonic() >= deadline:
                    status = "timeout"
                    self._terminate_tree(process, job)
                    break
                time.sleep(0.01)

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_ok = False
                self._terminate_tree(process, job)
            for thread in threads:
                thread.join(timeout=2)
                if thread.is_alive():
                    cleanup_ok = False
            exit_code = process.returncode
            stdout = bytes(stdout_reader.data)
            stderr_size = stderr_reader.size
            if stdout_overflow.is_set():
                status = "output_limit"
            elif stderr_overflow.is_set():
                status = "stderr_limit"
            elif stdout_reader.error or stderr_reader.error:
                status = "failed"
            elif status == "failed" and not cancel_event.is_set():
                status = "success" if exit_code == 0 else "failed"
        except BaseException:
            if process is not None:
                cleanup_ok = self._terminate_tree(process, job) and cleanup_ok
            status = "failed"
        finally:
            if job is not None:
                cleanup_ok = job.terminate_remaining() and cleanup_ok
                cleanup_ok = job.close() and cleanup_ok
            if call_dir is not None:
                try:
                    shutil.rmtree(call_dir)
                except OSError:
                    cleanup_ok = False
        return _ProcessOutcome(
            status,
            stdout,
            stderr_size,
            self._elapsed(started),
            exit_code,
            cleanup_ok,
        )

    @staticmethod
    def _fail_closed_process(process: subprocess.Popen, job: _WindowsJob | None) -> None:
        if job is not None:
            job.terminate()
        try:
            process.kill()
        except OSError:
            pass

    def _terminate_tree(
        self, process: subprocess.Popen, job: _WindowsJob | None
    ) -> bool:
        if os.name == "nt":
            if job is None or not job.terminate():
                try:
                    process.kill()
                except OSError:
                    pass
                return False
            return True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            try:
                process.kill()
            except OSError:
                return False
        return True

    def _convert_outcome(self, outcome: _ProcessOutcome) -> SkillExecutionResult:
        if not outcome.cleanup_ok:
            return SkillExecutionResult(
                status="cleanup_failed",
                output=None,
                stderr_summary="Skill process cleanup failed.",
                duration_ms=outcome.duration_ms,
                exit_code=outcome.exit_code,
            )
        if outcome.status != "success":
            summaries = {
                "timeout": "Skill execution timed out.",
                "output_limit": "Skill output exceeded its limit.",
                "stderr_limit": "Skill error output exceeded its limit.",
                "failed": "Skill process failed.",
            }
            return SkillExecutionResult(
                status=outcome.status,  # type: ignore[arg-type]
                output=None,
                stderr_summary=summaries.get(outcome.status, "Skill process failed."),
                duration_ms=outcome.duration_ms,
                exit_code=outcome.exit_code,
            )
        try:
            text = outcome.stdout.decode("utf-8", errors="strict")
            output = json.loads(text, parse_constant=_reject_non_finite_json)
            self._validate_json_shape(output)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
            MemoryError,
        ):
            return SkillExecutionResult(
                status="invalid_output",
                output=None,
                stderr_summary="Skill returned invalid JSON.",
                duration_ms=outcome.duration_ms,
                exit_code=outcome.exit_code,
            )
        return SkillExecutionResult(
            status="success",
            output=output,
            stderr_summary="" if outcome.stderr_size == 0 else "Skill wrote to stderr.",
            duration_ms=outcome.duration_ms,
            exit_code=outcome.exit_code,
        )

    def _validate_json_shape(self, output: Any) -> None:
        stack: list[tuple[Any, int]] = [(output, 1)]
        nodes = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if nodes > self._limits.max_json_nodes or depth > self._limits.max_json_depth:
                raise ValueError("JSON output exceeds structural budget")
            if value is None or type(value) in (bool, int, str):
                continue
            if type(value) is float:
                if not math.isfinite(value):
                    raise ValueError("JSON number must be finite")
                continue
            if type(value) is list:
                if nodes + len(stack) + len(value) > self._limits.max_json_nodes:
                    raise ValueError("JSON output exceeds node budget")
                stack.extend((item, depth + 1) for item in value)
                continue
            if type(value) is dict:
                if not all(type(key) is str for key in value):
                    raise ValueError("JSON object keys must be strings")
                if nodes + len(stack) + len(value) > self._limits.max_json_nodes:
                    raise ValueError("JSON output exceeds node budget")
                stack.extend((item, depth + 1) for item in value.values())
                continue
            raise ValueError("unsupported JSON output type")

    async def _audit(
        self, request: SkillExecutionRequest, result: SkillExecutionResult
    ) -> None:
        if self._audit_sink is None:
            return
        event = RunnerAuditEvent(
            user_id=request.user_id,
            skill_id=request.skill_id,
            request_id=request.request_id,
            status=result.status,
            duration_ms=result.duration_ms,
            exit_code=result.exit_code,
        )
        pending = self._audit_sink(event)
        if inspect.isawaitable(pending):
            await pending

    async def _audit_uncancellable(
        self, request: SkillExecutionRequest, result: SkillExecutionResult
    ) -> None:
        """Run exactly one audit task to completion before propagating cancel."""
        audit_task = asyncio.create_task(self._audit(request, result))
        cancelled = False
        while not audit_task.done():
            try:
                await asyncio.shield(audit_task)
            except asyncio.CancelledError:
                cancelled = True
                continue
            except Exception:
                break
        if audit_task.cancelled():
            logger.error("Runner audit task was unexpectedly cancelled")
        else:
            try:
                audit_task.result()
            except Exception:
                logger.exception("Runner audit failed")
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _elapsed(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    @classmethod
    def _result(cls, status, started: float, summary: str) -> SkillExecutionResult:
        return SkillExecutionResult(
            status=status,
            output=None,
            stderr_summary=summary,
            duration_ms=cls._elapsed(started),
            exit_code=None,
        )
