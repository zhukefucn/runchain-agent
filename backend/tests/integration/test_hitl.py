from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import SessionRecordRow, TeamRunRow, User
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema


def _async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


async def _pending_request(tmp_path: Path, username: str = "manager0001"):
    from app.agents.reception import ReceptionTeamRuntime

    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / (username + '.db')}")
    await create_schema(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        await seed_demo_data(db)
        users = {
            row.username: row
            for row in await db.scalars(
                select(User).where(User.username.in_(("manager0001", "manager0002")))
            )
        }
        db.add(
            SessionRecordRow(
                id="hitl-session",
                owner_user_id=users[username].id,
                agent_id="reception-leader",
            )
        )
        await db.commit()
    events = [
        event
        async for event in ReceptionTeamRuntime(sessions).chat(
            users[username].id,
            "hitl-session",
            "准备接待方案",
            request_id="chat-request",
        )
    ]
    return engine, sessions, users, events[-1].data["request_id"]


@_async_test
async def test_hitl_owner_isolation_idempotency_and_conflict(tmp_path):
    from app.agents.hitl import HitlConflict, HitlNotFound, HitlService

    engine, sessions, users, request_id = await _pending_request(tmp_path)
    service = HitlService(sessions)
    with pytest.raises(HitlNotFound):
        await service.decide(users["manager0002"].id, request_id, "approve", None)

    first, repeated = await asyncio.gather(
        service.decide(users["manager0001"].id, request_id, "approve", None),
        HitlService(sessions).decide(
            users["manager0001"].id, request_id, "approve", None
        ),
    )
    assert repeated == first
    assert first.status == "approved"
    assert first.events[-1].type == "complete"
    assert first.events[-1].data["decision"] == "approve"
    assert first.events[-1].data["plan"]["pickup"]["vehicle"] == "Mock 7-seat van"

    with pytest.raises(HitlConflict) as conflict:
        await service.decide(
            users["manager0001"].id,
            request_id,
            "modification",
            {"hotel": "mock-hotel-b"},
        )
    assert conflict.value.status_code == 409

    async with sessions() as db:
        run = await db.scalar(select(TeamRunRow).where(TeamRunRow.owner_user_id == users["manager0001"].id))
    assert run.status == "completed" and run.completed_at is not None
    await engine.dispose()


@pytest.mark.parametrize(
    ("decision", "modifications", "expected_status"),
    [
        (
            "modification",
            {
                "restaurant": "mock-vegetarian",
                "lodging": {"hotel": "Mock Conference Hotel"},
            },
            "modified",
        ),
        ("reject", None, "rejected"),
    ],
)
@_async_test
async def test_hitl_modification_and_reject_resume_to_complete(
    tmp_path, decision, modifications, expected_status
):
    from app.agents.hitl import HitlService

    engine, sessions, users, request_id = await _pending_request(
        tmp_path, username="manager0002"
    )
    result = await HitlService(sessions).decide(
        users["manager0002"].id,
        request_id,
        decision,
        modifications,
    )
    assert result.status == expected_status
    assert [event.type for event in result.events] == ["token", "complete"]
    assert result.events[0].data["phase"] == "resumed"
    assert result.events[-1].data["modifications"] == (modifications or {})
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(
                TeamRunRow.owner_user_id == users["manager0002"].id
            )
        )
    if decision == "modification":
        assert result.events[-1].data["plan"]["restaurant"] == "mock-vegetarian"
        assert run.result_data["restaurant"] == "mock-vegetarian"
        assert run.result_data["lodging"]["hotel"] == "Mock Conference Hotel"
        assert run.result_data["lodging"]["rooms"] == 2
    else:
        assert result.events[-1].data["plan"] is None
        assert run.result_data["status"] == "rejected"
    await engine.dispose()


@_async_test
async def test_concurrent_conflicting_hitl_decisions_only_one_wins(tmp_path):
    from app.agents.hitl import HitlConflict, HitlService

    engine, sessions, users, request_id = await _pending_request(tmp_path)
    owner = users["manager0001"].id
    outcomes = await asyncio.gather(
        HitlService(sessions).decide(owner, request_id, "approve", None),
        HitlService(sessions).decide(owner, request_id, "reject", None),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, HitlConflict) for item in outcomes) == 1
    async with sessions() as db:
        run = await db.scalar(
            select(TeamRunRow).where(TeamRunRow.owner_user_id == owner)
        )
    winner = next(item for item in outcomes if not isinstance(item, BaseException))
    assert run.result_data == winner.events[-1].data["plan"] or (
        winner.status == "rejected" and run.result_data["status"] == "rejected"
    )
    await engine.dispose()
