import asyncio
from datetime import timedelta

import jwt
import pytest
from pydantic import SecretStr

from app.auth.models import Principal
from app.auth.security import create_access_token, decode_access_token
from app.config import Settings
from app.db.models import Role
from app.errors import ApiError


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        workspace_root="test-workspace",
        jwt_secret_key=SecretStr("unit-test-jwt-secret-at-least-32-bytes"),
        model_api_key=SecretStr("unit-test-model-key"),
    )


def test_valid_token_round_trips_only_identity_claims():
    settings = _settings()
    principal = Principal(
        user_id="user-1", role=Role.MANAGER, tenant_id="tenant-1"
    )

    token = create_access_token(principal, settings=settings)
    claims = jwt.decode(
        token, settings.jwt_secret_key.get_secret_value(), algorithms=["HS256"]
    )

    assert set(claims) == {"sub", "role", "tenant_id", "exp"}
    assert decode_access_token(token, settings=settings) == principal


def test_forged_token_is_rejected():
    settings = _settings()
    token = create_access_token(
        Principal("user-1", Role.MANAGER, "tenant-1"), settings=settings
    )
    forged = f"{token[:-1]}{'a' if token[-1] != 'a' else 'b'}"

    with pytest.raises(ApiError) as exc_info:
        decode_access_token(forged, settings=settings)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "INVALID_TOKEN"


def test_expired_token_is_rejected():
    settings = _settings()
    token = create_access_token(
        Principal("user-1", Role.MANAGER, "tenant-1"),
        expires_delta=timedelta(seconds=-1),
        settings=settings,
    )

    with pytest.raises(ApiError) as exc_info:
        decode_access_token(token, settings=settings)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "TOKEN_EXPIRED"


def test_require_role_allows_an_allowed_principal():
    from app.auth.deps import require_role

    principal = Principal("admin-1", Role.SYSTEM_ADMIN, "tenant-1")
    dependency = require_role(Role.SYSTEM_ADMIN)

    assert asyncio.run(dependency(principal)) == principal


def test_require_role_rejects_a_disallowed_principal():
    from app.auth.deps import require_role

    principal = Principal("manager-1", Role.MANAGER, "tenant-1")
    dependency = require_role(Role.SYSTEM_ADMIN)

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(dependency(principal))

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "ROLE_FORBIDDEN"
