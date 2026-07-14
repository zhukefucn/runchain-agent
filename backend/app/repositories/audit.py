from __future__ import annotations

from math import isfinite
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditRecordRow


# Fail-closed audit metadata schema. Add a field here only after establishing
# that it cannot contain manager business content or credentials.
_ALLOWED_ENUM_FIELDS: dict[str, frozenset[str]] = {
    "operation": frozenset(
        {
            "authorize",
            "connect",
            "create",
            "delete",
            "disconnect",
            "execute",
            "install",
            "invoke",
            "list",
            "login",
            "logout",
            "read",
            "revoke",
            "update",
        }
    ),
    "role": frozenset({"manager", "business_admin", "system_admin"}),
    "status": frozenset(
        {
            "active",
            "allowed",
            "approved",
            "cancelled",
            "completed",
            "denied",
            "disabled",
            "enabled",
            "failed",
            "failure",
            "inactive",
            "not_found",
            "pending",
            "rejected",
            "running",
            "success",
        }
    ),
    "transport": frozenset({"stdio", "http", "sse", "mock"}),
}
_ALLOWED_BOOL_FIELDS = frozenset({"enabled", "retryable"})
_ALLOWED_NUMERIC_FIELDS: dict[str, tuple[tuple[type, ...], float, float]] = {
    "count": ((int,), 0, 1_000_000_000),
    "duration_ms": ((int, float), 0, 86_400_000),
    "status_code": ((int,), 100, 599),
}
_METRIC_FIELDS = frozenset({"count", "duration_ms"})
_OPERATION_FIELDS = frozenset({"operation", "status", "count", "duration_ms"})
_ROOT_FIELDS = frozenset(_ALLOWED_ENUM_FIELDS).union(
    _ALLOWED_BOOL_FIELDS, _ALLOWED_NUMERIC_FIELDS
)


def _sanitize_fields(value: Any, allowed_fields: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict:
        return {}
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str or key not in allowed_fields:
            continue
        if key in _ALLOWED_ENUM_FIELDS:
            if type(item) is str and item in _ALLOWED_ENUM_FIELDS[key]:
                sanitized[key] = item
            continue
        if key in _ALLOWED_BOOL_FIELDS:
            if type(item) is bool:
                sanitized[key] = item
            continue
        if key in _ALLOWED_NUMERIC_FIELDS:
            types, minimum, maximum = _ALLOWED_NUMERIC_FIELDS[key]
            if type(item) in types and minimum <= item <= maximum:
                if type(item) is not float or isfinite(item):
                    sanitized[key] = item
    return sanitized


def _sanitize_operations(value: Any) -> list[dict[str, Any]]:
    if type(value) not in (list, tuple) or len(value) > 50:
        return []
    operations = [
        _sanitize_fields(item, _OPERATION_FIELDS)
        for item in value
        if type(item) is dict
    ]
    return [operation for operation in operations if operation]


def sanitize_audit_details(value: Any) -> dict[str, Any]:
    """Keep only fixed-schema, low-risk metadata; omit every unknown field."""
    if type(value) is not dict:
        return {}
    sanitized = _sanitize_fields(value, _ROOT_FIELDS)
    if "metrics" in value:
        metrics = _sanitize_fields(value["metrics"], _METRIC_FIELDS)
        if metrics:
            sanitized["metrics"] = metrics
    if "operations" in value:
        operations = _sanitize_operations(value["operations"])
        if operations:
            sanitized["operations"] = operations
    return sanitized


class AuditRepository:
    """Write global audit metadata without manager business payloads."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    def add_pending(
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
        """Add sanitized audit metadata to the caller's current transaction."""
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
        return row

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
        """Backward-compatible convenience method that owns its transaction."""
        row = self.add_pending(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            request_id=request_id,
            details=details,
        )
        await self._db.commit()
        await self._db.refresh(row)
        return row
