import asyncio
import inspect
from datetime import datetime, timedelta
from typing import Literal

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage._model import (
    AgentData,
    AgentRecord,
    ChatModelConfig,
    EmbeddingModelConfig,
    KnowledgeBaseRecord,
    KnowledgeDocumentData,
    KnowledgeDocumentRecord,
    ScheduleData,
    ScheduleRecord,
    SessionConfig,
    SessionSource,
    TeamData,
    TeamMember,
    TeamRecord,
)
from agentscope.credential import CredentialBase
from agentscope.message import Msg
from agentscope.message import TextBlock
from agentscope.state import AgentState

from app.agentscope_ext.sqlite_storage import SQLiteStorage
from app.db.session import build_async_engine, create_schema
from agentscope.app.storage import StorageBase


class DemoCredential(CredentialBase):
    type: Literal["demo_credential"] = "demo_credential"
    api_key: SecretStr


def _agent(owner: str, record_id: str, *, source: str = "user") -> AgentRecord:
    return AgentRecord(
        id=record_id,
        user_id=owner,
        source=source,
        data=AgentData(
            id=record_id,
            name=f"agent-{owner}",
            context_config=ContextConfig(),
            react_config=ReActConfig(),
        ),
    )


def _config(name: str = "session") -> SessionConfig:
    return SessionConfig(workspace_id=f"workspace-{name}", name=name)


async def _with_storage(tmp_path, check):
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'storage.db'}")
    try:
        await create_schema(engine)
        storage = SQLiteStorage(async_sessionmaker(engine, expire_on_commit=False))
        await check(storage)
    finally:
        await engine.dispose()


def test_sqlite_storage_is_concrete() -> None:
    assert SQLiteStorage.__abstractmethods__ == frozenset()
    for method_name in StorageBase.__abstractmethods__:
        expected = inspect.signature(getattr(StorageBase, method_name))
        actual = inspect.signature(getattr(SQLiteStorage, method_name))
        assert list(actual.parameters) == list(expected.parameters)
        assert [item.default for item in actual.parameters.values()] == [
            item.default for item in expected.parameters.values()
        ]


def test_credentials_and_agents_round_trip_with_owner_isolation(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        secret = "test-secret-value"
        credential = DemoCredential(id="same", api_key=SecretStr(secret))
        assert await storage.upsert_credential("manager0001", credential) == "same"
        stored = await storage.get_credential("manager0001", "same")
        assert stored is not None
        assert stored.data["api_key"] == secret
        assert DemoCredential.model_validate(stored.data).api_key.get_secret_value() == secret
        assert [item.id for item in await storage.list_credentials("manager0001")] == [
            "same"
        ]
        assert await storage.get_credential("manager0002", "same") is None

        await storage.upsert_agent("manager0001", _agent("manager0001", "same"))
        await storage.upsert_agent("manager0002", _agent("manager0002", "same"))
        await storage.upsert_agent(
            "manager0001", _agent("manager0001", "worker", source="team")
        )
        assert [item.id for item in await storage.list_agents("manager0001")] == [
            "same"
        ]
        assert (await storage.get_agent("manager0002", "same")).user_id == "manager0002"
        assert not await storage.delete_agent("manager0002", "missing")
        assert await storage.delete_agent("manager0001", "same")
        assert await storage.get_agent("manager0002", "same") is not None
        assert not await storage.delete_credential("manager0002", "same")
        assert await storage.delete_credential("manager0001", "same")

    asyncio.run(_with_storage(tmp_path, check))


def test_sessions_messages_and_schedules_match_merge_and_sorting_contract(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        first = await storage.upsert_session(
            "manager0001", "agent", _config("first"), session_id="shared"
        )
        await storage.upsert_session(
            "manager0002", "agent", _config("other"), session_id="shared"
        )
        assert await storage.get_session("manager0002", "wrong-agent", "shared") is None
        state = AgentState(session_id="shared", summary="persisted")
        await storage.update_session_state("manager0001", "agent", "shared", state)
        assert (await storage.get_session("manager0001", "agent", "shared")).state == state
        with pytest.raises(KeyError):
            await storage.update_session_state(
                "manager0002", "agent", "not-found", AgentState()
            )

        msg = Msg(
            id="reply",
            name="assistant",
            role="assistant",
            content=[TextBlock(type="text", text="draft")],
        )
        await storage.upsert_message("manager0001", first.id, msg)
        msg.content = [TextBlock(type="text", text="final")]
        await storage.upsert_message("manager0001", first.id, msg)
        await storage.upsert_message(
            "manager0001",
            first.id,
            Msg(
                id="next",
                name="user",
                role="user",
                content=[TextBlock(type="text", text="next")],
            ),
        )
        messages = await storage.list_messages("manager0001", first.id)
        assert [item.id for item in messages] == ["reply", "next"]
        assert messages[0].content[0].text == "final"
        assert (await storage.get_message("manager0001", first.id, "reply")).id == "reply"
        assert [item.id for item in await storage.list_messages(
            "manager0001", first.id, offset=1, limit=1
        )] == ["next"]
        assert await storage.list_messages("manager0002", first.id) == []

        schedule = ScheduleRecord(
            id="schedule",
            user_id="manager0001",
            agent_id="agent",
            data=ScheduleData(
                name="daily",
                cron_expression="0 9 * * *",
                chat_model_config=ChatModelConfig(
                    type="demo", credential_id="credential", model="model", parameters={}
                ),
            ),
        )
        await storage.upsert_schedule("manager0001", schedule)
        assert (await storage.get_schedule("manager0001", "schedule")).id == "schedule"
        assert [item.id for item in await storage.list_schedules("manager0001")] == [
            "schedule"
        ]
        scheduled = await storage.upsert_session(
            "manager0001",
            "agent",
            _config("scheduled"),
            session_id="scheduled",
            source=SessionSource.SCHEDULE,
            source_schedule_id="schedule",
        )
        assert [item.id for item in await storage.list_sessions_by_schedule(
            "manager0001", "schedule"
        )] == [scheduled.id]
        assert [item.id for item in await storage.list_all_schedules()] == ["schedule"]
        assert await storage.delete_schedule("manager0001", "schedule")
        assert await storage.get_session("manager0001", "agent", "scheduled") is None

    asyncio.run(_with_storage(tmp_path, check))


def test_team_cascade_and_knowledge_document_lifecycle(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        owner = "manager0001"
        for agent_id, source in (("leader", "user"), ("created", "team"), ("invited", "user")):
            await storage.upsert_agent(owner, _agent(owner, agent_id, source=source))
        for agent_id in ("leader", "created", "invited"):
            await storage.upsert_session(
                owner, agent_id, _config(agent_id), session_id=f"{agent_id}-session"
            )
        team = TeamRecord(
            id="team",
            user_id=owner,
            session_id="leader-session",
            data=TeamData(
                name="team",
                members=[
                    TeamMember(
                        owner_id=owner,
                        agent_id="created",
                        session_id="created-session",
                        role="created",
                    ),
                    TeamMember(
                        owner_id=owner,
                        agent_id="invited",
                        session_id="invited-session",
                        role="invited",
                    ),
                ],
            ),
        )
        await storage.upsert_team(owner, team)
        await storage.set_session_team_id(owner, "leader-session", "team")
        assert (await storage.get_team(owner, "team")).model_dump() == team.model_dump()
        assert [item.id for item in await storage.list_teams(owner)] == ["team"]
        assert await storage.delete_team(owner, "team")
        assert await storage.get_agent(owner, "created") is None
        assert await storage.get_agent(owner, "invited") is not None
        assert await storage.get_session(owner, "invited", "invited-session") is None
        assert (await storage.get_session(owner, "leader", "leader-session")).team_id is None

        await storage.upsert_agent(owner, _agent(owner, "legacy", source="team"))
        await storage.upsert_session(
            owner, "legacy", _config("legacy"), session_id="legacy-session"
        )
        await storage.upsert_team(
            owner,
            TeamRecord(
                id="legacy-team",
                user_id=owner,
                session_id="leader-session",
                data=TeamData(name="legacy", member_ids=["legacy"]),
            ),
        )
        assert await storage.delete_team(owner, "legacy-team")
        assert await storage.get_agent(owner, "legacy") is None

        kb = KnowledgeBaseRecord(
            id="kb",
            user_id=owner,
            name="knowledge",
            embedding_model_config=EmbeddingModelConfig(
                type="demo", credential_id="credential", model="embedding", dimensions=3
            ),
            collection_name="kb_collection",
        )
        await storage.upsert_knowledge_base(owner, kb)
        assert [item.id for item in await storage.list_knowledge_bases(owner)] == ["kb"]
        old = datetime.now() - timedelta(minutes=10)
        document = KnowledgeDocumentRecord(
            id="document",
            user_id=owner,
            knowledge_base_id="kb",
            created_at=old,
            data=KnowledgeDocumentData(
                filename="demo.txt", size=4, blob_uri="local://demo"
            ),
        )
        await storage.upsert_knowledge_document(owner, document)
        assert [item.id for item in await storage.list_knowledge_documents(
            owner, "kb"
        )] == ["document"]
        assert [item.id for item in await storage.list_knowledge_documents_pending_since(
            datetime.now()
        )] == ["document"]
        assert await storage.acquire_knowledge_document_lease(
            owner, "kb", "document", "worker", timedelta(seconds=1), now=old
        )
        assert not await storage.acquire_knowledge_document_lease(
            owner, "kb", "document", "other", timedelta(seconds=1), now=old
        )
        assert not await storage.renew_knowledge_document_lease(
            owner, "kb", "document", "other", timedelta(seconds=1), now=old
        )
        assert await storage.renew_knowledge_document_lease(
            owner, "kb", "document", "worker", timedelta(seconds=1), now=old
        )
        assert [item.id for item in await storage.list_knowledge_documents_with_expired_lease(
            datetime.now()
        )] == ["document"]
        await storage.release_knowledge_document_lease(
            owner, "kb", "document", "worker"
        )
        race_time = datetime.now()
        lease_results = await asyncio.gather(
            storage.acquire_knowledge_document_lease(
                owner, "kb", "document", "worker-a", timedelta(minutes=1), race_time
            ),
            storage.acquire_knowledge_document_lease(
                owner, "kb", "document", "worker-b", timedelta(minutes=1), race_time
            ),
        )
        assert sum(lease_results) == 1
        await storage.update_knowledge_document_status(
            owner, "kb", "document", "ready", chunk_count=2
        )
        loaded = await storage.get_knowledge_document(owner, "kb", "document")
        assert loaded.data.status == "ready" and loaded.data.chunk_count == 2
        assert await storage.delete_knowledge_base(owner, "kb")
        assert await storage.get_knowledge_document(owner, "kb", "document") is None

    asyncio.run(_with_storage(tmp_path, check))


def test_document_identity_includes_owner_and_knowledge_base(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        for owner, kb_id, filename in (
            ("manager0001", "kb-a", "a.txt"),
            ("manager0001", "kb-b", "b.txt"),
            ("manager0002", "kb-a", "other.txt"),
        ):
            await storage.upsert_knowledge_document(
                owner,
                KnowledgeDocumentRecord(
                    id="same-document",
                    user_id=owner,
                    knowledge_base_id=kb_id,
                    data=KnowledgeDocumentData(
                        filename=filename,
                        size=1,
                        blob_uri=f"local://{filename}",
                    ),
                ),
            )

        first = await storage.get_knowledge_document(
            "manager0001", "kb-a", "same-document"
        )
        second = await storage.get_knowledge_document(
            "manager0001", "kb-b", "same-document"
        )
        other = await storage.get_knowledge_document(
            "manager0002", "kb-a", "same-document"
        )
        assert [first.data.filename, second.data.filename, other.data.filename] == [
            "a.txt",
            "b.txt",
            "other.txt",
        ]
        assert await storage.delete_knowledge_document(
            "manager0001", "kb-a", "same-document"
        )
        assert await storage.get_knowledge_document(
            "manager0001", "kb-b", "same-document"
        ) is not None

    asyncio.run(_with_storage(tmp_path, check))
