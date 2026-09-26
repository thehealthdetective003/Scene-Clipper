"""Local gradual-transition classification (spec 6.2).

Threshold detection alone does not cover arbitrary cross-dissolves, so this
module classifies the per-frame content-score series directly:

1. The neighbourhood median is taken over a two-second window around the frame,
   excluding the central half-second on each side.
2. At least three consecutive elevated frames -- score at or above
   ``max(8, 1.5 x neighbourhood median)`` -- form a gradual-transition run. One
   below-threshold frame is tolerated inside a run.
3. A one- or two-frame peak is a hard-cut event.
4. A dissolve is vetoed when a single global affine transform explains the
   motion (>= 70% inlier features and median motion-compensated luma difference
   below 12), which means coherent camera motion, not a transition.
5. A gradual-transition search is capped at two seconds; beyond that the
   two-second region centred on the highest score is retained conservatively.
6. A fade event expands until the frame-luma change settles.

Everything here is pure: frame-dependent work (the affine veto) is injected as
a callback, so the classifier is unit-testable against synthetic score series.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from app.media.timebase import Interval, frame_start_us

HARD_CUT = "hard-cut"
FADE = "fade"
DISSOLVE = "dissolve"

#: Absolute floor for "elevated", so a quiet neighbourhood cannot make noise
#: look like a transition (spec 6.2 step 2).
MIN_ELEVATED_SCORE = 8.0
ELEVATION_FACTOR = 1.5

#: A run must reach this length to be gradual rather than a hard cut.
MIN_GRADUAL_RUN_FRAMES = 3
#: Below-threshold frames tolerated inside one run.
MAX_RUN_GAP_FRAMES = 1

#: Gradual-transition search cap (spec 6.2 step 5).
MAX_GRADUAL_SECONDS = 2.0
#: Fade expansion cap in each direction (spec 6.2 step 6).
MAX_FADE_SEARCH_SECONDS = 3.0
#: Fade settles when the luma change stays below this for FADE_STABLE_FRAMES.
FADE_STABLE_LUMA_DELTA = 2.0
FADE_STABLE_SLOPE = 1.0
FADE_STABLE_FRAMES = 5

#: Dissolve veto thresholds (spec 6.2 step 4).
AFFINE_INLIER_RATIO = 0.70
AFFINE_RESIDUAL_LUMA = 12.0
#: Motion compensation must actually explain the change before it can veto a
#: dissolve. Without this, a cross-dissolve between two near-static images
#: trivially satisfies the other two conditions -- an identity transform "fits"
#: every feature, and one blend step is a small per-frame difference -- so every
#: dissolve would be dismissed as camera motion. Requiring the compensated
#: difference to be materially smaller than the raw one distinguishes "the
#: camera moved" from "the picture changed underneath a still camera".
AFFINE_EXPLAINED_RATIO = 0.6

#: Events this far apart (or closer) are merged (spec 6.2).
MERGE_TOLERANCE_FRAMES = 3


@dataclass(frozen=True, slots=True)
class Run:
    """A contiguous stretch of elevated content scores, inclusive of both ends."""

    start_frame: int
    end_frame: int
    peak_frame: int
    peak_score: float

    @property
    def length_frames(self) -> int:
        return self.end_frame - self.start_frame + 1


@dataclass(slots=True)
class TransitionEvent:
    """A transition expressed as an interval, never a single unsafe frame."""

    start_frame: int
    #: Exclusive, matching the half-open interval convention.
    end_frame: int
    kinds: list[str] = field(default_factory=list)
    peak_score: float = 0.0

    def to_interval(self, rate: Fraction) -> Interval:
        return Interval(
            frame_start_us(self.start_frame, rate, round_up=False),
            frame_start_us(self.end_frame, rate, round_up=True),
        )

    def to_json(self, rate: Fraction) -> dict[str, object]:
        interval = self.to_interval(rate)
        return {
            "startUs": interval.start_us,
            "endUs": interval.end_us,
            "kinds": sorted(set(self.kinds)),
        }


def frames_for_seconds(seconds: float, rate: Fraction) -> int:
    return max(1, int(round(float(rate) * seconds)))


def neighborhood_median(scores: Sequence[float], index: int, rate: Fraction) -> float:
    """Median of the two-second window around ``index``, centre half-second excluded."""
    one_second = frames_for_seconds(1.0, rate)
    half_second = frames_for_seconds(0.5, rate)

    left = scores[max(0, index - one_second) : max(0, index - half_second)]
    right = scores[
        min(len(scores), index + half_second + 1) : min(len(scores), index + one_second + 1)
    ]
    sample = list(left) + list(right)
    if not sample:
        # Too close to an edge for a proper neighbourhood: fall back to the
        # whole series so the elevation test still has a baseline.
        sample = [value for i, value in enumerate(scores) if i != index] or [0.0]
    return float(statistics.median(sample))


def elevation_threshold(scores: Sequence[float], index: int, rate: Fraction) -> float:
    return max(MIN_ELEVATED_SCORE, ELEVATION_FACTOR * neighborhood_median(scores, index, rate))


def find_runs(scores: Sequence[float], rate: Fraction) -> list[Run]:
    """Group elevated frames into runs, tolerating one internal gap each."""
    runs: list[Run] = []
    index = 0
    total = len(scores)

    while index < total:
        if scores[index] < elevation_threshold(scores, index, rate):
            index += 1
            continue

        start = index
        end = index
        gaps_used = 0
        cursor = index + 1
        while cursor < total:
            if scores[cursor] >= elevation_threshold(scores, cursor, rate):
                end = cursor
                cursor += 1
                continue
            # A single dip is tolerated, but only if the run continues after it.
            if (
                gaps_used < MAX_RUN_GAP_FRAMES
                and cursor + 1 < total
                and scores[cursor + 1] >= elevation_threshold(scores, cursor + 1, rate)
            ):
                gaps_used += 1
                end = cursor + 1
                cursor += 2
                continue
            break

        window = scores[start : end + 1]
        peak_offset = max(range(len(window)), key=lambda i: window[i])
        runs.append(
            Run(
                start_frame=start,
                end_frame=end,
                peak_frame=start + peak_offset,
                peak_score=float(window[peak_offset]),
            )
        )
        index = end + 1

    return runs


def cap_gradual_run(run: Run, rate: Fraction) -> tuple[int, int]:
    """Bound a gradual run to two seconds, centred on its peak when too long."""
    cap = frames_for_seconds(MAX_GRADUAL_SECONDS, rate)
    if run.length_frames <= cap:
        return run.start_frame, run.end_frame + 1

    half = cap // 2
    start = max(0, run.peak_frame - half)
    return start, start + cap


def expand_fade(
    luma: Sequence[float], run: Run, rate: Fraction
) -> tuple[int, int]:
    """Grow a fade until the luma change settles on both sides (spec 6.2 step 6)."""
    limit = frames_for_seconds(MAX_FADE_SEARCH_SECONDS, rate)

    start = run.start_frame
    lower_bound = max(0, run.start_frame - limit)
    while start > lower_bound and not _luma_settled(luma, start, -1):
        start -= 1

    end = run.end_frame
    upper_bound = min(len(luma) - 1, run.end_frame + limit)
    while end < upper_bound and not _luma_settled(luma, end, 1):
        end += 1

    return start, end + 1


def _luma_settled(luma: Sequence[float], index: int, direction: int) -> bool:
    """True when five consecutive frames are flat in luma level and slope."""
    positions = [index + direction * step for step in range(FADE_STABLE_FRAMES + 1)]
    if any(position < 0 or position >= len(luma) for position in positions):
        return False

    values = [float(luma[position]) for position in positions]
    deltas = [abs(values[i + 1] - values[i]) for i in range(len(values) - 1)]
    if statistics.median(deltas) >= FADE_STABLE_LUMA_DELTA:
        return False

    ordered = values[::-1] if direction < 0 else values
    return abs(_linear_slope(ordered)) < FADE_STABLE_SLOPE


def _linear_slope(values: Sequence[float]) -> float:
    """Least-squares slope in luma levels per frame."""
    count = len(values)
    if count < 2:
        return 0.0
    mean_x = (count - 1) / 2.0
    mean_y = sum(values) / count
    numerator = sum((i - mean_x) * (value - mean_y) for i, value in enumerate(values))
    denominator = sum((i - mean_x) ** 2 for i in range(count))
    return numerator / denominator if denominator else 0.0


def is_fade_run(luma: Sequence[float], run: Run, *, dark_level: float = 16.0) -> bool:
    """A fade reaches (or leaves) near-black inside the run."""
    window = [luma[i] for i in range(run.start_frame, min(run.end_frame + 1, len(luma)))]
    if not window:
        return False
    return min(window) <= dark_level


def classify(
    scores: Sequence[float],
    luma: Sequence[float],
    rate: Fraction,
    *,
    threshold_fade_frames: Sequence[int] = (),
    motion_veto: Callable[[int, int], bool] | None = None,
) -> list[TransitionEvent]:
    """Turn the content-score series into labelled transition events.

    ``threshold_fade_frames`` carries fade frames reported by the local
    threshold detector; ``motion_veto(start, end)`` returns ``True`` when a
    global affine transform explains the run as camera motion, which cancels a
    dissolve classification (spec 6.2 step 4).
    """
    events: list[TransitionEvent] = []
    fade_frames = set(threshold_fade_frames)

    for run in find_runs(scores, rate):
        touches_fade = any(
            run.start_frame <= frame <= run.end_frame for frame in fade_frames
        )

        if run.length_frames < MIN_GRADUAL_RUN_FRAMES and not touches_fade:
            # A one- or two-frame peak is a hard cut (spec 6.2 step 3).
            events.append(
                TransitionEvent(
                    start_frame=run.start_frame,
                    end_frame=run.end_frame + 1,
                    kinds=[HARD_CUT],
                    peak_score=run.peak_score,
                )
            )
            continue

        if touches_fade or is_fade_run(luma, run):
            start, end = expand_fade(luma, run, rate)
            events.append(
                TransitionEvent(
                    start_frame=start, end_frame=end, kinds=[FADE], peak_score=run.peak_score
                )
            )
            continue

        start, end = cap_gradual_run(run, rate)
        if motion_veto is not None and motion_veto(start, end):
            # Coherent camera motion, not a transition: no boundary is emitted,
            # which keeps pans and zooms inside a single eligible shot.
            continue
        events.append(
            TransitionEvent(
                start_frame=start, end_frame=end, kinds=[DISSOLVE], peak_score=run.peak_score
            )
        )

    # Fades the threshold detector found outside any elevated run still count.
    for frame in sorted(fade_frames):
        if any(event.start_frame <= frame < event.end_frame for event in events):
            continue
        run = Run(start_frame=frame, end_frame=frame, peak_frame=frame, peak_score=0.0)
        start, end = expand_fade(luma, run, rate)
        events.append(TransitionEvent(start_frame=start, end_frame=end, kinds=[FADE]))

    return merge_events(events)


def merge_events(
    events: list[TransitionEvent], *, tolerance_frames: int = MERGE_TOLERANCE_FRAMES
) -> list[TransitionEvent]:
    """Union overlapping events, or events within the frame tolerance.

    All contributing detector labels are retained (spec 6.2).
    """
    if not events:
        return []

    ordered = sorted(events, key=lambda event: (event.start_frame, event.end_frame))
    merged = [
        TransitionEvent(
            start_frame=ordered[0].start_frame,
            end_frame=ordered[0].end_frame,
            kinds=list(ordered[0].kinds),
            peak_score=ordered[0].peak_score,
        )
    ]

    for event in ordered[1:]:
        last = merged[-1]
        if event.start_frame - last.end_frame <= tolerance_frames:
            last.end_frame = max(last.end_frame, event.end_frame)
            last.kinds = sorted(set(last.kinds) | set(event.kinds))
            last.peak_score = max(last.peak_score, event.peak_score)
        else:
            merged.append(
                TransitionEvent(
                    start_frame=event.start_frame,
                    end_frame=event.end_frame,
                    kinds=list(event.kinds),
                    peak_score=event.peak_score,
                )
            )
    return merged


def affine_motion_veto(
    inlier_ratio: float,
    median_residual_luma: float,
    median_raw_luma: float | None = None,
) -> bool:
    """Decision rule for step 4, split out so it can be tested directly.

    ``median_raw_luma`` is the uncompensated interframe difference. When it is
    supplied, the veto additionally requires that motion compensation actually
    reduced the difference -- see :data:`AFFINE_EXPLAINED_RATIO`.
    """
    if inlier_ratio < AFFINE_INLIER_RATIO:
        return False
    if median_residual_luma >= AFFINE_RESIDUAL_LUMA:
        return False
    if median_raw_luma is None:
        return True
    # Both zero means nothing changed at all, which is not a transition either.
    return median_residual_luma <= AFFINE_EXPLAINED_RATIO * median_raw_luma


def shots_from_transitions(
    events: Sequence[TransitionEvent], total_frames: int, rate: Fraction
) -> list[Interval]:
    """Derive shot intervals as the gaps between transition intervals."""
    if total_frames <= 0:
        return []

    boundaries = sorted(events, key=lambda event: event.start_frame)
    shots: list[Interval] = []
    cursor = 0

    for event in boundaries:
        start = max(0, min(event.start_frame, total_frames))
        if start > cursor:
            shots.append(
                Interval(
                    frame_start_us(cursor, rate, round_up=True),
                    frame_start_us(start, rate, round_up=False),
                )
            )
        cursor = max(cursor, min(event.end_frame, total_frames))

    if cursor < total_frames:
        shots.append(
            Interval(
                frame_start_us(cursor, rate, round_up=True),
                frame_start_us(total_frames, rate, round_up=False),
            )
        )

    return [shot for shot in shots if shot.duration_us > 0]
