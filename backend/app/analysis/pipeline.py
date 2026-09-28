"""Analysis pipeline orchestration (spec 6, 5.5).

Stage order: probe -> detect -> safe intervals -> local features -> ranking ->
finalize. Each stage is idempotent and commits its checkpoint before the next
one starts, so a retry resumes from the last committed checkpoint rather than
repeating completed work, and a resumed run produces no duplicate candidates.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from app.models import CandidateShot, DetectedShot, Job, JobSource, Upload
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
MULTI_SOURCE_WORKERS = 3


class AnalysisCancelled(Exception):
    """Cooperative cancellation was requested."""


@dataclass(slots=True)
class _Context:
    job_id: str
    source_id: str
    source_order: int
    source_file_name: str
    settings: Settings
    source: Path
    source_label: dict | None = None
    content_prompt: str | None = None
    info: MediaInfo | None = None


def _source_display_name(context: _Context) -> str:
    label = (context.source_label or {}).get("text")
    return str(label or context.source_file_name)


def _record_source_progress(
    context: _Context,
    phase: str,
    percent: float,
    message: str,
) -> None:
    """Persist real source activity and roll it into the job's overall progress.

    Per-frame callbacks do not create durable SSE rows; the active job page
    polls this compact snapshot. Stage transitions still emit immediately.
    """
    with session_scope() as db:
        source_row = db.get(JobSource, context.source_id)
        job = db.get(Job, context.job_id)
        if source_row is None or job is None:
            return

        source_row.progress_phase = phase
        source_row.progress_percent = max(
            source_row.progress_percent,
            min(100.0, max(0.0, percent)),
        )
        source_row.progress_message = message[:255]
        source_row.state = "ready" if phase == "ready" else "processing"

        source_rows = list(
            db.execute(
                select(JobSource).where(JobSource.job_id == context.job_id)
            ).scalars()
        )
        average = sum(row.progress_percent for row in source_rows) / max(1, len(source_rows))
        ready_count = sum(row.progress_phase == "ready" for row in source_rows)
        source_message = f"{_source_display_name(context)}: {message}"
        overall_message = (
            f"{ready_count} of {len(source_rows)} sources ready · {source_message}"
            if len(source_rows) > 1
            else message
        )
        if job.state in job_service.RESUMABLE_STATES:
            job_service.set_progress(
                db,
                job,
                phase=job.state,
                percent=min(95.0, 2.0 + 0.93 * average),
                message=overall_message,
                emit=False,
            )


def run_analysis(
    job_id: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> None:  # noqa: PLR0912, PLR0915
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
        source_pairs = job_service.sources_for(db, job.id)
        if not source_pairs:
            job_service.fail(
                db, job, phase="probing", code="source_missing",
                message="The source upload is no longer available.", retryable=False,
            )
            return
        source_row, upload = source_pairs[0]
        source = storage.resolve(upload.relative_source_path, settings)
        checkpoint = job.checkpoint
        source_label = (
            {"text": source_row.source_name, "style": source_row.source_label_style}
            if source_row.source_name and source_row.source_label_style
            else None
        )
        contexts = [
            _Context(
                job_id=job_id,
                source_id=row.id,
                source_order=row.order_index,
                source_file_name=source_upload.file_name,
                settings=settings,
                source=storage.resolve(source_upload.relative_source_path, settings),
                source_label=(
                    {"text": row.source_name, "style": row.source_label_style}
                    if row.source_name and row.source_label_style
                    else None
                ),
                content_prompt=row.content_prompt_normalized,
            )
            for row, source_upload in source_pairs
        ]

    context = _Context(
        job_id=job_id,
        source_id=source_row.id,
        source_order=source_row.order_index,
        source_file_name=upload.file_name,
        settings=settings,
        source=source,
        source_label=source_label,
        content_prompt=source_row.content_prompt_normalized,
    )

    try:
        if len(contexts) > 1:
            _run_multi_analysis(contexts, cancelled, beat)
            return
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


def _run_multi_analysis(contexts: list[_Context], cancelled, beat) -> None:  # noqa: ANN001
    """Prepare independent sources concurrently, then rank/finalize them together."""
    job_id = contexts[0].job_id
    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job_service.transition(
            db,
            job,
            "detecting",
            message=f"Processing {len(contexts)} source videos in parallel.",
        )

    progress_lock = threading.Lock()

    def report(context: _Context, phase: str, percent: float, message: str) -> None:
        # SQLite supports concurrent readers but serializes writers. Keeping
        # these tiny progress commits ordered avoids lock contention between
        # source worker threads without serializing any media processing.
        with progress_lock:
            _record_source_progress(context, phase, percent, message)

    prepared: list[_Context] = []
    with ThreadPoolExecutor(
        max_workers=min(MULTI_SOURCE_WORKERS, len(contexts)),
        thread_name_prefix="scene-source",
    ) as executor:
        futures = {
            executor.submit(_prepare_source, context, cancelled, beat, report): context
            for context in contexts
        }
        for future in as_completed(futures):
            prepared.append(future.result())

    prepared.sort(key=lambda item: item.source_order)
    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job is not None
        source_rows = list(
            db.execute(select(JobSource).where(JobSource.job_id == job_id)).scalars()
        )
        job.detected_count = sum(row.detected_count or 0 for row in source_rows)
        job.eligible_count = sum(row.eligible_count or 0 for row in source_rows)
        if source_rows:
            job.video = source_rows[0].video
        job_service.commit_checkpoint(db, job, CHECKPOINT_PREPARED)

    _rank_multi_sources(prepared, cancelled, beat)


def _source_has_checkpoint(source: JobSource, checkpoint: str) -> bool:
    if source.checkpoint not in job_service.CHECKPOINT_ORDER:
        return False
    return job_service.CHECKPOINT_ORDER.index(
        source.checkpoint
    ) >= job_service.CHECKPOINT_ORDER.index(
        checkpoint,
    )


def _prepare_source(
    context: _Context, cancelled, beat, report
) -> _Context:  # noqa: ANN001, PLR0912, PLR0915
    if cancelled():
        raise AnalysisCancelled()
    report(context, "probing", 2.0, "Reading video metadata.")
    beat()
    info = probe_media(context.source, context.settings)
    context.info = info

    with session_scope() as db:
        source_row = db.get(JobSource, context.source_id)
        if source_row is None:
            raise FileNotFoundError(context.source_id)
        source_row.video = info.to_public_video()
        source_row.state = "processing"
        if not _source_has_checkpoint(source_row, CHECKPOINT_PROBED):
            source_row.checkpoint = CHECKPOINT_PROBED
        job = db.get(Job, context.job_id)
        if job is not None:
            for warning in info.warnings:
                job_service.add_warning(job, warning)
        already_detected = _source_has_checkpoint(source_row, CHECKPOINT_DETECTED)

    report(context, "detecting", 10.0, "Metadata read. Detecting shot boundaries.")

    if not already_detected:
        with session_scope() as db:
            db.execute(delete(CandidateShot).where(CandidateShot.source_id == context.source_id))
            db.execute(delete(DetectedShot).where(DetectedShot.source_id == context.source_id))

        def on_detection_progress(fraction: float) -> None:
            beat()
            report(
                context,
                "detecting",
                10.0 + 50.0 * fraction,
                f"Scanning video frames · {round(fraction * 100)}% complete.",
            )

        result = get_detector().detect(
            context.source,
            info,
            settings=context.settings,
            should_cancel=cancelled,
            on_progress=on_detection_progress,
        )
        safe_shots = interval_module.build_safe_shots(
            result.shots,
            result.events,
            result.frame_rate,
            source_duration_us=info.duration_us,
        )
        with session_scope() as db:
            job = db.get(Job, context.job_id)
            source_row = db.get(JobSource, context.source_id)
            assert job is not None and source_row is not None
            eligible = 0
            for safe_shot in safe_shots:
                detected = DetectedShot(
                    job_id=job.id,
                    source_id=context.source_id,
                    shot_number=safe_shot.shot_number,
                    source_start_us=safe_shot.source.start_us,
                    source_end_us=safe_shot.source.end_us,
                    safe_start_us=(
                        safe_shot.safe.start_us if safe_shot.safe else safe_shot.source.start_us
                    ),
                    safe_end_us=(
                        safe_shot.safe.end_us if safe_shot.safe else safe_shot.source.start_us
                    ),
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
                recommended = interval_module.recommend_interval(
                    safe_shot.safe, result.frame_rate
                )
                db.add(
                    CandidateShot(
                        job_id=job.id,
                        source_id=context.source_id,
                        detected_shot_id=detected.id,
                        shot_number=safe_shot.shot_number,
                        source_name=(context.source_label or {}).get("text"),
                        source_file_name=context.source_file_name,
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
            source_row.detected_count = len(safe_shots)
            source_row.eligible_count = eligible
            source_row.checkpoint = CHECKPOINT_DETECTED

        report(
            context,
            "measuring",
            62.0,
            f"Found {eligible} usable shot(s) of {len(safe_shots)} detected.",
        )

    with session_scope() as db:
        source_row = db.get(JobSource, context.source_id)
        assert source_row is not None
        already_prepared = _source_has_checkpoint(source_row, CHECKPOINT_PREPARED)
        candidates = list(
            db.execute(
                select(CandidateShot).where(CandidateShot.source_id == context.source_id)
            ).scalars()
        )
        candidate_specs = [
            (c.id, c.safe_start_us, c.safe_end_us, c.recommended_start_us, c.recommended_end_us)
            for c in candidates
        ]

    if not already_prepared:
        report(
            context,
            "measuring",
            62.0,
            f"Measuring quality for {len(candidate_specs)} candidate(s).",
        )
        raws: dict[str, feature_module.RawFeatures] = {}
        thumb_dir = storage.ensure_dir(
            f"{storage.job_subdir(context.job_id, 'contact-sheets')}/thumbs",
            context.settings,
        )
        if not candidate_specs:
            report(context, "ranking", 82.0, "No usable shots; source preparation complete.")
        for index, (candidate_id, safe_start, safe_end, rec_start, rec_end) in enumerate(
            candidate_specs,
            start=1,
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
            if index == 1 or index % 5 == 0 or index == len(candidate_specs):
                report(
                    context,
                    "measuring",
                    65.0 + 15.0 * (index / max(1, len(candidate_specs))),
                    f"Measuring candidate {index} of {len(candidate_specs)}.",
                )

        scored_features = feature_module.normalize_job(raws)
        with session_scope() as db:
            for candidate in db.execute(
                select(CandidateShot).where(CandidateShot.source_id == context.source_id)
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
            source_row = db.get(JobSource, context.source_id)
            assert source_row is not None
            source_row.checkpoint = CHECKPOINT_PREPARED

        report(context, "ranking", 82.0, "Local measurements complete.")

    return context


def _rank_multi_sources(
    contexts: list[_Context], cancelled, beat
) -> None:  # noqa: ANN001, PLR0912, PLR0915
    job_id = contexts[0].job_id
    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job_service.transition(
            db,
            job,
            "ranking",
            message=("Ranking candidates." if job.ranking_enabled else "Preparing manual review."),
        )
        ranking_enabled = job.ranking_enabled
        job_template = copy.copy(job)

    if cancelled():
        raise AnalysisCancelled()

    all_scored = []
    outcomes = []
    fine_windows: dict[str, Interval] = {}
    by_source: dict[str, list[CandidateShot]] = {}

    for context in contexts:
        _record_source_progress(
            context,
            "ranking",
            84.0,
            "Organizing candidates for manual review."
            if not ranking_enabled
            else "Ranking candidate clips.",
        )
        with session_scope() as db:
            source_row = db.get(JobSource, context.source_id)
            candidates = list(
                db.execute(
                    select(CandidateShot).where(CandidateShot.source_id == context.source_id)
                ).scalars()
            )
        by_source[context.source_id] = candidates
        if not ranking_enabled:
            _record_source_progress(
                context,
                "ranking",
                93.0,
                f"Organized {len(candidates)} candidate(s).",
            )
            continue
        assert source_row is not None and context.info is not None
        source_job = copy.copy(job_template)
        source_job.source_sha256 = source_row.source_sha256
        source_job.content_prompt = source_row.content_prompt
        source_job.content_prompt_normalized = source_row.content_prompt_normalized
        feature_map = {
            candidate.id: feature_module.Features(**candidate.features)
            for candidate in candidates
            if candidate.features
        }
        runner = RankingRunner(
            db_factory=session_scope,
            settings=context.settings,
            provider=get_provider(context.settings.gemini_timeout_seconds),
            job_id=job_id,
            source=context.source,
            info=context.info,
            should_cancel=cancelled,
        )
        outcome = runner.run(source_job, candidates, feature_map)
        if source_row.content_prompt_normalized:
            _screen_for_humans(context, outcome, candidates, context.info, cancelled)
        outcomes.append(outcome)
        all_scored.extend(outcome.scored.values())
        fine_windows.update(outcome.fine_windows)
        _record_source_progress(
            context,
            "ranking",
            93.0,
            f"Ranked {len(candidates)} candidate(s).",
        )

    source_order = {context.source_id: context.source_order for context in contexts}
    if ranking_enabled:
        ranked = scoring.rank(all_scored)
        ranked_ids = [item.candidate_id for item in ranked]
        scored_by_id = {item.candidate_id: item for item in ranked}
    else:
        ordered = sorted(
            [candidate for rows in by_source.values() for candidate in rows],
            key=lambda candidate: (source_order[candidate.source_id], candidate.source_start_us),
        )
        ranked_ids = [candidate.id for candidate in ordered]
        scored_by_id = {}

    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job is not None
        rows = {
            candidate.id: candidate
            for candidate in db.execute(
                select(CandidateShot).where(CandidateShot.job_id == job_id)
            ).scalars()
        }
        for position, candidate_id in enumerate(ranked_ids, start=1):
            candidate = rows[candidate_id]
            candidate.rank = position
            if ranking_enabled:
                item = scored_by_id[candidate_id]
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
                window = fine_windows.get(candidate_id)
                if window is not None and Interval(
                    candidate.safe_start_us, candidate.safe_end_us
                ).contains(window):
                    candidate.recommended_start_us = window.start_us
                    candidate.recommended_end_us = window.end_us
            else:
                candidate.score = 0.0
                candidate.confidence = 0.0
                candidate.reason = ""
                candidate.scoring_source = "local-fallback"
                candidate.prompt_relevance_evaluated = False

        for outcome in outcomes:
            for warning in outcome.warnings:
                job_service.add_warning(job, warning)
        if outcomes:
            statuses = {outcome.cache_status for outcome in outcomes}
            cache_status = "complete" if statuses == {"complete"} else (
                "partial" if statuses - {"none"} else "none"
            )
            budget_module.set_cache_status(db, job.id, cache_status)
            fallback = next(
                (outcome.fallback_reason for outcome in outcomes if outcome.fallback_reason),
                None,
            )
            if fallback:
                budget_module.mark_fallback(db, job.id, fallback)

        candidates = list(rows.values())
        selected = (
            review.auto_select(db, job, candidates)
            if ranking_enabled
            else []
        )
        if not ranking_enabled:
            review.begin_manual_review(db, job)

        for source_row in db.execute(
            select(JobSource).where(JobSource.job_id == job_id)
        ).scalars():
            source_row.checkpoint = CHECKPOINT_RANKED
            source_row.state = "processing"
            source_row.progress_phase = "previews"
            source_row.progress_percent = max(source_row.progress_percent, 95.0)
            source_row.progress_message = "Rendering review previews."
        job_service.commit_checkpoint(db, job, CHECKPOINT_RANKED)
        job_service.set_progress(
            db,
            job,
            phase="ranking",
            percent=95.0,
            message=(
                f"Ranked {len(candidates)} candidate(s)."
                if ranking_enabled
                else f"Prepared {len(candidates)} candidate(s) for manual review."
            ),
        )
        selected_by_source: dict[str, list[tuple[str, int, int]]] = {}
        for clip in selected[:EAGER_PREVIEW_LIMIT]:
            candidate = rows.get(clip.candidate_id)
            if candidate is not None:
                selected_by_source.setdefault(candidate.source_id, []).append(
                    (clip.candidate_id, clip.start_us, clip.end_us)
                )

    for context in contexts:
        _render_previews(
            context,
            selected_by_source.get(context.source_id, []),
            cancelled,
            beat,
        )

    with session_scope() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job_service.transition(
            db,
            job,
            "review-ready",
            message="Ready for review." if ranking_enabled else "Ready for manual review.",
        )
        events.append(
            db,
            job.id,
            events.CANDIDATES_READY,
            {
                "eligibleCount": job.eligible_count,
                "selectedCount": job.selected_count,
                "reviewRevision": job.review_revision,
            },
        )


# --- Stage A ---------------------------------------------------------------


def _stage_probe(context: _Context, checkpoint: str | None, cancelled, beat) -> None:  # noqa: ANN001
    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        if job_service.has_checkpoint(job, CHECKPOINT_PROBED) and job.video:
            context.info = _info_from_upload(db, job, context.settings)
            return
        job_service.transition(db, job, "probing", message="Reading video metadata.")

    _record_source_progress(context, "probing", 2.0, "Reading video metadata.")
    if cancelled():
        raise AnalysisCancelled()
    beat()

    info = probe_media(context.source, context.settings)
    context.info = info

    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        job.video = info.to_public_video()
        source_row = db.get(JobSource, context.source_id)
        if source_row is not None:
            source_row.video = info.to_public_video()
            source_row.checkpoint = CHECKPOINT_PROBED
            source_row.state = "processing"
        for warning in info.warnings:
            job_service.add_warning(job, warning)
        job_service.set_progress(
            db, job, phase="probing", percent=8.0, message="Video metadata read."
        )
        job_service.commit_checkpoint(db, job, CHECKPOINT_PROBED)
    _record_source_progress(
        context,
        "detecting",
        10.0,
        "Metadata read. Detecting shot boundaries.",
    )


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
        _record_source_progress(
            context,
            "detecting",
            10.0 + 50.0 * fraction,
            f"Scanning video frames · {round(fraction * 100)}% complete.",
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
                source_id=context.source_id,
                shot_number=safe_shot.shot_number,
                source_start_us=safe_shot.source.start_us,
                source_end_us=safe_shot.source.end_us,
                safe_start_us=(
                    safe_shot.safe.start_us
                    if safe_shot.safe
                    else safe_shot.source.start_us
                ),
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
                    source_id=context.source_id,
                    detected_shot_id=detected.id,
                    shot_number=safe_shot.shot_number,
                    source_name=(context.source_label or {}).get("text"),
                    source_file_name=context.source_file_name,
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
        source_row = db.get(JobSource, context.source_id)
        if source_row is not None:
            source_row.detected_count = len(safe_shots)
            source_row.eligible_count = eligible
            source_row.checkpoint = CHECKPOINT_DETECTED
        job_service.set_progress(
            db,
            job,
            phase="detecting",
            percent=45.0,
            message=f"Found {eligible} usable shot(s) of {len(safe_shots)} detected.",
        )
        job_service.commit_checkpoint(db, job, CHECKPOINT_DETECTED)

    _record_source_progress(
        context,
        "measuring",
        62.0,
        f"Found {eligible} usable shot(s) of {len(safe_shots)} detected.",
    )

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
        _record_source_progress(
            context,
            "ranking",
            82.0,
            "No usable shots; source preparation complete.",
        )
        return

    info = context.info
    assert info is not None
    raws: dict[str, feature_module.RawFeatures] = {}
    _record_source_progress(
        context,
        "measuring",
        62.0,
        f"Measuring quality for {len(candidate_specs)} candidate(s).",
    )

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

        completed = index + 1
        if completed == 1 or completed % 5 == 0 or completed == len(candidate_specs):
            _record_source_progress(
                context,
                "measuring",
                65.0 + 15.0 * (completed / max(1, len(candidate_specs))),
                f"Measuring candidate {completed} of {len(candidate_specs)}.",
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
        source_row = db.get(JobSource, context.source_id)
        if source_row is not None:
            source_row.checkpoint = CHECKPOINT_PREPARED
    _record_source_progress(context, "ranking", 82.0, "Local measurements complete.")


# --- Stage F through J -----------------------------------------------------


def _stage_rank(
    context: _Context, cancelled, beat
) -> None:  # noqa: ANN001, PLR0912, PLR0915
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

    if not job_snapshot.ranking_enabled:
        _record_source_progress(
            context,
            "ranking",
            84.0,
            "Organizing candidates for manual review.",
        )
        _finish_manual_ranking(context, candidates, cancelled, beat)
        return

    if cancelled():
        raise AnalysisCancelled()

    _record_source_progress(context, "ranking", 84.0, "Ranking candidate clips.")

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
    _record_source_progress(
        context,
        "ranking",
        93.0,
        f"Ranked {len(candidates)} candidate(s).",
    )

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
        source_row = db.get(JobSource, context.source_id)
        if source_row is not None:
            source_row.checkpoint = CHECKPOINT_RANKED
            source_row.state = "processing"
            source_row.progress_phase = "previews"
            source_row.progress_percent = max(source_row.progress_percent, 95.0)
            source_row.progress_message = "Rendering review previews."
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


def _finish_manual_ranking(
    context: _Context,
    candidates: list[CandidateShot],
    cancelled: Callable[[], bool],
    beat: Callable[[], None],
) -> None:
    """Publish candidates in source order without scoring or preselection."""
    if cancelled():
        raise AnalysisCancelled()
    beat()
    ordered_ids = [
        candidate.id
        for candidate in sorted(candidates, key=lambda item: item.source_start_us)
    ]
    with session_scope() as db:
        job = db.get(Job, context.job_id)
        assert job is not None
        rows = {
            candidate.id: candidate
            for candidate in db.execute(
                select(CandidateShot).where(CandidateShot.job_id == job.id)
            ).scalars()
        }
        for rank, candidate_id in enumerate(ordered_ids, start=1):
            candidate = rows[candidate_id]
            candidate.rank = rank
            candidate.score = 0.0
            candidate.confidence = 0.0
            candidate.reason = ""
            candidate.reason_code = None
            candidate.scoring_source = "local-fallback"
            candidate.prompt_relevance_evaluated = False

        review.begin_manual_review(db, job)
        job_service.commit_checkpoint(db, job, CHECKPOINT_RANKED)
        source_row = db.get(JobSource, context.source_id)
        if source_row is not None:
            source_row.checkpoint = CHECKPOINT_RANKED
            source_row.state = "ready"
            source_row.progress_phase = "ready"
            source_row.progress_percent = 100.0
            source_row.progress_message = "Analysis complete."
        job_service.set_progress(
            db,
            job,
            phase="ranking",
            percent=100.0,
            message=f"Prepared {len(ordered_ids)} candidate(s) for manual review.",
        )
        job_service.transition(db, job, "review-ready", message="Ready for manual review.")
        events.append(
            db,
            job.id,
            events.CANDIDATES_READY,
            {
                "eligibleCount": job.eligible_count,
                "selectedCount": 0,
                "reviewRevision": job.review_revision,
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
    targets = list(targets)
    _record_source_progress(
        context,
        "previews",
        95.0,
        f"Rendering {len(targets)} review preview(s)." if targets else "Finalizing review.",
    )
    preview_dir = storage.ensure_dir(
        storage.job_subdir(context.job_id, "previews"), context.settings
    )
    for index, (candidate_id, start_us, end_us) in enumerate(targets, start=1):
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
        _record_source_progress(
            context,
            "previews",
            95.0 + 5.0 * (index / max(1, len(targets))),
            f"Rendered preview {index} of {len(targets)}.",
        )
    _record_source_progress(context, "ready", 100.0, "Analysis complete.")


def ensure_preview(job_id: str, candidate_id: str, settings: Settings) -> Path:
    """Render a review preview on demand (used by the preview route)."""
    with session_scope() as db:
        candidate = db.get(CandidateShot, candidate_id)
        if candidate is None or candidate.job_id != job_id:
            raise FileNotFoundError(candidate_id)
        job = db.get(Job, job_id)
        source_row = db.get(JobSource, candidate.source_id)
        upload = db.get(Upload, source_row.upload_id) if source_row else None
        if job is None or source_row is None or upload is None:
            raise FileNotFoundError(candidate_id)
        source = storage.resolve(upload.relative_source_path, settings)
        start_us, end_us = candidate.recommended_start_us, candidate.recommended_end_us
        source_label = (
            {"text": source_row.source_name, "style": source_row.source_label_style}
            if source_row.source_name and source_row.source_label_style
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
