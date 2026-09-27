"""RQ queue definitions and enqueue helpers.

Queue payloads carry identifiers only. No decrypted key, no file content, and
no request body ever enters a job argument (spec 5.2: "MUST NOT place the
plaintext key in queue payloads").
"""

from __future__ import annotations

from typing import Any

from rq import Queue

from app.logging_setup import get_logger
from app.util.redis_client import get_redis

logger = get_logger("app.queue")

UPLOAD_QUEUE = "uploads"
DOWNLOAD_QUEUE = "downloads"
ANALYSIS_QUEUE = "analysis"
EXPORT_QUEUE = "exports"
MAINTENANCE_QUEUE = "maintenance"

ALL_QUEUES = (UPLOAD_QUEUE, DOWNLOAD_QUEUE, ANALYSIS_QUEUE, EXPORT_QUEUE, MAINTENANCE_QUEUE)

#: Generous ceilings; the worker enforces its own cooperative cancellation and
#: per-subprocess timeouts well before these fire.
_TIMEOUTS = {
    UPLOAD_QUEUE: 3600,
    DOWNLOAD_QUEUE: 21600,
    ANALYSIS_QUEUE: 21600,
    EXPORT_QUEUE: 21600,
    MAINTENANCE_QUEUE: 1800,
}


def get_queue(name: str) -> Queue:
    return Queue(name, connection=get_redis(), default_timeout=_TIMEOUTS.get(name, 3600))


def _enqueue(queue_name: str, func_path: str, job_id: str, **kwargs: Any) -> bool:
    """Enqueue by dotted path so the API never imports worker media modules."""
    try:
        get_queue(queue_name).enqueue(
            func_path,
            # RQ permits only letters, numbers, underscores and dashes in a job
            # id -- a colon separator is rejected outright.
            job_id=f"{queue_name}-{job_id}",
            # A re-enqueue of the same resource replaces the previous entry
            # rather than duplicating work.
            on_failure=None,
            result_ttl=3600,
            failure_ttl=86400,
            **kwargs,
        )
        return True
    except Exception:  # noqa: BLE001 - queue outage must not lose durable state
        logger.exception("failed to enqueue work", extra={"context": {"queue": queue_name}})
        return False


def enqueue_upload_verification(upload_id: str) -> bool:
    return _enqueue(
        UPLOAD_QUEUE, "app.workers.tasks.verify_upload", upload_id, args=(upload_id,)
    )


def enqueue_url_download(upload_id: str) -> bool:
    return _enqueue(
        DOWNLOAD_QUEUE, "app.workers.tasks.download_url_upload", upload_id, args=(upload_id,)
    )


def enqueue_analysis(job_id: str) -> bool:
    return _enqueue(ANALYSIS_QUEUE, "app.workers.tasks.run_analysis", job_id, args=(job_id,))


def enqueue_export(export_id: str) -> bool:
    return _enqueue(EXPORT_QUEUE, "app.workers.tasks.run_export", export_id, args=(export_id,))


def enqueue_job_cleanup(job_id: str) -> bool:
    return _enqueue(
        MAINTENANCE_QUEUE, "app.workers.tasks.cleanup_job", f"cleanup-{job_id}", args=(job_id,)
    )


def enqueue_provider_file_cleanup() -> bool:
    return _enqueue(
        MAINTENANCE_QUEUE, "app.workers.tasks.cleanup_provider_files", "provider-files", args=()
    )
