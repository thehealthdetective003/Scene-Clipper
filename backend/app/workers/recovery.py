"""Startup recovery for abandoned work (spec 5.5, 11).

On worker startup, any nonterminal job, export, or upload verification whose
lease has expired is requeued from its last incomplete stage. Because each
stage is idempotent and checkpoints are committed before acknowledgement, a
resumed run neither duplicates candidates nor republishes partial output.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Export, Job, Upload
from app.services import jobs as job_service
from app.workers import leases
from app.workers.queue import (
    enqueue_analysis,
    enqueue_export,
    enqueue_job_cleanup,
    enqueue_provider_file_cleanup,
    enqueue_upload_verification,
    enqueue_url_download,
)

logger = get_logger("app.workers.recovery")


def requeue_abandoned_work() -> dict[str, int]:  # noqa: PLR0912 - one sweep, five resources
    counts = {"jobs": 0, "exports": 0, "uploads": 0, "downloads": 0, "cleanups": 0}

    with session_scope() as db:
        leases.purge_expired(db)

        # --- Remote video downloads interrupted before publication --------
        for upload in db.execute(
            select(Upload).where(Upload.state == "downloading", Upload.deleted_at.is_(None))
        ).scalars():
            if leases.is_held_by_other(db, leases.lease_key("download", upload.id), "recovery"):
                continue
            if enqueue_url_download(upload.id):
                counts["downloads"] += 1

        # --- Uploads stuck in verification --------------------------------
        for upload in db.execute(
            select(Upload).where(Upload.state == "verifying", Upload.deleted_at.is_(None))
        ).scalars():
            if leases.is_held_by_other(db, leases.lease_key("upload", upload.id), "recovery"):
                continue
            if enqueue_upload_verification(upload.id):
                counts["uploads"] += 1

        # --- Analysis jobs ------------------------------------------------
        for job in db.execute(
            select(Job).where(
                Job.state.in_(tuple(job_service.ANALYSIS_ACTIVE_STATES)),
                Job.deleted_at.is_(None),
            )
        ).scalars():
            if leases.is_held_by_other(db, leases.lease_key("analysis", job.id), "recovery"):
                continue
            if enqueue_analysis(job.id):
                counts["jobs"] += 1

        # --- Exports ------------------------------------------------------
        for export in db.execute(
            select(Export).where(Export.state.in_(("queued", "exporting")))
        ).scalars():
            if leases.is_held_by_other(db, leases.lease_key("export", export.id), "recovery"):
                continue
            if enqueue_export(export.id):
                counts["exports"] += 1

        # --- Tombstoned jobs whose cleanup never finished ------------------
        for job in db.execute(
            select(Job).where(
                Job.deleted_at.is_not(None), Job.cleanup_completed_at.is_(None)
            )
        ).scalars():
            if enqueue_job_cleanup(job.id):
                counts["cleanups"] += 1

    # Remote provider files must be deleted even across crashes (spec 7.3).
    enqueue_provider_file_cleanup()

    logger.info("recovery sweep complete", extra={"context": counts})
    return counts
