"""durable per-source analysis progress

Revision ID: e2f4a6b8c0d1
Revises: d6a9f102bc34
Create Date: 2026-09-28 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f4a6b8c0d1"
down_revision: str | None = "d6a9f102bc34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_sources", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "progress_phase",
                sa.String(length=32),
                nullable=False,
                server_default="queued",
            )
        )
        batch_op.add_column(
            sa.Column(
                "progress_percent",
                sa.Float(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "progress_message",
                sa.String(length=255),
                nullable=False,
                server_default="Waiting to start.",
            )
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE job_sources
               SET progress_phase = CASE
                       WHEN checkpoint = 'ranked' OR state = 'ready' THEN 'ready'
                       WHEN checkpoint = 'prepared' THEN 'ranking'
                       WHEN checkpoint = 'detected' THEN 'measuring'
                       WHEN checkpoint = 'probed' THEN 'detecting'
                       ELSE 'queued'
                   END,
                   progress_percent = CASE
                       WHEN checkpoint = 'ranked' OR state = 'ready' THEN 100
                       WHEN checkpoint = 'prepared' THEN 80
                       WHEN checkpoint = 'detected' THEN 60
                       WHEN checkpoint = 'probed' THEN 10
                       ELSE 0
                   END,
                   progress_message = CASE
                       WHEN checkpoint = 'ranked' OR state = 'ready' THEN 'Analysis complete.'
                       WHEN checkpoint = 'prepared' THEN 'Preparing candidates for review.'
                       WHEN checkpoint = 'detected' THEN 'Measuring candidate quality.'
                       WHEN checkpoint = 'probed' THEN 'Detecting shot boundaries.'
                       ELSE 'Waiting to start.'
                   END
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("job_sources", schema=None) as batch_op:
        batch_op.drop_column("progress_message")
        batch_op.drop_column("progress_percent")
        batch_op.drop_column("progress_phase")
