"""add yt-dlp URL video imports

Revision ID: c4d7e8f901ab
Revises: b8e3c5a1f2d4
Create Date: 2026-09-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d7e8f901ab"
down_revision: str | None = "b8e3c5a1f2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("uploads", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_kind", sa.String(length=16), nullable=False, server_default="file"
            )
        )
        batch_op.add_column(sa.Column("source_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("uploads", schema=None) as batch_op:
        batch_op.drop_column("source_url")
        batch_op.drop_column("source_kind")
