"""Worker entry point: ``python -m app.workers.main``.

On startup the worker validates configuration, runs the recovery sweep so
abandoned work is requeued from its last incomplete stage, then serves the
queues in priority order.
"""

from __future__ import annotations

import signal
import sys
from types import FrameType

from rq import Queue, Worker

from app.config import ConfigurationError, get_settings
from app.logging_setup import configure_logging, get_logger
from app.util.redis_client import get_redis
from app.workers.queue import ANALYSIS_QUEUE, EXPORT_QUEUE, MAINTENANCE_QUEUE, UPLOAD_QUEUE
from app.workers.recovery import requeue_abandoned_work

logger = get_logger("app.workers.main")

#: Upload verification first: it gates every downstream stage and is cheap.
QUEUE_PRIORITY = (UPLOAD_QUEUE, ANALYSIS_QUEUE, EXPORT_QUEUE, MAINTENANCE_QUEUE)


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
    queues = [Queue(name, connection=connection) for name in QUEUE_PRIORITY]
    worker = Worker(queues, connection=connection)

    def handle_term(signum: int, _frame: FrameType | None) -> None:
        # Ask RQ to stop after the current job so a media subprocess is
        # terminated by its own cancellation path rather than mid-write.
        logger.info("shutdown requested", extra={"context": {"signal": signum}})
        worker.request_stop(signum, _frame)

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    logger.info("worker starting", extra={"context": {"queues": list(QUEUE_PRIORITY)}})
    # Windows has no fork; burst-free scheduling still works with SimpleWorker
    # semantics, but the default fork-based worker is used on POSIX.
    worker.work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
