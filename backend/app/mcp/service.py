from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import Principal
from app.db.models import McpAuthorizationRow, McpServerRow, Role, User
from app.repositories.audit import AuditRepository


_ADMIN_ROLES = frozenset({Role.BUSINESS_ADMIN, Role.SYSTEM_ADMIN})
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


class McpError(RuntimeError):
    pass


class McpPermissionError(McpError):
    pass


class McpValidationError(McpError):
    pass


class McpNotFoundError(McpError, LookupError):
    pass


class McpConflictError(McpError):
    pass


class McpUnavailableError(McpError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    stat = path.lstat()
    attributes = getattr(stat, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _reject_reparse_chain(path: Path) -> None:
    raw = path.absolute()
    for component in reversed((raw, *raw.parents)):
        if component.exists() and _is_reparse(component):
            raise McpValidationError("path contains a symlink or reparse point")


@dataclass(slots=True)
class _Runtime:
    key: "RuntimeKey"
    parameters: StdioServerParameters
    actor_user_id: str
    generation: str = field(default_factory=lambda: str(uuid4()))
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    session: ClientSession | None = None
    task: asyncio.Task[None] | None = None
    failure: BaseException | None = None
    tools: dict[str, Any] = field(default_factory=dict)

    intentional_stop: bool = False

    async def run_protocol(self) -> None:
        try:
            async with stdio_client(self.parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    listed = await session.list_tools()
                    self.tools = {tool.name: tool for tool in listed.tools}
                    self.ready.set()
                    while not self.stop_requested.is_set():
                        try:
                            await asyncio.wait_for(
                                self.stop_requested.wait(), timeout=0.2
                            )
                        except asyncio.TimeoutError:
                            await asyncio.wait_for(session.send_ping(), timeout=0.5)
        except BaseException as exc:
            self.failure = exc
            self.ready.set()
        finally:
            self.session = None


@dataclass(frozen=True, slots=True)
class RuntimeKey:
    namespace: str
    server_id: str
    config_fingerprint: str


@dataclass(slots=True)
class McpRuntimeRegistry:
    """Application-scoped owner of all MCP runtimes and process cleanup."""

    application_namespace: str
    server_root: Path
    python_executable: Path
    session_factory: async_sessionmaker[AsyncSession]
    max_calls: int = 4
    max_running_servers: int = 2
    allowed_server_scripts: frozenset[str] = frozenset({"mock_pickup_server.py"})
    runtimes: dict[RuntimeKey, _Runtime] = field(default_factory=dict, init=False)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    call_slots: asyncio.Semaphore = field(init=False)
    _global_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _config_fingerprint: str = field(init=False)
    _notifications: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    closing: bool = field(default=False, init=False)
    closed: bool = field(default=False, init=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not self.application_namespace or self.max_calls < 1 or self.max_running_servers < 1:
            raise ValueError("invalid MCP runtime registry configuration")
        _reject_reparse_chain(self.server_root)
        _reject_reparse_chain(self.python_executable)
        self.server_root = self.server_root.resolve(strict=True)
        self.python_executable = self.python_executable.resolve(strict=True)
        self.call_slots = asyncio.Semaphore(self.max_calls)
        material = "\0".join(
            (
                self.application_namespace,
                os.path.normcase(str(self.server_root)),
                os.path.normcase(str(self.python_executable)),
                _sha256(self.python_executable),
                str(self.max_calls),
                str(self.max_running_servers),
                *sorted(self.allowed_server_scripts),
            )
        )
        self._config_fingerprint = hashlib.sha256(material.encode()).hexdigest()

    def lock(self, server_id: str) -> asyncio.Lock:
        return self.locks.setdefault(server_id, asyncio.Lock())

    @property
    def config_fingerprint(self) -> str:
        return self._config_fingerprint

    async def current(self, server_id: str) -> _Runtime | None:
        async with self._global_lock:
            return next(
                (runtime for key, runtime in self.runtimes.items() if key.server_id == server_id),
                None,
            )

    async def reserve(self, runtime: _Runtime) -> None:
        async with self._global_lock:
            if self.closing or self.closed:
                raise McpUnavailableError("MCP runtime registry is closed")
            current = next(
                (item for key, item in self.runtimes.items() if key.server_id == runtime.key.server_id),
                None,
            )
            if current is not None:
                raise McpConflictError("an MCP runtime already exists for this server")
            if len(self.runtimes) >= self.max_running_servers:
                raise McpUnavailableError("MCP running-server limit reached")
            self.runtimes[runtime.key] = runtime
            runtime.task = asyncio.create_task(
                self._run(runtime), name=f"mcp-{runtime.key.server_id}-{runtime.generation}"
            )

    async def _run(self, runtime: _Runtime) -> None:
        await runtime.run_protocol()
        notification = asyncio.create_task(self._on_exit(runtime))
        self._notifications.add(notification)
        notification.add_done_callback(self._notification_done)

    def _notification_done(self, task: asyncio.Task[None]) -> None:
        self._notifications.discard(task)
        if not task.cancelled():
            task.exception()

    async def _on_exit(self, runtime: _Runtime) -> None:
        async with self.lock(runtime.key.server_id):
            removed = await self.compare_remove(runtime)
            if not removed or runtime.intentional_stop:
                return
            async with self.session_factory() as db:
                row = await db.get(McpServerRow, runtime.key.server_id)
                if row is None:
                    return
                row.status = "failed"
                row.last_error = "PROCESS_EXITED"
                AuditRepository(db).add_pending(
                    actor_user_id=runtime.actor_user_id,
                    action="mcp.lifecycle",
                    resource_type="mcp_server",
                    resource_id=row.id,
                    result="failure",
                    request_id=None,
                    details={"operation": "disconnect", "status": "failure"},
                )
                await db.commit()

    async def compare_remove(self, runtime: _Runtime) -> bool:
        async with self._global_lock:
            if self.runtimes.get(runtime.key) is not runtime:
                return False
            del self.runtimes[runtime.key]
            return True

    async def retire(self, runtime: _Runtime) -> bool:
        removed = await self.compare_remove(runtime)
        if not removed:
            return False
        runtime.intentional_stop = True
        await _stop_runtime_process(runtime)
        return True

    async def shutdown_all(self) -> None:
        async with self._global_lock:
            if self._close_task is None:
                self.closing = True
                self._close_task = asyncio.create_task(self._close_impl())
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        async with self._global_lock:
            runtimes = list(self.runtimes.values())
            self.runtimes.clear()
            for runtime in runtimes:
                runtime.intentional_stop = True
        await asyncio.gather(
            *(_stop_runtime_process(runtime) for runtime in runtimes),
            return_exceptions=True,
        )
        while self._notifications:
            await asyncio.gather(*tuple(self._notifications), return_exceptions=True)
        async with self._global_lock:
            self.closed = True
            self.closing = False

    async def aclose(self) -> None:
        await self.shutdown_all()


async def _stop_runtime_process(runtime: _Runtime) -> None:
    runtime.stop_requested.set()
    if runtime.task is None or runtime.task is asyncio.current_task():
        return
    try:
        await asyncio.wait_for(asyncio.shield(runtime.task), timeout=5)
    except asyncio.TimeoutError:
        runtime.task.cancel()
        await asyncio.gather(runtime.task, return_exceptions=True)
    except BaseException:
        pass


class McpService:
    """Govern and run checked-in stdio MCP servers without a shell."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        runtime_registry: McpRuntimeRegistry,
        application_namespace: str,
        server_root: Path,
        python_executable: Path,
        start_timeout: float = 10.0,
        call_timeout: float = 10.0,
        max_input_bytes: int = 16 * 1024,
        max_output_bytes: int = 64 * 1024,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        allowed_server_scripts: frozenset[str] = frozenset({"mock_pickup_server.py"}),
    ) -> None:
        _reject_reparse_chain(server_root)
        _reject_reparse_chain(python_executable)
        self._sessions = session_factory or runtime_registry.session_factory
        self._root = server_root.resolve(strict=True)
        self._python = python_executable.resolve(strict=True)
        if (
            application_namespace != runtime_registry.application_namespace
            or self._root != runtime_registry.server_root
            or self._python != runtime_registry.python_executable
            or allowed_server_scripts != runtime_registry.allowed_server_scripts
            or self._sessions is not runtime_registry.session_factory
        ):
            raise ValueError("MCP service configuration does not match its application registry")
        self._namespace = application_namespace
        self._start_timeout = start_timeout
        self._call_timeout = call_timeout
        self._max_input_bytes = max_input_bytes
        self._max_output_bytes = max_output_bytes
        self._allowed_server_scripts = allowed_server_scripts
        self._registry = runtime_registry
        self._runtimes = self._registry.runtimes
        self._calls = self._registry.call_slots

    @staticmethod
    def _require_admin(actor: Principal) -> None:
        if actor.role not in _ADMIN_ROLES:
            raise McpPermissionError("global administrator role required")

    def _lock(self, server_id: str) -> asyncio.Lock:
        return self._registry.lock(server_id)

    def _validate_registration(
        self,
        *,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> tuple[Path, Path]:
        if not _NAME.fullmatch(name):
            raise McpValidationError("invalid server name")
        if type(command) is not str or not Path(command).is_absolute():
            raise McpValidationError("command must be the canonical Python executable")
        try:
            _reject_reparse_chain(Path(command))
            executable = Path(command).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise McpValidationError("invalid command") from exc
        if os.path.normcase(str(executable)) != os.path.normcase(str(self._python)):
            raise McpValidationError("executable is not allowlisted")
        if _is_reparse(executable) or not executable.is_file():
            raise McpValidationError("executable must be a regular non-reparse file")
        if type(args) is not list or len(args) != 1 or type(args[0]) is not str:
            raise McpValidationError("exactly one structured server-script argument is required")
        relative = Path(args[0])
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name != args[0]:
            raise McpValidationError("server script must be a bundled file name")
        if relative.name not in self._allowed_server_scripts:
            raise McpValidationError("server script is not allowlisted")
        try:
            _reject_reparse_chain(self._root / relative)
            script = (self._root / relative).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise McpValidationError("server script does not exist") from exc
        if script.parent != self._root or script.suffix.lower() != ".py":
            raise McpValidationError("server script escapes the bundled root")
        if _is_reparse(script) or not script.is_file():
            raise McpValidationError("server script must be a regular non-reparse file")
        if type(env) is not dict or env:
            raise McpValidationError("custom environment variables are disabled in Phase 1")
        return executable, script

    async def register_local(
        self,
        actor: Principal,
        *,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        request_id: str | None = None,
    ) -> McpServerRow:
        self._require_admin(actor)
        executable, script = self._validate_registration(
            name=name, command=command, args=args, env=env
        )
        row = McpServerRow(
            created_by_user_id=actor.user_id,
            name=name,
            transport="stdio",
            configuration={"command": str(executable), "args": [str(script)], "env": {}},
            executable_sha256=_sha256(executable),
            script_sha256=_sha256(script),
            status="stopped",
        )
        async with self._sessions() as db:
            db.add(row)
            try:
                await db.flush()
                AuditRepository(db).add_pending(
                    actor_user_id=actor.user_id,
                    action="mcp.register",
                    resource_type="mcp_server",
                    resource_id=row.id,
                    result="success",
                    request_id=request_id,
                    details={"operation": "create", "transport": "stdio"},
                )
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise McpConflictError("server name already exists") from exc
            except BaseException:
                await db.rollback()
                raise
            await db.refresh(row)
        return row

    async def authorize(
        self,
        actor: Principal,
        server_id: str,
        manager_user_id: str,
        request_id: str | None = None,
    ) -> McpAuthorizationRow:
        self._require_admin(actor)
        async with self._sessions() as db:
            server = await self._get(db, server_id)
            manager = await db.get(User, manager_user_id)
            if manager is None or manager.role != Role.MANAGER or not manager.is_active:
                raise McpPermissionError("authorization target must be an active manager")
            existing = await db.scalar(
                select(McpAuthorizationRow).where(
                    McpAuthorizationRow.server_id == server.id,
                    McpAuthorizationRow.user_id == manager_user_id,
                )
            )
            if existing is not None:
                return existing
            row = McpAuthorizationRow(
                server_id=server.id,
                user_id=manager_user_id,
                granted_by_user_id=actor.user_id,
            )
            db.add(row)
            AuditRepository(db).add_pending(
                actor_user_id=actor.user_id,
                action="mcp.authorize",
                resource_type="mcp_server",
                resource_id=server.id,
                result="success",
                request_id=request_id,
                details={"operation": "authorize", "role": "manager"},
            )
            await db.commit()
            await db.refresh(row)
            return row

    @staticmethod
    async def _get(db: AsyncSession, server_id: str) -> McpServerRow:
        row = await db.get(McpServerRow, server_id)
        if row is None:
            raise McpNotFoundError("MCP server not found")
        return row

    def _verify_integrity(self, row: McpServerRow) -> tuple[Path, Path]:
        configuration = row.configuration
        if type(configuration) is not dict:
            raise McpConflictError("stored server configuration is invalid")
        command = configuration.get("command")
        args = configuration.get("args")
        env = configuration.get("env")
        if not isinstance(command, str) or not isinstance(args, list) or len(args) != 1:
            raise McpConflictError("stored server configuration is invalid")
        script_path = Path(args[0]) if isinstance(args[0], str) else Path()
        if (
            os.path.normcase(command) != os.path.normcase(str(self._python))
            or script_path.parent != self._root
            or script_path.name not in self._allowed_server_scripts
            or env != {}
        ):
            raise McpConflictError("stored server configuration violates policy")
        try:
            _reject_reparse_chain(Path(command))
            _reject_reparse_chain(script_path)
            executable = Path(command).resolve(strict=True)
            script = script_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise McpConflictError("registered MCP files are unavailable") from exc
        if _is_reparse(executable) or _is_reparse(script):
            raise McpConflictError("registered MCP files became reparse points")
        if _sha256(executable) != row.executable_sha256 or _sha256(script) != row.script_sha256:
            raise McpConflictError("registered MCP file integrity mismatch")
        return executable, script

    def _runtime_key(self, row: McpServerRow) -> RuntimeKey:
        material = json.dumps(
            {
                "registry": self._registry.config_fingerprint,
                "configuration": row.configuration,
                "executable_sha256": row.executable_sha256,
                "script_sha256": row.script_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return RuntimeKey(
            self._namespace,
            row.id,
            hashlib.sha256(material).hexdigest(),
        )

    async def start(
        self, actor: Principal, server_id: str, request_id: str | None = None
    ) -> None:
        self._require_admin(actor)
        async with self._lock(server_id):
            async with self._sessions() as db:
                row = await self._get(db, server_id)
            try:
                executable, script = self._verify_integrity(row)
            except BaseException:
                current = await self._registry.current(server_id)
                if current is not None:
                    await self._registry.retire(current)
                await self._record_state_audit(
                    actor=actor,
                    server_id=server_id,
                    status="failed",
                    error_code="INTEGRITY_FAILED",
                    action="mcp.start",
                    result="failure",
                    details={"operation": "connect", "status": "failure"},
                    request_id=request_id,
                )
                raise
            key = self._runtime_key(row)
            current = await self._registry.current(server_id)
            if current is not None:
                if current.key != key:
                    raise McpConflictError("running MCP configuration differs from the database")
                if current.task is not None and not current.task.done() and current.session is not None:
                    return
                raise McpUnavailableError("existing MCP runtime is not ready")
            runtime = _Runtime(
                key,
                StdioServerParameters(
                    command=str(executable), args=[str(script)], env={}, cwd=str(self._root)
                ),
                actor.user_id,
            )
            try:
                await self._registry.reserve(runtime)
            except McpUnavailableError:
                await self._record_state_audit(
                    actor=actor,
                    server_id=server_id,
                    status="failed",
                    error_code="START_LIMIT",
                    action="mcp.start",
                    result="failure",
                    details={"operation": "connect", "status": "failure"},
                    request_id=request_id,
                )
                raise
            try:
                await asyncio.wait_for(runtime.ready.wait(), timeout=self._start_timeout)
                if runtime.failure is not None or runtime.session is None:
                    raise McpUnavailableError("MCP server failed to initialize")
            except BaseException:
                await self._registry.retire(runtime)
                await self._record_state_audit(
                    actor=actor,
                    server_id=server_id,
                    status="failed",
                    error_code="START_FAILED",
                    action="mcp.start",
                    result="failure",
                    details={"operation": "connect", "status": "failure"},
                    request_id=request_id,
                )
                raise
            await self._record_state_audit(
                actor=actor,
                server_id=server_id,
                status="running",
                error_code=None,
                action="mcp.start",
                result="success",
                details={"operation": "connect", "transport": "stdio"},
                request_id=request_id,
            )

    async def _record_state_audit(
        self,
        *,
        actor: Principal,
        server_id: str,
        status: str,
        error_code: str | None,
        action: str,
        result: str,
        details: dict[str, Any],
        request_id: str | None = None,
    ) -> None:
        async with self._sessions() as db:
            row = await self._get(db, server_id)
            row.status = status
            row.last_error = error_code
            AuditRepository(db).add_pending(
                actor_user_id=actor.user_id,
                action=action,
                resource_type="mcp_server",
                resource_id=server_id,
                result=result,
                request_id=request_id,
                details=details,
            )
            await db.commit()

    async def _stop_runtime(self, runtime: _Runtime) -> None:
        await self._registry.retire(runtime)

    async def stop(self, actor: Principal, server_id: str) -> None:
        self._require_admin(actor)
        async with self._lock(server_id):
            async with self._sessions() as db:
                await self._get(db, server_id)
            runtime = await self._registry.current(server_id)
            if runtime is not None:
                await self._registry.retire(runtime)
            await self._record_state_audit(
                actor=actor,
                server_id=server_id,
                status="stopped",
                error_code=None,
                action="mcp.stop",
                result="success",
                details={"operation": "disconnect", "transport": "stdio"},
            )

    async def health(
        self, actor: Principal, server_id: str, request_id: str | None = None
    ) -> bool:
        self._require_admin(actor)
        async with self._sessions() as db:
            await self._get(db, server_id)
        runtime = await self._registry.current(server_id)
        healthy = bool(
            runtime is not None
            and runtime.task is not None
            and not runtime.task.done()
            and runtime.session is not None
        )
        await self._audit_operation(
            actor,
            server_id,
            "mcp.health",
            "connect",
            "success" if healthy else "failure",
            request_id=request_id,
        )
        return healthy

    async def list_tools(
        self, actor: Principal, server_id: str, request_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_admin(actor)
        async with self._sessions() as db:
            await self._get(db, server_id)
        try:
            runtime = await self._active_runtime(server_id)
        except McpUnavailableError:
            await self._audit_operation(
                actor,
                server_id,
                "mcp.list_tools",
                "list",
                "failure",
                count=0,
                request_id=request_id,
            )
            raise
        tools = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema,
            }
            for tool in sorted(runtime.tools.values(), key=lambda value: value.name)
        ]
        await self._audit_operation(
            actor,
            server_id,
            "mcp.list_tools",
            "list",
            "success",
            count=len(tools),
            request_id=request_id,
        )
        return tools

    async def _audit_operation(
        self,
        actor: Principal,
        server_id: str,
        action: str,
        operation: str,
        result: str,
        *,
        count: int | None = None,
        request_id: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"operation": operation, "status": result}
        if count is not None:
            details["count"] = count
        async with self._sessions() as db:
            AuditRepository(db).add_pending(
                actor_user_id=actor.user_id,
                action=action,
                resource_type="mcp_server",
                resource_id=server_id,
                result=result,
                request_id=request_id,
                details=details,
            )
            await db.commit()

    async def _active_runtime(self, server_id: str) -> _Runtime:
        runtime = await self._registry.current(server_id)
        if (
            runtime is None
            or runtime.task is None
            or runtime.task.done()
            or runtime.session is None
        ):
            raise McpUnavailableError("MCP server is not running")
        return runtime

    @staticmethod
    async def _can_call(db: AsyncSession, actor: Principal, server_id: str) -> bool:
        if actor.role != Role.MANAGER:
            return False
        return bool(
            await db.scalar(
                select(McpAuthorizationRow.id)
                .join(User, User.id == McpAuthorizationRow.user_id)
                .where(
                    McpAuthorizationRow.server_id == server_id,
                    McpAuthorizationRow.user_id == actor.user_id,
                    User.is_active.is_(True),
                    User.role == Role.MANAGER,
                )
            )
        )

    async def call_tool(
        self,
        actor: Principal,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        result = "failure"
        runtime: _Runtime | None = None
        retire_code: str | None = None
        request_started = False
        try:
            async with self._sessions() as db:
                await self._get(db, server_id)
                can_call = await self._can_call(db, actor, server_id)
            if not can_call:
                result = "denied"
                raise McpPermissionError("MCP tool is not authorized")
            runtime = await self._active_runtime(server_id)
            tool = runtime.tools.get(tool_name)
            if tool is None:
                raise McpValidationError("tool was not advertised by the MCP server")
            if type(arguments) is not dict:
                raise McpValidationError("tool arguments must be an object")
            try:
                encoded_input = json.dumps(
                    arguments, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise McpValidationError(
                    "tool input must be JSON serializable"
                ) from exc
            if len(encoded_input) > self._max_input_bytes:
                raise McpValidationError("tool input exceeded the configured limit")
            declared = tool.inputSchema.get("properties", {})
            if type(declared) is not dict or set(arguments).difference(declared):
                raise McpValidationError("tool arguments contain undeclared fields")
            try:
                validator = Draft202012Validator(
                    tool.inputSchema, format_checker=FormatChecker()
                )
                errors = sorted(
                    validator.iter_errors(arguments), key=lambda item: list(item.path)
                )
            except (TypeError, ValueError):
                retire_code = "CALL_FAILED"
                raise
            if errors:
                raise McpValidationError(
                    "tool arguments do not match the advertised schema"
                )
            async with self._calls:
                assert runtime.session is not None
                try:
                    request_started = True
                    response = await asyncio.wait_for(
                        runtime.session.call_tool(tool_name, arguments),
                        timeout=self._call_timeout,
                    )
                except asyncio.TimeoutError as exc:
                    retire_code = "CALL_TIMEOUT"
                    raise McpUnavailableError("MCP tool call timed out") from exc
                except BaseException:
                    retire_code = "CALL_FAILED"
                    raise
            if response.isError:
                raise McpValidationError("MCP tool rejected the request")
            output = response.structuredContent
            if type(output) is not dict:
                retire_code = "PROTOCOL_FAILED"
                raise McpUnavailableError(
                    "MCP tool did not return a structured object"
                )
            try:
                encoded = json.dumps(
                    output, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                retire_code = "PROTOCOL_FAILED"
                raise McpUnavailableError("MCP tool returned invalid JSON") from exc
            if len(encoded) > self._max_output_bytes:
                raise McpUnavailableError(
                    "MCP tool output exceeded the configured limit"
                )
            result = "success"
            return output
        except asyncio.CancelledError:
            result = "cancelled"
            if request_started:
                retire_code = "CALL_CANCELLED"
            raise
        finally:
            if retire_code is not None and runtime is not None:
                await self._retire_failed_runtime(
                    actor, server_id, runtime, retire_code
                )
            await self._audit_call_uncancellable(
                actor, server_id, result, request_id=request_id
            )

    async def _retire_failed_runtime(
        self,
        actor: Principal,
        server_id: str,
        runtime: _Runtime,
        error_code: str,
    ) -> None:
        async with self._lock(server_id):
            removed = await self._registry.retire(runtime)
            if removed:
                await self._record_state_audit(
                    actor=actor,
                    server_id=server_id,
                    status="failed",
                    error_code=error_code,
                    action="mcp.lifecycle",
                    result="failure",
                    details={"operation": "disconnect", "status": "failure"},
                )

    async def _audit_call(
        self,
        actor: Principal,
        server_id: str,
        result: str,
        request_id: str | None = None,
    ) -> None:
        async with self._sessions() as db:
            AuditRepository(db).add_pending(
                actor_user_id=actor.user_id,
                action="mcp.call",
                resource_type="mcp_server",
                resource_id=server_id,
                result=result,
                request_id=request_id,
                details={"operation": "invoke", "status": result},
            )
            await db.commit()

    async def _audit_call_uncancellable(
        self,
        actor: Principal,
        server_id: str,
        result: str,
        request_id: str | None = None,
    ) -> None:
        task = asyncio.create_task(
            self._audit_call(actor, server_id, result, request_id=request_id)
        )
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                continue
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def aclose(self) -> None:
        """Release this request-scoped façade without touching app runtimes."""
        return None
