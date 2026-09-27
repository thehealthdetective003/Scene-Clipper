"""Worker entry point: ``python -m app.workers.main``.

On startup the worker validates configuration, runs the recovery sweep so
abandoned work is requeued from its last incomplete stage, then serves the
queues in priority order.
"""

from __future__ import annotations

import sys

from rq.worker_pool import WorkerPool

from app.config import ConfigurationError, get_settings
from app.logging_setup import configure_logging, get_logger
from app.util.redis_client import get_redis
from app.workers.queue import (
    ANALYSIS_QUEUE,
    DOWNLOAD_QUEUE,
    EXPORT_QUEUE,
    MAINTENANCE_QUEUE,
    UPLOAD_QUEUE,
)
from app.workers.recovery import requeue_abandoned_work

logger = get_logger("app.workers.main")

#: Upload verification first: it gates every downstream stage and is cheap.
QUEUE_PRIORITY = (
    UPLOAD_QUEUE,
    DOWNLOAD_QUEUE,
    ANALYSIS_QUEUE,
    EXPORT_QUEUE,
    MAINTENANCE_QUEUE,
)


def main() -> int:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    configure_logging(settings.log_level)
    settings.ensure_directories()

    try:
        requeue_abandoned_work()
    except Exception:  # noqa: BLE001 - never block startup on recovery
        logger.exception("recovery sweep failed")

    connection = get_redis()
    pool = WorkerPool(
        QUEUE_PRIORITY,
        connection=connection,
        num_workers=settings.worker_processes,
    )
    logger.info(
        "worker pool starting",
        extra={
            "context": {
                "queues": list(QUEUE_PRIORITY),
                "processes": settings.worker_processes,
            }
        },
    )
    # WorkerPool uses independent RQ workers, each with scheduling enabled, so
    # uploads/downloads and source analyses can advance concurrently.
    pool.start(burst=False, logging_level=settings.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
