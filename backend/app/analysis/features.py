"""Stage E: deterministic local feature preparation (spec 6.5).

    localScore = 0.30 x exposureScore
               + 0.25 x nonBlackScore
               + 0.20 x nonFreezeScore
               + 0.15 x focusScore
               + 0.10 x motionVariationScore

These measurements affect ordering only. **No candidate is ever rejected for
blur, shake, or motion** (spec 2.4): the continuity gate is the sole
eligibility rule.

``focusScore`` and ``motionVariationScore`` are normalized across all eligible
candidates in the job using the 5th and 95th percentiles, so they are computed
in two passes: :func:`measure_candidate` per candidate, then
:func:`normalize_job` once every candidate has been measured.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from app.config import Settings
from app.media.frames import ANALYSIS_FPS, MAX_ANALYSIS_FRAMES, extract_gray_frames
from app.versions import FEATURE_ALGORITHM_VERSION

#: Pixels at or below/above these luma levels count as clipped (spec 6.5).
CLIP_LOW = 8
CLIP_HIGH = 247
#: Exposure reaches zero once half the pixels are clipped.
MAX_CLIPPED_FRACTION = 0.50
#: A frame is "non-black" at or above this mean luma.
NON_BLACK_LUMA = 16.0
#: An adjacent pair is "non-frozen" at or above this compensated luma delta.
NON_FREEZE_DELTA = 1.5
#: Returned when a measurement has too little data to discriminate.
NEUTRAL_SCORE = 50.0

WEIGHTS = {
    "exposure": 0.30,
    "non_black": 0.25,
    "non_freeze": 0.20,
    "focus": 0.15,
    "motion_variation": 0.10,
}


@dataclass(frozen=True, slots=True)
class RawFeatures:
    """Per-candidate measurements, before job-level normalization."""

    sample_count: int
    exposure_score: float
    non_black_score: float
    non_freeze_score: float
    #: median log1p(varianceOfLaplacian) -- normalized later.
    focus_raw: float
    #: median motion-compensated interframe luma difference -- normalized later.
    motion_raw: float


@dataclass(frozen=True, slots=True)
class Features:
    """Final per-candidate feature vector and its deterministic local score."""

    sample_count: int
    exposure_score: float
    non_black_score: float
    non_freeze_score: float
    focus_score: float
    motion_variation_score: float
    focus_raw: float
    motion_raw: float
    local_score: float
    feature_version: str = FEATURE_ALGORITHM_VERSION

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def exposure_score(frames: list[np.ndarray]) -> float:
    """``100 x clamp(1 - meanClippedPixelFraction / 0.50, 0, 1)``."""
    if not frames:
        return NEUTRAL_SCORE
    fractions = []
    for frame in frames:
        clipped = np.count_nonzero((frame <= CLIP_LOW) | (frame >= CLIP_HIGH))
        fractions.append(clipped / frame.size)
    mean_clipped = float(statistics.fmean(fractions))
    return 100.0 * _clamp(1.0 - mean_clipped / MAX_CLIPPED_FRACTION)


def non_black_score(frames: list[np.ndarray]) -> float:
    """``100 x fractionOfFrames(meanLuma >= 16)``."""
    if not frames:
        return NEUTRAL_SCORE
    qualifying = sum(1 for frame in frames if float(frame.mean()) >= NON_BLACK_LUMA)
    return 100.0 * (qualifying / len(frames))


def _compensated_difference(previous: np.ndarray, current: np.ndarray) -> float:
    """Mean absolute luma difference after removing global camera motion.

    Compensation matters because a pan is not a freeze and must not be scored
    as one, nor counted as subject motion.
    """
    try:
        flow_matrix = _estimate_affine(previous, current)
        if flow_matrix is not None:
            warped = cv2.warpAffine(
                previous,
                flow_matrix,
                (current.shape[1], current.shape[0]),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            return float(np.abs(warped.astype(np.int16) - current.astype(np.int16)).mean())
    except cv2.error:
        pass
    return float(np.abs(previous.astype(np.int16) - current.astype(np.int16)).mean())


def _estimate_affine(previous: np.ndarray, current: np.ndarray) -> np.ndarray | None:
    features = cv2.goodFeaturesToTrack(
        previous, maxCorners=200, qualityLevel=0.01, minDistance=8
    )
    if features is None or len(features) < 6:
        return None
    tracked, statuses, _ = cv2.calcOpticalFlowPyrLK(previous, current, features, None)
    if tracked is None or statuses is None:
        return None
    mask = statuses.ravel() == 1
    if int(mask.sum()) < 6:
        return None
    matrix, _ = cv2.estimateAffinePartial2D(
        features[mask], tracked[mask], method=cv2.RANSAC, ransacReprojThreshold=3.0
    )
    return matrix


def non_freeze_score(differences: list[float]) -> float:
    """``100 x fractionOfAdjacentPairs(delta >= 1.5)``; 50 for a single sample."""
    if not differences:
        return NEUTRAL_SCORE
    qualifying = sum(1 for value in differences if value >= NON_FREEZE_DELTA)
    return 100.0 * (qualifying / len(differences))


def focus_raw(frames: list[np.ndarray]) -> float:
    """Median ``log1p(varianceOfLaplacian)`` across the samples."""
    if not frames:
        return 0.0
    values = [
        math.log1p(float(cv2.Laplacian(frame, cv2.CV_64F).var())) for frame in frames
    ]
    return float(statistics.median(values))


def measure_candidate(
    source: Path,
    *,
    start_us: int,
    end_us: int,
    source_width: int,
    source_height: int,
    settings: Settings,
    should_cancel=None,  # noqa: ANN001
) -> RawFeatures:
    """Decode the analysis samples for one candidate and measure them."""
    frames = extract_gray_frames(
        source,
        start_us=start_us,
        duration_us=end_us - start_us,
        source_width=source_width,
        source_height=source_height,
        fps=ANALYSIS_FPS,
        max_frames=MAX_ANALYSIS_FRAMES,
        settings=settings,
        should_cancel=should_cancel,
    )
    if not frames:
        return RawFeatures(
            sample_count=0,
            exposure_score=NEUTRAL_SCORE,
            non_black_score=NEUTRAL_SCORE,
            non_freeze_score=NEUTRAL_SCORE,
            focus_raw=0.0,
            motion_raw=0.0,
        )

    differences = [
        _compensated_difference(previous, current)
        for previous, current in zip(frames, frames[1:])
    ]
    return RawFeatures(
        sample_count=len(frames),
        exposure_score=exposure_score(frames),
        non_black_score=non_black_score(frames),
        non_freeze_score=non_freeze_score(differences),
        focus_raw=focus_raw(frames),
        motion_raw=float(statistics.median(differences)) if differences else 0.0,
    )


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile; stable for tiny samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def normalize_to_percentiles(value: float, low: float, high: float) -> float:
    """Map onto 0-100 using the job's 5th/95th percentiles (spec 6.5)."""
    if high <= low:
        # Every candidate measured the same: no ordering information.
        return NEUTRAL_SCORE
    return 100.0 * _clamp((value - low) / (high - low))


def local_score(
    *,
    exposure: float,
    non_black: float,
    non_freeze: float,
    focus: float,
    motion_variation: float,
) -> float:
    return (
        WEIGHTS["exposure"] * exposure
        + WEIGHTS["non_black"] * non_black
        + WEIGHTS["non_freeze"] * non_freeze
        + WEIGHTS["focus"] * focus
        + WEIGHTS["motion_variation"] * motion_variation
    )


def normalize_job(raws: dict[str, RawFeatures]) -> dict[str, Features]:
    """Second pass: normalize focus/motion across the job and score each candidate."""
    if not raws:
        return {}

    focus_values = [raw.focus_raw for raw in raws.values()]
    motion_values = [raw.motion_raw for raw in raws.values()]

    focus_low, focus_high = percentile(focus_values, 0.05), percentile(focus_values, 0.95)
    motion_low, motion_high = percentile(motion_values, 0.05), percentile(motion_values, 0.95)

    results: dict[str, Features] = {}
    for candidate_id, raw in raws.items():
        focus = normalize_to_percentiles(raw.focus_raw, focus_low, focus_high)
        motion = normalize_to_percentiles(raw.motion_raw, motion_low, motion_high)
        results[candidate_id] = Features(
            sample_count=raw.sample_count,
            exposure_score=round(raw.exposure_score, 4),
            non_black_score=round(raw.non_black_score, 4),
            non_freeze_score=round(raw.non_freeze_score, 4),
            focus_score=round(focus, 4),
            motion_variation_score=round(motion, 4),
            focus_raw=round(raw.focus_raw, 6),
            motion_raw=round(raw.motion_raw, 6),
            local_score=round(
                local_score(
                    exposure=raw.exposure_score,
                    non_black=raw.non_black_score,
                    non_freeze=raw.non_freeze_score,
                    focus=focus,
                    motion_variation=motion,
                ),
                4,
            ),
        )
    return results


def score_frames(frames: list[np.ndarray]) -> float:
    """Local score for an ad-hoc window (Stage H window ranking).

    Uses neutral normalization for focus and motion because a window is scored
    only against sibling windows of the same shot.
    """
    if not frames:
        return 0.0
    differences = [
        _compensated_difference(previous, current)
        for previous, current in zip(frames, frames[1:])
    ]
    raw_focus = focus_raw(frames)
    raw_motion = float(statistics.median(differences)) if differences else 0.0
    return local_score(
        exposure=exposure_score(frames),
        non_black=non_black_score(frames),
        non_freeze=non_freeze_score(differences),
        # log1p(varLaplacian) saturates around 12-14 for sharp footage; the
        # divisors map the useful range onto 0-100 without a job-wide pass.
        focus=100.0 * _clamp(raw_focus / 14.0),
        motion_variation=100.0 * _clamp(raw_motion / 24.0),
    )
