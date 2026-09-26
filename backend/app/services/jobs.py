"""Job lifecycle and state machine (spec 5.4, 5.5).

State machine::

    uploaded -> probing -> detecting -> ranking -> review-ready -> exporting -> complete
                    |          |           |
                    +----------+-----------+--> failed / cancelled
    complete -> exporting -> complete   (later export)

Progress is monotonic within a stage, stage checkpoints are committed before a
stage is acknowledged, and a retry resumes from the last committed checkpoint
rather than repeating completed work.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import conflict, invalid_job_state, not_found, validation_error
from app.config import Settings as AppSettings
from app.models import AnalysisUsage, Export, Job, Upload, utcnow
from app.services import events, settings_service
from app.source_labels import normalize_source_name
from app.versions import ANALYSIS_PIPELINE_VERSION, FEATURE_ALGORITHM_VERSION

#: Ordered analysis stages. A retry resumes at the first stage after the last
#: committed checkpoint.
STAGE_ORDER = ("probing", "detecting", "ranking")

CHECKPOINT_PROBED = "probed"
CHECKPOINT_DETECTED = "detected"
CHECKPOINT_PREPARED = "prepared"
CHECKPOINT_RANKED = "ranked"

CHECKPOINT_ORDER = (
    CHECKPOINT_PROBED,
    CHECKPOINT_DETECTED,
    CHECKPOINT_PREPARED,
    CHECKPOINT_RANKED,
)

TERMINAL_STATES = {"complete", "failed", "cancelled"}
ANALYSIS_ACTIVE_STATES = {"probing", "detecting", "ranking"}
RESUMABLE_STATES = {"uploaded", "probing", "detecting", "ranking"}

#: Percentage floor for each stage, so progress is monotonic across stages.
_STAGE_FLOOR = {
    "uploaded": 0.0,
    "probing": 2.0,
    "detecting": 10.0,
    "ranking": 55.0,
    "review-ready": 100.0,
    "exporting": 100.0,
    "complete": 100.0,
}


@dataclass(frozen=True, slots=True)
class JobPage:
    items: list[tuple[Job, str, Export | None]]
    next_cursor: str | None


# --- Prompt normalization --------------------------------------------------


def normalize_prompt(prompt: str | None) -> str | None:
    """Canonical form used for cache keys and provider requests (spec 6.10).

    Normalization must be stable: two prompts that differ only in whitespace or
    Unicode composition must produce the same cache key.
    """
    if prompt is None:
        return None
    text = unicodedata.normalize("NFKC", prompt)
    text = "".join(ch for ch in text if ch.isprintable() or ch in " \t\n")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# --- Creation --------------------------------------------------------------


def create_job(
    db: DbSession,
    app_settings: AppSettings,
    *,
    upload_id: str,
    target_clip_count: int,
    content_prompt: str | None,
    source_name: str | None,
    use_gemini: bool | None,
) -> Job:
    """Snapshot configuration so later settings changes cannot alter this run."""
    upload = db.get(Upload, upload_id)
    if upload is None or upload.deleted_at is not None:
        raise not_found("upload")
    if upload.state != "ready":
        raise conflict(
            "upload_not_ready",
            "The upload must finish verifying before a job can start.",
            {"uploadState": upload.state},
        )
    if not upload.sha256:
        raise conflict("upload_not_ready", "The upload has no verified checksum yet.")
    if not 1 <= target_clip_count <= 100:
        raise validation_error(
            "invalid_target_clip_count", "targetClipCount must be between 1 and 100."
        )

    settings_row = settings_service.get_or_create(db, app_settings)
    key_configured = settings_service.any_key_configured(db)

    # useGemini defaults to true when a key is configured, false otherwise.
    resolved_use_gemini = key_configured if use_gemini is None else bool(use_gemini)
    if resolved_use_gemini and not key_configured:
        # Not an error: the job runs locally and reports the fallback reason.
        resolved_use_gemini = False

    normalized = normalize_prompt(content_prompt)
    normalized_source_name = normalize_source_name(source_name)
    job = Job(
        upload_id=upload.id,
        state="uploaded",
        source_sha256=upload.sha256,
        target_clip_count=target_clip_count,
        content_prompt=content_prompt.strip() if content_prompt else None,
        content_prompt_normalized=normalized,
        source_name=normalized_source_name,
        source_label_style=(
            settings_service.source_label_style(settings_row)
            if normalized_source_name is not None
            else None
        ),
        use_gemini=resolved_use_gemini,
        gemini_model=settings_row.gemini_model if resolved_use_gemini else None,
        gemini_request_cap=settings_row.gemini_request_cap if resolved_use_gemini else 0,
        detector_config_version=app_settings.detector_config_version,
        pipeline_version=ANALYSIS_PIPELINE_VERSION,
        feature_version=FEATURE_ALGORITHM_VERSION,
        progress_phase="uploaded",
        progress_percent=0.0,
        progress_message="Queued for analysis.",
        warnings=[],
    )
    db.add(job)
    db.flush()

    db.add(
        AnalysisUsage(
            job_id=job.id,
            model=job.gemini_model,
            request_cap=job.gemini_request_cap,
        )
    )
    # A ready upload may back multiple jobs; reference-count its source file.
    upload.reference_count += 1
    db.flush()

    if not key_configured and (use_gemini is True):
        add_warning(job, "gemini_key_not_configured")

    events.append(db, job.id, events.JOB_UPDATED, {"state": job.state, "percent": 0.0})
    return job


# --- Lookup ----------------------------------------------------------------


def get_job(db: DbSession, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.deleted_at is not None:
        raise not_found("job")
    return job


def latest_export(db: DbSession, job_id: str) -> Export | None:
    return db.execute(
        select(Export).where(Export.job_id == job_id).order_by(desc(Export.created_at)).limit(1)
    ).scalar_one_or_none()


def active_export(db: DbSession, job_id: str) -> Export | None:
    return db.execute(
        select(Export)
        .where(Export.job_id == job_id, Export.state.in_(("queued", "exporting")))
        .order_by(desc(Export.created_at))
        .limit(1)
    ).scalar_one_or_none()


def usage_for(db: DbSession, job_id: str) -> AnalysisUsage | None:
    return db.get(AnalysisUsage, job_id)


def encode_cursor(job_id: str) -> str:
    return base64.urlsafe_b64encode(job_id.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise validation_error("invalid_cursor", "The pagination cursor is not valid.") from exc


def list_jobs(db: DbSession, *, cursor: str | None, limit: int) -> JobPage:
    """Newest first. UUIDv7 ids sort by creation time, so the id is the cursor."""
    limit = max(1, min(limit, 100))
    after_id = decode_cursor(cursor)

    query = select(Job).where(Job.deleted_at.is_(None)).order_by(desc(Job.id)).limit(limit + 1)
    if after_id:
        query = query.where(Job.id < after_id)
    rows = list(db.execute(query).scalars())

    has_more = len(rows) > limit
    rows = rows[:limit]

    items: list[tuple[Job, str, Export | None]] = []
    for job in rows:
        upload = db.get(Upload, job.upload_id)
        items.append((job, upload.file_name if upload else "(deleted source)", latest_export(db, job.id)))

    return JobPage(items=items, next_cursor=encode_cursor(rows[-1].id) if has_more and rows else None)


# --- State transitions -----------------------------------------------------


def add_warning(job: Job, warning: str) -> None:
    existing = list(job.warnings or [])
    if warning not in existing:
        existing.append(warning)
        job.warnings = existing


def set_progress(
    db: DbSession,
    job: Job,
    *,
    phase: str,
    percent: float,
    message: str,
    emit: bool = True,
) -> None:
    """Advance progress. Percentage is clamped so it never moves backwards."""
    floor = _STAGE_FLOOR.get(phase, 0.0)
    ceiling = 100.0
    candidate = max(floor, min(ceiling, percent))
    if phase == job.progress_phase:
        candidate = max(candidate, job.progress_percent)

    job.progress_phase = phase
    job.progress_percent = candidate
    job.progress_message = message
    job.updated_at = utcnow()
    db.flush()

    if emit:
        events.append(
            db,
            job.id,
            events.JOB_UPDATED,
            {"state": job.state, "phase": phase, "percent": round(candidate, 2), "message": message},
        )


def transition(
    db: DbSession, job: Job, new_state: str, *, message: str | None = None, emit: bool = True
) -> None:
    job.state = new_state
    job.progress_phase = new_state if new_state in _STAGE_FLOOR else job.progress_phase
    if new_state in _STAGE_FLOOR:
        job.progress_percent = max(job.progress_percent, _STAGE_FLOOR[new_state])
    if message:
        job.progress_message = message
    job.updated_at = utcnow()
    db.flush()

    if emit:
        events.append(
            db,
            job.id,
            events.JOB_UPDATED,
            {
                "state": new_state,
                "phase": job.progress_phase,
                "percent": round(job.progress_percent, 2),
                "message": job.progress_message,
            },
        )


def commit_checkpoint(db: DbSession, job: Job, checkpoint: str) -> None:
    """Record a completed stage before acknowledging it (spec 5.5)."""
    if checkpoint not in CHECKPOINT_ORDER:
        raise ValueError(f"Unknown checkpoint {checkpoint!r}.")
    current_index = CHECKPOINT_ORDER.index(job.checkpoint) if job.checkpoint in CHECKPOINT_ORDER else -1
    if CHECKPOINT_ORDER.index(checkpoint) > current_index:
        job.checkpoint = checkpoint
        job.updated_at = utcnow()
        db.flush()


def has_checkpoint(job: Job, checkpoint: str) -> bool:
    if job.checkpoint not in CHECKPOINT_ORDER:
        return False
    return CHECKPOINT_ORDER.index(job.checkpoint) >= CHECKPOINT_ORDER.index(checkpoint)


def fail(
    db: DbSession,
    job: Job,
    *,
    phase: str,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    """Terminate with a sanitized, user-facing error. Local work is retained."""
    job.state = "failed"
    job.error = {
        "phase": phase,
        "code": code,
        "message": message,
        "retryable": retryable,
        "occurredAt": utcnow().isoformat().replace("+00:00", "Z"),
    }
    job.updated_at = utcnow()
    db.flush()
    events.append(db, job.id, events.JOB_FAILED, {"code": code, "message": message, "retryable": retryable})


def request_cancel(db: DbSession, job: Job) -> Job:
    """Idempotently request cancellation (spec 8.4)."""
    if job.state == "exporting":
        raise conflict(
            "export_active",
            "An export is running. Cancel the export instead.",
        )
    if job.state in TERMINAL_STATES or job.state == "review-ready":
        # Nothing in flight; cancellation is a no-op rather than an error.
        return job
    job.cancel_requested = True
    job.updated_at = utcnow()
    db.flush()
    events.append(db, job.id, events.JOB_UPDATED, {"state": job.state, "cancelRequested": True})
    return job


def mark_cancelled(db: DbSession, job: Job) -> None:
    job.state = "cancelled"
    job.cancel_requested = False
    job.progress_message = "Cancelled."
    job.updated_at = utcnow()
    db.flush()
    events.append(db, job.id, events.JOB_UPDATED, {"state": "cancelled"})


def prepare_retry(db: DbSession, job: Job) -> Job:
    """Resume a failed or cancelled job from its last committed checkpoint."""
    if job.state not in ("failed", "cancelled"):
        raise invalid_job_state(
            "Only a failed or cancelled job can be retried.", {"state": job.state}
        )
    if job.error and not job.error.get("retryable", True):
        raise invalid_job_state(
            "This job failed for a reason that cannot be retried.",
            {"code": job.error.get("code")},
        )

    job.error = None
    job.cancel_requested = False
    resume_state = _resume_state(job)
    job.state = resume_state
    job.progress_phase = resume_state
    job.progress_message = "Resuming from the last checkpoint."
    job.updated_at = utcnow()
    db.flush()
    events.append(db, job.id, events.JOB_UPDATED, {"state": resume_state, "resumed": True})
    return job


def _resume_state(job: Job) -> str:
    if job.checkpoint == CHECKPOINT_RANKED:
        return "ranking"
    if job.checkpoint in (CHECKPOINT_DETECTED, CHECKPOINT_PREPARED):
        return "ranking"
    if job.checkpoint == CHECKPOINT_PROBED:
        return "detecting"
    return "probing"


def set_partial_result(db: DbSession, job: Job, reason: str | None) -> None:
    job.partial_result_reason = reason
    job.updated_at = utcnow()
    db.flush()


def tombstone(db: DbSession, job: Job) -> None:
    """Hide the job from every API immediately; cleanup continues in the worker."""
    job.deleted_at = utcnow()
    job.cancel_requested = True
    job.updated_at = utcnow()
    upload = db.get(Upload, job.upload_id)
    if upload is not None and upload.reference_count > 0:
        upload.reference_count -= 1
    db.flush()


def snapshot(db: DbSession, job: Job) -> dict[str, Any]:
    """Compact job snapshot used to seed a reconnecting SSE client."""
    return {
        "id": job.id,
        "state": job.state,
        "phase": job.progress_phase,
        "percent": round(job.progress_percent, 2),
        "message": job.progress_message,
        "reviewRevision": job.review_revision,
        "selectedCount": job.selected_count,
        "eligibleCount": job.eligible_count,
        "warnings": list(job.warnings or []),
        "partialResultReason": job.partial_result_reason,
        "error": job.error,
    }
