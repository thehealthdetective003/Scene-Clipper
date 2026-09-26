"""Stages F through J: provider-assisted ranking with local fallback.

Guiding invariant (spec 1.5): a Gemini failure or cap exhaustion MUST NOT
discard completed local analysis. Every path through this module ends with a
fully ranked candidate set -- the only difference is whether a candidate's
score came from a contact sheet, a proxy video, or local measurements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session as DbSession

from app.analysis import budget as budget_module
from app.analysis import cache as cache_module
from app.analysis import contact_sheets, features, intervals, scoring
from app.analysis.budget import STAGE_COARSE, STAGE_FINE, STAGE_PROXY, Allocation
from app.config import Settings
from app.logging_setup import get_logger
from app.media.clips import render_proxy
from app.media.frames import extract_gray_frames, extract_jpeg, representative_frame_count, uniform_positions
from app.media.probe import MediaInfo
from app.media.timebase import LONG_SHOT_WINDOW_US, Interval
from app.models import CandidateShot, Job, ProviderFile, utcnow
from app.providers.base import (
    ContactSheet,
    ProviderAuthError,
    ProviderError,
    ProviderInvalidResponse,
    ProviderQuotaError,
    ProviderTransientError,
    ProxyClip,
    RemoteFile,
    WindowStoryboard,
)
from app.providers.gemini import sleep_backoff
from app.security.crypto import DecryptionError
from app.services import storage

logger = get_logger("app.analysis.ranking")

#: Stage H window generation limits (spec 6.8).
MAX_FINE_WINDOWS = 24
FINE_WINDOW_STEP_US = 1_000_000

#: Stage I trigger thresholds (spec 6.9).
PROXY_CONFIDENCE_THRESHOLD = 0.55
PROXY_RANK_MARGIN = 5
PROXY_PADDING_US = 2_000_000
PROXY_START_OPTION_STEP_US = 500_000

FALLBACK_REASONS = {
    "no_key": "No usable Gemini key was configured, so ranking used local scores.",
    "disabled": "Gemini was disabled for this job, so ranking used local scores.",
    "cap_zero": "The request cap is 0, so ranking used local scores.",
    "budget": "The request cap was reached, so remaining candidates used local scores.",
    "auth": "The Gemini key was rejected, so ranking fell back to local scores.",
    "quota": "Gemini was over quota, so ranking fell back to local scores.",
    "unavailable": "Gemini was unavailable, so ranking fell back to local scores.",
    "invalid": "Gemini returned an unusable response, so ranking fell back to local scores.",
    "all_keys_unavailable": (
        "Every configured Gemini key is exhausted or invalid, so ranking used local scores."
    ),
}


def shot_key(candidate: CandidateShot) -> str:
    """A candidate identity that is stable across jobs.

    Candidate ids are per-job UUIDs, so they cannot key a cache that is shared
    between jobs over the same source. The detector configuration version is
    part of the cache key, which means the same source yields the same shots --
    so the shot number and its safe bounds identify a candidate reliably.
    """
    return f"{candidate.shot_number}:{candidate.safe_start_us}:{candidate.safe_end_us}"


@dataclass(slots=True)
class RankingOutcome:
    scored: dict[str, scoring.ScoredCandidate]
    fine_windows: dict[str, Interval] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    partial_result_reason: str | None = None
    fallback_reason: str | None = None
    cache_status: str = cache_module.CACHE_NONE
    unsent_candidate_ids: list[str] = field(default_factory=list)


class RankingRunner:
    """Owns one job's provider interaction, budget, and cache use."""

    def __init__(
        self,
        *,
        db_factory: Callable[[], Any],
        settings: Settings,
        provider: Any,
        job_id: str,
        source: Path,
        info: MediaInfo,
        should_cancel: Callable[[], bool],
    ) -> None:
        self._db_factory = db_factory
        self._settings = settings
        self._provider = provider
        self._job_id = job_id
        self._source = source
        self._info = info
        self._should_cancel = should_cancel

    # --- entry point ------------------------------------------------------

    def run(
        self,
        job: Job,
        candidates: list[CandidateShot],
        feature_map: dict[str, features.Features],
    ) -> RankingOutcome:
        outcome = RankingOutcome(scored={})
        if not candidates:
            return outcome

        has_focus_prompt = bool(job.content_prompt_normalized)
        local_scores = {
            candidate.id: feature_map[candidate.id].local_score
            if candidate.id in feature_map
            else candidate.local_score
            for candidate in candidates
        }

        # Everything starts as a local-only result. Provider results overwrite
        # entries as they arrive, so any failure leaves local analysis intact.
        for candidate in candidates:
            outcome.scored[candidate.id] = scoring.score_local_candidate(
                candidate_id=candidate.id,
                source_start_us=candidate.source_start_us,
                local=local_scores[candidate.id],
            )

        if not job.use_gemini:
            outcome.fallback_reason = FALLBACK_REASONS["disabled"]
            return outcome
        if job.gemini_request_cap <= 0:
            outcome.fallback_reason = FALLBACK_REASONS["cap_zero"]
            return outcome

        identity = cache_module.identity_for_job(job)
        long_shots = [c for c in candidates if c.is_long_shot]
        allocation = budget_module.allocate(
            job.gemini_request_cap, has_long_shots=bool(long_shots)
        )

        # --- Stage F: coarse ranking (cache first) ------------------------
        coarse_payload = self._load_cache(identity, cache_module.STAGE_COARSE)
        coarse_hit = coarse_payload is not None

        if coarse_hit:
            self._apply_coarse_payload(
                outcome, coarse_payload, candidates, local_scores, has_focus_prompt
            )
        else:
            self._run_coarse(
                job,
                outcome,
                candidates,
                local_scores,
                allocation,
                identity,
                has_focus_prompt=has_focus_prompt,
            )

        # --- Stage H: fine selection for long shots ------------------------
        fine_required = bool(long_shots)
        fine_hit = False
        if fine_required:
            fine_payload = self._load_cache(identity, cache_module.STAGE_FINE)
            if fine_payload is not None:
                fine_hit = True
                self._apply_fine_payload(outcome, fine_payload, long_shots)
            else:
                self._run_fine(job, outcome, candidates, long_shots, allocation, identity)

        # --- Stage I: rare proxy-video fallback ---------------------------
        if not coarse_hit:
            self._run_proxy(job, outcome, candidates, allocation, has_focus_prompt)

        outcome.cache_status = cache_module.combined_status(coarse_hit, fine_hit, fine_required)
        return outcome

    # --- Stage F ----------------------------------------------------------

    def _run_coarse(
        self,
        job: Job,
        outcome: RankingOutcome,
        candidates: list[CandidateShot],
        local_scores: dict[str, float],
        allocation: Allocation,
        identity: cache_module.CacheIdentity,
        *,
        has_focus_prompt: bool,
    ) -> None:
        sheet_dir = storage.ensure_dir(
            storage.job_subdir(self._job_id, "contact-sheets"), self._settings
        )
        # How many candidates the coarse allocation can actually cover.
        capacity = allocation.coarse * contact_sheets.MAX_CANDIDATES_PER_REQUEST
        selected_ids, deferred_ids = scoring.select_for_coarse(
            [(c.id, local_scores[c.id], c.source_start_us) for c in candidates], capacity
        )
        if deferred_ids:
            outcome.unsent_candidate_ids = deferred_ids
            outcome.warnings.append("coarse_capacity_limited")
            outcome.partial_result_reason = (
                f"{len(deferred_ids)} candidate(s) were ranked locally because the "
                "request cap did not cover them; prompt relevance was not evaluated for them."
            )

        selected = [c for c in candidates if c.id in set(selected_ids)]
        if not selected:
            outcome.fallback_reason = FALLBACK_REASONS["budget"]
            return

        entries = self._build_sheet_entries(selected, sheet_dir)
        sheet_paths: list[Path] = []
        for index, chunk in enumerate(contact_sheets.chunk_candidates(entries)):
            if self._should_cancel():
                return
            path = sheet_dir / f"sheet-{index:03d}.jpg"
            contact_sheets.render_sheet(chunk, path)
            sheet_paths.append(path)

        sheet_by_path = {
            path: [entry.candidate_id for entry in chunk]
            for path, chunk in zip(
                sheet_paths, contact_sheets.chunk_candidates(entries)
            )
        }
        batches = contact_sheets.pack_sheets_into_requests(
            sheet_paths, payload_limit_bytes=self._settings.gemini_inline_payload_limit_bytes
        )

        collected: list[dict[str, Any]] = []
        for batch in batches:
            if self._should_cancel():
                return
            sheets = [
                ContactSheet(
                    path=path,
                    candidate_ids=sheet_by_path[path],
                    bytes_size=path.stat().st_size,
                )
                for path in batch
            ]
            expected = {cid for sheet in sheets for cid in sheet.candidate_ids}

            response = self._call_provider(
                stage=STAGE_COARSE,
                stage_limit=allocation.coarse,
                cap=job.gemini_request_cap,
                outcome=outcome,
                call=lambda key, sheets=sheets: self._provider.rank_contact_sheets(
                    api_key=key,
                    model=job.gemini_model,
                    sheets=sheets,
                    focus_prompt=job.content_prompt_normalized,
                ),
                validate=lambda resp, expected=expected: _validate_coarse(resp, expected),
                image_bytes=sum(sheet.bytes_size for sheet in sheets),
            )
            if response is None:
                continue

            for result in response.coarse:
                candidate = next((c for c in selected if c.id == result.candidate_id), None)
                if candidate is None:
                    continue
                outcome.scored[candidate.id] = scoring.score_api_candidate(
                    candidate_id=candidate.id,
                    source_start_us=candidate.source_start_us,
                    local=local_scores[candidate.id],
                    relevance=result.relevance,
                    interest=result.interest,
                    clarity=result.clarity,
                    confidence=result.confidence,
                    reason=result.reason,
                    reason_code=result.reason_code,
                    has_focus_prompt=has_focus_prompt,
                    motion_ambiguous=result.motion_ambiguous,
                    human_present=result.human_present,
                    product_visible=result.product_visible,
                    product_prominence=result.product_prominence,
                )
                collected.append(
                    {
                        # Keyed by a cross-job-stable identity, not the per-job id.
                        "shotKey": shot_key(candidate),
                        "relevance": result.relevance,
                        "interest": result.interest,
                        "clarity": result.clarity,
                        "confidence": result.confidence,
                        "motionAmbiguous": result.motion_ambiguous,
                        "reasonCode": result.reason_code,
                        "reason": result.reason,
                        "humanPresent": result.human_present,
                        "productVisible": result.product_visible,
                        "productProminence": result.product_prominence,
                    }
                )

        if collected:
            self._store_cache(
                identity,
                cache_module.STAGE_COARSE,
                {"results": collected, "hasFocusPrompt": has_focus_prompt},
            )

    def _build_sheet_entries(
        self, candidates: list[CandidateShot], sheet_dir: Path
    ) -> list[contact_sheets.SheetEntry]:
        frames_dir = sheet_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        entries: list[contact_sheets.SheetEntry] = []
        for candidate in candidates:
            safe = Interval(candidate.safe_start_us, candidate.safe_end_us)
            count = representative_frame_count(safe.duration_us)
            # Samples stay inside the safe interval, away from the guards.
            positions = uniform_positions(safe.start_us, safe.end_us, count)
            paths: list[Path] = []
            for index, position in enumerate(positions):
                destination = frames_dir / f"{candidate.id}-{index}.jpg"
                if not destination.exists():
                    extract_jpeg(
                        self._source,
                        destination,
                        position_us=position,
                        width=480,
                        quality=contact_sheets.JPEG_QUALITY,
                        settings=self._settings,
                        should_cancel=self._should_cancel,
                    )
                paths.append(destination)
            entries.append(
                contact_sheets.SheetEntry(
                    candidate_id=candidate.id,
                    start_us=candidate.safe_start_us,
                    end_us=candidate.safe_end_us,
                    frame_paths=paths,
                )
            )
        return entries

    def _apply_coarse_payload(
        self,
        outcome: RankingOutcome,
        payload: dict[str, Any],
        candidates: list[CandidateShot],
        local_scores: dict[str, float],
        has_focus_prompt: bool,
    ) -> None:
        by_shot = {shot_key(candidate): candidate for candidate in candidates}
        for entry in payload.get("results", []):
            candidate = by_shot.get(entry.get("shotKey"))
            if candidate is None:
                continue
            outcome.scored[candidate.id] = scoring.score_api_candidate(
                candidate_id=candidate.id,
                source_start_us=candidate.source_start_us,
                local=local_scores[candidate.id],
                relevance=entry.get("relevance"),
                interest=int(entry.get("interest", 0)),
                clarity=int(entry.get("clarity", 0)),
                confidence=float(entry.get("confidence", 0.0)),
                reason=str(entry.get("reason", "")),
                reason_code=entry.get("reasonCode"),
                has_focus_prompt=has_focus_prompt,
                motion_ambiguous=bool(entry.get("motionAmbiguous", False)),
                human_present=entry.get("humanPresent"),
                product_visible=entry.get("productVisible"),
                product_prominence=entry.get("productProminence"),
                # Cache status is orthogonal to scoring source: this result
                # still originated from a contact sheet (spec 6.10).
                scoring_source=scoring.SCORING_CONTACT_SHEET,
                cache_status=cache_module.CACHE_COMPLETE,
            )

    # --- Stage H ----------------------------------------------------------

    def _run_fine(
        self,
        job: Job,
        outcome: RankingOutcome,
        candidates: list[CandidateShot],
        long_shots: list[CandidateShot],
        allocation: Allocation,
        identity: cache_module.CacheIdentity,
    ) -> None:
        shortlist = self._fine_shortlist(job, outcome, candidates, long_shots)
        if not shortlist:
            return

        board_dir = storage.ensure_dir(
            storage.job_subdir(self._job_id, "contact-sheets"), self._settings
        )
        storyboards: list[WindowStoryboard] = []
        window_index: dict[str, dict[str, Interval]] = {}

        for candidate in shortlist:
            if self._should_cancel():
                return
            windows = self._score_windows(candidate)
            if not windows:
                continue
            rows: list[tuple[str, int, int, list[Path]]] = []
            window_index[candidate.id] = {}
            for order, (window, _score) in enumerate(windows):
                window_id = f"w{order:02d}"
                window_index[candidate.id][window_id] = window
                frame_paths = []
                for frame_order, position in enumerate(
                    uniform_positions(window.start_us, window.end_us, 3)
                ):
                    destination = (
                        board_dir / "frames" / f"{candidate.id}-{window_id}-{frame_order}.jpg"
                    )
                    if not destination.exists():
                        extract_jpeg(
                            self._source,
                            destination,
                            position_us=position,
                            width=420,
                            settings=self._settings,
                            should_cancel=self._should_cancel,
                        )
                    frame_paths.append(destination)
                rows.append((window_id, window.start_us, window.end_us, frame_paths))

            board_path = board_dir / f"windows-{candidate.id}.jpg"
            contact_sheets.render_window_storyboard(candidate.id, rows, board_path)
            storyboards.append(
                WindowStoryboard(
                    path=board_path,
                    candidate_id=candidate.id,
                    window_ids=list(window_index[candidate.id]),
                    bytes_size=board_path.stat().st_size,
                )
            )

        # Local best window is the answer whenever the provider cannot be used.
        for candidate in shortlist:
            if candidate.id not in outcome.fine_windows:
                best = self._best_local_window(candidate)
                if best is not None:
                    outcome.fine_windows[candidate.id] = best

        if not storyboards:
            return

        expected = {board.candidate_id: set(board.window_ids) for board in storyboards}
        response = self._call_provider(
            stage=STAGE_FINE,
            stage_limit=max(allocation.fine, 0),
            cap=job.gemini_request_cap,
            outcome=outcome,
            call=lambda key: self._provider.choose_windows(
                api_key=key,
                model=job.gemini_model,
                storyboards=storyboards,
                focus_prompt=job.content_prompt_normalized,
            ),
            validate=lambda resp: _validate_fine(resp, expected),
            image_bytes=sum(board.bytes_size for board in storyboards),
        )
        if response is None:
            return

        cached: list[dict[str, Any]] = []
        for choice in response.fine:
            window = window_index.get(choice.candidate_id, {}).get(choice.window_id)
            if window is None:
                continue
            outcome.fine_windows[choice.candidate_id] = window
            scored = outcome.scored.get(choice.candidate_id)
            if scored is not None and choice.reason:
                scored.reason = choice.reason
            cached_candidate = next(
                (c for c in shortlist if c.id == choice.candidate_id), None
            )
            if cached_candidate is not None:
                cached.append(
                    {
                        "shotKey": shot_key(cached_candidate),
                        "startUs": window.start_us,
                        "endUs": window.end_us,
                        "confidence": choice.confidence,
                        "reason": choice.reason,
                    }
                )

        if cached:
            self._store_cache(identity, cache_module.STAGE_FINE, {"choices": cached})

    def _fine_shortlist(
        self,
        job: Job,
        outcome: RankingOutcome,
        candidates: list[CandidateShot],
        long_shots: list[CandidateShot],
    ) -> list[CandidateShot]:
        """Take the shortlist from the provisional ranking, then keep long shots."""
        size = scoring.fine_shortlist_size(job.target_clip_count)
        provisional = scoring.rank(list(outcome.scored.values()))[:size]
        shortlisted_ids = {item.candidate_id for item in provisional}
        long_shot_ids = {candidate.id for candidate in long_shots}
        return [
            candidate
            for candidate in candidates
            if candidate.id in shortlisted_ids and candidate.id in long_shot_ids
        ]

    def _score_windows(self, candidate: CandidateShot) -> list[tuple[Interval, float]]:
        """Generate, locally score, and prune maximum-length windows (spec 6.8)."""
        safe = Interval(candidate.safe_start_us, candidate.safe_end_us)
        windows = intervals.long_shot_windows(
            safe, self._info.frame_rate, step_us=FINE_WINDOW_STEP_US
        )
        scored: list[tuple[Interval, float]] = []
        for window in windows:
            frames = extract_gray_frames(
                self._source,
                start_us=window.start_us,
                duration_us=window.duration_us,
                source_width=self._info.display_width,
                source_height=self._info.display_height,
                settings=self._settings,
                should_cancel=self._should_cancel,
            )
            scored.append((window, features.score_frames(frames)))

        if len(scored) <= MAX_FINE_WINDOWS:
            return scored

        # Half by highest local score, half evenly spaced across the shot.
        half = MAX_FINE_WINDOWS // 2
        by_score = sorted(scored, key=lambda item: -item[1])[:half]
        chosen = {item[0].start_us for item in by_score}

        step = len(scored) / (MAX_FINE_WINDOWS - half)
        spaced: list[tuple[Interval, float]] = []
        for index in range(MAX_FINE_WINDOWS - half):
            position = min(len(scored) - 1, int(round(index * step)))
            item = scored[position]
            if item[0].start_us not in chosen:
                chosen.add(item[0].start_us)
                spaced.append(item)

        merged = by_score + spaced
        return sorted(merged, key=lambda item: item[0].start_us)[:MAX_FINE_WINDOWS]

    def _best_local_window(self, candidate: CandidateShot) -> Interval | None:
        windows = self._score_windows(candidate)
        if not windows:
            return None
        return max(windows, key=lambda item: item[1])[0]

    # --- Stage I ----------------------------------------------------------

    def _run_proxy(
        self,
        job: Job,
        outcome: RankingOutcome,
        candidates: list[CandidateShot],
        allocation: Allocation,
        has_focus_prompt: bool,
    ) -> None:
        """Upload at most five short proxies when motion remains ambiguous."""
        if allocation.proxy <= 0:
            return

        ranked = scoring.rank(list(outcome.scored.values()))
        eligible_ids = {
            item.candidate_id
            for item in ranked[: job.target_clip_count + PROXY_RANK_MARGIN]
        }
        shortlist = [
            item
            for item in ranked
            if item.candidate_id in eligible_ids
            and item.scoring_source == scoring.SCORING_CONTACT_SHEET
            and (item.motion_ambiguous or item.confidence < PROXY_CONFIDENCE_THRESHOLD)
        ]
        if not shortlist:
            return

        # Lowest confidence first, then highest provisional rank.
        order = {item.candidate_id: index for index, item in enumerate(ranked)}
        shortlist.sort(key=lambda item: (item.confidence, order[item.candidate_id]))
        shortlist = shortlist[: budget_module.MAX_PROXY_CANDIDATES]

        by_id = {candidate.id: candidate for candidate in candidates}
        proxy_dir = storage.ensure_dir(
            storage.job_subdir(self._job_id, "previews"), self._settings
        )

        clips: list[ProxyClip] = []
        option_index: dict[str, dict[str, int]] = {}
        for item in shortlist:
            candidate = by_id.get(item.candidate_id)
            if candidate is None or self._should_cancel():
                continue
            window = outcome.fine_windows.get(
                candidate.id,
                Interval(candidate.recommended_start_us, candidate.recommended_end_us),
            )
            safe = Interval(candidate.safe_start_us, candidate.safe_end_us)
            # Up to two seconds of padding, still inside the same shot.
            excerpt = Interval(
                max(safe.start_us, window.start_us - PROXY_PADDING_US),
                min(safe.end_us, window.end_us + PROXY_PADDING_US),
            )
            path = proxy_dir / f"proxy-{candidate.id}.mp4"
            storage.require_free_space(64 * 1024 * 1024, self._settings)
            render_proxy(
                self._source,
                path,
                info=self._info,
                start_us=excerpt.start_us,
                end_us=excerpt.end_us,
                settings=self._settings,
                should_cancel=self._should_cancel,
            )

            options: dict[str, int] = {}
            cursor = excerpt.start_us
            while cursor + LONG_SHOT_WINDOW_US <= excerpt.end_us:
                options[f"s{len(options):02d}"] = cursor
                cursor += PROXY_START_OPTION_STEP_US
            if not options:
                options["s00"] = excerpt.start_us
            option_index[candidate.id] = options

            clips.append(
                ProxyClip(
                    path=path,
                    candidate_id=candidate.id,
                    start_option_ids=list(options),
                    bytes_size=path.stat().st_size,
                    duration_seconds=excerpt.duration_us / 1_000_000,
                )
            )

        if not clips:
            return

        selection = self._select_key(set())
        if selection is None:
            return
        _key_id, _label, api_key = selection

        remote_files: dict[str, RemoteFile] = {}
        try:
            for clip in clips:
                # The remote identifier is persisted *before* upload so a crash
                # still leaves a cleanup record behind (spec 7.3).
                with self._db_factory() as db:
                    record = ProviderFile(
                        job_id=self._job_id,
                        candidate_id=clip.candidate_id,
                        purpose="proxy-video",
                        cleanup_status="pending",
                    )
                    db.add(record)
                    db.flush()
                    record_id = record.id

                remote = self._provider.upload_file(
                    api_key=api_key, path=clip.path, mime_type="video/mp4"
                )
                remote_files[clip.candidate_id] = remote
                with self._db_factory() as db:
                    stored = db.get(ProviderFile, record_id)
                    if stored is not None:
                        stored.provider_name = remote.name
                    budget_module.record_provider_file_operation(db, self._job_id)

            expected = {clip.candidate_id: set(clip.start_option_ids) for clip in clips}
            response = self._call_provider(
                stage=STAGE_PROXY,
                stage_limit=max(allocation.proxy, 0),
                cap=job.gemini_request_cap,
                outcome=outcome,
                call=lambda key: self._provider.review_proxies(
                    api_key=key,
                    model=job.gemini_model,
                    clips=clips,
                    remote_files=remote_files,
                    focus_prompt=job.content_prompt_normalized,
                ),
                validate=lambda resp: _validate_proxy(resp, expected),
                video_bytes=sum(clip.bytes_size for clip in clips),
                video_seconds=sum(clip.duration_seconds for clip in clips),
            )

            with self._db_factory() as db:
                usage = budget_module
                usage.record_payload(db, self._job_id)
                from app.models import AnalysisUsage

                row = db.get(AnalysisUsage, self._job_id)
                if row is not None:
                    row.proxy_video_candidates = len(clips)
                    db.flush()

            if response is not None:
                for choice in response.proxy:
                    candidate = by_id.get(choice.candidate_id)
                    options = option_index.get(choice.candidate_id, {})
                    start_us = options.get(choice.start_option_id)
                    if candidate is None or start_us is None:
                        continue
                    safe = Interval(candidate.safe_start_us, candidate.safe_end_us)
                    outcome.fine_windows[candidate.id] = intervals.clamp_window(
                        safe, start_us, self._info.frame_rate
                    )
                    scored = outcome.scored.get(candidate.id)
                    if scored is not None:
                        scored.scoring_source = scoring.SCORING_PROXY_VIDEO
                        scored.confidence = max(scored.confidence, choice.confidence)
                        if choice.reason:
                            scored.reason = choice.reason
        finally:
            self._delete_remote_files(api_key, remote_files)

    def _delete_remote_files(self, api_key: str, remote_files: dict[str, RemoteFile]) -> None:
        """Delete remote proxies immediately, on success or failure alike."""
        for remote in remote_files.values():
            try:
                self._provider.delete_file(api_key=api_key, name=remote.name)
                status, deleted = "deleted", utcnow()
            except ProviderError:
                logger.warning("provider file cleanup deferred")
                status, deleted = "pending", None
            with self._db_factory() as db:
                from sqlalchemy import select

                row = db.execute(
                    select(ProviderFile).where(
                        ProviderFile.job_id == self._job_id,
                        ProviderFile.provider_name == remote.name,
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.cleanup_status = status
                    row.deleted_at = deleted
                    row.cleanup_attempts += 1
                budget_module.record_provider_file_operation(db, self._job_id)

    # --- Provider plumbing ------------------------------------------------

    def _call_provider(
        self,
        *,
        stage: str,
        stage_limit: int,
        cap: int,
        outcome: RankingOutcome,
        call: Callable[[str], Any],
        validate: Callable[[Any], None],
        image_bytes: int = 0,
        video_bytes: int = 0,
        video_seconds: float = 0.0,
    ) -> Any | None:
        """Reserve, transmit, settle -- failing over across the key pool.

        Two loops are in play and they count differently:

        * a *retry* is the same request sent again after a transient fault, and
          spec 6.7 says it consumes another budget unit;
        * a *failover* is the same request sent on the next key after this one
          was rejected or exhausted. That is also a real outbound request, so it
          too consumes a unit -- the cap stays honest either way.

        A key that fails is marked immediately (quota -> ``exhausted``, auth ->
        ``invalid``), which is what turns it red in Settings.
        """
        attempts = 0
        tried_keys: set[str] = set()

        while attempts < 2:
            is_retry = attempts > 0

            selection = self._select_key(tried_keys)
            if selection is None:
                # Nothing usable left: either no key is configured at all, or
                # every one is exhausted/invalid right now.
                outcome.fallback_reason = FALLBACK_REASONS[
                    "no_key" if not tried_keys else "all_keys_unavailable"
                ]
                outcome.warnings.append(
                    "gemini_key_not_configured" if not tried_keys else "all_gemini_keys_unavailable"
                )
                return None
            key_id, key_label, api_key = selection

            try:
                with self._db_factory() as db:
                    attempt = budget_module.reserve(
                        db, self._job_id, stage, cap=cap, stage_limit=stage_limit, is_retry=is_retry
                    )
                    attempt_id = attempt.id
            except budget_module.BudgetExhausted:
                outcome.fallback_reason = FALLBACK_REASONS["budget"]
                outcome.warnings.append("request_cap_reached")
                return None

            try:
                response = call(api_key)
                validate(response)
            except (ProviderAuthError, ProviderQuotaError) as exc:
                # This credential is done for now. Record why, then try the
                # next key without burning a retry slot -- the request itself
                # has not been answered yet.
                self._settle(attempt_id, success=False, error_code=exc.code)
                self._mark_key_failure(key_id, exc.code)
                tried_keys.add(key_id)
                logger.info(
                    "gemini key failed over",
                    extra={
                        "job_id": self._job_id,
                        "context": {"key": key_label, "reason": exc.code},
                    },
                )
                outcome.warnings.append(
                    "gemini_key_rejected"
                    if isinstance(exc, ProviderAuthError)
                    else "gemini_key_exhausted"
                )
                continue
            except (ProviderTransientError, ProviderInvalidResponse) as exc:
                # A timeout, a 5xx, or malformed output says nothing about this
                # credential, so its health is left alone and the one bounded
                # retry (spec 6.7) goes to the same key. Failing over here
                # would burn through the whole pool on a provider outage and
                # paint every key red for something none of them did.
                self._settle(attempt_id, success=False, error_code=exc.code)
                attempts += 1
                if attempts >= 2:
                    outcome.fallback_reason = FALLBACK_REASONS[
                        "invalid" if isinstance(exc, ProviderInvalidResponse) else "unavailable"
                    ]
                    outcome.warnings.append("gemini_fallback")
                    return None
                sleep_backoff(attempts)
                continue
            except ProviderError as exc:
                # A rejected request (any other 4xx) is about what was sent,
                # not about who sent it -- no key is blamed.
                self._settle(attempt_id, success=False, error_code=exc.code)
                outcome.fallback_reason = FALLBACK_REASONS["unavailable"]
                outcome.warnings.append("gemini_fallback")
                return None

            self._settle(attempt_id, success=True)
            self._mark_key_success(key_id)
            with self._db_factory() as db:
                usage = getattr(response, "usage", None)
                budget_module.record_payload(
                    db,
                    self._job_id,
                    image_bytes=image_bytes,
                    video_bytes=video_bytes,
                    video_seconds=video_seconds,
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                    total_tokens=getattr(usage, "total_tokens", None),
                )
            return response

        return None

    def _select_key(self, tried: set[str]) -> tuple[str, str, str] | None:
        """Pick the next usable credential and decrypt it for one call.

        Returns ``(key_id, label, plaintext)``. The plaintext is handed straight
        to the provider adapter and is never stored on the runner (spec 5.2).
        """
        from app.services import settings_service

        with self._db_factory() as db:
            row = settings_service.next_key(db, skip_ids=tried)
            while row is not None:
                try:
                    return row.id, row.label, settings_service.reveal_key(
                        db, self._settings, row
                    )
                except DecryptionError:
                    # Ciphertext that will not authenticate is unusable; flag it
                    # so the operator sees which credential is broken.
                    settings_service.mark_failure(db, row.id, "provider_auth_failed")
                    tried.add(row.id)
                    row = settings_service.next_key(db, skip_ids=tried)
        return None

    def _mark_key_success(self, key_id: str) -> None:
        from app.services import settings_service

        with self._db_factory() as db:
            settings_service.mark_success(db, key_id)

    def _mark_key_failure(self, key_id: str, error_code: str) -> None:
        from app.services import settings_service

        with self._db_factory() as db:
            settings_service.mark_failure(db, key_id, error_code)

    def _settle(self, attempt_id: str, *, success: bool, error_code: str | None = None) -> None:
        from app.models import AnalysisAttempt

        with self._db_factory() as db:
            attempt = db.get(AnalysisAttempt, attempt_id)
            if attempt is not None:
                budget_module.settle(db, attempt, success=success, error_code=error_code)

    def _load_cache(self, identity: cache_module.CacheIdentity, stage: str) -> dict[str, Any] | None:
        with self._db_factory() as db:
            return cache_module.load(db, identity, stage)

    def _store_cache(
        self, identity: cache_module.CacheIdentity, stage: str, payload: dict[str, Any]
    ) -> None:
        with self._db_factory() as db:
            cache_module.store(db, identity, stage, payload)

    def _apply_fine_payload(
        self, outcome: RankingOutcome, payload: dict[str, Any], long_shots: list[CandidateShot]
    ) -> None:
        by_shot = {shot_key(candidate): candidate for candidate in long_shots}
        for entry in payload.get("choices", []):
            candidate = by_shot.get(entry.get("shotKey"))
            if candidate is None:
                continue
            start_us = int(entry.get("startUs", 0))
            end_us = int(entry.get("endUs", 0))
            safe = Interval(candidate.safe_start_us, candidate.safe_end_us)
            window = Interval(start_us, end_us)
            # A cached window is re-validated against the current safe interval.
            if safe.contains(window):
                outcome.fine_windows[candidate.id] = window


# --- Response validation ---------------------------------------------------


def _validate_coarse(response: Any, expected: set[str]) -> None:
    """Exactly one result per supplied candidate; no unknown ids (spec 6.6)."""
    returned = {result.candidate_id for result in response.coarse}
    unknown = returned - expected
    missing = expected - returned
    if unknown:
        raise ProviderInvalidResponse("The response referenced unknown candidate ids.")
    if missing:
        raise ProviderInvalidResponse("The response omitted candidates that were supplied.")


def _validate_fine(response: Any, expected: dict[str, set[str]]) -> None:
    """Exactly one valid supplied window id for every requested long shot."""
    returned = {choice.candidate_id for choice in response.fine}
    if returned != set(expected):
        raise ProviderInvalidResponse("The response did not answer every requested long shot.")
    for choice in response.fine:
        if choice.window_id not in expected[choice.candidate_id]:
            raise ProviderInvalidResponse("The response returned a window id that was not offered.")


def _validate_proxy(response: Any, expected: dict[str, set[str]]) -> None:
    for choice in response.proxy:
        allowed = expected.get(choice.candidate_id)
        if allowed is None:
            raise ProviderInvalidResponse("The response referenced an unknown candidate id.")
        if choice.start_option_id not in allowed:
            raise ProviderInvalidResponse(
                "The response returned a start option that was not offered."
            )
