from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

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


@dataclass(slots=True)
class _Runtime:
    parameters: StdioServerParameters
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    session: ClientSession | None = None
    task: asyncio.Task[None] | None = None
    failure: BaseException | None = None
    tools: dict[str, Any] = field(default_factory=dict)

    async def run(self) -> None:
        try:
            async with stdio_client(self.parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    listed = await session.list_tools()
                    self.tools = {tool.name: tool for tool in listed.tools}
                    self.ready.set()
                    await self.stop_requested.wait()
        except BaseException as exc:
            self.failure = exc
            self.ready.set()
            raise
        finally:
            self.session = None


@dataclass(slots=True)
class McpRuntimeRegistry:
    """Process-local lifecycle state shared by request-scoped services."""

    runtimes: dict[str, _Runtime] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    call_slots: asyncio.Semaphore | None = None

    def lock(self, server_id: str) -> asyncio.Lock:
        return self.locks.setdefault(server_id, asyncio.Lock())


class McpService:
    """Govern and run checked-in stdio MCP servers without a shell."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        server_root: Path,
        python_executable: Path,
        start_timeout: float = 10.0,
        call_timeout: float = 10.0,
        max_input_bytes: int = 16 * 1024,
        max_output_bytes: int = 64 * 1024,
        max_concurrent_calls: int = 4,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        runtime_registry: McpRuntimeRegistry | None = None,
        allowed_server_scripts: frozenset[str] = frozenset({"mock_pickup_server.py"}),
    ) -> None:
        self._sessions = session_factory or async_sessionmaker(
            db.bind, expire_on_commit=False
        )
        self._root = server_root.resolve(strict=True)
        self._python = python_executable.resolve(strict=True)
        self._start_timeout = start_timeout
        self._call_timeout = call_timeout
        self._max_input_bytes = max_input_bytes
        self._max_output_bytes = max_output_bytes
        self._allowed_server_scripts = allowed_server_scripts
        self._registry = runtime_registry or McpRuntimeRegistry()
        self._runtimes = self._registry.runtimes
        if self._registry.call_slots is None:
            self._registry.call_slots = asyncio.Semaphore(max_concurrent_calls)
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
                    request_id=None,
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
        self, actor: Principal, server_id: str, manager_user_id: str
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
                request_id=None,
                details={"operation": "authorize", "role": "manager"},
            )
            await db.commit()
            await db.refresh(row)
            return row

    @staticmethod
    async def _get(db: AsyncSession, server_id: str) -> McpServerRow:
        row = await db.get(McpServerRow, server_id)
        if row is None:
            raise McpValidationError("MCP server not found")
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
            executable = Path(command).resolve(strict=True)
            script = script_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise McpConflictError("registered MCP files are unavailable") from exc
        if _is_reparse(executable) or _is_reparse(script):
            raise McpConflictError("registered MCP files became reparse points")
        if _sha256(executable) != row.executable_sha256 or _sha256(script) != row.script_sha256:
            raise McpConflictError("registered MCP file integrity mismatch")
        return executable, script

    async def start(self, actor: Principal, server_id: str) -> None:
        self._require_admin(actor)
        async with self._lock(server_id):
            current = self._runtimes.get(server_id)
            if current is not None and current.task is not None and not current.task.done():
                return
            async with self._sessions() as db:
                row = await self._get(db, server_id)
            try:
                executable, script = self._verify_integrity(row)
            except BaseException:
                await self._record_state_audit(
                    actor=actor,
                    server_id=server_id,
                    status="failed",
                    error_code="INTEGRITY_FAILED",
                    action="mcp.start",
                    result="failure",
                    details={"operation": "connect", "status": "failure"},
                )
                raise
            runtime = _Runtime(
                StdioServerParameters(
                    command=str(executable), args=[str(script)], env={}, cwd=str(self._root)
                )
            )
            runtime.task = asyncio.create_task(runtime.run(), name=f"mcp-{server_id}")
            runtime.task.add_done_callback(self._consume_runtime_exception)
            self._runtimes[server_id] = runtime
            try:
                await asyncio.wait_for(runtime.ready.wait(), timeout=self._start_timeout)
                if runtime.failure is not None or runtime.session is None:
                    raise McpUnavailableError("MCP server failed to initialize")
            except BaseException:
                await self._stop_runtime(runtime)
                self._runtimes.pop(server_id, None)
                await self._record_state_audit(
                    actor=actor,
                    server_id=server_id,
                    status="failed",
                    error_code="START_FAILED",
                    action="mcp.start",
                    result="failure",
                    details={"operation": "connect", "status": "failure"},
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
                request_id=None,
                details=details,
            )
            await db.commit()

    @staticmethod
    def _consume_runtime_exception(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _stop_runtime(self, runtime: _Runtime) -> None:
        runtime.stop_requested.set()
        if runtime.task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(runtime.task), timeout=5)
        except asyncio.TimeoutError:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
        except BaseException:
            pass

    async def stop(self, actor: Principal, server_id: str) -> None:
        self._require_admin(actor)
        async with self._lock(server_id):
            async with self._sessions() as db:
                await self._get(db, server_id)
            runtime = self._runtimes.pop(server_id, None)
            if runtime is not None:
                await self._stop_runtime(runtime)
            await self._record_state_audit(
                actor=actor,
                server_id=server_id,
                status="stopped",
                error_code=None,
                action="mcp.stop",
                result="success",
                details={"operation": "disconnect", "transport": "stdio"},
            )

    async def health(self, actor: Principal, server_id: str) -> bool:
        self._require_admin(actor)
        async with self._sessions() as db:
            await self._get(db, server_id)
        runtime = self._runtimes.get(server_id)
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
        )
        return healthy

    async def list_tools(self, actor: Principal, server_id: str) -> list[dict[str, Any]]:
        self._require_admin(actor)
        async with self._sessions() as db:
            await self._get(db, server_id)
        try:
            runtime = self._active_runtime(server_id)
        except McpUnavailableError:
            await self._audit_operation(
                actor, server_id, "mcp.list_tools", "list", "failure", count=0
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
            actor, server_id, "mcp.list_tools", "list", "success", count=len(tools)
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
                request_id=None,
                details=details,
            )
            await db.commit()

    def _active_runtime(self, server_id: str) -> _Runtime:
        runtime = self._runtimes.get(server_id)
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
        if actor.role in _ADMIN_ROLES:
            return True
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
    ) -> dict[str, Any]:
        async with self._sessions() as db:
            await self._get(db, server_id)
            can_call = await self._can_call(db, actor, server_id)
        if not can_call:
            await self._audit_call(actor, server_id, "denied")
            raise McpPermissionError("MCP tool is not authorized")
        runtime = self._active_runtime(server_id)
        tool = runtime.tools.get(tool_name)
        if tool is None:
            await self._audit_call(actor, server_id, "failure")
            raise McpValidationError("tool was not advertised by the MCP server")
        if type(arguments) is not dict:
            await self._audit_call(actor, server_id, "failure")
            raise McpValidationError("tool arguments must be an object")
        try:
            encoded_input = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            await self._audit_call(actor, server_id, "failure")
            raise McpValidationError("tool input must be JSON serializable") from exc
        if len(encoded_input) > self._max_input_bytes:
            await self._audit_call(actor, server_id, "failure")
            raise McpValidationError("tool input exceeded the configured limit")
        declared = tool.inputSchema.get("properties", {})
        if type(declared) is not dict or set(arguments).difference(declared):
            await self._audit_call(actor, server_id, "failure")
            raise McpValidationError("tool arguments contain undeclared fields")
        validator = Draft202012Validator(tool.inputSchema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(arguments), key=lambda item: list(item.path))
        if errors:
            await self._audit_call(actor, server_id, "failure")
            raise McpValidationError("tool arguments do not match the advertised schema")
        async with self._calls:
            try:
                assert runtime.session is not None
                response = await asyncio.wait_for(
                    runtime.session.call_tool(tool_name, arguments), timeout=self._call_timeout
                )
            except asyncio.TimeoutError as exc:
                await self._retire_failed_runtime(
                    actor, server_id, runtime, "CALL_TIMEOUT"
                )
                raise McpUnavailableError("MCP tool call timed out") from exc
            except BaseException:
                await self._retire_failed_runtime(
                    actor, server_id, runtime, "CALL_FAILED"
                )
                raise
        if response.isError:
            await self._audit_call(actor, server_id, "failure")
            raise McpValidationError("MCP tool rejected the request")
        output = response.structuredContent
        if type(output) is not dict:
            raise McpUnavailableError("MCP tool did not return a structured object")
        encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._max_output_bytes:
            await self._audit_call(actor, server_id, "failure")
            raise McpUnavailableError("MCP tool output exceeded the configured limit")
        await self._audit_call(actor, server_id, "success")
        return output

    async def _retire_failed_runtime(
        self,
        actor: Principal,
        server_id: str,
        runtime: _Runtime,
        error_code: str,
    ) -> None:
        async with self._lock(server_id):
            if self._runtimes.get(server_id) is runtime:
                self._runtimes.pop(server_id, None)
                await self._stop_runtime(runtime)
            await self._record_state_audit(
                actor=actor,
                server_id=server_id,
                status="failed",
                error_code=error_code,
                action="mcp.call",
                result="failure",
                details={"operation": "invoke", "status": "failure"},
            )

    async def _audit_call(self, actor: Principal, server_id: str, result: str) -> None:
        async with self._sessions() as db:
            AuditRepository(db).add_pending(
                actor_user_id=actor.user_id,
                action="mcp.call",
                resource_type="mcp_server",
                resource_id=server_id,
                result=result,
                request_id=None,
                details={"operation": "invoke", "status": result},
            )
            await db.commit()

    async def aclose(self) -> None:
        runtimes = list(self._runtimes.values())
        self._runtimes.clear()
        await asyncio.gather(
            *(self._stop_runtime(runtime) for runtime in runtimes),
            return_exceptions=True,
        )
