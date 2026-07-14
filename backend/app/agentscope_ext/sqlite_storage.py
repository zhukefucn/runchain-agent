"""SQLite implementation of the checked-in AgentScope storage contract."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentscope.app.storage import StorageBase
from agentscope.app.storage._model import (
    AgentRecord,
    CredentialRecord,
    KnowledgeBaseRecord,
    KnowledgeDocumentRecord,
    KnowledgeDocumentStatus,
    ScheduleRecord,
    SessionConfig,
    SessionRecord,
    SessionSource,
    TeamRecord,
)
from agentscope.app.storage._utils import _dump_with_secrets, _ensure_team_members
from agentscope.credential import CredentialBase
from agentscope.message import Msg
from agentscope.state import AgentState

from app.db.models import AgentScopeStorageRow
from app.db.session import async_session_factory


class SQLiteStorage(StorageBase):
    """Persist AgentScope records in the demo database with owner scoping."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session_factory

    @staticmethod
    def _dump(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json")

    @staticmethod
    def _document_key(knowledge_base_id: str, document_id: str) -> str:
        return f"{len(knowledge_base_id)}:{knowledge_base_id}{document_id}"

    async def _get_row(
        self, owner: str, namespace: str, record_id: str
    ) -> AgentScopeStorageRow | None:
        async with self._session_factory() as db:
            return await db.get(AgentScopeStorageRow, (owner, namespace, record_id))

    async def _get_payload(
        self, owner: str, namespace: str, record_id: str
    ) -> dict[str, Any] | None:
        row = await self._get_row(owner, namespace, record_id)
        return dict(row.payload) if row is not None else None

    async def _put(
        self,
        owner: str,
        namespace: str,
        record_id: str,
        payload: dict[str, Any],
        *,
        parent_id: str | None = None,
        ordinal: int = 0,
    ) -> None:
        async with self._session_factory() as db:
            row = await db.get(AgentScopeStorageRow, (owner, namespace, record_id))
            if row is None:
                row = AgentScopeStorageRow(
                    owner_user_id=owner,
                    namespace=namespace,
                    record_id=record_id,
                    parent_id=parent_id,
                    ordinal=ordinal,
                    payload=payload,
                )
                db.add(row)
            else:
                row.payload = payload
                row.parent_id = parent_id
                row.ordinal = ordinal
                row.updated_at = datetime.now()
            await db.commit()

    async def _list_payloads(
        self,
        owner: str | None,
        namespace: str,
        *,
        parent_id: str | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        statement = select(AgentScopeStorageRow).where(
            AgentScopeStorageRow.namespace == namespace
        )
        if owner is not None:
            statement = statement.where(AgentScopeStorageRow.owner_user_id == owner)
        if parent_id is not None:
            statement = statement.where(AgentScopeStorageRow.parent_id == parent_id)
        order = AgentScopeStorageRow.created_at.desc() if newest_first else AgentScopeStorageRow.created_at.asc()
        statement = statement.order_by(order, AgentScopeStorageRow.record_id.asc())
        async with self._session_factory() as db:
            return [dict(row.payload) for row in await db.scalars(statement)]

    async def _delete(self, owner: str, namespace: str, record_id: str) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                delete(AgentScopeStorageRow).where(
                    AgentScopeStorageRow.owner_user_id == owner,
                    AgentScopeStorageRow.namespace == namespace,
                    AgentScopeStorageRow.record_id == record_id,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def upsert_credential(
        self, user_id: str, credential_data: CredentialBase
    ) -> str:
        if not credential_data.name:
            credential_type = getattr(credential_data, "type", "")
            base = credential_type.removesuffix("_credential").replace("_", " ").title()
            base = base or "Credential"
            existing = await self.list_credentials(user_id)
            names = [
                item.data.get("name", "")
                for item in existing
                if item.data.get("type") == credential_type
                and item.id != credential_data.id
            ]
            credential_data.name = base
            suffix = 2
            while credential_data.name in names:
                credential_data.name = f"{base} ({suffix})"
                suffix += 1
        data = _dump_with_secrets(credential_data)
        current = await self.get_credential(user_id, credential_data.id)
        record = CredentialRecord(
            id=credential_data.id,
            user_id=user_id,
            data=data,
            created_at=current.created_at if current else datetime.now(),
            updated_at=datetime.now(),
        )
        await self._put(user_id, "credential", record.id, self._dump(record))
        return record.id

    async def list_credentials(self, user_id: str) -> list[CredentialRecord]:
        return [
            CredentialRecord.model_validate(value)
            for value in await self._list_payloads(user_id, "credential")
        ]

    async def get_credential(
        self, user_id: str, credential_id: str
    ) -> CredentialRecord | None:
        value = await self._get_payload(user_id, "credential", credential_id)
        return CredentialRecord.model_validate(value) if value else None

    async def delete_credential(self, user_id: str, credential_id: str) -> bool:
        return await self._delete(user_id, "credential", credential_id)

    async def upsert_agent(self, user_id: str, agent_record: AgentRecord) -> str:
        if agent_record.user_id != user_id:
            raise ValueError("agent_record.user_id does not match user_id")
        await self._put(user_id, "agent", agent_record.id, self._dump(agent_record))
        return agent_record.id

    async def list_agents(self, user_id: str) -> list[AgentRecord]:
        records = [
            AgentRecord.model_validate(value)
            for value in await self._list_payloads(user_id, "agent")
        ]
        return [record for record in records if record.source == "user"]

    async def get_agent(self, user_id: str, agent_id: str) -> AgentRecord | None:
        value = await self._get_payload(user_id, "agent", agent_id)
        return AgentRecord.model_validate(value) if value else None

    async def delete_agent(self, user_id: str, agent_id: str) -> bool:
        if await self.get_agent(user_id, agent_id) is None:
            return False
        for session in await self.list_sessions(user_id, agent_id):
            await self.delete_session(user_id, agent_id, session.id)
        for schedule in await self.list_schedules(user_id):
            if schedule.agent_id == agent_id:
                await self.delete_schedule(user_id, schedule.id)
        for team in await self.list_teams(user_id):
            dirty = agent_id in team.data.member_ids
            old_count = len(team.data.members)
            team.data.member_ids = [i for i in team.data.member_ids if i != agent_id]
            team.data.members = [m for m in team.data.members if m.agent_id != agent_id]
            if dirty or len(team.data.members) != old_count:
                await self.upsert_team(user_id, team)
        return await self._delete(user_id, "agent", agent_id)

    async def upsert_session(
        self,
        user_id: str,
        agent_id: str,
        config: SessionConfig,
        state: AgentState | None = None,
        session_id: str | None = None,
        source: SessionSource = SessionSource.USER,
        source_schedule_id: str | None = None,
    ) -> SessionRecord:
        current = (
            await self.get_session(user_id, agent_id, session_id)
            if session_id
            else None
        )
        if current:
            current.config = config
            if state is not None:
                current.state = state
            current.updated_at = datetime.now()
            record = current
        else:
            kwargs = {"id": session_id} if session_id else {}
            record = SessionRecord(
                user_id=user_id,
                agent_id=agent_id,
                config=config,
                state=state or AgentState(),
                source=source,
                source_schedule_id=source_schedule_id,
                **kwargs,
            )
        await self._put(
            user_id, "session", record.id, self._dump(record), parent_id=agent_id
        )
        return record

    async def set_session_team_id(
        self, user_id: str, session_id: str, team_id: str | None
    ) -> None:
        value = await self._get_payload(user_id, "session", session_id)
        if not value:
            return
        record = SessionRecord.model_validate(value)
        if record.team_id == team_id:
            return
        record.team_id = team_id
        record.updated_at = datetime.now()
        await self._put(
            user_id, "session", record.id, self._dump(record), parent_id=record.agent_id
        )

    async def update_session_state(
        self, user_id: str, agent_id: str, session_id: str, state: AgentState
    ) -> None:
        record = await self.get_session(user_id, agent_id, session_id)
        if record is None:
            raise KeyError(f"Session {session_id!r} not found.")
        record.state = state
        record.updated_at = datetime.now()
        await self._put(
            user_id, "session", record.id, self._dump(record), parent_id=agent_id
        )

    async def list_sessions(self, user_id: str, agent_id: str) -> list[SessionRecord]:
        records = [
            SessionRecord.model_validate(value)
            for value in await self._list_payloads(
                user_id, "session", parent_id=agent_id, newest_first=True
            )
        ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def delete_session(
        self, user_id: str, agent_id: str, session_id: str
    ) -> bool:
        record = await self.get_session(user_id, agent_id, session_id)
        if record is None:
            return False
        if record.team_id:
            team = await self.get_team(user_id, record.team_id)
            if team and team.session_id == session_id:
                await self.delete_team(user_id, team.id)
        async with self._session_factory() as db:
            await db.execute(
                delete(AgentScopeStorageRow).where(
                    AgentScopeStorageRow.owner_user_id == user_id,
                    AgentScopeStorageRow.namespace == "message",
                    AgentScopeStorageRow.parent_id == session_id,
                )
            )
            await db.execute(
                delete(AgentScopeStorageRow).where(
                    AgentScopeStorageRow.owner_user_id == user_id,
                    AgentScopeStorageRow.namespace == "session",
                    AgentScopeStorageRow.record_id == session_id,
                    AgentScopeStorageRow.parent_id == agent_id,
                )
            )
            await db.commit()
        return True

    async def get_session(
        self, user_id: str, agent_id: str, session_id: str
    ) -> SessionRecord | None:
        value = await self._get_payload(user_id, "session", session_id)
        if not value:
            return None
        record = SessionRecord.model_validate(value)
        return record if record.agent_id == agent_id else None

    async def list_sessions_by_schedule(
        self, user_id: str, schedule_id: str
    ) -> list[SessionRecord]:
        records = [
            SessionRecord.model_validate(value)
            for value in await self._list_payloads(user_id, "session")
        ]
        return sorted(
            [r for r in records if r.source_schedule_id == schedule_id],
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def upsert_schedule(self, user_id: str, record: ScheduleRecord) -> str:
        if record.user_id != user_id:
            raise ValueError("record.user_id does not match user_id")
        await self._put(user_id, "schedule", record.id, self._dump(record))
        return record.id

    async def get_schedule(
        self, user_id: str, schedule_id: str
    ) -> ScheduleRecord | None:
        value = await self._get_payload(user_id, "schedule", schedule_id)
        return ScheduleRecord.model_validate(value) if value else None

    async def list_schedules(self, user_id: str) -> list[ScheduleRecord]:
        return [
            ScheduleRecord.model_validate(value)
            for value in await self._list_payloads(user_id, "schedule")
        ]

    async def delete_schedule(self, user_id: str, schedule_id: str) -> bool:
        record = await self.get_schedule(user_id, schedule_id)
        if record is None:
            return False
        for session in await self.list_sessions_by_schedule(user_id, schedule_id):
            await self.delete_session(user_id, record.agent_id, session.id)
        return await self._delete(user_id, "schedule", schedule_id)

    async def list_all_schedules(self) -> list[ScheduleRecord]:
        # System-startup restore only. Business routes must use list_schedules.
        return [
            ScheduleRecord.model_validate(value)
            for value in await self._list_payloads(None, "schedule")
        ]

    async def upsert_message(self, user_id: str, session_id: str, msg: Msg) -> None:
        async with self._session_factory() as db:
            statement = (
                select(AgentScopeStorageRow)
                .where(
                    AgentScopeStorageRow.owner_user_id == user_id,
                    AgentScopeStorageRow.namespace == "message",
                    AgentScopeStorageRow.parent_id == session_id,
                )
                .order_by(AgentScopeStorageRow.ordinal.desc())
                .limit(1)
            )
            last = await db.scalar(statement)
            if last is not None and last.payload.get("id") == msg.id:
                last.payload = self._dump(msg)
            else:
                ordinal = (last.ordinal + 1) if last else 0
                db.add(
                    AgentScopeStorageRow(
                        owner_user_id=user_id,
                        namespace="message",
                        record_id=f"{session_id}:{ordinal:020d}",
                        parent_id=session_id,
                        ordinal=ordinal,
                        payload=self._dump(msg),
                    )
                )
            await db.commit()

    async def get_message(
        self, user_id: str, session_id: str, message_id: str
    ) -> Msg | None:
        values = await self._list_payloads(user_id, "message", parent_id=session_id)
        for value in reversed(values):
            if value.get("id") == message_id:
                return Msg.model_validate(value)
        return None

    async def list_messages(
        self, user_id: str, session_id: str, offset: int = 0, limit: int = 50
    ) -> list[Msg]:
        if offset < 0 or limit <= 0:
            return []
        async with self._session_factory() as db:
            statement = (
                select(AgentScopeStorageRow)
                .where(
                    AgentScopeStorageRow.owner_user_id == user_id,
                    AgentScopeStorageRow.namespace == "message",
                    AgentScopeStorageRow.parent_id == session_id,
                )
                .order_by(AgentScopeStorageRow.ordinal.asc())
                .offset(offset)
                .limit(limit)
            )
            return [Msg.model_validate(row.payload) for row in await db.scalars(statement)]

    async def upsert_team(self, user_id: str, record: TeamRecord) -> TeamRecord:
        if record.user_id != user_id:
            raise ValueError("record.user_id does not match user_id")
        current = await self.get_team(user_id, record.id)
        if current:
            record.created_at = current.created_at
        record.updated_at = datetime.now()
        await self._put(user_id, "team", record.id, self._dump(record))
        return record

    async def get_team(self, user_id: str, team_id: str) -> TeamRecord | None:
        value = await self._get_payload(user_id, "team", team_id)
        return TeamRecord.model_validate(value) if value else None

    async def list_teams(self, user_id: str) -> list[TeamRecord]:
        return [
            TeamRecord.model_validate(value)
            for value in await self._list_payloads(user_id, "team")
        ]

    async def delete_team(self, user_id: str, team_id: str) -> bool:
        team = await self.get_team(user_id, team_id)
        if team is None:
            return False
        for member in await _ensure_team_members(self, user_id, team):
            if member.role == "created":
                await self.delete_agent(member.owner_id, member.agent_id)
            else:
                await self.delete_session(
                    member.owner_id, member.agent_id, member.session_id
                )
        await self.set_session_team_id(user_id, team.session_id, None)
        return await self._delete(user_id, "team", team_id)

    async def upsert_knowledge_base(
        self, user_id: str, record: KnowledgeBaseRecord
    ) -> KnowledgeBaseRecord:
        if record.user_id != user_id:
            raise ValueError("record.user_id does not match user_id")
        current = await self.get_knowledge_base(user_id, record.id)
        if current:
            record.created_at = current.created_at
        record.updated_at = datetime.now()
        await self._put(user_id, "knowledge_base", record.id, self._dump(record))
        return record

    async def get_knowledge_base(
        self, user_id: str, knowledge_base_id: str
    ) -> KnowledgeBaseRecord | None:
        value = await self._get_payload(user_id, "knowledge_base", knowledge_base_id)
        return KnowledgeBaseRecord.model_validate(value) if value else None

    async def list_knowledge_bases(self, user_id: str) -> list[KnowledgeBaseRecord]:
        return [
            KnowledgeBaseRecord.model_validate(value)
            for value in await self._list_payloads(user_id, "knowledge_base")
        ]

    async def delete_knowledge_base(
        self, user_id: str, knowledge_base_id: str
    ) -> bool:
        if await self.get_knowledge_base(user_id, knowledge_base_id) is None:
            return False
        for document in await self.list_knowledge_documents(user_id, knowledge_base_id):
            await self.delete_knowledge_document(user_id, knowledge_base_id, document.id)
        return await self._delete(user_id, "knowledge_base", knowledge_base_id)

    async def upsert_knowledge_document(
        self, user_id: str, record: KnowledgeDocumentRecord
    ) -> KnowledgeDocumentRecord:
        if record.user_id != user_id:
            raise ValueError("record.user_id does not match user_id")
        current = await self.get_knowledge_document(
            user_id, record.knowledge_base_id, record.id
        )
        if current:
            record.created_at = current.created_at
        record.updated_at = datetime.now()
        await self._put(
            user_id,
            "knowledge_document",
            self._document_key(record.knowledge_base_id, record.id),
            self._dump(record),
            parent_id=record.knowledge_base_id,
        )
        return record

    async def get_knowledge_document(
        self, user_id: str, knowledge_base_id: str, document_id: str
    ) -> KnowledgeDocumentRecord | None:
        value = await self._get_payload(
            user_id,
            "knowledge_document",
            self._document_key(knowledge_base_id, document_id),
        )
        if not value:
            return None
        record = KnowledgeDocumentRecord.model_validate(value)
        return record if record.knowledge_base_id == knowledge_base_id else None

    async def list_knowledge_documents(
        self, user_id: str, knowledge_base_id: str
    ) -> list[KnowledgeDocumentRecord]:
        return [
            KnowledgeDocumentRecord.model_validate(value)
            for value in await self._list_payloads(
                user_id, "knowledge_document", parent_id=knowledge_base_id
            )
        ]

    async def delete_knowledge_document(
        self, user_id: str, knowledge_base_id: str, document_id: str
    ) -> bool:
        if await self.get_knowledge_document(user_id, knowledge_base_id, document_id) is None:
            return False
        return await self._delete(
            user_id,
            "knowledge_document",
            self._document_key(knowledge_base_id, document_id),
        )

    async def update_knowledge_document_status(
        self,
        user_id: str,
        knowledge_base_id: str,
        document_id: str,
        status: KnowledgeDocumentStatus,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> None:
        record = await self.get_knowledge_document(
            user_id, knowledge_base_id, document_id
        )
        if record is None:
            return
        record.data.status = status
        if error is not None:
            record.data.error = error
        if chunk_count is not None:
            record.data.chunk_count = chunk_count
        await self.upsert_knowledge_document(user_id, record)

    async def acquire_knowledge_document_lease(
        self,
        user_id: str,
        knowledge_base_id: str,
        document_id: str,
        processing_node: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now()
        key = self._document_key(knowledge_base_id, document_id)
        async with self._session_factory() as db:
            # SQLite has no row-level FOR UPDATE. BEGIN IMMEDIATE serializes
            # lease compare-and-swap writers without locking ordinary reads.
            await db.execute(text("BEGIN IMMEDIATE"))
            row = await db.get(
                AgentScopeStorageRow, (user_id, "knowledge_document", key)
            )
            if row is None:
                await db.rollback()
                return False
            record = KnowledgeDocumentRecord.model_validate(row.payload)
            if (
                record.knowledge_base_id != knowledge_base_id
                or (
                    record.processing_node is not None
                    and record.data.lease_expires_at is not None
                    and record.data.lease_expires_at > now
                )
            ):
                await db.rollback()
                return False
            record.processing_node = processing_node
            record.data.lease_expires_at = now + lease_ttl
            record.updated_at = now
            row.payload = self._dump(record)
            await db.commit()
            return True

    async def renew_knowledge_document_lease(
        self,
        user_id: str,
        knowledge_base_id: str,
        document_id: str,
        processing_node: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now()
        key = self._document_key(knowledge_base_id, document_id)
        async with self._session_factory() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            row = await db.get(
                AgentScopeStorageRow, (user_id, "knowledge_document", key)
            )
            if row is None:
                await db.rollback()
                return False
            record = KnowledgeDocumentRecord.model_validate(row.payload)
            if record.processing_node != processing_node:
                await db.rollback()
                return False
            record.data.lease_expires_at = now + lease_ttl
            record.updated_at = now
            row.payload = self._dump(record)
            await db.commit()
            return True

    async def release_knowledge_document_lease(
        self,
        user_id: str,
        knowledge_base_id: str,
        document_id: str,
        processing_node: str,
    ) -> None:
        key = self._document_key(knowledge_base_id, document_id)
        async with self._session_factory() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            row = await db.get(
                AgentScopeStorageRow, (user_id, "knowledge_document", key)
            )
            if row is None:
                await db.rollback()
                return
            record = KnowledgeDocumentRecord.model_validate(row.payload)
            if record.processing_node != processing_node:
                await db.rollback()
                return
            record.processing_node = None
            record.data.lease_expires_at = None
            record.updated_at = datetime.now()
            row.payload = self._dump(record)
            await db.commit()

    async def list_knowledge_documents_with_expired_lease(
        self, now: datetime | None = None
    ) -> list[KnowledgeDocumentRecord]:
        # Internal index-worker recovery scan; never expose through business APIs.
        now = now or datetime.now()
        records = [
            KnowledgeDocumentRecord.model_validate(value)
            for value in await self._list_payloads(None, "knowledge_document")
        ]
        return [
            record
            for record in records
            if record.data.status not in {"ready", "error"}
            and record.processing_node is not None
            and record.data.lease_expires_at is not None
            and record.data.lease_expires_at < now
        ]

    async def list_knowledge_documents_pending_since(
        self, threshold: datetime
    ) -> list[KnowledgeDocumentRecord]:
        # Internal index-worker recovery scan; never expose through business APIs.
        records = [
            KnowledgeDocumentRecord.model_validate(value)
            for value in await self._list_payloads(None, "knowledge_document")
        ]
        return [
            record
            for record in records
            if record.data.status == "pending" and record.created_at < threshold
        ]
