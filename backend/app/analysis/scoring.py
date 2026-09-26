"""Stage F scoring and final ranking (spec 6.6).

    with focus prompt:    geminiScore = 0.50 x relevance + 0.30 x interest + 0.20 x clarity
    without focus prompt: geminiScore = 0.60 x interest  + 0.40 x clarity

    API-scored candidate: finalScore = 0.85 x geminiScore + 0.15 x localScore
    local-only candidate: finalScore = localScore

Sorting is by final score descending, confidence descending, then source start
ascending. The no-cut rule is a separate eligibility gate and plays no part in
this score. API-scored candidates are *not* automatically ranked above stronger
local candidates -- one comparison applies to every candidate.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Confidence assigned to a candidate that was never sent to the provider.
LOCAL_FALLBACK_CONFIDENCE = 0.35

SCORING_CONTACT_SHEET = "contact-sheet"
SCORING_PROXY_VIDEO = "proxy-video"
SCORING_LOCAL_FALLBACK = "local-fallback"

#: Why a candidate is held back from automatic selection when the job names a
#: product. Both are advisory: the candidate stays in review with the reason
#: shown, so the editor can still pick it by hand.
EXCLUDED_HUMAN_PRESENT = "human_present"
EXCLUDED_PRODUCT_ABSENT = "product_absent"

#: Where a human-presence verdict came from.
HUMAN_SOURCE_MODEL = "model"
HUMAN_SOURCE_LOCAL = "local"


@dataclass(slots=True)
class ScoredCandidate:
    """Everything the ranking sort needs, independent of the ORM."""

    candidate_id: str
    source_start_us: int
    local_score: float
    final_score: float
    confidence: float
    reason: str
    reason_code: str | None
    scoring_source: str
    prompt_relevance_evaluated: bool
    cache_status: str = "none"
    motion_ambiguous: bool = False
    relevance: int | None = None
    interest: int | None = None
    clarity: int | None = None
    # --- product focus ---------------------------------------------------
    #: Tri-state: ``None`` means nobody assessed it, which is not the same as
    #: "no person is present".
    human_present: bool | None = None
    human_source: str | None = None
    product_visible: bool | None = None
    product_prominence: int | None = None
    excluded_reason: str | None = None

    @property
    def is_excluded(self) -> bool:
        return self.excluded_reason is not None


def product_exclusion(
    *, human_present: bool | None, product_visible: bool | None
) -> str | None:
    """Whether a candidate fails the product-only brief, and why.

    A person in frame disqualifies a shot outright -- that is the stricter of
    the two rules, so it is reported first. An unassessed signal (``None``)
    never excludes: the pipeline says so explicitly with a warning rather than
    silently dropping shots it did not actually look at.
    """
    if human_present is True:
        return EXCLUDED_HUMAN_PRESENT
    if product_visible is False:
        return EXCLUDED_PRODUCT_ABSENT
    return None


def gemini_score(
    *, relevance: int | None, interest: int, clarity: int, has_focus_prompt: bool
) -> float:
    if has_focus_prompt:
        if relevance is None:
            # A prompt was supplied but the model returned no relevance; treat
            # the candidate as unevaluated for relevance rather than inventing
            # a value, and fall back to the no-prompt weighting.
            return 0.60 * interest + 0.40 * clarity
        return 0.50 * relevance + 0.30 * interest + 0.20 * clarity
    return 0.60 * interest + 0.40 * clarity


def final_score(*, gemini: float | None, local: float) -> float:
    if gemini is None:
        return local
    return 0.85 * gemini + 0.15 * local


def score_api_candidate(
    *,
    candidate_id: str,
    source_start_us: int,
    local: float,
    relevance: int | None,
    interest: int,
    clarity: int,
    confidence: float,
    reason: str,
    reason_code: str | None,
    has_focus_prompt: bool,
    motion_ambiguous: bool = False,
    scoring_source: str = SCORING_CONTACT_SHEET,
    cache_status: str = "none",
    human_present: bool | None = None,
    product_visible: bool | None = None,
    product_prominence: int | None = None,
) -> ScoredCandidate:
    gemini = gemini_score(
        relevance=relevance,
        interest=interest,
        clarity=clarity,
        has_focus_prompt=has_focus_prompt,
    )
    return ScoredCandidate(
        candidate_id=candidate_id,
        source_start_us=source_start_us,
        local_score=local,
        final_score=final_score(gemini=gemini, local=local),
        confidence=confidence,
        reason=reason,
        reason_code=reason_code,
        scoring_source=scoring_source,
        prompt_relevance_evaluated=bool(has_focus_prompt and relevance is not None),
        cache_status=cache_status,
        motion_ambiguous=motion_ambiguous,
        relevance=relevance,
        interest=interest,
        clarity=clarity,
        human_present=human_present,
        human_source=HUMAN_SOURCE_MODEL if human_present is not None else None,
        product_visible=product_visible,
        product_prominence=product_prominence,
        excluded_reason=product_exclusion(
            human_present=human_present, product_visible=product_visible
        ),
    )


def score_local_candidate(
    *,
    candidate_id: str,
    source_start_us: int,
    local: float,
    reason: str = "Ranked locally; prompt relevance was not evaluated.",
    cache_status: str = "none",
) -> ScoredCandidate:
    """A local-only candidate: confidence 0.35, prompt relevance unevaluated."""
    return ScoredCandidate(
        candidate_id=candidate_id,
        source_start_us=source_start_us,
        local_score=local,
        final_score=final_score(gemini=None, local=local),
        confidence=LOCAL_FALLBACK_CONFIDENCE,
        reason=reason,
        reason_code=None,
        scoring_source=SCORING_LOCAL_FALLBACK,
        prompt_relevance_evaluated=False,
        cache_status=cache_status,
    )


def rank(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """One global ordering, applied regardless of scoring source (spec 6.7).

    Candidates that fail the product-only brief sort after every candidate that
    passes it, whatever they score. They keep their relative order among
    themselves and are not removed: the editor still sees them in review, with
    the reason attached, and can select one by hand.
    """
    return sorted(
        candidates,
        key=lambda item: (
            item.excluded_reason is not None,
            -item.final_score,
            -item.confidence,
            item.source_start_us,
        ),
    )


def select_for_coarse(
    candidates: list[tuple[str, float, int]], capacity: int
) -> tuple[list[str], list[str]]:
    """Choose which candidates to send when capacity is short (spec 6.7).

    ``candidates`` is ``(candidateId, localScore, sourceStartUs)``. 75% of the
    slots go to the highest local scores and 25% to stratified coverage of the
    source timeline, so a strong-but-clustered opening does not crowd out the
    rest of the video. Returns ``(selected, deferred)``; deferred candidates are
    retained as local-fallback rather than deleted.
    """
    if capacity <= 0:
        return [], [candidate_id for candidate_id, _, _ in candidates]
    if capacity >= len(candidates):
        return [candidate_id for candidate_id, _, _ in candidates], []

    by_score = sorted(candidates, key=lambda item: (-item[1], item[2]))
    score_slots = int(capacity * 0.75)
    selected: list[str] = [candidate_id for candidate_id, _, _ in by_score[:score_slots]]
    chosen = set(selected)

    coverage_slots = capacity - len(selected)
    if coverage_slots > 0:
        remaining = sorted(
            (item for item in candidates if item[0] not in chosen), key=lambda item: item[2]
        )
        if remaining:
            # Stratify: one pick from each equal slice of the source timeline.
            step = len(remaining) / coverage_slots
            for index in range(coverage_slots):
                position = min(len(remaining) - 1, int(round(index * step)))
                candidate_id = remaining[position][0]
                if candidate_id not in chosen:
                    chosen.add(candidate_id)
                    selected.append(candidate_id)

        # Stratified picks can collide; top up from the best unselected.
        for candidate_id, _, _ in by_score:
            if len(selected) >= capacity:
                break
            if candidate_id not in chosen:
                chosen.add(candidate_id)
                selected.append(candidate_id)

    deferred = [candidate_id for candidate_id, _, _ in candidates if candidate_id not in chosen]
    return selected, deferred


def fine_shortlist_size(target_clip_count: int) -> int:
    """``min(max(target x 2, 40), 200)`` (spec 6.8)."""
    return min(max(target_clip_count * 2, 40), 200)
