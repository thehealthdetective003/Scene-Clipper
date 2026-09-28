"""suggest source names from URL channel metadata

Revision ID: f3a5b7c9d1e2
Revises: e2f4a6b8c0d1
Create Date: 2026-09-28 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a5b7c9d1e2"
down_revision: str | None = "e2f4a6b8c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("uploads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("suggested_source_name", sa.String(length=48), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("uploads", schema=None) as batch_op:
        batch_op.drop_column("suggested_source_name")
