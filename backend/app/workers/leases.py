"""Worker leases and heartbeats (spec 5.5).

A worker must hold a lease before touching a job or export, and must refresh it
while working. If a worker dies, its lease expires and the resource becomes
reclaimable; on startup, abandoned nonterminal work is requeued from the last
incomplete stage.

The lease is held in SQLite rather than Redis so it shares a transaction with
the state change it protects -- a worker cannot hold a lease that a concurrent
transaction has already reassigned.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import threading

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.models import WorkerLease, utcnow

#: A lease survives a slow media subprocess but not a dead worker.
DEFAULT_TTL_SECONDS = 120
HEARTBEAT_INTERVAL_SECONDS = 30


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def lease_key(kind: str, resource_id: str) -> str:
    return f"{kind}:{resource_id}"


def acquire(
    db: DbSession, key: str, owner: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> bool:
    """Take the lease if it is free, expired, or already ours."""
    now = utcnow()
    expires = now + dt.timedelta(seconds=ttl_seconds)

    existing = db.execute(
        select(WorkerLease).where(WorkerLease.lease_key == key)
    ).scalar_one_or_none()

    if existing is None:
        db.add(
            WorkerLease(
                lease_key=key,
                owner_id=owner,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=expires,
            )
        )
        try:
            db.flush()
        except IntegrityError:
            # Another worker inserted first.
            db.rollback()
            return False
        return True

    if existing.owner_id == owner or existing.expires_at <= now:
        existing.owner_id = owner
        existing.acquired_at = now
        existing.heartbeat_at = now
        existing.expires_at = expires
        db.flush()
        return True

    return False


def heartbeat(
    db: DbSession, key: str, owner: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> bool:
    """Extend our lease. Returns ``False`` when it has been taken over."""
    now = utcnow()
    lease = db.execute(
        select(WorkerLease).where(WorkerLease.lease_key == key)
    ).scalar_one_or_none()
    if lease is None or lease.owner_id != owner:
        return False
    lease.heartbeat_at = now
    lease.expires_at = now + dt.timedelta(seconds=ttl_seconds)
    db.flush()
    return True


def release(db: DbSession, key: str, owner: str) -> None:
    db.execute(
        delete(WorkerLease).where(WorkerLease.lease_key == key, WorkerLease.owner_id == owner)
    )
    db.flush()


def purge_expired(db: DbSession) -> int:
    result = db.execute(delete(WorkerLease).where(WorkerLease.expires_at <= utcnow()))
    return int(result.rowcount or 0)


def is_held_by_other(db: DbSession, key: str, owner: str) -> bool:
    lease = db.execute(
        select(WorkerLease).where(WorkerLease.lease_key == key)
    ).scalar_one_or_none()
    if lease is None:
        return False
    return lease.owner_id != owner and lease.expires_at > utcnow()
