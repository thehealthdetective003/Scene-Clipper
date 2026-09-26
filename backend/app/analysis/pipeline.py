"""Analysis pipeline orchestration (spec 6, 5.5).

Stage order: probe -> detect -> safe intervals -> local features -> ranking ->
finalize. Each stage is idempotent and commits its checkpoint before the next
one starts, so a retry resumes from the last committed checkpoint rather than
repeating completed work, and a resumed run produces no duplicate candidates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.analysis import budget as budget_module
from app.analysis import features as feature_module
from app.analysis import intervals as interval_module
from app.analysis import scoring
from app.analysis.detector import DetectorCancelled, get_detector
from app.analysis.ranking import RankingRunner
from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.media.clips import ClipRenderError, render_preview
from app.media.frames import extract_jpeg
from app.media.probe import MediaInfo, UnsupportedMediaError, probe_media
from app.media.runner import MediaToolCancelled, MediaToolError
from app.media.timebase import Interval
from app.models import CandidateShot, DetectedShot, Job, Upload
from app.providers.gemini import get_provider
from app.services import events, review, storage
from app.services import jobs as job_service
from app.services.jobs import (
    CHECKPOINT_DETECTED,
    CHECKPOINT_PREPARED,
    CHECKPOINT_PROBED,
    CHECKPOINT_RANKED,
)

logger = get_logger("app.analysis.pipeline")

#: Previews are generated eagerly for this many top-ranked candidates; the rest
#: are rendered on demand by the preview route.
EAGER_PREVIEW_LIMIT = 60


class AnalysisCancelled(Exception):
    """Cooperative cancellation was requested."""


@dataclass(slots=True)
class _Context:
    job_id: str
    settings: Settings
    source: Path
    source_label: dict | None = None
    info: MediaInfo | None = None


def run_analysis(
    job_id: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    """Run (or resume) the analysis pipeline for one job."""
    settings = get_settings()

    def cancelled() -> bool:
        if should_cancel is not None and should_cancel():
            return True
        with session_scope() as db:
            job = db.get(Job, job_id)
            return job is None or job.cancel_requested or job.deleted_at is not None

    def beat() -> None:
        if heartbeat is not None:
            heartbeat()

    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None or job.deleted_at is not None:
            logger.info("skipping deleted job", extra={"job_id": job_id})
            return
        if job.state in ("review-ready", "complete", "exporting"):
            logger.info("analysis already finished", extra={"job_id": job_id})
            return
        upload = db.get(Upload, job.upload_id)
        if upload is None:
            job_service.fail(
                db, job, phase="probing", code="source_missing",
                message="The source upload is no longer available.", retryable=False,
            )
            return
        source = storage.resolve(upload.relative_source_path, settings)
        checkpoint = job.checkpoint
        source_label = (
            {"text": job.source_name, "style": job.source_label_style}
            if job.source_name and job.source_label_style
            else None
        )

    context = _Context(
        job_id=job_id, settings=settings, source=source, source_label=source_label
    )

    try:
        _stage_probe(context, checkpoint, cancelled, beat)
        _stage_detect(context, checkpoint, cancelled, beat)
        _stage_features(context, checkpoint, cancelled, beat)
        _stage_rank(context, cancelled, beat)
    except AnalysisCancelled:
        _finish_cancelled(job_id, settings)
    except (MediaToolCancelled, DetectorCancelled):
        _finish_cancelled(job_id, settings)
    except UnsupportedMediaError as exc:
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is not None:
                job_service.fail(
                    db, job, phase="probing", code=exc.code, message=exc.message, retryable=False
                )
    except MediaToolError as exc:
        # Protected diagnostics only; the client sees a stable code.
        logger.error(
            "media tool failure",
            extra={"job_id": job_id, "context": {"exitCode": exc.exit_code}},
        )
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is not None:
                job_service.fail(
                    db, job, phase=job.progress_phase, code="media_processing_failed",
                    message="The video could not be processed. You can retry this job.",
                    retryable=True,
                )
    except Exception:
        logger.exception("analysis failed", extra={"job_id": job_id})
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job is not None:
                job_service.fail(
                    db, job, phase=job.progress_phase, code="analysis_failed",
                    message="Analysis failed unexpectedly. You can retry this job.",
                    retryable=True,
                )
    finally:
        # Attempt-only scratch never survives a run, however it ended.
        storage.remove_tree(f"{storage.job_dir(job_id)}/attempts", settings)


# --- Stage A ---------------------------------------------------------------


def _stage_probe(context: _Context, checkpoint: str | None, cancelled, beat) -> None:  # noqa: ANN001
    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        if job_service.has_checkpoint(job, CHECKPOINT_PROBED) and job.video:
            context.info = _info_from_upload(db, job, context.settings)
            return
        job_service.transition(db, job, "probing", message="Reading video metadata.")

    if cancelled():
        raise AnalysisCancelled()
    beat()

    info = probe_media(context.source, context.settings)
    context.info = info

    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        job.video = info.to_public_video()
        for warning in info.warnings:
            job_service.add_warning(job, warning)
        job_service.set_progress(
            db, job, phase="probing", percent=8.0, message="Video metadata read."
        )
        job_service.commit_checkpoint(db, job, CHECKPOINT_PROBED)


def _info_from_upload(db, job: Job, settings: Settings) -> MediaInfo:  # noqa: ANN001
    """Rebuild MediaInfo on resume without re-probing where possible."""
    upload = db.get(Upload, job.upload_id)
    source = storage.resolve(upload.relative_source_path, settings)
    return probe_media(source, settings)


# --- Stage B, C, D ---------------------------------------------------------


def _stage_detect(context: _Context, checkpoint: str | None, cancelled, beat) -> None:  # noqa: ANN001
    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        if job_service.has_checkpoint(job, CHECKPOINT_DETECTED):
            return
        job_service.transition(db, job, "detecting", message="Detecting shot boundaries.")
        # Idempotent re-run: discard any partial output from a previous attempt
        # so a resumed job cannot produce duplicate candidates.
        db.execute(delete(CandidateShot).where(CandidateShot.job_id == job.id))
        db.execute(delete(DetectedShot).where(DetectedShot.job_id == job.id))

    if cancelled():
        raise AnalysisCancelled()

    info = context.info
    assert info is not None

    def on_progress(fraction: float) -> None:
        beat()
        with session_scope() as db:
            job = db.get(Job, context.job_id)
            if job is not None:
                job_service.set_progress(
                    db,
                    job,
                    phase="detecting",
                    percent=10.0 + 30.0 * fraction,
                    message="Detecting shot boundaries.",
                    emit=False,
                )

    result = get_detector().detect(
        context.source,
        info,
        settings=context.settings,
        should_cancel=cancelled,
        on_progress=on_progress,
    )

    safe_shots = interval_module.build_safe_shots(
        result.shots, result.events, result.frame_rate, source_duration_us=info.duration_us
    )

    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        eligible = 0
        for safe_shot in safe_shots:
            detected = DetectedShot(
                job_id=job.id,
                shot_number=safe_shot.shot_number,
                source_start_us=safe_shot.source.start_us,
                source_end_us=safe_shot.source.end_us,
                safe_start_us=safe_shot.safe.start_us if safe_shot.safe else safe_shot.source.start_us,
                safe_end_us=safe_shot.safe.end_us if safe_shot.safe else safe_shot.source.start_us,
                usable_duration_us=safe_shot.usable_duration_us,
                incoming_boundary=safe_shot.incoming,
                outgoing_boundary=safe_shot.outgoing,
                eligible=safe_shot.eligible,
                ineligible_reason=safe_shot.ineligible_reason,
            )
            db.add(detected)
            db.flush()

            if not safe_shot.eligible or safe_shot.safe is None:
                continue

            recommended = interval_module.recommend_interval(safe_shot.safe, result.frame_rate)
            # One candidate per detected shot: at most one clip can ever come
            # from a single shot (spec 1.3).
            db.add(
                CandidateShot(
                    job_id=job.id,
                    detected_shot_id=detected.id,
                    shot_number=safe_shot.shot_number,
                    source_start_us=safe_shot.source.start_us,
                    source_end_us=safe_shot.source.end_us,
                    safe_start_us=safe_shot.safe.start_us,
                    safe_end_us=safe_shot.safe.end_us,
                    usable_duration_us=safe_shot.usable_duration_us,
                    recommended_start_us=recommended.start_us,
                    recommended_end_us=recommended.end_us,
                    is_long_shot=safe_shot.is_long_shot,
                    incoming_boundary=safe_shot.incoming,
                    outgoing_boundary=safe_shot.outgoing,
                    feature_version=job.feature_version,
                )
            )
            eligible += 1

        job.detected_count = len(safe_shots)
        job.eligible_count = eligible
        job_service.set_progress(
            db,
            job,
            phase="detecting",
            percent=45.0,
            message=f"Found {eligible} usable shot(s) of {len(safe_shots)} detected.",
        )
        job_service.commit_checkpoint(db, job, CHECKPOINT_DETECTED)

    logger.info(
        "detection complete",
        extra={"job_id": context.job_id, "context": result.diagnostics},
    )


# --- Stage E ---------------------------------------------------------------


def _stage_features(context: _Context, checkpoint: str | None, cancelled, beat) -> None:  # noqa: ANN001
    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        if job_service.has_checkpoint(job, CHECKPOINT_PREPARED):
            return
        candidates = list(
            db.execute(
                select(CandidateShot).where(CandidateShot.job_id == job.id)
            ).scalars()
        )
        candidate_specs = [
            (c.id, c.safe_start_us, c.safe_end_us, c.recommended_start_us, c.recommended_end_us)
            for c in candidates
        ]

    if not candidate_specs:
        with session_scope() as db:
            job = db.get(Job, context.job_id)
            if job is not None:
                job_service.commit_checkpoint(db, job, CHECKPOINT_PREPARED)
        return

    info = context.info
    assert info is not None
    raws: dict[str, feature_module.RawFeatures] = {}

    thumb_dir = storage.ensure_dir(
        f"{storage.job_subdir(context.job_id, 'contact-sheets')}/thumbs", context.settings
    )

    for index, (candidate_id, safe_start, safe_end, rec_start, rec_end) in enumerate(
        candidate_specs
    ):
        if cancelled():
            raise AnalysisCancelled()
        beat()

        raws[candidate_id] = feature_module.measure_candidate(
            context.source,
            start_us=safe_start,
            end_us=safe_end,
            source_width=info.display_width,
            source_height=info.display_height,
            settings=context.settings,
            should_cancel=cancelled,
        )

        thumbnail = thumb_dir / f"{candidate_id}.jpg"
        if not thumbnail.exists():
            extract_jpeg(
                context.source,
                thumbnail,
                position_us=(rec_start + rec_end) // 2,
                width=480,
                settings=context.settings,
                should_cancel=cancelled,
            )

        if index % 5 == 0:
            with session_scope() as db:
                job = db.get(Job, context.job_id)
                if job is not None:
                    job_service.set_progress(
                        db,
                        job,
                        phase="detecting",
                        percent=45.0 + 10.0 * (index / max(1, len(candidate_specs))),
                        message=f"Measuring candidate {index + 1} of {len(candidate_specs)}.",
                        emit=False,
                    )

    scored_features = feature_module.normalize_job(raws)

    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        for candidate in db.execute(
            select(CandidateShot).where(CandidateShot.job_id == job.id)
        ).scalars():
            computed = scored_features.get(candidate.id)
            if computed is None:
                continue
            candidate.features = computed.to_json()
            candidate.local_score = computed.local_score
            candidate.feature_version = computed.feature_version
            candidate.thumbnail_path = storage.relative_of(
                thumb_dir / f"{candidate.id}.jpg", context.settings
            )
        job_service.set_progress(
            db, job, phase="detecting", percent=55.0, message="Local measurements complete."
        )
        job_service.commit_checkpoint(db, job, CHECKPOINT_PREPARED)


# --- Stage F through J -----------------------------------------------------


def _stage_rank(context: _Context, cancelled, beat) -> None:  # noqa: ANN001
    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        job_service.transition(db, job, "ranking", message="Ranking candidates.")
        candidates = list(
            db.execute(
                select(CandidateShot).where(CandidateShot.job_id == job.id)
            ).scalars()
        )
        job_snapshot = job

    if cancelled():
        raise AnalysisCancelled()

    info = context.info
    assert info is not None

    feature_map = {
        candidate.id: feature_module.Features(**candidate.features)
        for candidate in candidates
        if candidate.features
    }

    runner = RankingRunner(
        db_factory=session_scope,
        settings=context.settings,
        provider=get_provider(context.settings.gemini_timeout_seconds),
        job_id=context.job_id,
        source=context.source,
        info=info,
        should_cancel=cancelled,
    )
    outcome = runner.run(job_snapshot, candidates, feature_map)

    if cancelled():
        raise AnalysisCancelled()

    if job_snapshot.content_prompt_normalized:
        _screen_for_humans(context, outcome, candidates, info, cancelled)

    if cancelled():
        raise AnalysisCancelled()

    ranked = scoring.rank(list(outcome.scored.values()))

    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        rows = {
            candidate.id: candidate
            for candidate in db.execute(
                select(CandidateShot).where(CandidateShot.job_id == job.id)
            ).scalars()
        }

        for position, item in enumerate(ranked, start=1):
            candidate = rows.get(item.candidate_id)
            if candidate is None:
                continue
            candidate.rank = position
            candidate.score = item.final_score
            candidate.confidence = item.confidence
            candidate.reason = item.reason
            candidate.reason_code = item.reason_code
            candidate.scoring_source = item.scoring_source
            candidate.cache_status = item.cache_status
            candidate.prompt_relevance_evaluated = item.prompt_relevance_evaluated
            candidate.motion_ambiguous = item.motion_ambiguous
            candidate.gemini_relevance = item.relevance
            candidate.gemini_interest = item.interest
            candidate.gemini_clarity = item.clarity
            candidate.human_present = item.human_present
            candidate.human_source = item.human_source
            candidate.product_visible = item.product_visible
            candidate.product_prominence = item.product_prominence
            candidate.excluded_reason = item.excluded_reason

            window = outcome.fine_windows.get(item.candidate_id)
            if window is not None:
                safe = Interval(candidate.safe_start_us, candidate.safe_end_us)
                if safe.contains(window):
                    candidate.recommended_start_us = window.start_us
                    candidate.recommended_end_us = window.end_us

        for warning in outcome.warnings:
            job_service.add_warning(job, warning)
        if outcome.partial_result_reason:
            job.partial_result_reason = outcome.partial_result_reason

        budget_module.set_cache_status(db, job.id, outcome.cache_status)
        if outcome.fallback_reason:
            budget_module.mark_fallback(db, job.id, outcome.fallback_reason)

        eligible = list(rows.values())
        selected = review.auto_select(db, job, eligible)

        if len(eligible) < job.target_clip_count:
            job.partial_result_reason = (
                job.partial_result_reason
                or (
                    f"Requested {job.target_clip_count} clip(s); "
                    f"{len(eligible)} eligible shot(s) were found."
                )
            )
            job_service.add_warning(job, "fewer_eligible_than_requested")

        job_service.commit_checkpoint(db, job, CHECKPOINT_RANKED)
        job_service.set_progress(
            db,
            job,
            phase="ranking",
            percent=95.0,
            message=f"Ranked {len(eligible)} candidate(s).",
        )
        preview_targets = [
            (clip.candidate_id, clip.start_us, clip.end_us) for clip in selected
        ][:EAGER_PREVIEW_LIMIT]

    _render_previews(context, preview_targets, cancelled, beat)

    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        job_service.transition(db, job, "review-ready", message="Ready for review.")
        events.append(
            db,
            job.id,
            events.CANDIDATES_READY,
            {
                "eligibleCount": job.eligible_count,
                "selectedCount": job.selected_count,
                "reviewRevision": job.review_revision,
                "partialResultReason": job.partial_result_reason,
            },
        )


#: Cap on the local human screen. It costs a decode per candidate, so the whole
#: set is screened only when it is small enough not to dominate the run; beyond
#: that the highest-scoring candidates are screened and the rest are reported as
#: unverified rather than silently cleared.
MAX_LOCAL_HUMAN_SCREEN = 120


def _screen_for_humans(
    context: _Context,
    outcome,  # noqa: ANN001 - RankingOutcome
    candidates: list[CandidateShot],
    info: MediaInfo,
    cancelled,  # noqa: ANN001
) -> None:
    """Fill in human presence for candidates the model never assessed.

    The product-only rule has to hold for every shot that reaches the export,
    not only the ones that happened to fit inside the request cap. Shots Gemini
    already judged keep its verdict -- it sees things the local screen cannot.
    Shots it never saw are screened here, and anything still unverified is
    reported as such so the result never *implies* a shot is people-free when
    nobody checked.
    """
    from app.analysis import humans

    unassessed = [
        candidate
        for candidate in candidates
        if candidate.id in outcome.scored
        and outcome.scored[candidate.id].human_present is None
    ]
    if not unassessed:
        return

    # Screen the strongest first, so a truncated pass protects the shots most
    # likely to be selected.
    unassessed.sort(key=lambda c: -outcome.scored[c.id].final_score)
    screened, unverified = unassessed[:MAX_LOCAL_HUMAN_SCREEN], unassessed[MAX_LOCAL_HUMAN_SCREEN:]

    found = 0
    for candidate in screened:
        if cancelled():
            raise AnalysisCancelled()
        result = humans.screen_candidate(
            context.source,
            start_us=candidate.recommended_start_us,
            end_us=candidate.recommended_end_us,
            source_width=info.display_width,
            source_height=info.display_height,
            settings=context.settings,
            should_cancel=cancelled,
        )
        if not result.assessed:
            unverified.append(candidate)
            continue

        item = outcome.scored[candidate.id]
        if result.present:
            found += 1
            item.human_present = True
            item.human_source = scoring.HUMAN_SOURCE_LOCAL
            item.excluded_reason = scoring.EXCLUDED_HUMAN_PRESENT
            item.reason = (
                f"A person was detected locally ({result.detector}); "
                "held back from automatic selection."
            )[:255]
        else:
            # A clean local screen is weak evidence, so record that it ran
            # without asserting the shot is definitely people-free.
            item.human_present = False
            item.human_source = scoring.HUMAN_SOURCE_LOCAL

    if found:
        outcome.warnings.append("human_present_shots_excluded")
    if unverified:
        outcome.warnings.append("human_check_incomplete")

    logger.info(
        "local human screen complete",
        extra={
            "job_id": context.job_id,
            "context": {
                "screened": len(screened),
                "found": found,
                "unverified": len(unverified),
            },
        },
    )


def _render_previews(context: _Context, targets, cancelled, beat) -> None:  # noqa: ANN001
    info = context.info
    assert info is not None
    preview_dir = storage.ensure_dir(
        storage.job_subdir(context.job_id, "previews"), context.settings
    )
    for candidate_id, start_us, end_us in targets:
        if cancelled():
            return
        beat()
        destination = preview_dir / f"{candidate_id}.mp4"
        if destination.exists():
            continue
        try:
            render_preview(
                context.source,
                destination,
                info=info,
                start_us=start_us,
                end_us=end_us,
                source_label=context.source_label,
                settings=context.settings,
                should_cancel=cancelled,
            )
        except (MediaToolError, ClipRenderError):
            # A preview is a convenience; its failure must not fail the job.
            logger.warning("preview render failed", extra={"job_id": context.job_id})


def ensure_preview(job_id: str, candidate_id: str, settings: Settings) -> Path:
    """Render a review preview on demand (used by the preview route)."""
    with session_scope() as db:
        candidate = db.get(CandidateShot, candidate_id)
        if candidate is None or candidate.job_id != job_id:
            raise FileNotFoundError(candidate_id)
        job = db.get(Job, job_id)
        upload = db.get(Upload, job.upload_id) if job else None
        if upload is None:
            raise FileNotFoundError(candidate_id)
        source = storage.resolve(upload.relative_source_path, settings)
        start_us, end_us = candidate.recommended_start_us, candidate.recommended_end_us
        source_label = (
            {"text": job.source_name, "style": job.source_label_style}
            if job and job.source_name and job.source_label_style
            else None
        )

    preview_dir = storage.ensure_dir(storage.job_subdir(job_id, "previews"), settings)
    destination = preview_dir / f"{candidate_id}.mp4"
    if destination.exists():
        return destination

    info = probe_media(source, settings)
    render_preview(
        source,
        destination,
        info=info,
        start_us=start_us,
        end_us=end_us,
        source_label=source_label,
        settings=settings,
    )
    return destination


def _finish_cancelled(job_id: str, settings: Settings) -> None:
    """Publish no partial analysis; retain the source and committed checkpoints."""
    storage.remove_tree(f"{storage.job_dir(job_id)}/attempts", settings)
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is not None and job.state not in job_service.TERMINAL_STATES:
            job_service.mark_cancelled(db, job)
