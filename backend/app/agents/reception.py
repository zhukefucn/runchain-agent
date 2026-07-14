from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentscope.app import SubAgentTemplate

from app.db.models import HitlRequestRow, Role, SessionRecordRow, TeamNodeRunRow, TeamRunRow, User
from .sse import StableEvent


AgentTool = Callable[[str, str], Awaitable[dict[str, Any]]]
_AGENT_TYPES = ("pickup", "lodging", "dining")
_TOOL_PATHS = {
    "pickup": ("mock_mcp", "mock_mcp.plan_pickup"),
    "lodging": ("in_process_mock_tool", "mock_lodging.plan_stay"),
    "dining": ("authorized_python_skill", "mock_dining_skill.execute"),
}


def reception_subagent_templates() -> list[SubAgentTemplate]:
    prompt = (
        "你是远方客人接待专家团的 {member_name}。团队目标：{team_description}。"
        "仅使用已授权工具生成确定性的 Mock 建议，并向主管 {leader_name} 返回结构化结果。"
    )
    return [
        SubAgentTemplate(type="pickup", description="接站专家：通过本机 Mock MCP 规划车辆、时间和路线。", system_prompt_template=prompt),
        SubAgentTemplate(type="lodging", description="住宿专家：通过进程内 Mock Tool 规划酒店与入住。", system_prompt_template=prompt),
        SubAgentTemplate(type="dining", description="餐饮专家：通过授权 Python Skill 形态规划餐饮。", system_prompt_template=prompt),
    ]


class MockMcpPickupPath:
    """MCP-client-shaped deterministic adapter; it performs no network I/O."""

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"tool": tool_name, "arrival": "18:00", "vehicle": "Mock 7-seat van", "route": "Mock Station -> Mock Hotel", "guest_request": arguments["prompt"]}

    async def __call__(self, owner: str, prompt: str) -> dict[str, Any]:
        return await self.call_tool("plan_pickup", {"owner": owner, "prompt": prompt})


class MockLodgingTool:
    async def __call__(self, _owner: str, _prompt: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"hotel": "Mock Riverside Hotel", "rooms": 2, "check_in": "19:00"}


@dataclass(frozen=True)
class MockSkillExecutionRequest:
    user_id: str
    input_data: dict[str, Any]


class MockAuthorizedDiningSkillPath:
    """Controlled-runner service-shaped mock retaining trusted user identity."""

    async def execute(self, request: MockSkillExecutionRequest) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"restaurant": "Mock Garden Restaurant", "budget_cny": 800, "preference": "light", "owner": request.user_id}

    async def __call__(self, owner: str, prompt: str) -> dict[str, Any]:
        return await self.execute(MockSkillExecutionRequest(owner, {"prompt": prompt}))


async def _await_uncancellable(awaitable) -> None:
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    await task
    if cancelled:
        raise asyncio.CancelledError


class ReceptionTeamRuntime:
    """Deterministic Phase-1 orchestration over real AgentScope templates."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, pickup_tool: AgentTool | None = None, lodging_tool: AgentTool | None = None, dining_skill: AgentTool | None = None, templates: list[SubAgentTemplate] | None = None) -> None:
        self._sessions = session_factory
        self.tool_paths = {
            "pickup": pickup_tool or MockMcpPickupPath(),
            "lodging": lodging_tool or MockLodgingTool(),
            "dining": dining_skill or MockAuthorizedDiningSkillPath(),
        }
        self._tools = self.tool_paths
        self.templates = list(templates) if templates is not None else reception_subagent_templates()

    @property
    def tool_path_calls(self) -> list[tuple[str, str]]:
        return [(agent_type, _TOOL_PATHS[agent_type][0]) for agent_type in _AGENT_TYPES]

    @staticmethod
    def _event(event_type, stream_request_id: str, session_id: str, run_id: str, **data: Any) -> StableEvent:
        return StableEvent(type=event_type, request_id=stream_request_id, session_id=session_id, run_id=run_id, data=data)

    async def _start_run(self, owner: str, session_id: str, request_id: str) -> str:
        now = datetime.now(timezone.utc)
        async with self._sessions() as db:
            session = await db.scalar(select(SessionRecordRow).join(User, User.id == SessionRecordRow.owner_user_id).where(SessionRecordRow.id == session_id, SessionRecordRow.owner_user_id == owner, User.role == Role.MANAGER, User.is_active.is_(True)))
            if session is None:
                raise PermissionError("manager session not found")
            run = TeamRunRow(session_id=session_id, owner_user_id=owner, request_id=request_id, status="running", started_at=now)
            db.add(run)
            await db.flush()
            db.add_all(TeamNodeRunRow(team_run_id=run.id, owner_user_id=owner, agent_type=agent_type, status="running", started_at=now) for agent_type in _AGENT_TYPES)
            await db.commit()
            return run.id

    async def _persist_results(self, owner: str, run_id: str, results: dict[str, dict[str, Any] | BaseException]) -> bool:
        now = datetime.now(timezone.utc)
        failed = False
        async with self._sessions() as db:
            for agent_type, result in results.items():
                if isinstance(result, BaseException):
                    failed = True
                    values = {"status": "error", "completed_at": now, "error_code": "MOCK_TOOL_FAILED", "result_data": None}
                else:
                    values = {"status": "completed", "completed_at": now, "error_code": None, "result_data": result}
                await db.execute(update(TeamNodeRunRow).where(TeamNodeRunRow.team_run_id == run_id, TeamNodeRunRow.owner_user_id == owner, TeamNodeRunRow.agent_type == agent_type).values(**values))
            if failed:
                await db.execute(update(TeamRunRow).where(TeamRunRow.id == run_id, TeamRunRow.owner_user_id == owner).values(status="error", completed_at=now))
            await db.commit()
        return failed

    async def _persist_hitl(self, owner: str, run_id: str, prompt: str, results: dict[str, dict[str, Any] | BaseException]) -> str:
        request = HitlRequestRow(team_run_id=run_id, owner_user_id=owner, prompt=prompt, status="pending")
        summary = {key: value for key, value in results.items() if isinstance(value, dict)}
        async with self._sessions() as db:
            db.add(request)
            await db.execute(update(TeamRunRow).where(TeamRunRow.id == run_id, TeamRunRow.owner_user_id == owner).values(status="hitl_pending", result_data=summary))
            await db.commit()
            await db.refresh(request)
            return request.id

    async def _persist_cancelled(self, owner: str, run_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._sessions() as db:
            await db.execute(update(TeamNodeRunRow).where(TeamNodeRunRow.team_run_id == run_id, TeamNodeRunRow.owner_user_id == owner, TeamNodeRunRow.status == "running").values(status="cancelled", completed_at=now, error_code="RUN_CANCELLED"))
            await db.execute(update(TeamRunRow).where(TeamRunRow.id == run_id, TeamRunRow.owner_user_id == owner).values(status="cancelled", completed_at=now))
            await db.commit()

    async def chat(self, owner_user_id: str, session_id: str, prompt: str, *, request_id: str | None = None) -> AsyncIterator[StableEvent]:
        request_id = request_id or str(uuid4())
        run_id = await self._start_run(owner_user_id, session_id, request_id)
        hitl_persisted = False
        try:
            yield self._event("run_started", request_id, session_id, run_id, mode="reception_team")
            yield self._event("token", request_id, session_id, run_id, agent_type="leader", text="主管已拆解接站、住宿、餐饮三个并行任务。")
            for agent_type in _AGENT_TYPES:
                yield self._event("agent_started", request_id, session_id, run_id, agent_type=agent_type)
                tool_path, tool_name = _TOOL_PATHS[agent_type]
                yield self._event("tool_call", request_id, session_id, run_id, agent_type=agent_type, tool_path=tool_path, tool_name=tool_name)
            raw_results = await asyncio.gather(*(self._tools[k](owner_user_id, prompt) for k in _AGENT_TYPES), return_exceptions=True)
            results = dict(zip(_AGENT_TYPES, raw_results, strict=True))
            failed = await self._persist_results(owner_user_id, run_id, results)
            for agent_type in _AGENT_TYPES:
                result = results[agent_type]
                if isinstance(result, BaseException):
                    continue
                yield self._event("tool_result", request_id, session_id, run_id, agent_type=agent_type, result=result)
                yield self._event("agent_completed", request_id, session_id, run_id, agent_type=agent_type, result=result)
            if failed:
                yield self._event("error", request_id, session_id, run_id, code="TEAM_NODE_FAILED", message="Mock expert team execution failed")
                return
            hitl_id = await self._persist_hitl(owner_user_id, run_id, prompt, results)
            hitl_persisted = True
            yield self._event("hitl_pending", request_id, session_id, run_id, request_id=hitl_id, summary={k: v for k, v in results.items() if isinstance(v, dict)})
        except asyncio.CancelledError:
            if not hitl_persisted:
                try:
                    await _await_uncancellable(self._persist_cancelled(owner_user_id, run_id))
                except asyncio.CancelledError:
                    pass
            raise
        except GeneratorExit:
            if not hitl_persisted:
                await asyncio.shield(self._persist_cancelled(owner_user_id, run_id))
            raise


__all__ = [
    "MockAuthorizedDiningSkillPath",
    "MockLodgingTool",
    "MockMcpPickupPath",
    "ReceptionTeamRuntime",
    "reception_subagent_templates",
]
