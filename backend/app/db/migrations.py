from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url


def _sync_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername == "sqlite+aiosqlite":
        url = url.set(drivername="sqlite")
    return url.render_as_string(hide_password=False)


def _config(database_url: str) -> Config:
    url = make_url(database_url)
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "migrations"))
    config.set_main_option("sqlalchemy.url", _sync_url(database_url).replace("%", "%%"))
    return config


def upgrade_database_url(database_url: str) -> None:
    """Upgrade an existing Phase-1 SQLite database to the current schema."""
    if make_url(database_url).database == ":memory:":
        return
    command.upgrade(_config(database_url), "head")


def downgrade_database_url(database_url: str) -> None:
    """Restore the pre-reception Task-10 schema (development/test helper)."""
    if make_url(database_url).database == ":memory:":
        return
    command.downgrade(_config(database_url), "base")


__all__ = ["downgrade_database_url", "upgrade_database_url"]
