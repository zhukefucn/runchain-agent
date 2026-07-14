import asyncio
import inspect
from datetime import datetime, timedelta
from typing import Literal

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from app.db.models import AgentScopeStorageRow, MessageRow, Role, User
from app.db.session import build_async_engine, create_schema
from app.repositories.manager import ManagerRepository
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


def _knowledge_base(owner: str, record_id: str) -> KnowledgeBaseRecord:
    return KnowledgeBaseRecord(
        id=record_id,
        user_id=owner,
        name=record_id,
        embedding_model_config=EmbeddingModelConfig(
            type="demo",
            credential_id="credential",
            model="embedding",
            dimensions=3,
        ),
        collection_name=f"collection_{owner}_{record_id}",
    )


async def _with_storage(tmp_path, check):
    engine = build_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'storage.db'}")
    try:
        await create_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add_all(
                [
                    User(
                        id=owner,
                        username=owner,
                        password_hash="test-only",
                        role=Role.MANAGER,
                    )
                    for owner in ("manager0001", "manager0002")
                ]
            )
            await db.commit()
        storage = SQLiteStorage(factory)
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

        competing = [_agent("manager0002", "atomic") for _ in range(8)]
        for index, record in enumerate(competing):
            record.data.name = f"version-{index}"
        await asyncio.gather(
            *(storage.upsert_agent("manager0002", record) for record in competing)
        )
        assert (await storage.get_agent("manager0002", "atomic")).data.name.startswith(
            "version-"
        )

        with pytest.raises(IntegrityError):
            await storage.upsert_agent("missing-owner", _agent("missing-owner", "x"))
        async with storage._session_factory() as db:
            await db.execute(delete(User).where(User.id == "manager0002"))
            await db.commit()
            remaining = await db.scalar(
                select(func.count()).select_from(AgentScopeStorageRow).where(
                    AgentScopeStorageRow.owner_user_id == "manager0002"
                )
            )
        assert remaining == 0

    asyncio.run(_with_storage(tmp_path, check))


def test_sessions_messages_and_schedules_match_merge_and_sorting_contract(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        first = await storage.upsert_session(
            "manager0001", "agent", _config("first"), session_id="shared"
        )
        await storage.upsert_session(
            "manager0002", "agent", _config("other"), session_id="shared"
        )
        rebound = await storage.upsert_session(
            "manager0001", "other-agent", _config("renamed"), session_id="shared"
        )
        assert rebound.agent_id == "agent"
        assert (await storage.get_session("manager0001", "wrong-agent", "shared")).agent_id == "agent"
        state = AgentState(session_id="shared", summary="persisted")
        await storage.update_session_state("manager0001", "agent", "shared", state)
        assert (await storage.get_session("manager0001", "agent", "shared")).state == state
        with pytest.raises(KeyError):
            await storage.update_session_state(
                "manager0002", "agent", "not-found", AgentState()
            )
        await storage.update_session_state(
            "manager0001", "wrong-agent", "shared", AgentState(summary="wrong-agent-ok")
        )
        assert (await storage.get_session(
            "manager0001", "agent", "shared"
        )).state.summary == "wrong-agent-ok"
        await storage.upsert_session(
            "manager0001", "agent", _config("delete"), session_id="delete-wrong"
        )
        assert await storage.delete_session(
            "manager0001", "wrong-agent", "delete-wrong"
        )
        assert await storage.get_session(
            "manager0001", "agent", "delete-wrong"
        ) is None

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
        await storage.upsert_agent(
            "manager0002", _agent("manager0002", "foreign-agent")
        )
        await storage.upsert_session(
            "manager0002",
            "foreign-agent",
            _config("foreign"),
            session_id="foreign-session",
        )
        malicious_team = TeamRecord(
            id="malicious-team",
            user_id=owner,
            session_id="leader-session",
            data=TeamData(
                name="malicious",
                members=[
                    TeamMember(
                        owner_id="manager0002",
                        agent_id="foreign-agent",
                        session_id="foreign-session",
                        role="created",
                    )
                ],
            ),
        )
        with pytest.raises(ValueError):
            await storage.upsert_team(owner, malicious_team)
        assert await storage.get_team(owner, "malicious-team") is None
        assert await storage.get_agent("manager0002", "foreign-agent") is not None
        # Defensive read path: even a corrupt record left by an older build
        # cannot use its untrusted member.owner_id to delete another manager.
        await storage._put(
            owner,
            "team",
            "legacy-corrupt-team",
            storage._dump(malicious_team.model_copy(update={"id": "legacy-corrupt-team"})),
        )
        assert await storage.delete_team(owner, "legacy-corrupt-team")
        assert await storage.get_agent("manager0002", "foreign-agent") is not None
        assert await storage.get_session(
            "manager0002", "foreign-agent", "foreign-session"
        ) is not None
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
        leased = await storage.get_knowledge_document(owner, "kb", "document")
        lease_owner = leased.processing_node
        lease_deadline = leased.data.lease_expires_at
        await storage.update_knowledge_document_status(
            owner, "kb", "document", "ready", chunk_count=2
        )
        loaded = await storage.get_knowledge_document(owner, "kb", "document")
        assert loaded.data.status == "ready" and loaded.data.chunk_count == 2
        assert loaded.processing_node == lease_owner
        assert loaded.data.lease_expires_at == lease_deadline
        assert await storage.delete_knowledge_base(owner, "kb")
        assert await storage.get_knowledge_document(owner, "kb", "document") is None

    asyncio.run(_with_storage(tmp_path, check))


def test_concurrent_messages_are_serialized_without_loss_or_duplicate_ordinals(
    tmp_path,
) -> None:
    async def check(storage: SQLiteStorage) -> None:
        await storage.upsert_session(
            "manager0001", "agent", _config("concurrent"), session_id="concurrent"
        )
        messages = [
            Msg(
                id=f"message-{index}",
                name="user",
                role="user",
                content=[TextBlock(type="text", text=str(index))],
            )
            for index in range(12)
        ]
        await asyncio.gather(
            *(
                storage.upsert_message("manager0001", "concurrent", message)
                for message in messages
            )
        )
        stored = await storage.list_messages("manager0001", "concurrent")
        assert {item.id for item in stored} == {item.id for item in messages}
        async with storage._session_factory() as db:
            ordinals = list(
                await db.scalars(
                    select(MessageRow.ordinal).where(
                        MessageRow.owner_user_id == "manager0001",
                        MessageRow.session_id == "concurrent",
                    )
                )
            )
        assert len(ordinals) == len(set(ordinals)) == 12

        await storage.upsert_session(
            "manager0001", "agent", _config("merge"), session_id="merge"
        )
        same_id = [
            Msg(
                id="same-reply",
                name="assistant",
                role="assistant",
                content=[TextBlock(type="text", text=f"version-{index}")],
            )
            for index in range(8)
        ]
        await asyncio.gather(
            *(storage.upsert_message("manager0001", "merge", item) for item in same_id)
        )
        merged = await storage.list_messages("manager0001", "merge")
        assert len(merged) == 1 and merged[0].id == "same-reply"

        await storage.upsert_session(
            "manager0001",
            "agent",
            _config("repository-concurrent"),
            session_id="repository-concurrent",
        )

        async def repository_write(session_id: str, index: int) -> None:
            async with storage._session_factory() as db:
                created = await ManagerRepository(db).create_message(
                    "manager0001",
                    session_id,
                    role="user",
                    content=f"repository-{index}",
                )
                assert created is not None

        await asyncio.gather(
            *(repository_write("repository-concurrent", index) for index in range(12))
        )
        repository_messages = await storage.list_messages(
            "manager0001", "repository-concurrent"
        )
        assert len(repository_messages) == 12
        async with storage._session_factory() as db:
            repository_ordinals = list(
                await db.scalars(
                    select(MessageRow.ordinal).where(
                        MessageRow.owner_user_id == "manager0001",
                        MessageRow.session_id == "repository-concurrent",
                    )
                )
            )
        assert len(repository_ordinals) == len(set(repository_ordinals)) == 12

        await storage.upsert_session(
            "manager0001", "agent", _config("mixed"), session_id="mixed"
        )

        await asyncio.gather(
            *(
                storage.upsert_message(
                    "manager0001",
                    "mixed",
                    Msg(
                        id=f"adapter-{index}",
                        name="assistant",
                        role="assistant",
                        content=[TextBlock(type="text", text=f"adapter-{index}")],
                    ),
                )
                for index in range(6)
            ),
            *(repository_write("mixed", index) for index in range(6)),
        )
        mixed = await storage.list_messages("manager0001", "mixed")
        assert len(mixed) == 12
        async with storage._session_factory() as db:
            mixed_ordinals = list(
                await db.scalars(
                    select(MessageRow.ordinal).where(
                        MessageRow.owner_user_id == "manager0001",
                        MessageRow.session_id == "mixed",
                    )
                )
            )
        assert len(mixed_ordinals) == len(set(mixed_ordinals)) == 12

    asyncio.run(_with_storage(tmp_path, check))


def test_repository_team_session_delete_delegates_or_fails_closed(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        owner = "manager0001"
        for agent_id, source in (
            ("repo-leader", "user"),
            ("repo-created", "team"),
            ("repo-invited", "user"),
            ("closed-leader", "user"),
        ):
            await storage.upsert_agent(owner, _agent(owner, agent_id, source=source))
        await storage.upsert_schedule(
            owner,
            ScheduleRecord(
                id="repo-team-schedule",
                user_id=owner,
                agent_id="repo-leader",
                data=ScheduleData(
                    name="team schedule",
                    cron_expression="0 9 * * *",
                    chat_model_config=ChatModelConfig(
                        type="demo",
                        credential_id="credential",
                        model="model",
                        parameters={},
                    ),
                ),
            ),
        )
        for agent_id in (
            "repo-leader",
            "repo-created",
            "repo-invited",
            "closed-leader",
        ):
            await storage.upsert_session(
                owner,
                agent_id,
                _config(agent_id),
                session_id=f"{agent_id}-session",
                source=(
                    SessionSource.SCHEDULE
                    if agent_id == "repo-leader"
                    else SessionSource.USER
                ),
                source_schedule_id=(
                    "repo-team-schedule" if agent_id == "repo-leader" else None
                ),
            )
        team = TeamRecord(
            id="repo-team",
            user_id=owner,
            session_id="repo-leader-session",
            data=TeamData(
                name="repo-team",
                members=[
                    TeamMember(
                        owner_id=owner,
                        agent_id="repo-created",
                        session_id="repo-created-session",
                        role="created",
                    ),
                    TeamMember(
                        owner_id=owner,
                        agent_id="repo-invited",
                        session_id="repo-invited-session",
                        role="invited",
                    ),
                ],
            ),
        )
        await storage.upsert_team(owner, team)
        for session_id in (
            "repo-leader-session",
            "repo-created-session",
            "repo-invited-session",
        ):
            await storage.set_session_team_id(owner, session_id, "repo-team")

        async with storage._session_factory() as db:
            repository = ManagerRepository(db, session_storage=storage)
            assert await repository.delete_session(owner, "repo-leader-session")
        assert await storage.get_team(owner, "repo-team") is None
        assert await storage.get_agent(owner, "repo-created") is None
        assert await storage.get_session(
            owner, "repo-created", "repo-created-session"
        ) is None
        assert await storage.get_agent(owner, "repo-invited") is not None
        assert await storage.get_session(
            owner, "repo-invited", "repo-invited-session"
        ) is None
        assert await storage.get_session(
            owner, "repo-leader", "repo-leader-session"
        ) is None
        assert await storage.get_schedule(owner, "repo-team-schedule") is not None
        assert await storage.list_sessions_by_schedule(
            owner, "repo-team-schedule"
        ) == []

        await storage.upsert_team(
            owner,
            TeamRecord(
                id="closed-team",
                user_id=owner,
                session_id="closed-leader-session",
                data=TeamData(name="closed"),
            ),
        )
        await storage.set_session_team_id(
            owner, "closed-leader-session", "closed-team"
        )
        async with storage._session_factory() as db:
            repository = ManagerRepository(db)
            assert not await repository.delete_session(
                owner, "closed-leader-session"
            )
            assert not await repository.delete_session(
                "manager0002", "closed-leader-session"
            )
        assert await storage.get_session(
            owner, "closed-leader", "closed-leader-session"
        ) is not None

    asyncio.run(_with_storage(tmp_path, check))


def test_document_registration_and_kb_delete_are_one_transaction(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        owner = "manager0001"
        for index in range(6):
            kb_id = f"race-kb-{index}"
            await storage.upsert_knowledge_base(
                owner, _knowledge_base(owner, kb_id)
            )
            gate = asyncio.Event()

            async def register_document() -> object:
                await gate.wait()
                try:
                    return await storage.upsert_knowledge_document(
                        owner,
                        KnowledgeDocumentRecord(
                            id="race-document",
                            user_id=owner,
                            knowledge_base_id=kb_id,
                            data=KnowledgeDocumentData(
                                filename="race.txt",
                                size=1,
                                blob_uri="local://race",
                            ),
                        ),
                    )
                except ValueError as error:
                    return error

            async def delete_parent() -> bool:
                await gate.wait()
                return await storage.delete_knowledge_base(owner, kb_id)

            register_task = asyncio.create_task(register_document())
            delete_task = asyncio.create_task(delete_parent())
            gate.set()
            await asyncio.gather(register_task, delete_task)
            assert await storage.get_knowledge_base(owner, kb_id) is None
            assert await storage.get_knowledge_document(
                owner, kb_id, "race-document"
            ) is None

    asyncio.run(_with_storage(tmp_path, check))


def test_manager_repository_and_adapter_share_canonical_sessions_and_messages(
    tmp_path,
) -> None:
    async def check(storage: SQLiteStorage) -> None:
        adapter_session = await storage.upsert_session(
            "manager0001", "adapter-agent", _config("adapter"), session_id="adapter-session"
        )
        async with storage._session_factory() as db:
            repository = ManagerRepository(db)
            visible = await repository.get_session("manager0001", adapter_session.id)
            assert visible is not None and visible.agent_id == "adapter-agent"
            repository_session = await repository.create_session(
                "manager0001", "repository-agent", "repository"
            )

        restored = await storage.get_session(
            "manager0001", "repository-agent", repository_session.id
        )
        assert restored is not None and restored.config.name == "repository"

        await storage.upsert_message(
            "manager0001",
            repository_session.id,
            Msg(
                id="adapter-message",
                name="assistant",
                role="assistant",
                content=[TextBlock(type="text", text="from adapter")],
            ),
        )
        async with storage._session_factory() as db:
            repository = ManagerRepository(db)
            rows = await repository.list_messages(
                "manager0001", repository_session.id
            )
            assert len(rows) == 1 and rows[0].content == "from adapter"
            repository_message = await repository.create_message(
                "manager0001",
                repository_session.id,
                role="user",
                content="from repository",
            )
            assert repository_message is not None

        visible_messages = await storage.list_messages(
            "manager0001", repository_session.id
        )
        assert [item.content[0].text for item in visible_messages] == [
            "from adapter",
            "from repository",
        ]
        async with storage._session_factory() as db:
            duplicate_count = await db.scalar(
                select(func.count()).select_from(AgentScopeStorageRow).where(
                    AgentScopeStorageRow.namespace.in_(["session", "message"])
                )
            )
        assert duplicate_count == 0

    asyncio.run(_with_storage(tmp_path, check))


def test_document_identity_includes_owner_and_knowledge_base(tmp_path) -> None:
    async def check(storage: SQLiteStorage) -> None:
        for owner, kb_id in (
            ("manager0001", "kb-a"),
            ("manager0001", "kb-b"),
            ("manager0002", "kb-a"),
        ):
            await storage.upsert_knowledge_base(
                owner, _knowledge_base(owner, kb_id)
            )
        with pytest.raises(ValueError):
            await storage.upsert_knowledge_document(
                "manager0001",
                KnowledgeDocumentRecord(
                    id="orphan",
                    user_id="manager0001",
                    knowledge_base_id="missing-kb",
                    data=KnowledgeDocumentData(
                        filename="orphan.txt", size=1, blob_uri="local://orphan"
                    ),
                ),
            )
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
