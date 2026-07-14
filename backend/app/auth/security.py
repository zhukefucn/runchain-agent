from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
import jwt

from app.auth.models import Principal
from app.config import Settings, get_settings
from app.db.models import Role
from app.errors import ApiError


ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(minutes=30)
BANK_DEMO_TENANT_ID = "bank_demo"
_password_hasher = PasswordHasher()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(
    principal: Principal,
    *,
    expires_delta: timedelta = ACCESS_TOKEN_TTL,
    settings: Settings | None = None,
) -> str:
    if principal.tenant_id != BANK_DEMO_TENANT_ID:
        raise ValueError("tenant_id must be bank_demo")
    resolved_settings = settings or get_settings()
    claims = {
        "sub": principal.user_id,
        "role": principal.role.value,
        "tenant_id": principal.tenant_id,
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    return jwt.encode(
        claims,
        resolved_settings.jwt_secret_key.get_secret_value(),
        algorithm=ALGORITHM,
    )


def decode_access_token(
    token: str, *, settings: Settings | None = None
) -> Principal:
    resolved_settings = settings or get_settings()
    try:
        claims = jwt.decode(
            token,
            resolved_settings.jwt_secret_key.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": ["sub", "role", "tenant_id", "exp"]},
        )
        if set(claims) != {"sub", "role", "tenant_id", "exp"}:
            raise jwt.InvalidTokenError("unexpected claims")
        if claims["tenant_id"] != BANK_DEMO_TENANT_ID:
            raise jwt.InvalidTokenError("unexpected tenant")
        return Principal(
            user_id=str(claims["sub"]),
            role=Role(claims["role"]),
            tenant_id=str(claims["tenant_id"]),
        )
    except jwt.ExpiredSignatureError as error:
        raise ApiError(401, "TOKEN_EXPIRED", "登录凭证已过期") from error
    except (jwt.InvalidTokenError, ValueError, TypeError) as error:
        raise ApiError(401, "INVALID_TOKEN", "登录凭证无效") from error
