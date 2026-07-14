from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Principal
from app.auth.security import decode_access_token
from app.config import Settings, get_settings
from app.db.models import Role, User
from app.db.session import async_session_factory
from app.errors import ApiError


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session


async def get_principal(
    token: str | None = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if not token:
        raise ApiError(401, "INVALID_TOKEN", "登录凭证无效")
    principal = decode_access_token(token, settings=settings)
    user = await session.get(User, principal.user_id)
    if user is None or user.role != principal.role:
        raise ApiError(401, "INVALID_TOKEN", "登录凭证无效")
    if not user.is_active:
        raise ApiError(401, "USER_DISABLED", "用户已停用")
    return principal


def require_role(*allowed: Role):
    async def dependency(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        if principal.role not in allowed:
            raise ApiError(403, "ROLE_FORBIDDEN", "当前角色无权访问")
        return principal

    return dependency
