"""Review selection, ordering, and trim validation (spec 5.6).

Server-side validation is authoritative. A reviewed trim is rejected when it
falls outside the clip duration bounds (:data:`MIN_CLIP_US`/:data:`MAX_CLIP_US`),
leaves the candidate's safe interval, references another job's candidate,
selects the same candidate twice, or produces non-gapless order values.

Concurrency uses an optimistic review revision: a stale update receives
``409 stale_review_revision`` together with the latest review state and mutates
nothing.
"""

from __future__ import annotations

from fractions import Fraction

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import conflict, invalid_job_state, validation_error
from app.media.timebase import (
    MAX_CLIP_SECONDS,
    MAX_CLIP_US,
    MIN_CLIP_SECONDS,
    MIN_CLIP_US,
    Interval,
    frame_duration_us_ceil,
    parse_rational,
    quantize_end_us,
    quantize_start_us,
)
from app.models import CandidateShot, Job, SelectedClip, utcnow
from app.services import events

#: States in which the review may be edited (spec 5.6).
EDITABLE_STATES = {"review-ready", "complete"}


def job_frame_rate(job: Job) -> Fraction:
    video = job.video or {}
    return parse_rational(video.get("averageFrameRate") or "25")


def list_candidates(db: DbSession, job_id: str) -> list[CandidateShot]:
    return list(
        db.execute(
            select(CandidateShot)
            .where(CandidateShot.job_id == job_id)
            .order_by(CandidateShot.rank.asc(), CandidateShot.source_start_us.asc())
        ).scalars()
    )


def list_selected(db: DbSession, job_id: str) -> list[SelectedClip]:
    return list(
        db.execute(
            select(SelectedClip)
            .where(SelectedClip.job_id == job_id)
            .order_by(SelectedClip.order_index.asc())
        ).scalars()
    )


def validate_trim(
    candidate: CandidateShot, *, start_us: int, end_us: int, rate: Fraction
) -> tuple[int, int]:
    """Quantize and validate one reviewed trim, or raise.

    Quantization moves both bounds inward to source-frame boundaries, so the
    result is always inside the requested range and inside the safe interval.
    """
    if end_us <= start_us:
        raise validation_error(
            "invalid_trim", "The clip end must come after its start.",
            {"candidateId": candidate.id},
        )

    safe = Interval(candidate.safe_start_us, candidate.safe_end_us)
    requested = Interval(start_us, end_us)
    if not safe.contains(requested):
        raise validation_error(
            "trim_outside_safe_interval",
            "The clip must stay inside the detected shot's safe interval.",
            {
                "candidateId": candidate.id,
                "safeStartUs": safe.start_us,
                "safeEndUs": safe.end_us,
            },
        )

    quantized_start = quantize_start_us(start_us, rate)
    quantized_end = quantize_end_us(end_us, rate)
    if quantized_end <= quantized_start:
        raise validation_error(
            "invalid_trim", "The clip is shorter than one source frame.",
            {"candidateId": candidate.id},
        )

    # Quantization may push a bound past the safe edge by a sub-frame amount;
    # clamp back inside so the invariant holds exactly.
    quantized_start = max(quantized_start, safe.start_us)
    quantized_end = min(quantized_end, safe.end_us)

    tolerance = frame_duration_us_ceil(rate)
    duration = quantized_end - quantized_start
    if duration < MIN_CLIP_US - tolerance:
        raise validation_error(
            "trim_too_short",
            f"A clip must be at least {MIN_CLIP_SECONDS:g} seconds long.",
            {"candidateId": candidate.id, "durationUs": duration},
        )
    if duration > MAX_CLIP_US + tolerance:
        raise validation_error(
            "trim_too_long",
            f"A clip may be at most {MAX_CLIP_SECONDS:g} seconds long.",
            {"candidateId": candidate.id, "durationUs": duration},
        )
    return quantized_start, quantized_end


def stale_revision_error(db: DbSession, job: Job) -> Exception:
    from app.api.serializers import selected_clip_model

    return conflict(
        "stale_review_revision",
        "The review was changed by another request. Reload and try again.",
        {
            "currentRevision": job.review_revision,
            "selectedClips": [
                selected_clip_model(clip).model_dump(by_alias=True)
                for clip in list_selected(db, job.id)
            ],
        },
    )


def replace_review(
    db: DbSession,
    job: Job,
    *,
    revision: int,
    clips: list[tuple[str, int, int, int]],
    export_active: bool,
) -> tuple[int, list[SelectedClip]]:
    """Atomically replace the selection. ``clips`` is ``(candidateId, order, start, end)``."""
    if job.state not in EDITABLE_STATES:
        raise invalid_job_state(
            "The job must finish analysis before its review can be edited.",
            {"state": job.state},
        )
    if export_active:
        raise conflict(
            "export_active", "An export is running. Wait for it to finish before editing."
        )
    if revision != job.review_revision:
        raise stale_revision_error(db, job)

    candidate_ids = [clip[0] for clip in clips]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise validation_error(
            "duplicate_candidate", "A candidate may be selected at most once."
        )

    orders = sorted(clip[1] for clip in clips)
    if orders != list(range(1, len(clips) + 1)):
        raise validation_error(
            "non_gapless_order", "Clip order values must be gapless and start at 1."
        )

    candidates = {
        candidate.id: candidate
        for candidate in db.execute(
            select(CandidateShot).where(CandidateShot.id.in_(candidate_ids or [""]))
        ).scalars()
    }
    rate = job_frame_rate(job)

    validated: list[tuple[CandidateShot, int, int, int]] = []
    for candidate_id, order, start_us, end_us in clips:
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise validation_error(
                "unknown_candidate", "A selected candidate does not exist.",
                {"candidateId": candidate_id},
            )
        if candidate.job_id != job.id:
            # Cross-job reference: report it as unknown rather than confirming
            # that the id exists elsewhere.
            raise validation_error(
                "unknown_candidate", "A selected candidate does not exist.",
                {"candidateId": candidate_id},
            )
        quantized_start, quantized_end = validate_trim(
            candidate, start_us=start_us, end_us=end_us, rate=rate
        )
        validated.append((candidate, order, quantized_start, quantized_end))

    # Replace wholesale inside the caller's transaction.
    db.execute(delete(SelectedClip).where(SelectedClip.job_id == job.id))
    db.flush()

    job.review_revision += 1
    rows: list[SelectedClip] = []
    for candidate, order, start_us, end_us in sorted(validated, key=lambda item: item[1]):
        row = SelectedClip(
            job_id=job.id,
            candidate_id=candidate.id,
            order_index=order,
            start_us=start_us,
            end_us=end_us,
            duration_us=end_us - start_us,
            review_revision=job.review_revision,
        )
        db.add(row)
        rows.append(row)

    job.selected_count = len(rows)
    job.updated_at = utcnow()
    db.flush()

    events.append(
        db,
        job.id,
        events.JOB_UPDATED,
        {
            "state": job.state,
            "reviewRevision": job.review_revision,
            "selectedCount": job.selected_count,
        },
    )
    return job.review_revision, rows


def auto_select(db: DbSession, job: Job, candidates: list[CandidateShot]) -> list[SelectedClip]:
    """Select up to the target count in rank order (spec 5.6).

    Fewer eligible candidates than requested is a successful result, not an
    error; the shortfall is reported through ``eligibleCount`` and the
    partial-result reason.

    When the job named a product, candidates carrying an ``excluded_reason``
    are passed over: they either show a person or do not show the product.
    They remain in review with the reason attached, so the editor can still
    select one by hand -- this is a default, not a prohibition. If *every*
    candidate is excluded, the ranked order is used unchanged rather than
    returning nothing, so the job still produces a reviewable result.
    """
    db.execute(delete(SelectedClip).where(SelectedClip.job_id == job.id))
    db.flush()

    selectable = [c for c in candidates if c.excluded_reason is None] or candidates
    chosen = sorted(selectable, key=lambda c: c.rank)[: job.target_clip_count]
    # Default export order is ascending source time (spec 5.7).
    chosen = sorted(chosen, key=lambda c: c.source_start_us)

    job.review_revision += 1
    rows: list[SelectedClip] = []
    for index, candidate in enumerate(chosen, start=1):
        row = SelectedClip(
            job_id=job.id,
            candidate_id=candidate.id,
            order_index=index,
            start_us=candidate.recommended_start_us,
            end_us=candidate.recommended_end_us,
            duration_us=candidate.recommended_end_us - candidate.recommended_start_us,
            review_revision=job.review_revision,
        )
        db.add(row)
        rows.append(row)

    job.selected_count = len(rows)
    job.updated_at = utcnow()
    db.flush()
    return rows
