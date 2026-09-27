"""Alembic environment.

The database URL always comes from the validated application settings so that
migrations can never run against a different database than the application.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, event, pool

from alembic import context
from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
settings.ensure_directories()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most columns in place; batch mode rebuilds tables.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # PRAGMAs run at connect time, on the raw DBAPI connection. SQLite requires
    # foreign-key enforcement to be disabled while Alembic's batch mode
    # rebuilds referenced tables. Integrity is checked before this connection
    # is accepted, and every application connection enables enforcement again.
    # Issuing these through the SQLAlchemy connection would also open a
    # transaction before Alembic starts its own and could roll back the version
    # stamp after SQLite had already committed the DDL.
    @event.listens_for(connectable, "connect")
    def _set_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=OFF")
        finally:
            cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLAlchemy 2 never autocommits; without this the stamp is discarded.
        connection.commit()
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        connection.commit()
        if violations:
            raise RuntimeError(
                f"Database migration left {len(violations)} foreign-key violation(s)."
            )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
