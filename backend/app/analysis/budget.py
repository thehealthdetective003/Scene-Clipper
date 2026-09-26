"""Stage G: request budgeting and durable reservations (spec 6.7).

Every actual model-inference request, retries included, consumes one unit of
the job's snapshotted cap. Provider file upload/status/delete operations are
recorded separately and consume no inference unit; the five-candidate proxy
limit is what constrains them.

A unit is reserved durably *before* transmission. A worker that dies after
transmitting but before recording the outcome leaves an indeterminate
reservation, which stays consumed -- deliberately conservative, so the cap
still holds across crashes.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.models import AnalysisAttempt, AnalysisUsage, utcnow

STAGE_COARSE = "coarse"
STAGE_FINE = "fine"
STAGE_PROXY = "proxy"

#: Never more than five unique proxy candidates per job (spec 6.9).
MAX_PROXY_CANDIDATES = 5


@dataclass(frozen=True, slots=True)
class Allocation:
    """Deterministic split of the snapshotted cap across the three stages."""

    cap: int
    coarse: int
    fine: int
    proxy: int

    @property
    def total(self) -> int:
        return self.coarse + self.fine + self.proxy

    @property
    def local_only(self) -> bool:
        return self.cap <= 0


def allocate(cap: int, *, has_long_shots: bool) -> Allocation:
    """Apply the allocation rules in their specified order.

    1. A cap of 0 performs local-only ranking.
    2. A cap of at least 3 reserves one unit for a proxy-video fallback batch.
    3. One unit goes to fine long-shot selection when a long shot exists and at
       least two non-proxy units remain.
    4. Everything left goes to coarse ranking; a positive cap always assigns at
       least one coarse unit.
    """
    if cap <= 0:
        return Allocation(cap=0, coarse=0, fine=0, proxy=0)

    proxy = 1 if cap >= 3 else 0
    remaining = cap - proxy

    fine = 1 if (has_long_shots and remaining >= 2) else 0
    remaining -= fine

    coarse = max(1, remaining)
    if coarse > remaining:
        # Guaranteeing a coarse unit takes precedence over the proxy reserve.
        shortfall = coarse - remaining
        if proxy >= shortfall:
            proxy -= shortfall
        elif fine >= shortfall:
            fine -= shortfall

    return Allocation(cap=cap, coarse=coarse, fine=fine, proxy=proxy)


def roll_forward(allocation: Allocation, *, coarse_used: int, fine_used: int) -> Allocation:
    """Unused coarse units roll into fine; unused fine units roll into proxy.

    Unused proxy capacity is *not* spent merely to exhaust the cap.
    """
    coarse_left = max(0, allocation.coarse - coarse_used)
    fine_total = allocation.fine + coarse_left
    fine_left = max(0, fine_total - fine_used)
    return Allocation(
        cap=allocation.cap,
        coarse=allocation.coarse,
        fine=fine_total,
        proxy=allocation.proxy + fine_left,
    )


# --- Durable reservation ledger -------------------------------------------


class BudgetExhausted(Exception):
    """No reservable unit remains for this job."""


def consumed_units(db: DbSession, job_id: str) -> int:
    """Every reservation counts, including indeterminate ones."""
    return int(
        db.execute(
            select(func.count())
            .select_from(AnalysisAttempt)
            .where(AnalysisAttempt.job_id == job_id)
        ).scalar_one()
    )


def stage_units(db: DbSession, job_id: str, stage: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(AnalysisAttempt)
            .where(AnalysisAttempt.job_id == job_id, AnalysisAttempt.stage == stage)
        ).scalar_one()
    )


def reserve(
    db: DbSession, job_id: str, stage: str, *, cap: int, stage_limit: int, is_retry: bool = False
) -> AnalysisAttempt:
    """Atomically claim one unit and persist a unique attempt id.

    The caller must commit before transmitting, then settle the attempt with
    :func:`settle`.
    """
    if cap <= 0:
        raise BudgetExhausted("This job is configured for local-only analysis.")
    if consumed_units(db, job_id) >= cap:
        raise BudgetExhausted("The job's request cap is exhausted.")
    if stage_units(db, job_id, stage) >= stage_limit:
        raise BudgetExhausted(f"No {stage} requests remain in this job's allocation.")

    attempt = AnalysisAttempt(
        job_id=job_id, stage=stage, status="reserved", is_retry=is_retry
    )
    db.add(attempt)
    db.flush()

    usage = db.get(AnalysisUsage, job_id)
    if usage is not None:
        usage.requests_used += 1
        if stage == STAGE_COARSE:
            usage.coarse_requests += 1
        elif stage == STAGE_FINE:
            usage.fine_requests += 1
        elif stage == STAGE_PROXY:
            usage.proxy_video_requests += 1
        db.flush()
    return attempt


def settle(
    db: DbSession, attempt: AnalysisAttempt, *, success: bool, error_code: str | None = None
) -> None:
    """Record the outcome. The unit stays consumed either way."""
    attempt.status = "succeeded" if success else "failed"
    attempt.error_code = error_code
    attempt.settled_at = utcnow()
    db.flush()


def record_provider_file_operation(db: DbSession, job_id: str, count: int = 1) -> None:
    """File operations are tracked separately and consume no inference unit."""
    usage = db.get(AnalysisUsage, job_id)
    if usage is not None:
        usage.provider_file_operations += count
        db.flush()


def record_payload(
    db: DbSession,
    job_id: str,
    *,
    image_bytes: int = 0,
    video_bytes: int = 0,
    video_seconds: float = 0.0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
) -> None:
    usage = db.get(AnalysisUsage, job_id)
    if usage is None:
        return
    usage.image_bytes_sent += image_bytes
    usage.proxy_video_bytes_sent += video_bytes
    usage.proxy_video_seconds_sent += video_seconds
    if input_tokens is not None:
        usage.input_tokens = (usage.input_tokens or 0) + input_tokens
    if output_tokens is not None:
        usage.output_tokens = (usage.output_tokens or 0) + output_tokens
    if total_tokens is not None:
        usage.total_tokens = (usage.total_tokens or 0) + total_tokens
    db.flush()


def mark_fallback(db: DbSession, job_id: str, reason: str) -> None:
    usage = db.get(AnalysisUsage, job_id)
    if usage is not None:
        usage.local_fallback_used = True
        usage.fallback_reason = reason
        db.flush()


def set_cache_status(db: DbSession, job_id: str, status: str) -> None:
    usage = db.get(AnalysisUsage, job_id)
    if usage is not None:
        usage.cache_status = status
        db.flush()
