from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditRecordRow


_REDACTED = "[REDACTED]"
_SENSITIVE_TOKENS = {
    "authorization",
    "body",
    "content",
    "cookie",
    "credential",
    "key",
    "message",
    "password",
    "payload",
    "secret",
    "token",
}
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:\b(?:authorization|api[_-]?key|password|secret|token)\b\s*[:=]"
    r"|\bbearer\s+[a-z0-9._~+/-]+)"
)
_SAFE_ENUM_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")

# Fail-closed audit metadata schema. Add a field here only after establishing
# that it cannot contain manager business content or credentials.
_ALLOWED_SCALAR_FIELDS: dict[str, tuple[type, ...]] = {
    "count": (int,),
    "duration_ms": (int, float),
    "enabled": (bool,),
    "error_code": (str,),
    "operation": (str,),
    "reason_code": (str,),
    "retryable": (bool,),
    "role": (str,),
    "status": (str,),
    "status_code": (int, str),
    "transport": (str,),
}
_ALLOWED_MAPPING_FIELDS = {"metrics"}
_ALLOWED_SEQUENCE_FIELDS = {"operations"}


def _key_tokens(key: object) -> set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
    return {token for token in re.split(r"[^a-z0-9]+", text) if token}


def _is_sensitive_key(key: object) -> bool:
    tokens = _key_tokens(key)
    return bool(tokens & _SENSITIVE_TOKENS) or "apikey" in "".join(tokens)


def _sanitize_scalar(key: str, value: Any) -> Any:
    allowed_types = _ALLOWED_SCALAR_FIELDS[key]
    if type(value) not in allowed_types:
        return _REDACTED
    if isinstance(value, str):
        if _SENSITIVE_VALUE.search(value) or not _SAFE_ENUM_VALUE.fullmatch(value):
            return _REDACTED
    if isinstance(value, float) and not isfinite(value):
        return _REDACTED
    return value


def _sanitize_mapping(value: Any) -> dict[str, Any] | str:
    if not isinstance(value, Mapping):
        return _REDACTED
    if any(not isinstance(key, str) for key in value):
        return _REDACTED
    return {key: _sanitize_field(key, item) for key, item in value.items()}


def _sanitize_sequence(value: Any) -> list[Any] | str:
    if (
        isinstance(value, (str, bytes, bytearray, memoryview, set, frozenset))
        or not isinstance(value, Sequence)
        or len(value) > 50
    ):
        return _REDACTED
    return [
        _sanitize_mapping(item) if isinstance(item, Mapping) else _REDACTED
        for item in value
    ]


def _sanitize_field(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return _REDACTED
    if key in _ALLOWED_SCALAR_FIELDS:
        return _sanitize_scalar(key, value)
    if key in _ALLOWED_MAPPING_FIELDS:
        return _sanitize_mapping(value)
    if key in _ALLOWED_SEQUENCE_FIELDS:
        return _sanitize_sequence(value)
    return _REDACTED


def sanitize_audit_details(value: Any) -> dict[str, Any]:
    """Keep only allowlisted, low-risk metadata; redact every unknown field."""
    sanitized = _sanitize_mapping(value)
    return sanitized if isinstance(sanitized, dict) else {}


class AuditRepository:
    """Write global audit metadata without manager business payloads."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def record(
        self,
        *,
        actor_user_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        result: str,
        request_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> AuditRecordRow:
        row = AuditRecordRow(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            request_id=request_id,
            details=sanitize_audit_details(details or {}),
        )
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return row
