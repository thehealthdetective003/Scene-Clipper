"""configurable per-job source label typography

Revision ID: b8e3c5a1f2d4
Revises: 7c1e3a9d42b8
Create Date: 2026-09-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8e3c5a1f2d4"
down_revision: str | None = "7c1e3a9d42b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Server defaults backfill the singleton row on existing deployments.  New
    # rows also have matching ORM defaults, so a fresh database and an upgraded
    # database behave identically.
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_label_font_preset",
                sa.String(length=32),
                nullable=False,
                server_default="bebas-neue",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_label_fill_color",
                sa.String(length=7),
                nullable=False,
                server_default="#FFFFFF",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_label_outline_color",
                sa.String(length=7),
                nullable=False,
                server_default="#000000",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_label_size_percent",
                sa.Float(),
                nullable=False,
                server_default="4.0",
            )
        )

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_name", sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column("source_label_style", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("source_label_style")
        batch_op.drop_column("source_name")

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("source_label_size_percent")
        batch_op.drop_column("source_label_outline_color")
        batch_op.drop_column("source_label_fill_color")
        batch_op.drop_column("source_label_font_preset")

