"""Opt-in smoke test for the approved StepFun development credential."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    ChatModelConfig,
    SessionConfig,
)
from agentscope.message import TextBlock, UserMsg

from app.agentscope_ext.sqlite_storage import RUNTIME_PLACEHOLDER_CREDENTIAL_ID
from app.config import Settings
from app.main import create_root_app


pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        os.getenv("RUN_REAL_MODEL_TESTS") != "1",
        reason="set RUN_REAL_MODEL_TESTS=1 to call the configured model",
    ),
]


def _event_payloads(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _text(response) -> str:
    return "".join(
        block.text for block in response.content if isinstance(block, TextBlock)
    ).strip()


async def _exercise(tmp_path: Path) -> None:
    settings = Settings(
        app_env="real",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'real-smoke.db'}",
        workspace_root=tmp_path / "workspace",
        skill_root=tmp_path / "skills",
        runner_root=tmp_path / "runner",
        frontend_dist=tmp_path / "missing-dist",
    )
    app = create_root_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=90,
        ) as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "manager0001", "password": "12345678"},
            )
            login.raise_for_status()
            headers = {
                "Authorization": f"Bearer {login.json()['access_token']}"
            }

            # Seed native AgentScope records. The mounted ChatService creates
            # the app-specific RuntimeAgent, which replaces this persisted
            # placeholder with the genuine Step model.
            me = await client.get("/api/auth/me", headers=headers)
            me.raise_for_status()
            user_id = me.json()["user_id"]
            agent_id = "real-step-runtime-agent"
            session_id = "real-step-runtime-session"
            prompt = "Reply with one short, normal greeting for this smoke test."
            await app.state.storage.upsert_agent(
                user_id,
                AgentRecord(
                    id=agent_id,
                    user_id=user_id,
                    data=AgentData(
                        name="real_step_runtime_smoke",
                        system_prompt="Reply directly and briefly. Do not call tools.",
                        context_config=ContextConfig(),
                        react_config=ReActConfig(),
                    ),
                ),
            )
            await app.state.storage.upsert_session(
                user_id,
                agent_id,
                SessionConfig(
                    name="real-step-runtime-smoke",
                    workspace_id=app.state.workspace_manager.assign_workspace_id(
                        user_id=user_id,
                        agent_id=agent_id,
                        session_id=session_id,
                    ),
                    chat_model_config=ChatModelConfig(
                        type="openai_credential",
                        credential_id=RUNTIME_PLACEHOLDER_CREDENTIAL_ID,
                        model="runtime-placeholder-never-called",
                        parameters={},
                    ),
                ),
                session_id=session_id,
            )
            assert (
                app.state.agentscope_app.state.custom_agent_cls.__name__
                == "RunChainRuntimeAgent"
            )
            assert type(app.state.model).__name__ == "OpenAIChatModel"

            native_chat = await client.post(
                "/internal/agentscope/chat/",
                headers=headers,
                json={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "input": UserMsg("smoke-user", prompt).model_dump(mode="json"),
                },
            )
            native_chat.raise_for_status()

            native_reply = ""
            for _ in range(180):
                messages = await app.state.storage.list_messages(user_id, session_id)
                reply_texts = [
                    text
                    for message in messages
                    if (text := _text(message)) and text != prompt
                ]
                if reply_texts:
                    native_reply = reply_texts[-1]
                    break
                await asyncio.sleep(0.5)
            assert native_reply

            # The deterministic expert-team demo is supplementary to the real
            # mounted RuntimeAgent/Step assertion above.
            session = await client.post(
                "/api/manager/sessions",
                headers=headers,
                json={
                    "title": "real-model-smoke-reception",
                    "agent_id": "reception-leader",
                },
            )
            session.raise_for_status()
            chat = await client.post(
                f"/api/manager/sessions/{session.json()['id']}/chat",
                headers=headers,
                json={
                    "prompt": (
                        "Receive three visiting guests and arrange pickup, "
                        "lodging, and dining."
                    )
                },
            )
            chat.raise_for_status()

        events = _event_payloads(chat.text)
        assert {
            event["data"].get("agent_type")
            for event in events
            if event["type"] == "agent_completed"
        } == {"pickup", "lodging", "dining"}
        assert any(event["type"] == "tool_call" for event in events)
        assert events[-1]["type"] == "hitl_pending"


def test_stepfun_reply_through_mounted_runtime_agent(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path))
