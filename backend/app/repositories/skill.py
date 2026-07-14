from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Role, SkillAuthorizationRow, SkillRow, User


class SkillRepository:
    """Persistence operations for versioned global Skill governance."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, skill_id: str) -> SkillRow | None:
        return await self.db.get(SkillRow, skill_id)

    async def get_version(self, name: str, version: str) -> SkillRow | None:
        return await self.db.scalar(
            select(SkillRow).where(SkillRow.name == name, SkillRow.version == version)
        )

    async def add(self, row: SkillRow) -> SkillRow:
        self.db.add(row)
        await self.db.flush()
        return row

    async def retire_published_versions(self, name: str, except_id: str) -> None:
        await self.db.execute(
            update(SkillRow)
            .where(
                SkillRow.name == name,
                SkillRow.status == "published",
                SkillRow.id != except_id,
            )
            .values(status="draft")
        )

    async def manager_is_active(self, user_id: str) -> bool:
        return bool(
            await self.db.scalar(
                select(User.id).where(
                    User.id == user_id,
                    User.role == Role.MANAGER,
                    User.is_active.is_(True),
                )
            )
        )

    async def authorize(
        self, skill_id: str, user_id: str, granted_by_user_id: str
    ) -> SkillAuthorizationRow:
        existing = await self.db.scalar(
            select(SkillAuthorizationRow).where(
                SkillAuthorizationRow.skill_id == skill_id,
                SkillAuthorizationRow.user_id == user_id,
            )
        )
        if existing is not None:
            return existing
        row = SkillAuthorizationRow(
            skill_id=skill_id,
            user_id=user_id,
            granted_by_user_id=granted_by_user_id,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def revoke(self, skill_id: str, user_id: str) -> bool:
        result = await self.db.execute(
            delete(SkillAuthorizationRow).where(
                SkillAuthorizationRow.skill_id == skill_id,
                SkillAuthorizationRow.user_id == user_id,
            )
        )
        return result.rowcount == 1

    async def effective(self, user_id: str) -> list[SkillRow]:
        statement = (
            select(SkillRow)
            .join(
                SkillAuthorizationRow,
                SkillAuthorizationRow.skill_id == SkillRow.id,
            )
            .join(User, User.id == SkillAuthorizationRow.user_id)
            .where(
                SkillAuthorizationRow.user_id == user_id,
                SkillRow.status == "published",
                User.role == Role.MANAGER,
                User.is_active.is_(True),
            )
            .order_by(SkillRow.name, SkillRow.version, SkillRow.id)
        )
        return list(await self.db.scalars(statement))
