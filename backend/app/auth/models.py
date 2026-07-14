from dataclasses import dataclass

from app.db.models import Role


DEMO_TENANT_ID = "bank_demo"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    role: Role
    tenant_id: str
