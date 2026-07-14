from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


StableEventType = Literal[
    "run_started", "token", "agent_started", "tool_call", "tool_result",
    "agent_completed", "hitl_pending", "complete", "error",
]


class StableEvent(BaseModel):
    type: StableEventType
    request_id: str
    session_id: str
    run_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def agent_type(self) -> str | None:
        value = self.data.get("agent_type")
        return value if isinstance(value, str) else None


def encode_sse(event: StableEvent) -> str:
    payload = event.model_dump(mode="json")
    return f"event: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


__all__ = ["StableEvent", "StableEventType", "encode_sse"]
