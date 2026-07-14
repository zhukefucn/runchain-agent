"""Opt-in smoke test for the approved StepFun development credential."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from agentscope.message import TextBlock, UserMsg

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
        try:
            generated = await app.state.model(
                [
                    UserMsg(
                        name="smoke-user",
                        content="Reply with one short, normal greeting.",
                    )
                ]
            )
            responses = [item async for item in generated]
            assert responses and responses[-1].is_last
            assert _text(responses[-1])
        except BaseException:
            app.state.model_connectivity = "unreachable"
            raise
        else:
            app.state.model_connectivity = "reachable"

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
                json={"prompt": "接待 3 位远方客人，安排接站、住宿和吃饭。"},
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
        assert app.state.model_connectivity == "reachable"


def test_stepfun_reply_and_deterministic_reception_path(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path))
