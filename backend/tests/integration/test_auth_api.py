import asyncio
from datetime import timedelta

from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import router
from app.auth.deps import get_session, require_role
from app.auth.models import Principal
from app.auth.security import create_access_token, decode_access_token
from app.config import Settings, get_settings
from app.db.models import Role, User
from app.db.seed import seed_demo_data
from app.db.session import build_async_engine, create_schema
from app.errors import install_error_handlers


async def _with_auth_app(tmp_path, check):
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
    await create_schema(engine)
    async with sessions() as session:
        await seed_demo_data(session)

    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)

    async def session_override(_request: Request):
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/test/system-only")
    async def system_only(
        principal: Principal = Depends(require_role(Role.SYSTEM_ADMIN)),
    ) -> Principal:
        return principal

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await check(client, sessions, settings)
    finally:
        await engine.dispose()


def _assert_api_error(response, status_code, code, request_id=None):
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == code
    assert body["message"]
    assert response.headers["X-Request-ID"] == body["request_id"]
    if request_id is not None:
        assert body["request_id"] == request_id


@pytest.mark.parametrize(
    ("username", "role"),
    [
        ("manager0001", Role.MANAGER),
        ("manager0002", Role.MANAGER),
        ("business_admin01", Role.BUSINESS_ADMIN),
        ("system_admin01", Role.SYSTEM_ADMIN),
    ],
)
def test_login_and_me_for_each_seeded_user(tmp_path, username, role):
    async def check(client, _sessions, settings):
        request_id = f"login-{username}"
        login = await client.post(
            "/api/auth/login",
            json={"username": username, "password": "12345678"},
            headers={"X-Request-ID": request_id},
        )
        assert login.status_code == 200
        assert login.json()["token_type"] == "bearer"
        assert login.headers["X-Request-ID"] == request_id
        assert (
            decode_access_token(
                login.json()["access_token"], settings=settings
            ).tenant_id
            == "bank_demo"
        )

        me = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json() == {
            "user_id": me.json()["user_id"],
            "role": role.value,
            "tenant_id": "bank_demo",
        }

    asyncio.run(_with_auth_app(tmp_path, check))


def test_login_rejects_invalid_credentials(tmp_path):
    async def check(client, _sessions, _settings):
        response = await client.post(
            "/api/auth/login",
            json={"username": "manager0001", "password": "not-the-password"},
        )
        _assert_api_error(response, 401, "INVALID_CREDENTIALS")

    asyncio.run(_with_auth_app(tmp_path, check))


def test_forged_token_is_rejected(tmp_path):
    async def check(client, _sessions, settings):
        forged = create_access_token(
            Principal("user-1", Role.MANAGER, "bank_demo"),
            settings=settings.model_copy(
                update={
                    "jwt_secret_key": SecretStr(
                        "different-integration-signing-key-at-least-32-bytes"
                    )
                }
            ),
        )
        response = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {forged}"}
        )
        _assert_api_error(response, 401, "INVALID_TOKEN")

    asyncio.run(_with_auth_app(tmp_path, check))


def test_expired_token_is_rejected(tmp_path):
    async def check(client, _sessions, settings):
        token = create_access_token(
            Principal("user-1", Role.MANAGER, "bank_demo"),
            expires_delta=timedelta(seconds=-1),
            settings=settings,
        )
        response = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        _assert_api_error(response, 401, "TOKEN_EXPIRED")

    asyncio.run(_with_auth_app(tmp_path, check))


def test_disabled_user_token_is_rejected(tmp_path):
    async def check(client, sessions, settings):
        async with sessions() as session:
            user = await session.scalar(
                select(User).where(User.username == "manager0001")
            )
            user.is_active = False
            await session.commit()
            user_id = user.id

        token = create_access_token(
            Principal(user_id, Role.MANAGER, "bank_demo"), settings=settings
        )
        response = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        _assert_api_error(response, 401, "USER_DISABLED")

    asyncio.run(_with_auth_app(tmp_path, check))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic not-a-bearer-token"},
        {"Authorization": "Bearer malformed"},
    ],
)
def test_missing_or_invalid_bearer_credentials_use_api_error_contract(
    tmp_path, headers
):
    async def check(client, _sessions, _settings):
        request_headers = {**headers, "X-Request-ID": "request-from-client"}
        response = await client.get("/api/auth/me", headers=request_headers)
        _assert_api_error(response, 401, "INVALID_TOKEN", "request-from-client")

    asyncio.run(_with_auth_app(tmp_path, check))


def test_fastapi_role_dependency_returns_contract_403(tmp_path):
    async def check(client, _sessions, _settings):
        login = await client.post(
            "/api/auth/login",
            json={"username": "manager0001", "password": "12345678"},
        )
        response = await client.get(
            "/test/system-only",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        _assert_api_error(response, 403, "ROLE_FORBIDDEN")

    asyncio.run(_with_auth_app(tmp_path, check))
