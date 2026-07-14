from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import AgentData, AgentRecord, StorageBase
from agentscope.message import TextBlock, UserMsg

from app.agents.sse import StableEvent


GENERAL_AGENT_ID = "general-assistant"
RECEPTION_AGENT_ID = "reception-leader"
ALLOWED_MANAGER_AGENT_IDS = frozenset({GENERAL_AGENT_ID, RECEPTION_AGENT_ID})


class GeneralAgentRuntime:
    """Run the manager's persisted ordinary AgentScope assistant."""

    def __init__(
        self,
        storage: StorageBase,
        *,
        chat_service_provider: Callable[[], Any],
    ) -> None:
        self._storage = storage
        self._chat_service_provider = chat_service_provider

    async def _ensure_agent(self, owner_user_id: str) -> None:
        if await self._storage.get_agent(owner_user_id, GENERAL_AGENT_ID) is not None:
            return
        await self._storage.upsert_agent(
            owner_user_id,
            AgentRecord(
                id=GENERAL_AGENT_ID,
                user_id=owner_user_id,
                data=AgentData(
                    name="润辰智能助手",
                    system_prompt=(
                        "你是润辰科技多租户智能体平台中的通用助手。"
                        "直接、准确地回答当前客户经理的问题；需要时使用已授权工具，"
                        "不得访问其他经理的会话、文件或能力。"
                    ),
                    context_config=ContextConfig(),
                    react_config=ReActConfig(),
                ),
            ),
        )

    @staticmethod
    def _text(message: Any) -> str:
        return "".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock)
        ).strip()

    async def chat(
        self,
        owner_user_id: str,
        session_id: str,
        prompt: str,
        *,
        request_id: str,
    ) -> AsyncIterator[StableEvent]:
        run_id = str(uuid4())
        yield StableEvent(
            type="run_started",
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            data={"mode": "general"},
        )
        try:
            await self._ensure_agent(owner_user_id)
            before = {
                message.id
                for message in await self._storage.list_messages(
                    owner_user_id, session_id, limit=500
                )
            }
            chat_service = self._chat_service_provider()
            await chat_service._run_impl(
                user_id=owner_user_id,
                session_id=session_id,
                agent_id=GENERAL_AGENT_ID,
                input_msg=UserMsg("manager", prompt),
            )
            messages = await self._storage.list_messages(
                owner_user_id, session_id, limit=500
            )
            reply = next(
                (
                    self._text(message)
                    for message in reversed(messages)
                    if message.id not in before
                    and message.role == "assistant"
                    and self._text(message)
                ),
                "",
            )
            if not reply:
                raise RuntimeError("ordinary AgentScope run produced no assistant text")
            yield StableEvent(
                type="token",
                request_id=request_id,
                session_id=session_id,
                run_id=run_id,
                data={"text": reply, "agent_type": "general"},
            )
            yield StableEvent(
                type="complete",
                request_id=request_id,
                session_id=session_id,
                run_id=run_id,
                data={"status": "completed", "mode": "general"},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            yield StableEvent(
                type="error",
                request_id=request_id,
                session_id=session_id,
                run_id=run_id,
                data={
                    "code": "GENERAL_AGENT_FAILED",
                    "message": "普通 Agent 执行失败",
                },
            )


__all__ = [
    "ALLOWED_MANAGER_AGENT_IDS",
    "GENERAL_AGENT_ID",
    "GeneralAgentRuntime",
    "RECEPTION_AGENT_ID",
]
