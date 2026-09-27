"""multi-source jobs and optional automatic ranking

Revision ID: d6a9f102bc34
Revises: c4d7e8f901ab
Create Date: 2026-09-28 00:00:00.000000
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.models import UtcDateTime

revision: str = "d6a9f102bc34"
down_revision: str | None = "c4d7e8f901ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ranking_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )

    op.create_table(
        "job_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("upload_id", sa.String(length=36), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=48), nullable=True),
        sa.Column("source_label_style", sa.JSON(), nullable=True),
        sa.Column("content_prompt", sa.Text(), nullable=True),
        sa.Column("content_prompt_normalized", sa.Text(), nullable=True),
        sa.Column("video", sa.JSON(), nullable=True),
        sa.Column("checkpoint", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("detected_count", sa.Integer(), nullable=True),
        sa.Column("eligible_count", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["upload_id"], ["uploads.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "order_index", name="uq_job_sources_order"),
        sa.UniqueConstraint("job_id", "upload_id", name="uq_job_sources_upload"),
    )
    with op.batch_alter_table("job_sources", schema=None) as batch_op:
        batch_op.create_index("ix_job_sources_job", ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_job_sources_upload_id"), ["upload_id"], unique=False)

    with op.batch_alter_table("detected_shots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_id", sa.String(length=36), nullable=True))

    with op.batch_alter_table("candidate_shots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("source_name", sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column("source_file_name", sa.String(length=512), nullable=True))

    connection = op.get_bind()
    now = dt.datetime.now(dt.UTC)
    jobs = connection.execute(
        sa.text(
            """
            SELECT j.id, j.upload_id, j.source_sha256, j.source_name,
                   j.source_label_style, j.content_prompt,
                   j.content_prompt_normalized, j.video, j.checkpoint,
                   j.detected_count, j.eligible_count, u.file_name
              FROM jobs AS j
              JOIN uploads AS u ON u.id = j.upload_id
            """
        )
    ).mappings()
    for row in jobs:
        source_id = str(uuid.uuid4())
        connection.execute(
            sa.text(
                """
                INSERT INTO job_sources (
                    id, job_id, upload_id, order_index, source_sha256,
                    source_name, source_label_style, content_prompt,
                    content_prompt_normalized, video, checkpoint, state,
                    detected_count, eligible_count, created_at, updated_at
                ) VALUES (
                    :id, :job_id, :upload_id, 0, :source_sha256,
                    :source_name, :source_label_style, :content_prompt,
                    :content_prompt_normalized, :video, :checkpoint, :state,
                    :detected_count, :eligible_count, :created_at, :updated_at
                )
                """
            ),
            {
                "id": source_id,
                "job_id": row["id"],
                "upload_id": row["upload_id"],
                "source_sha256": row["source_sha256"],
                "source_name": row["source_name"],
                "source_label_style": row["source_label_style"],
                "content_prompt": row["content_prompt"],
                "content_prompt_normalized": row["content_prompt_normalized"],
                "video": row["video"],
                "checkpoint": row["checkpoint"],
                "state": "ready" if row["checkpoint"] == "ranked" else "queued",
                "detected_count": row["detected_count"],
                "eligible_count": row["eligible_count"],
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            sa.text("UPDATE detected_shots SET source_id = :source_id WHERE job_id = :job_id"),
            {"source_id": source_id, "job_id": row["id"]},
        )
        connection.execute(
            sa.text(
                """
                UPDATE candidate_shots
                   SET source_id = :source_id,
                       source_name = :source_name,
                       source_file_name = :file_name
                 WHERE job_id = :job_id
                """
            ),
            {
                "source_id": source_id,
                "source_name": row["source_name"],
                "file_name": row["file_name"],
                "job_id": row["id"],
            },
        )

    with op.batch_alter_table("detected_shots", schema=None) as batch_op:
        batch_op.drop_constraint("uq_detected_shots_job_number", type_="unique")
        batch_op.alter_column("source_id", existing_type=sa.String(length=36), nullable=False)
        batch_op.create_foreign_key(
            "fk_detected_shots_source_id", "job_sources", ["source_id"], ["id"]
        )
        batch_op.create_unique_constraint(
            "uq_detected_shots_source_number", ["source_id", "shot_number"]
        )
        batch_op.create_index(batch_op.f("ix_detected_shots_source_id"), ["source_id"])

    with op.batch_alter_table("candidate_shots", schema=None) as batch_op:
        batch_op.alter_column("source_id", existing_type=sa.String(length=36), nullable=False)
        batch_op.alter_column(
            "source_file_name", existing_type=sa.String(length=512), nullable=False
        )
        batch_op.create_foreign_key(
            "fk_candidate_shots_source_id", "job_sources", ["source_id"], ["id"]
        )
        batch_op.create_index(batch_op.f("ix_candidate_shots_source_id"), ["source_id"])


def downgrade() -> None:
    with op.batch_alter_table("candidate_shots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_candidate_shots_source_id"))
        batch_op.drop_constraint("fk_candidate_shots_source_id", type_="foreignkey")
        batch_op.drop_column("source_file_name")
        batch_op.drop_column("source_name")
        batch_op.drop_column("source_id")

    with op.batch_alter_table("detected_shots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_detected_shots_source_id"))
        batch_op.drop_constraint("uq_detected_shots_source_number", type_="unique")
        batch_op.drop_constraint("fk_detected_shots_source_id", type_="foreignkey")
        batch_op.create_unique_constraint(
            "uq_detected_shots_job_number", ["job_id", "shot_number"]
        )
        batch_op.drop_column("source_id")

    with op.batch_alter_table("job_sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_sources_upload_id"))
        batch_op.drop_index("ix_job_sources_job")
    op.drop_table("job_sources")

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("ranking_enabled")
