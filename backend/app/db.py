"""SQLite engine, WAL configuration, and session helpers.

SQLite is the required store (spec 7.1) and runs in WAL mode so the API can
read while a worker writes. Every connection sets a busy timeout and enforces
foreign keys, which SQLite leaves off by default.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

#: Wait rather than fail immediately when a writer holds the database lock.
BUSY_TIMEOUT_MS = 15_000


def _configure_connection(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
    if not isinstance(dbapi_connection, sqlite3.Connection):  # pragma: no cover
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # NORMAL is durable under WAL for everything except an OS-level crash,
        # and avoids an fsync on every transaction during chunked uploads.
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def build_engine(settings: Settings) -> Engine:
    settings.ensure_directories()
    engine = create_engine(
        settings.database_url,
        future=True,
        echo=False,
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000},
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _configure_connection)
    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine(get_settings())
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped transactional session."""
    with session_scope() as session:
        yield session


def reset_engine(engine: Engine | None = None) -> None:
    """Test hook: rebind the module-level engine and session factory."""
    global _engine, _session_factory
    if _engine is not None and engine is not _engine:
        _engine.dispose()
    _engine = engine
    _session_factory = None
