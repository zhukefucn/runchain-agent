"""Persistence repositories with explicit authorization boundaries."""

from app.repositories.audit import AuditRepository
from app.repositories.manager import ManagerRepository

__all__ = ["AuditRepository", "ManagerRepository"]
