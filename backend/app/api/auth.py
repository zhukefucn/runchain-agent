from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_principal, get_session
from app.auth.models import DEMO_TENANT_ID, Principal
from app.auth.security import create_access_token, verify_password
from app.config import Settings, get_settings
from app.db.models import User
from app.errors import ApiError


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = await session.scalar(
        select(User).where(User.username == credentials.username)
    )
    if (
        user is None
        or not user.is_active
        or not verify_password(credentials.password, user.password_hash)
    ):
        raise ApiError(401, "INVALID_CREDENTIALS", "用户名或密码错误")

    principal = Principal(user.id, user.role, DEMO_TENANT_ID)
    return TokenResponse(
        access_token=create_access_token(principal, settings=settings)
    )


@router.get("/me")
async def me(principal: Principal = Depends(get_principal)) -> Principal:
    return principal
