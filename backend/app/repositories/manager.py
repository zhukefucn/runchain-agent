from __future__ import annotations

from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MessageRow,
    Role,
    SessionRecordRow,
    TeamRunRow,
    User,
    WorkspaceFileRow,
)


class ManagerRepository:
    """Access manager business data only through an explicit owner boundary."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    @staticmethod
    def _parent_is_owned(child_model, owner_user_id: str):
        return exists().where(
            SessionRecordRow.id == child_model.session_id,
            SessionRecordRow.owner_user_id == owner_user_id,
            User.id == SessionRecordRow.owner_user_id,
            User.id == owner_user_id,
            User.role == Role.MANAGER,
        )

    @staticmethod
    def _owner_is_manager(owner_user_id: str):
        return exists().where(
            User.id == owner_user_id,
            User.role == Role.MANAGER,
        )

    async def create_session(
        self, owner_user_id: str, agent_id: str, title: str
    ) -> SessionRecordRow:
        manager_id = await self._db.scalar(
            select(User.id).where(
                User.id == owner_user_id,
                User.role == Role.MANAGER,
            )
        )
        if manager_id is None:
            raise PermissionError("manager business ownership requires manager role")
        row = SessionRecordRow(
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            title=title,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def list_sessions(self, owner_user_id: str) -> list[SessionRecordRow]:
        statement = (
            select(SessionRecordRow)
            .where(
                SessionRecordRow.owner_user_id == owner_user_id,
                self._owner_is_manager(owner_user_id),
            )
            .order_by(SessionRecordRow.created_at, SessionRecordRow.id)
        )
        return list(await self._db.scalars(statement))

    async def get_session(
        self, owner_user_id: str, session_id: str
    ) -> SessionRecordRow | None:
        statement = select(SessionRecordRow).where(
            SessionRecordRow.id == session_id,
            SessionRecordRow.owner_user_id == owner_user_id,
            self._owner_is_manager(owner_user_id),
        )
        return await self._db.scalar(statement)

    async def update_session(
        self,
        owner_user_id: str,
        session_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
    ) -> bool:
        values = {
            key: value
            for key, value in {"title": title, "status": status}.items()
            if value is not None
        }
        if not values:
            return await self.get_session(owner_user_id, session_id) is not None
        result = await self._db.execute(
            update(SessionRecordRow)
            .where(
                SessionRecordRow.id == session_id,
                SessionRecordRow.owner_user_id == owner_user_id,
                self._owner_is_manager(owner_user_id),
            )
            .values(**values)
        )
        await self._db.commit()
        return result.rowcount == 1

    async def delete_session(self, owner_user_id: str, session_id: str) -> bool:
        result = await self._db.execute(
            delete(SessionRecordRow).where(
                SessionRecordRow.id == session_id,
                SessionRecordRow.owner_user_id == owner_user_id,
                self._owner_is_manager(owner_user_id),
            )
        )
        await self._db.commit()
        return result.rowcount == 1

    async def create_message(
        self,
        owner_user_id: str,
        session_id: str,
        *,
        role: str,
        content: str,
    ) -> MessageRow | None:
        if await self.get_session(owner_user_id, session_id) is None:
            return None
        row = MessageRow(
            session_id=session_id,
            owner_user_id=owner_user_id,
            role=role,
            content=content,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def list_messages(
        self, owner_user_id: str, session_id: str
    ) -> list[MessageRow]:
        statement = (
            select(MessageRow)
            .where(
                MessageRow.session_id == session_id,
                MessageRow.owner_user_id == owner_user_id,
                self._parent_is_owned(MessageRow, owner_user_id),
            )
            .order_by(MessageRow.created_at, MessageRow.id)
        )
        return list(await self._db.scalars(statement))

    async def get_message(
        self, owner_user_id: str, message_id: str
    ) -> MessageRow | None:
        return await self._db.scalar(
            select(MessageRow).where(
                MessageRow.id == message_id,
                MessageRow.owner_user_id == owner_user_id,
                self._parent_is_owned(MessageRow, owner_user_id),
            )
        )

    async def update_message(
        self, owner_user_id: str, message_id: str, *, content: str
    ) -> bool:
        result = await self._db.execute(
            update(MessageRow)
            .where(
                MessageRow.id == message_id,
                MessageRow.owner_user_id == owner_user_id,
                self._parent_is_owned(MessageRow, owner_user_id),
            )
            .values(content=content)
        )
        await self._db.commit()
        return result.rowcount == 1

    async def delete_message(self, owner_user_id: str, message_id: str) -> bool:
        result = await self._db.execute(
            delete(MessageRow).where(
                MessageRow.id == message_id,
                MessageRow.owner_user_id == owner_user_id,
                self._parent_is_owned(MessageRow, owner_user_id),
            )
        )
        await self._db.commit()
        return result.rowcount == 1

    async def create_workspace_file(
        self, owner_user_id: str, session_id: str, relative_path: str
    ) -> WorkspaceFileRow | None:
        if await self.get_session(owner_user_id, session_id) is None:
            return None
        row = WorkspaceFileRow(
            session_id=session_id,
            owner_user_id=owner_user_id,
            relative_path=relative_path,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def list_workspace_files(
        self, owner_user_id: str, session_id: str
    ) -> list[WorkspaceFileRow]:
        statement = (
            select(WorkspaceFileRow)
            .where(
                WorkspaceFileRow.session_id == session_id,
                WorkspaceFileRow.owner_user_id == owner_user_id,
                self._parent_is_owned(WorkspaceFileRow, owner_user_id),
            )
            .order_by(WorkspaceFileRow.created_at, WorkspaceFileRow.id)
        )
        return list(await self._db.scalars(statement))

    async def get_workspace_file(
        self, owner_user_id: str, file_id: str
    ) -> WorkspaceFileRow | None:
        return await self._db.scalar(
            select(WorkspaceFileRow).where(
                WorkspaceFileRow.id == file_id,
                WorkspaceFileRow.owner_user_id == owner_user_id,
                self._parent_is_owned(WorkspaceFileRow, owner_user_id),
            )
        )

    async def update_workspace_file(
        self, owner_user_id: str, file_id: str, *, relative_path: str
    ) -> bool:
        result = await self._db.execute(
            update(WorkspaceFileRow)
            .where(
                WorkspaceFileRow.id == file_id,
                WorkspaceFileRow.owner_user_id == owner_user_id,
                self._parent_is_owned(WorkspaceFileRow, owner_user_id),
            )
            .values(relative_path=relative_path)
        )
        await self._db.commit()
        return result.rowcount == 1

    async def delete_workspace_file(self, owner_user_id: str, file_id: str) -> bool:
        result = await self._db.execute(
            delete(WorkspaceFileRow).where(
                WorkspaceFileRow.id == file_id,
                WorkspaceFileRow.owner_user_id == owner_user_id,
                self._parent_is_owned(WorkspaceFileRow, owner_user_id),
            )
        )
        await self._db.commit()
        return result.rowcount == 1

    async def create_team_run(
        self, owner_user_id: str, session_id: str, *, status: str = "pending"
    ) -> TeamRunRow | None:
        if await self.get_session(owner_user_id, session_id) is None:
            return None
        row = TeamRunRow(
            session_id=session_id,
            owner_user_id=owner_user_id,
            status=status,
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def list_team_runs(
        self, owner_user_id: str, session_id: str
    ) -> list[TeamRunRow]:
        statement = (
            select(TeamRunRow)
            .where(
                TeamRunRow.session_id == session_id,
                TeamRunRow.owner_user_id == owner_user_id,
                self._parent_is_owned(TeamRunRow, owner_user_id),
            )
            .order_by(TeamRunRow.created_at, TeamRunRow.id)
        )
        return list(await self._db.scalars(statement))

    async def get_team_run(
        self, owner_user_id: str, run_id: str
    ) -> TeamRunRow | None:
        return await self._db.scalar(
            select(TeamRunRow).where(
                TeamRunRow.id == run_id,
                TeamRunRow.owner_user_id == owner_user_id,
                self._parent_is_owned(TeamRunRow, owner_user_id),
            )
        )

    async def update_team_run(
        self, owner_user_id: str, run_id: str, *, status: str
    ) -> bool:
        result = await self._db.execute(
            update(TeamRunRow)
            .where(
                TeamRunRow.id == run_id,
                TeamRunRow.owner_user_id == owner_user_id,
                self._parent_is_owned(TeamRunRow, owner_user_id),
            )
            .values(status=status)
        )
        await self._db.commit()
        return result.rowcount == 1

    async def delete_team_run(self, owner_user_id: str, run_id: str) -> bool:
        result = await self._db.execute(
            delete(TeamRunRow).where(
                TeamRunRow.id == run_id,
                TeamRunRow.owner_user_id == owner_user_id,
                self._parent_is_owned(TeamRunRow, owner_user_id),
            )
        )
        await self._db.commit()
        return result.rowcount == 1
