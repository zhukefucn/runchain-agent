import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from pydantic import SecretStr

from app.auth.models import Principal
from app.auth.security import (
    BANK_DEMO_TENANT_ID,
    create_access_token,
    decode_access_token,
)
from app.config import Settings
from app.db.models import Role
from app.errors import ApiError


def _settings(jwt_secret: str = "unit-test-jwt-secret-at-least-32-bytes") -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        workspace_root="test-workspace",
        jwt_secret_key=SecretStr(jwt_secret),
        model_api_key=SecretStr("unit-test-model-key"),
    )


def test_valid_token_round_trips_only_identity_claims():
    settings = _settings()
    principal = Principal(
        user_id="user-1", role=Role.MANAGER, tenant_id=BANK_DEMO_TENANT_ID
    )

    token = create_access_token(principal, settings=settings)
    claims = jwt.decode(
        token, settings.jwt_secret_key.get_secret_value(), algorithms=["HS256"]
    )

    assert set(claims) == {"sub", "role", "tenant_id", "exp"}
    assert decode_access_token(token, settings=settings) == principal


def test_forged_token_is_rejected():
    settings = _settings()
    forged = create_access_token(
        Principal("user-1", Role.MANAGER, BANK_DEMO_TENANT_ID),
        settings=_settings("different-signing-key-at-least-32-bytes"),
    )

    with pytest.raises(ApiError) as exc_info:
        decode_access_token(forged, settings=settings)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "INVALID_TOKEN"


def test_expired_token_is_rejected():
    settings = _settings()
    token = create_access_token(
        Principal("user-1", Role.MANAGER, BANK_DEMO_TENANT_ID),
        expires_delta=timedelta(seconds=-1),
        settings=settings,
    )

    with pytest.raises(ApiError) as exc_info:
        decode_access_token(token, settings=settings)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "TOKEN_EXPIRED"


def test_token_issuance_rejects_a_non_bank_demo_tenant():
    with pytest.raises(ValueError, match="bank_demo"):
        create_access_token(
            Principal("user-1", Role.MANAGER, "legacy-user-uuid"),
            settings=_settings(),
        )


def test_decoder_rejects_a_signed_legacy_tenant_token():
    settings = _settings()
    token = jwt.encode(
        {
            "sub": "user-1",
            "role": Role.MANAGER.value,
            "tenant_id": "legacy-user-uuid",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(ApiError) as exc_info:
        decode_access_token(token, settings=settings)

    assert exc_info.value.code == "INVALID_TOKEN"


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (Role.MANAGER, (Role.MANAGER,)),
        (Role.BUSINESS_ADMIN, (Role.BUSINESS_ADMIN,)),
        (Role.SYSTEM_ADMIN, (Role.SYSTEM_ADMIN,)),
        (
            Role.BUSINESS_ADMIN,
            (Role.MANAGER, Role.BUSINESS_ADMIN, Role.SYSTEM_ADMIN),
        ),
    ],
)
def test_require_role_allows_an_allowed_principal(role, allowed):
    from app.auth.deps import require_role

    principal = Principal("user-1", role, "tenant-1")
    dependency = require_role(*allowed)

    assert asyncio.run(dependency(principal)) == principal


def test_require_role_rejects_a_disallowed_principal():
    from app.auth.deps import require_role

    principal = Principal("manager-1", Role.MANAGER, "tenant-1")
    dependency = require_role(Role.SYSTEM_ADMIN)

    with pytest.raises(ApiError) as exc_info:
        asyncio.run(dependency(principal))

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "ROLE_FORBIDDEN"
