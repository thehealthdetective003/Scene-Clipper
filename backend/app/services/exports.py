"""Export lifecycle (spec 5.7, 8.5).

Only one export may run per job. The first export moves the job from
``review-ready`` to ``exporting``; success moves it to ``complete`` while
failure or cancellation returns it to ``review-ready``. A later export from a
completed job temporarily moves it to ``exporting`` and returns it to
``complete`` however it ends. Prior successful exports always stay downloadable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import conflict, invalid_job_state, not_found, validation_error
from app.models import Export, ExportFile, Job, utcnow
from app.services import events, jobs, review, storage

ACTIVE_STATES = ("queued", "exporting")
RESOLUTIONS = ("original", "max1080p", "max720p")

#: ZIP folder name for each requested preset (spec 5.7).
FOLDER_NAMES = {"original": "original", "max1080p": "1080p", "max720p": "720p"}


def create_export(
    db: DbSession,
    job: Job,
    *,
    review_revision: int,
    resolutions: list[str],
    include_audio: bool,
) -> Export:
    if job.state not in ("review-ready", "complete"):
        raise invalid_job_state(
            "The job must be review-ready before export.", {"state": job.state}
        )
    if jobs.active_export(db, job.id) is not None:
        raise conflict("export_active", "An export is already running for this job.")

    unknown = [value for value in resolutions if value not in RESOLUTIONS]
    if unknown:
        raise validation_error(
            "invalid_resolution", "Unknown output resolution requested.", {"resolutions": unknown}
        )
    if review_revision != job.review_revision:
        raise review.stale_revision_error(db, job)

    clips = review.list_selected(db, job.id)
    if not clips:
        raise validation_error(
            "no_selected_clips", "Select at least one clip before exporting."
        )

    export = Export(
        job_id=job.id,
        state="queued",
        review_revision=review_revision,
        resolutions=list(dict.fromkeys(resolutions)),
        include_audio=include_audio,
        progress_percent=0.0,
        progress_message="Queued for export.",
    )
    db.add(export)
    db.flush()

    # The job's prior stable state is recoverable from its own history: a first
    # export comes from review-ready, a re-export from complete.
    jobs.transition(db, job, "exporting", message="Exporting clips.")
    return export


def get_export(db: DbSession, job_id: str, export_id: str) -> Export:
    export = db.get(Export, export_id)
    if export is None or export.job_id != job_id:
        raise not_found("export")
    return export


def previous_stable_state(export: Export, db: DbSession) -> str:
    """Where the job returns to when this export stops (spec 8.5)."""
    completed_before = db.execute(
        select(Export.id)
        .where(
            Export.job_id == export.job_id,
            Export.state == "complete",
            Export.id != export.id,
        )
        .limit(1)
    ).first()
    return "complete" if completed_before is not None else "review-ready"


def request_cancel(db: DbSession, export: Export) -> Export:
    if export.state not in ACTIVE_STATES:
        # Already terminal: cancellation is idempotent, not an error.
        return export
    export.cancel_requested = True
    export.updated_at = utcnow()
    db.flush()
    return export


def mark_running(db: DbSession, export: Export) -> None:
    export.state = "exporting"
    export.progress_message = "Encoding clips."
    export.updated_at = utcnow()
    db.flush()


def set_progress(db: DbSession, export: Export, percent: float, message: str) -> None:
    export.progress_percent = max(export.progress_percent, min(100.0, percent))
    export.progress_message = message
    export.updated_at = utcnow()
    db.flush()


def complete(
    db: DbSession,
    export: Export,
    job: Job,
    *,
    zip_relative_path: str,
    zip_size_bytes: int,
    zip_sha256: str,
) -> None:
    export.state = "complete"
    export.zip_relative_path = zip_relative_path
    export.zip_size_bytes = zip_size_bytes
    export.zip_sha256 = zip_sha256
    export.manifest_available = True
    export.progress_percent = 100.0
    export.progress_message = "Export complete."
    export.completed_at = utcnow()
    export.cancel_requested = False
    export.updated_at = utcnow()
    db.flush()

    jobs.transition(db, job, "complete", message="Export complete.")
    events.append(
        db,
        job.id,
        events.EXPORT_READY,
        {"exportId": export.id, "state": "complete", "sizeBytes": zip_size_bytes},
    )


def fail(
    db: DbSession,
    export: Export,
    job: Job,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    """Fail only this attempt; prior successful exports remain intact."""
    export.state = "failed"
    export.error = {
        "phase": "exporting",
        "code": code,
        "message": message,
        "retryable": retryable,
        "occurredAt": utcnow().isoformat().replace("+00:00", "Z"),
    }
    export.progress_message = "Export failed."
    export.cancel_requested = False
    export.updated_at = utcnow()
    db.flush()

    jobs.transition(db, job, previous_stable_state(export, db), message="Export failed.")
    events.append(
        db, job.id, events.EXPORT_READY, {"exportId": export.id, "state": "failed", "code": code}
    )


def cancel(db: DbSession, export: Export, job: Job) -> None:
    export.state = "cancelled"
    export.progress_message = "Export cancelled."
    export.cancel_requested = False
    export.updated_at = utcnow()
    db.flush()

    jobs.transition(db, job, previous_stable_state(export, db), message="Export cancelled.")
    events.append(
        db, job.id, events.EXPORT_READY, {"exportId": export.id, "state": "cancelled"}
    )


def reset_for_retry(db: DbSession, export: Export, job: Job) -> Export:
    if export.state != "failed":
        raise invalid_job_state(
            "Only a failed export can be retried.", {"state": export.state}
        )
    if jobs.active_export(db, job.id) is not None:
        raise conflict("export_active", "An export is already running for this job.")

    export.state = "queued"
    export.error = None
    export.cancel_requested = False
    export.progress_percent = 0.0
    export.progress_message = "Queued for export."
    export.updated_at = utcnow()
    db.flush()

    jobs.transition(db, job, "exporting", message="Exporting clips.")
    return export


def record_file(
    db: DbSession,
    export: Export,
    *,
    candidate_id: str,
    serial: int,
    resolution: str,
    relative_path: str,
    width: int,
    height: int,
    size_bytes: int,
    sha256: str,
    start_us: int,
    end_us: int,
) -> ExportFile:
    """Ledger entry for one validated output file; a retry reuses it."""
    row = ExportFile(
        export_id=export.id,
        candidate_id=candidate_id,
        serial=serial,
        resolution=resolution,
        archive_path=f"{FOLDER_NAMES[resolution]}/{storage.serial_name(serial)}",
        relative_path=relative_path,
        width=width,
        height=height,
        size_bytes=size_bytes,
        sha256=sha256,
        start_us=start_us,
        end_us=end_us,
        duration_us=end_us - start_us,
        validated=True,
    )
    db.add(row)
    db.flush()
    return row


def list_files(db: DbSession, export_id: str) -> list[ExportFile]:
    """Every recorded file, ordered as an editor would expect to see them."""
    return list(
        db.execute(
            select(ExportFile)
            .where(ExportFile.export_id == export_id)
            .order_by(ExportFile.serial.asc(), ExportFile.resolution.asc())
        ).scalars()
    )


def get_file(db: DbSession, export_id: str, file_id: str) -> ExportFile | None:
    """Scoped by export, so a file id cannot be probed across exports."""
    row = db.get(ExportFile, file_id)
    if row is None or row.export_id != export_id:
        return None
    return row


def existing_files(db: DbSession, export_id: str) -> dict[tuple[str, int], ExportFile]:
    rows = db.execute(select(ExportFile).where(ExportFile.export_id == export_id)).scalars()
    return {(row.resolution, row.serial): row for row in rows}


def manifest_summary(db: DbSession, export: Export) -> dict[str, Any]:
    files = list(db.execute(select(ExportFile).where(ExportFile.export_id == export.id)).scalars())
    return {
        "clipCount": len({row.serial for row in files}),
        "fileCount": len(files),
        "resolutions": list(export.resolutions),
    }
