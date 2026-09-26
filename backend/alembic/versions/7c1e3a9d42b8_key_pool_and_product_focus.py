"""gemini key pool and product-focus signals

Adds the ``gemini_keys`` failover pool and carries any credential already held
on the singleton ``settings`` row into it, so an existing deployment keeps
working across the upgrade without the operator re-entering a key.

The envelope is copied verbatim. That is only sound because the AAD binds the
ciphertext to the record id, and ``settings.id`` is preserved as the new key
row's id -- see the ``id=settings.id`` assignment below. Changing it would make
the ciphertext undecryptable.

Also adds the per-candidate product-focus columns (human presence, product
visibility and prominence, exclusion reason).

Revision ID: 7c1e3a9d42b8
Revises: 534a80b567ee
Create Date: 2026-09-14 00:00:00.000000
"""

from __future__ import annotations

import datetime as dt
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Custom column types (e.g. UtcDateTime) are rendered fully qualified.
import app.models

revision: str = "7c1e3a9d42b8"
down_revision: Union[str, None] = "534a80b567ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gemini_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("key_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("key_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_tag", sa.LargeBinary(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", app.models.UtcDateTime(), nullable=True),
        sa.Column("last_success_at", app.models.UtcDateTime(), nullable=True),
        sa.Column("cooldown_until", app.models.UtcDateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("requests_succeeded", sa.Integer(), nullable=False),
        sa.Column("requests_failed", sa.Integer(), nullable=False),
        sa.Column("created_at", app.models.UtcDateTime(), nullable=False),
        sa.Column("updated_at", app.models.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("position", name="uq_gemini_keys_position"),
    )

    _migrate_existing_key()

    with op.batch_alter_table("candidate_shots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("human_present", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("human_source", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("product_visible", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("product_prominence", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("excluded_reason", sa.String(length=32), nullable=True))


def _migrate_existing_key() -> None:
    """Copy the singleton key, if one is configured, into the pool at slot 1."""
    connection = op.get_bind()

    # Typed lightweight tables so the driver converts binary and datetime
    # columns the same way the ORM does.
    settings = sa.table(
        "settings",
        sa.column("id", sa.String),
        sa.column("key_version", sa.Integer),
        sa.column("key_nonce", sa.LargeBinary),
        sa.column("key_ciphertext", sa.LargeBinary),
        sa.column("key_tag", sa.LargeBinary),
        sa.column("key_updated_at", sa.DateTime),
    )
    keys = sa.table(
        "gemini_keys",
        sa.column("id", sa.String),
        sa.column("label", sa.String),
        sa.column("position", sa.Integer),
        sa.column("key_version", sa.Integer),
        sa.column("key_nonce", sa.LargeBinary),
        sa.column("key_ciphertext", sa.LargeBinary),
        sa.column("key_tag", sa.LargeBinary),
        sa.column("status", sa.String),
        sa.column("consecutive_failures", sa.Integer),
        sa.column("requests_succeeded", sa.Integer),
        sa.column("requests_failed", sa.Integer),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )

    row = connection.execute(
        sa.select(
            settings.c.id,
            settings.c.key_version,
            settings.c.key_nonce,
            settings.c.key_ciphertext,
            settings.c.key_tag,
            settings.c.key_updated_at,
        )
        .where(settings.c.key_ciphertext.is_not(None))
        .limit(1)
    ).first()
    if row is None:
        return

    # Columns are stored as naive UTC (see app.models.UtcDateTime).
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    connection.execute(
        sa.insert(keys).values(
            # Must equal settings.id: the envelope's AAD binds the ciphertext
            # to the record id, so a fresh UUID here would be undecryptable.
            id=row.id,
            label="Key 1",
            position=1,
            key_version=row.key_version,
            key_nonce=row.key_nonce,
            key_ciphertext=row.key_ciphertext,
            key_tag=row.key_tag,
            status="active",
            consecutive_failures=0,
            requests_succeeded=0,
            requests_failed=0,
            created_at=row.key_updated_at or now,
            updated_at=now,
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("candidate_shots", schema=None) as batch_op:
        batch_op.drop_column("excluded_reason")
        batch_op.drop_column("product_prominence")
        batch_op.drop_column("product_visible")
        batch_op.drop_column("human_source")
        batch_op.drop_column("human_present")

    # The settings row still holds the original envelope, so downgrading loses
    # only the keys added after the upgrade -- which have nowhere to live in
    # the old schema anyway.
    op.drop_table("gemini_keys")
