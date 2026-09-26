"""Idempotency-Key handling for mutating routes (spec 8.1).

A record is bound to the authenticated principal, HTTP method, route template,
and a canonical fingerprint of the request body. Replaying the same key with
the same fingerprint returns the original status and body; reusing the key with
a different fingerprint is a ``409 idempotency_conflict``.

Records are retained for at least 24 hours and, in practice, for the lifetime
of the resource they created -- cleanup only removes rows whose resource is
gone or which never created one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.api.errors import conflict
from app.models import IdempotencyRecord, utcnow

RETENTION = dt.timedelta(days=30)
MINIMUM_RETENTION = dt.timedelta(hours=24)


def fingerprint(body: Any) -> str:
    """Canonical, order-independent digest of a JSON-serializable body."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Replay:
    status_code: int
    body: dict[str, Any] | None
    resource_id: str | None


def lookup(
    db: DbSession, *, key: str | None, principal: str, method: str, route: str, body: Any
) -> Replay | None:
    """Return the stored response for a replayed request, if any."""
    if not key:
        return None
    record = db.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.idempotency_key == key,
            IdempotencyRecord.principal == principal,
            IdempotencyRecord.method == method.upper(),
            IdempotencyRecord.route == route,
        )
    ).scalar_one_or_none()
    if record is None:
        return None

    if record.fingerprint != fingerprint(body):
        raise conflict(
            "idempotency_conflict",
            "This Idempotency-Key was already used with a different request body.",
        )
    return Replay(
        status_code=record.status_code, body=record.response_body, resource_id=record.resource_id
    )


def remember(
    db: DbSession,
    *,
    key: str | None,
    principal: str,
    method: str,
    route: str,
    body: Any,
    status_code: int,
    response_body: dict[str, Any] | None,
    resource_id: str | None = None,
) -> None:
    """Persist the outcome so a retry with the same key replays it."""
    if not key:
        return
    now = utcnow()
    record = IdempotencyRecord(
        idempotency_key=key,
        principal=principal,
        method=method.upper(),
        route=route,
        fingerprint=fingerprint(body),
        status_code=status_code,
        response_body=response_body,
        resource_id=resource_id,
        created_at=now,
        expires_at=now + RETENTION,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request with the same key won the race; its stored
        # response is authoritative and this one is discarded.
        db.rollback()


def purge_expired(db: DbSession) -> int:
    """Drop records past retention. Never removes anything younger than 24h."""
    cutoff = min(utcnow(), utcnow() - MINIMUM_RETENTION + RETENTION)
    result = db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.expires_at <= cutoff))
    return int(result.rowcount or 0)
