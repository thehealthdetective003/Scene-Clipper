"""Migration coverage for existing single-source installations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.config import get_settings, reset_settings_cache


def test_existing_job_and_candidates_are_attached_to_a_backfilled_source(
    tmp_path, monkeypatch
):
    migration_data = tmp_path / "migration-data"
    monkeypatch.setenv("DATA_DIR", str(migration_data))
    reset_settings_cache()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    upload_id = "10000000-0000-4000-8000-000000000001"
    job_id = "20000000-0000-4000-8000-000000000001"
    shot_id = "30000000-0000-4000-8000-000000000001"
    candidate_id = "40000000-0000-4000-8000-000000000001"
    now = "2026-09-28 00:00:00"

    try:
        command.upgrade(config, "c4d7e8f901ab")
        database = get_settings().data_dir / "app.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                INSERT INTO uploads (
                    id, file_name, declared_size_bytes, verified_offset_bytes,
                    chunk_size_bytes, state, source_kind, sha256, storage_ext,
                    reference_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    "legacy.mp4",
                    1024,
                    1024,
                    1024,
                    "ready",
                    "file",
                    "a" * 64,
                    "mp4",
                    1,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    id, upload_id, state, source_sha256, target_clip_count,
                    content_prompt, content_prompt_normalized, source_name,
                    source_label_style, use_gemini, gemini_request_cap,
                    detector_config_version, pipeline_version, feature_version,
                    video, progress_phase, progress_percent, progress_message,
                    checkpoint, eligible_count, detected_count, selected_count,
                    review_revision, warnings, cancel_requested, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    job_id,
                    upload_id,
                    "review-ready",
                    "a" * 64,
                    1,
                    "cars",
                    "cars",
                    "LEGACY SOURCE",
                    '{"fontPreset":"bebas-neue","fillColor":"#FFFFFF",'
                    '"outlineColor":"#000000","sizePercent":4.0}',
                    0,
                    0,
                    "1",
                    "1",
                    "1",
                    '{"durationUs":5000000,"width":320,"height":180,'
                    '"averageFrameRate":"25/1","hasAudio":false}',
                    "review-ready",
                    100.0,
                    "Ready",
                    "ranked",
                    1,
                    1,
                    1,
                    1,
                    "[]",
                    0,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO detected_shots (
                    id, job_id, shot_number, source_start_us, source_end_us,
                    safe_start_us, safe_end_us, usable_duration_us, eligible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (shot_id, job_id, 1, 0, 5_000_000, 0, 5_000_000, 5_000_000, 1),
            )
            connection.execute(
                """
                INSERT INTO candidate_shots (
                    id, job_id, detected_shot_id, shot_number, source_start_us,
                    source_end_us, safe_start_us, safe_end_us, usable_duration_us,
                    recommended_start_us, recommended_end_us, is_long_shot,
                    local_score, feature_version, rank, score, confidence, reason,
                    motion_ambiguous, scoring_source, cache_status,
                    prompt_relevance_evaluated
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    candidate_id,
                    job_id,
                    shot_id,
                    1,
                    0,
                    5_000_000,
                    0,
                    5_000_000,
                    5_000_000,
                    0,
                    5_000_000,
                    0,
                    50.0,
                    "1",
                    1,
                    50.0,
                    0.5,
                    "legacy",
                    0,
                    "local-fallback",
                    "none",
                    0,
                ),
            )

        command.upgrade(config, "head")

        with sqlite3.connect(database) as connection:
            source = connection.execute(
                """
                SELECT id, upload_id, source_name, state,
                       progress_phase, progress_percent, progress_message
                FROM job_sources WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            candidate = connection.execute(
                """
                SELECT source_id, source_name, source_file_name
                FROM candidate_shots WHERE id = ?
                """,
                (candidate_id,),
            ).fetchone()
            detected_source = connection.execute(
                "SELECT source_id FROM detected_shots WHERE id = ?", (shot_id,)
            ).fetchone()
            ranking_enabled = connection.execute(
                "SELECT ranking_enabled FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()

        assert source is not None
        assert source[1:] == (
            upload_id,
            "LEGACY SOURCE",
            "ready",
            "ready",
            100.0,
            "Analysis complete.",
        )
        assert candidate == (source[0], "LEGACY SOURCE", "legacy.mp4")
        assert detected_source == (source[0],)
        assert ranking_enabled == (1,)
    finally:
        reset_settings_cache()
