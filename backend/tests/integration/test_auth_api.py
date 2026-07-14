import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import router
from app.auth.deps import get_session
from app.auth.models import Principal
from app.auth.security import create_access_token
from app.config import Settings, get_settings
from app.db.models import Role, User
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema
from app.errors import install_error_handlers


@pytest.fixture
def auth_client(tmp_path):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
        workspace_root=tmp_path / "workspace",
        jwt_secret_key=SecretStr("integration-test-jwt-secret-at-least-32-bytes"),
        model_api_key=SecretStr("integration-test-model-key"),
    )
    engine = build_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        await create_schema(engine)
        async with sessions() as session:
            await seed_demo_data(session)

    asyncio.run(prepare())

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    install_error_handlers(app)

    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        yield client, sessions, settings


@pytest.mark.parametrize(
    ("username", "role"),
    [
        ("manager0001", Role.MANAGER),
        ("manager0002", Role.MANAGER),
        ("business_admin01", Role.BUSINESS_ADMIN),
        ("system_admin01", Role.SYSTEM_ADMIN),
    ],
)
def test_login_and_me_for_each_seeded_user(auth_client, username, role):
    client, _sessions, _settings = auth_client

    login = client.post(
        "/api/auth/login", json={"username": username, "password": "12345678"}
    )
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"

    me = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json() == {
        "user_id": me.json()["user_id"],
        "role": role.value,
        "tenant_id": me.json()["user_id"],
    }


def test_login_rejects_invalid_credentials(auth_client):
    client, _sessions, _settings = auth_client
    response = client.post(
        "/api/auth/login",
        json={"username": "manager0001", "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


def test_forged_token_is_rejected(auth_client):
    client, _sessions, _settings = auth_client
    response = client.get(
        "/api/auth/me", headers={"Authorization": "Bearer forged"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


def test_expired_token_is_rejected(auth_client):
    client, _sessions, settings = auth_client
    token = create_access_token(
        Principal("user-1", Role.MANAGER, "user-1"),
        expires_delta=timedelta(seconds=-1),
        settings=settings,
    )

    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_EXPIRED"


def test_disabled_user_token_is_rejected(auth_client):
    client, sessions, settings = auth_client

    async def disable_user():
        async with sessions() as session:
            user = await session.scalar(
                select(User).where(User.username == "manager0001")
            )
            user.is_active = False
            await session.commit()
            return user.id

    user_id = asyncio.run(disable_user())
    token = create_access_token(
        Principal(user_id, Role.MANAGER, user_id), settings=settings
    )

    response = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "USER_DISABLED"
