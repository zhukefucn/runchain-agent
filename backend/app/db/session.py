from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.base import Base


def build_async_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url)

    if make_url(database_url).get_backend_name() == "sqlite":
        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


def _ensure_sqlite_directory(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


_database_url = get_settings().database_url
_ensure_sqlite_directory(_database_url)
engine = build_async_engine(_database_url)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(target_engine: AsyncEngine | None = None) -> None:
    schema_engine = target_engine or engine
    async with schema_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
