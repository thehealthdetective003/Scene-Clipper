"""RQ task entry points.

Every task takes a lease before touching a resource and refreshes it while
working, so an abandoned job can be reclaimed safely after a worker dies
(spec 5.5). Tasks receive identifiers only -- never a decrypted key or any
request payload (spec 5.2).
"""

from __future__ import annotations

import time
from typing import Callable

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Export, Job, ProviderFile, Upload, utcnow
from app.security.crypto import DecryptionError
from app.services import jobs as job_service
from app.services import settings_service, storage, uploads as upload_service
from app.workers import leases

logger = get_logger("app.workers.tasks")


class _Heartbeat:
    """Refreshes a lease at most once per interval; reports loss of ownership."""

    def __init__(self, key: str, owner: str) -> None:
        self._key = key
        self._owner = owner
        self._last = 0.0
        self.lost = False

    def __call__(self) -> None:
        now = time.monotonic()
        if now - self._last < leases.HEARTBEAT_INTERVAL_SECONDS:
            return
        self._last = now
        with session_scope() as db:
            if not leases.heartbeat(db, self._key, self._owner):
                self.lost = True


def _with_lease(kind: str, resource_id: str, work: Callable[[_Heartbeat], None]) -> None:
    owner = leases.worker_identity()
    key = leases.lease_key(kind, resource_id)

    with session_scope() as db:
        if not leases.acquire(db, key, owner):
            logger.info(
                "resource is leased by another worker",
                extra={"context": {"kind": kind, "resource": resource_id}},
            )
            return

    heartbeat = _Heartbeat(key, owner)
    try:
        work(heartbeat)
    finally:
        with session_scope() as db:
            leases.release(db, key, owner)


# --- Upload verification ---------------------------------------------------


def verify_upload(upload_id: str) -> None:
    """Compute the authoritative digest and validate the media (spec 5.3)."""

    def work(heartbeat: _Heartbeat) -> None:
        settings = get_settings()
        with session_scope() as db:
            upload = db.get(Upload, upload_id)
            if upload is None or upload.deleted_at is not None:
                return
            if upload.state == "ready":
                return
            if upload.state != "verifying":
                return
            path = upload_service.source_path(upload, settings)
            client_sha = upload.client_sha256

        heartbeat()
        digest = upload_service.compute_sha256(path)

        if client_sha and client_sha.lower() != digest:
            with session_scope() as db:
                upload = db.get(Upload, upload_id)
                if upload is not None:
                    # The server digest is authoritative and stays in protected
                    # job metadata; the client is told only that they disagree.
                    upload_service.fail_upload(
                        db,
                        upload,
                        "upload_hash_mismatch",
                        "The uploaded file does not match the checksum you provided.",
                    )
            return

        heartbeat()
        from app.media.probe import UnsupportedMediaError, verify_source

        try:
            info = verify_source(path, settings)
        except UnsupportedMediaError as exc:
            with session_scope() as db:
                upload = db.get(Upload, upload_id)
                if upload is not None:
                    upload_service.fail_upload(db, upload, exc.code, exc.message)
            return

        with session_scope() as db:
            upload = db.get(Upload, upload_id)
            if upload is not None:
                upload_service.mark_ready(db, upload, sha256=digest, probe=info.to_json())

    _with_lease("upload", upload_id, work)


# --- Analysis --------------------------------------------------------------


def run_analysis(job_id: str) -> None:
    from app.analysis.pipeline import run_analysis as execute

    def work(heartbeat: _Heartbeat) -> None:
        def should_cancel() -> bool:
            return heartbeat.lost

        execute(job_id, should_cancel=should_cancel, heartbeat=heartbeat)

    _with_lease("analysis", job_id, work)


# --- Export ----------------------------------------------------------------


def run_export(export_id: str) -> None:
    from app.exporting.runner import run_export as execute

    def work(heartbeat: _Heartbeat) -> None:
        def should_cancel() -> bool:
            return heartbeat.lost

        execute(export_id, should_cancel=should_cancel, heartbeat=heartbeat)

    _with_lease("export", export_id, work)


# --- Cleanup ---------------------------------------------------------------


def cleanup_job(job_id: str) -> None:
    """Remove a tombstoned job's derivatives (spec 7.3).

    Previews, contact sheets, local proxies, attempts, and exports go first.
    The source file and shared cache entries are reference-counted and are
    removed only when the final reference disappears.
    """

    def work(heartbeat: _Heartbeat) -> None:
        settings = get_settings()
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is None or job.deleted_at is None:
                return
            upload_id = job.upload_id

        storage.remove_tree(storage.job_dir(job_id), settings)
        storage.remove_tree(f"exports/{job_id}", settings)
        heartbeat()

        cleanup_provider_files(job_id=job_id)

        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            upload = db.get(Upload, upload_id)
            if upload is not None and upload.reference_count <= 0:
                storage.remove_tree(upload.relative_dir, settings)
                upload.deleted_at = utcnow()
            job.cleanup_completed_at = utcnow()
            db.flush()

    _with_lease("cleanup", job_id, work)


def cleanup_provider_files(job_id: str | None = None) -> None:
    """Retry remote deletion until the provider confirms it or the file expires."""
    settings = get_settings()

    with session_scope() as db:
        query = select(ProviderFile).where(ProviderFile.cleanup_status == "pending")
        if job_id is not None:
            query = query.where(ProviderFile.job_id == job_id)
        pending = [
            (row.id, row.provider_name)
            for row in db.execute(query.limit(100)).scalars()
            if row.provider_name
        ]
        # Deletion is best effort and cheap, so any readable key will do --
        # provider file handles are not scoped to the key that uploaded them
        # within one project, and a failed delete is retried on the next pass.
        api_key = None
        if pending:
            for candidate in settings_service.available_keys(db):
                try:
                    api_key = settings_service.reveal_key(db, settings, candidate)
                    break
                except DecryptionError:
                    continue

    if not pending or not api_key:
        return

    from app.providers.base import ProviderError
    from app.providers.gemini import get_provider

    provider = get_provider(settings.gemini_timeout_seconds)
    for record_id, provider_name in pending:
        try:
            provider.delete_file(api_key=api_key, name=provider_name)
            status, deleted_at = "deleted", utcnow()
        except ProviderError:
            status, deleted_at = "pending", None

        with session_scope() as db:
            row = db.get(ProviderFile, record_id)
            if row is not None:
                row.cleanup_status = status
                row.deleted_at = deleted_at
                row.cleanup_attempts += 1
                # Give up only once the provider's own retention has expired.
                if row.cleanup_attempts >= 20:
                    row.cleanup_status = "abandoned"
                db.flush()


def purge_expired_records() -> None:
    """Periodic housekeeping for sessions, leases, and idempotency records."""
    from app.security.sessions import purge_expired_sessions
    from app.services.idempotency import purge_expired

    with session_scope() as db:
        sessions = purge_expired_sessions(db)
        records = purge_expired(db)
        expired_leases = leases.purge_expired(db)

    logger.info(
        "housekeeping complete",
        extra={
            "context": {
                "sessions": sessions,
                "idempotency": records,
                "leases": expired_leases,
            }
        },
    )
