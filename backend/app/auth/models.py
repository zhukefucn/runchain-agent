from dataclasses import dataclass

from app.db.models import Role


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    role: Role
    tenant_id: str
