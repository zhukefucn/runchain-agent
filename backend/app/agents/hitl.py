from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import HitlRequestRow, SessionRecordRow, TeamRunRow
from .sse import StableEvent


Decision = Literal["approve", "reject", "modification"]


def _deep_merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


class HitlNotFound(LookupError):
    status_code = 404


class HitlConflict(RuntimeError):
    status_code = 409


@dataclass(frozen=True)
class HitlDecisionResult:
    request_id: str
    status: str
    events: tuple[StableEvent, ...]


class HitlService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @staticmethod
    def _deserialize(row: HitlRequestRow) -> HitlDecisionResult:
        payload = row.result_data or {}
        return HitlDecisionResult(
            request_id=row.id,
            status=row.status,
            events=tuple(StableEvent.model_validate(item) for item in payload.get("events", [])),
        )

    async def decide(
        self,
        owner_user_id: str,
        request_id: str,
        decision: Decision,
        modifications: dict[str, Any] | None,
    ) -> HitlDecisionResult:
        if decision not in {"approve", "reject", "modification"}:
            raise ValueError("unsupported HITL decision")
        normalized = dict(modifications or {})
        if decision == "modification" and not normalized:
            raise ValueError("modification decision requires changes")
        if decision != "modification" and normalized:
            raise ValueError("changes are only accepted for modification decisions")

        async with self._sessions() as db:
            # Serialize decisions on SQLite so two concurrent requests cannot resume twice.
            await db.execute(text("BEGIN IMMEDIATE"))
            row = await db.scalar(
                select(HitlRequestRow)
                .join(TeamRunRow, TeamRunRow.id == HitlRequestRow.team_run_id)
                .join(
                    SessionRecordRow,
                    (SessionRecordRow.id == TeamRunRow.session_id)
                    & (SessionRecordRow.owner_user_id == TeamRunRow.owner_user_id),
                )
                .where(
                    HitlRequestRow.id == request_id,
                    HitlRequestRow.owner_user_id == owner_user_id,
                    TeamRunRow.owner_user_id == owner_user_id,
                    SessionRecordRow.owner_user_id == owner_user_id,
                )
            )
            if row is None:
                await db.rollback()
                raise HitlNotFound("HITL request not found")

            if row.status != "pending":
                if row.decision == decision and (row.modifications or {}) == normalized:
                    result = self._deserialize(row)
                    await db.rollback()
                    return result
                await db.rollback()
                raise HitlConflict("HITL request already has a different decision")

            run = await db.scalar(
                select(TeamRunRow).where(
                    TeamRunRow.id == row.team_run_id,
                    TeamRunRow.owner_user_id == owner_user_id,
                )
            )
            if run is None or run.status != "hitl_pending":
                await db.rollback()
                raise HitlConflict("team run is not waiting for HITL")

            status = {
                "approve": "approved",
                "reject": "rejected",
                "modification": "modified",
            }[decision]
            original_plan = deepcopy(run.result_data or {})
            if decision == "reject":
                final_plan = None
                terminal_result = {
                    "status": "rejected",
                    "decision": "reject",
                    "original_plan": original_plan,
                }
            else:
                final_plan = deepcopy(original_plan)
                if decision == "modification":
                    final_plan = _deep_merge(final_plan, normalized)
                final_plan["status"] = status
                final_plan["decision"] = decision
                terminal_result = final_plan
            now = datetime.now(timezone.utc)
            events = (
                StableEvent(
                    type="token",
                    request_id=request_id,
                    session_id=run.session_id,
                    run_id=run.id,
                    data={"phase": "resumed", "decision": decision},
                ),
                StableEvent(
                    type="complete",
                    request_id=request_id,
                    session_id=run.session_id,
                    run_id=run.id,
                    data={
                        "decision": decision,
                        "status": status,
                        "modifications": normalized,
                        "plan": final_plan,
                        "result": terminal_result,
                    },
                ),
            )
            payload = {"events": [event.model_dump(mode="json") for event in events]}
            await db.execute(
                update(HitlRequestRow)
                .where(
                    HitlRequestRow.id == request_id,
                    HitlRequestRow.owner_user_id == owner_user_id,
                    HitlRequestRow.status == "pending",
                )
                .values(
                    status=status,
                    decision=decision,
                    modifications=normalized,
                    result_data=payload,
                    decided_at=now,
                )
            )
            await db.execute(
                update(TeamRunRow)
                .where(
                    TeamRunRow.id == run.id,
                    TeamRunRow.owner_user_id == owner_user_id,
                )
                .values(
                    status="rejected" if decision == "reject" else "completed",
                    completed_at=now,
                    result_data=terminal_result,
                )
            )
            await db.commit()
            return HitlDecisionResult(request_id, status, events)


__all__ = [
    "HitlConflict",
    "HitlDecisionResult",
    "HitlNotFound",
    "HitlService",
]
