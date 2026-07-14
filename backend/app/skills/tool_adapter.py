from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from app.auth.models import Principal
from app.auth.security import BANK_DEMO_TENANT_ID
from app.db.models import (
    McpAuthorizationRow,
    McpServerRow,
    Role,
    SessionRecordRow,
    User,
)
from app.runner.protocol import SkillExecutionRequest


_SAFE_NAME = re.compile(r"[^a-z0-9_]+")
_RESERVED_NAMES = frozenset(
    {
        "reset_tools",
        "skill_viewer",
        "tool_stop",
        "agent_create",
        "agent_get",
        "agent_list",
        "agent_message",
    }
)


class AuthorizedFunctionTool(ToolBase):
    """AgentScope tool whose callback closes over a verified manager scope."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        callback: Callable[[dict[str, Any]], Awaitable[Any]],
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self.name = name
        self.description = description[:1000]
        self.input_schema = input_schema
        self.is_concurrency_safe = False
        self.is_read_only = read_only
        self._callback = callback

    async def call(self, **kwargs: Any) -> ToolChunk:
        result = await self._callback(kwargs)
        return ToolChunk(
            content=[TextBlock(text=json.dumps(result, ensure_ascii=False, default=str))],
            state=ToolResultState.SUCCESS,
        )

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message=f"Run authorized tool {self.name}?",
        )


def _canonical_name(name: str) -> str:
    canonical = _SAFE_NAME.sub("_", name.casefold()).strip("_")
    if not canonical or len(canonical) > 64 or canonical[0].isdigit():
        raise PermissionError("tool name is not safe")
    return canonical


def _bounded_schema(value: Any) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PermissionError("tool schema is invalid") from exc
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise PermissionError("tool schema is too large")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise PermissionError("tool schema must be an object")
    stack: list[tuple[Any, int]] = [(decoded, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 512 or depth > 16:
            raise PermissionError("tool schema exceeds structural limits")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    if decoded.get("type") != "object":
        decoded = {"type": "object", "properties": decoded}
    decoded["additionalProperties"] = False
    return decoded


class AuthorizedToolService:
    """Build a new, authoritative manager-specific tool list per session."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        skill_service,
        skill_executor,
        mcp_service,
        mcp_registry,
    ) -> None:
        self._sessions = session_factory
        self._skills = skill_service
        self._executor = skill_executor
        self._mcp = mcp_service
        self._mcp_registry = mcp_registry

    async def _verified_manager(
        self, user_id: str, agent_id: str, session_id: str
    ) -> User:
        async with self._sessions() as db:
            user = await db.scalar(
                select(User).where(or_(User.id == user_id, User.username == user_id))
            )
            if user is None or not user.is_active or user.role != Role.MANAGER:
                raise PermissionError("active manager required")
            session = await db.get(SessionRecordRow, (user.id, session_id))
            if session is None or session.agent_id != agent_id or session.status != "active":
                raise PermissionError("session does not belong to this manager and agent")
            return user

    async def authorized_tools(
        self, user_id: str, agent_id: str, session_id: str
    ) -> list[ToolBase]:
        manager = await self._verified_manager(user_id, agent_id, session_id)
        tools: list[ToolBase] = []
        names = set(_RESERVED_NAMES)

        for skill in await self._skills.effective_skills(manager.id):
            name = _canonical_name(skill.name)
            self._reserve_name(names, name)
            parameters = json.loads(skill.parameters_json or "{}")
            schema = _bounded_schema(parameters)
            if skill.type == "python":
                tools.append(self._python_tool(manager.id, session_id, skill, name, schema))
            else:
                tools.append(self._prompt_tool(manager.id, skill, name))

        if self._mcp is not None and self._mcp_registry is not None:
            async with self._sessions() as db:
                rows = list(
                    await db.execute(
                        select(McpServerRow)
                        .join(
                            McpAuthorizationRow,
                            McpAuthorizationRow.server_id == McpServerRow.id,
                        )
                        .where(
                            McpAuthorizationRow.user_id == manager.id,
                            McpServerRow.status == "running",
                        )
                        .order_by(McpServerRow.name, McpServerRow.id)
                    )
                )
            for (server,) in rows:
                runtime = await self._mcp_registry.current(server.id)
                if runtime is None or runtime.session is None or runtime.task is None or runtime.task.done():
                    continue
                for advertised in sorted(runtime.tools.values(), key=lambda item: item.name):
                    name = _canonical_name(advertised.name)
                    self._reserve_name(names, name)
                    tools.append(
                        self._mcp_tool(
                            manager,
                            server.id,
                            advertised.name,
                            name,
                            advertised.description or "Authorized MCP tool",
                            _bounded_schema(advertised.inputSchema),
                        )
                    )
        return tools

    @staticmethod
    def _reserve_name(names: set[str], name: str) -> None:
        if name in names:
            raise PermissionError("tool name collision")
        names.add(name)

    def _python_tool(self, user_id, session_id, skill, name, schema):
        async def invoke(kwargs: dict[str, Any]):
            input_data = kwargs.get("input_data", kwargs)
            if type(input_data) is not dict:
                raise ValueError("skill input must be an object")
            # A fresh governance/integrity check precedes every process launch.
            await self._skills.resolve_execution_skill(user_id, skill.id, skill.version)
            result = await self._executor.execute(
                SkillExecutionRequest(
                    user_id=user_id,
                    skill_id=skill.id,
                    version=skill.version,
                    input_data=input_data,
                    request_id=str(uuid4()),
                )
            )
            return {
                "status": result.status,
                "output": result.output,
                "error": result.stderr_summary or None,
            }

        public_schema = {
            "type": "object",
            "properties": {"input_data": schema},
            "required": ["input_data"],
            "additionalProperties": False,
        }
        return AuthorizedFunctionTool(
            name=name,
            description=skill.description or f"Run authorized Skill {skill.name}",
            input_schema=public_schema,
            callback=invoke,
        )

    def _prompt_tool(self, user_id, skill, name):
        async def invoke(_kwargs: dict[str, Any]):
            current = await self._skills.resolve_execution_skill(
                user_id, skill.id, skill.version
            )
            instruction_path = Path(current.install_path) / "SKILL.md"
            text = instruction_path.read_text(encoding="utf-8")
            if len(text.encode("utf-8")) > 64 * 1024:
                raise PermissionError("Skill instruction is too large")
            return {"instruction": text}

        return AuthorizedFunctionTool(
            name=name,
            description=skill.description or f"Read authorized Skill {skill.name}",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            callback=invoke,
            read_only=True,
        )

    def _mcp_tool(self, manager, server_id, advertised_name, name, description, schema):
        principal = Principal(manager.id, Role.MANAGER, BANK_DEMO_TENANT_ID)

        async def invoke(arguments: dict[str, Any]):
            return await self._mcp.call_tool(
                principal, server_id, advertised_name, arguments
            )

        return AuthorizedFunctionTool(
            name=name,
            description=description,
            input_schema=schema,
            callback=invoke,
        )


__all__ = ["AuthorizedFunctionTool", "AuthorizedToolService"]
