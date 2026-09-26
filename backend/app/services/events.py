"""Durable job events and the SSE stream (spec 8.4).

Every event is persisted with a per-job monotonically increasing sequence
before it is delivered, so a client that reconnects with ``Last-Event-ID``
resumes exactly after the last event it saw. Events are retained until the job
is deleted. A disconnected client never affects the worker (spec 5.5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.db import session_scope
from app.models import JobEvent, utcnow

JOB_UPDATED = "job.updated"
CANDIDATES_READY = "candidates.ready"
EXPORT_READY = "export.ready"
JOB_FAILED = "job.failed"
HEARTBEAT = "heartbeat"

#: How often the stream polls for newly committed events.
POLL_INTERVAL_SECONDS = 1.0
#: Keeps proxies and browsers from idling the connection out.
HEARTBEAT_INTERVAL_SECONDS = 15.0


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def append(
    db: DbSession, job_id: str, event_type: str, payload: dict[str, Any] | None = None
) -> JobEvent:
    """Persist one event, assigning the next sequence for this job."""
    next_sequence = (
        db.execute(
            select(func.coalesce(func.max(JobEvent.sequence), 0)).where(JobEvent.job_id == job_id)
        ).scalar_one()
        + 1
    )
    event = JobEvent(
        job_id=job_id,
        sequence=next_sequence,
        type=event_type,
        payload=payload or {},
        occurred_at=utcnow(),
    )
    db.add(event)
    db.flush()
    return event


def envelope(event: JobEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "type": event.type,
        "jobId": event.job_id,
        "occurredAt": _iso(event.occurred_at),
        "payload": event.payload or {},
    }


def latest_sequence(db: DbSession, job_id: str) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.max(JobEvent.sequence), 0)).where(JobEvent.job_id == job_id)
        ).scalar_one()
    )


def events_after(db: DbSession, job_id: str, after_sequence: int, limit: int = 200) -> list[JobEvent]:
    return list(
        db.execute(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.sequence > after_sequence)
            .order_by(JobEvent.sequence.asc())
            .limit(limit)
        ).scalars()
    )


def sequence_exists(db: DbSession, job_id: str, sequence: int) -> bool:
    if sequence <= 0:
        return True
    return (
        db.execute(
            select(JobEvent.id).where(JobEvent.job_id == job_id, JobEvent.sequence == sequence)
        ).first()
        is not None
    )


def format_sse(event_id: int | None, event_type: str, data: dict[str, Any]) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, default=str)}")
    return "\n".join(lines) + "\n\n"


def parse_last_event_id(value: str | None) -> int:
    if not value:
        return 0
    try:
        return max(0, int(value.strip()))
    except ValueError:
        return 0


async def stream(job_id: str, last_event_id: int, snapshot_factory) -> Any:  # noqa: ANN001
    """Async generator producing the SSE body for one job.

    ``snapshot_factory`` is called with an open session to build the current
    job snapshot, which is sent first whenever the requested ``Last-Event-ID``
    is unknown or has already been pruned (spec 8.4).
    """
    cursor = last_event_id

    with session_scope() as db:
        known = sequence_exists(db, job_id, cursor)
        if not known:
            cursor = 0
        needs_snapshot = not known or cursor == 0
        snapshot = snapshot_factory(db) if needs_snapshot else None
        current = latest_sequence(db, job_id)

    if snapshot is not None:
        yield format_sse(
            None,
            JOB_UPDATED,
            {
                "sequence": current,
                "type": JOB_UPDATED,
                "jobId": job_id,
                "occurredAt": _iso(utcnow()),
                "payload": {"snapshot": snapshot},
            },
        )

    last_heartbeat = asyncio.get_event_loop().time()
    while True:
        with session_scope() as db:
            pending = events_after(db, job_id, cursor)
            batch = [envelope(event) for event in pending]
            cursor = pending[-1].sequence if pending else cursor

        for item in batch:
            yield format_sse(item["sequence"], item["type"], item)

        now = asyncio.get_event_loop().time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            last_heartbeat = now
            yield format_sse(
                None,
                HEARTBEAT,
                {
                    "sequence": cursor,
                    "type": HEARTBEAT,
                    "jobId": job_id,
                    "occurredAt": _iso(utcnow()),
                    "payload": {},
                },
            )

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
