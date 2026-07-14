"""Opt-in smoke test for the approved StepFun development credential."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

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

            assert (
                app.state.agentscope_app.state.custom_agent_cls.__name__
                == "RunChainRuntimeAgent"
            )
            assert type(app.state.model).__name__ == "OpenAIChatModel"

            general_session = await client.post(
                "/api/manager/sessions",
                headers=headers,
                json={
                    "title": "real-model-smoke-general",
                    "agent_id": "general-assistant",
                },
            )
            general_session.raise_for_status()
            general_chat = await client.post(
                f"/api/manager/sessions/{general_session.json()['id']}/chat",
                headers=headers,
                json={"prompt": "Reply with one short greeting for this smoke test."},
            )
            general_chat.raise_for_status()
            general_events = _event_payloads(general_chat.text)
            assert "".join(
                str(event["data"].get("text", ""))
                for event in general_events
                if event["type"] == "token"
            ).strip()
            assert general_events[-1]["type"] == "complete"

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
