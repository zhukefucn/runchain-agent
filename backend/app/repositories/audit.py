from __future__ import annotations

import re
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


def _key_tokens(key: object) -> set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
    return {token for token in re.split(r"[^a-z0-9]+", text) if token}


def _is_sensitive_key(key: object) -> bool:
    tokens = _key_tokens(key)
    return bool(tokens & _SENSITIVE_TOKENS) or "apikey" in "".join(tokens)


def sanitize_audit_details(value: Any) -> Any:
    """Recursively redact secret or business-content fields before persistence."""
    if isinstance(value, dict):
        return {
            str(key): _REDACTED if _is_sensitive_key(key) else sanitize_audit_details(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_audit_details(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _REDACTED
    if isinstance(value, str):
        return _REDACTED if _SENSITIVE_VALUE.search(value) else value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


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
