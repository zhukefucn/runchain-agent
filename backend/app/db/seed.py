from argon2 import PasswordHasher
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import McpServerRow, Role, User


DEMO_USERS = {
    "manager0001": Role.MANAGER,
    "manager0002": Role.MANAGER,
    "business_admin01": Role.BUSINESS_ADMIN,
    "system_admin01": Role.SYSTEM_ADMIN,
}
_DEMO_PASSWORD = "12345678"
_password_hasher = PasswordHasher()


async def seed_demo_data(session: AsyncSession) -> None:
    await session.execute(
        update(McpServerRow)
        .where(McpServerRow.status == "running")
        .values(status="stopped", last_error=None)
    )
    existing_usernames = set(
        await session.scalars(select(User.username).where(User.username.in_(DEMO_USERS)))
    )
    session.add_all(
        User(
            username=username,
            password_hash=_password_hasher.hash(_DEMO_PASSWORD),
            role=role,
        )
        for username, role in DEMO_USERS.items()
        if username not in existing_usernames
    )
    await session.commit()
