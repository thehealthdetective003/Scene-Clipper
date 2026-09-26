"""Stage B: local transition detection (spec 6.2).

Detection is entirely local. No LLM or video-language model participates in
shot-boundary decisions (spec 1). The detector sits behind
:class:`TransitionDetector` so a different local detector can be added later
without changing job or candidate schemas.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from app.analysis import transitions as tx
from app.config import Settings
from app.logging_setup import get_logger
from app.media.probe import MediaInfo
from app.media.timebase import Interval, frame_start_us

logger = get_logger("app.analysis.detector")

#: Near-black threshold used to label a cut as a fade rather than a hard cut.
FADE_LUMA_LEVEL = 16.0

#: Feature-matching budget for the affine veto; ORB is fast and license-clean.
_ORB_FEATURES = 400
_MIN_MATCHES = 12


class DetectorCancelled(Exception):
    """Raised when cancellation was requested during detection."""


@dataclass(slots=True)
class DetectionResult:
    shots: list[Interval]
    events: list[tx.TransitionEvent]
    frame_rate: Fraction
    frame_count: int
    detector_config_version: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def boundary_intervals(self) -> list[Interval]:
        return [event.to_interval(self.frame_rate) for event in self.events]


class TransitionDetector(Protocol):
    def detect(
        self,
        source: Path,
        info: MediaInfo,
        *,
        settings: Settings,
        should_cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> DetectionResult: ...


class PySceneDetectDetector:
    """AdaptiveDetector + ThresholdDetector, plus the local gradual classifier.

    Detection runs at reduced spatial resolution but never skips temporal
    frames -- a skipped frame could hide a two-frame cut (spec 6.2).
    """

    #: Detection runs in bounded slices so cancellation, progress, and the
    #: worker heartbeat all get a turn without skipping any frame.
    SEGMENT_SECONDS = 2.0

    def detect(
        self,
        source: Path,
        info: MediaInfo,
        *,
        settings: Settings,
        should_cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> DetectionResult:
        from scenedetect import AdaptiveDetector, SceneManager, StatsManager, ThresholdDetector
        from scenedetect import open_video

        rate = info.frame_rate
        video = open_video(str(source), backend="opencv")

        stats = StatsManager()
        manager = SceneManager(stats_manager=stats)
        manager.auto_downscale = False
        manager.downscale = _downscale_factor(info.display_width, settings.detect_analysis_width)

        manager.add_detector(
            AdaptiveDetector(
                adaptive_threshold=settings.detect_adaptive_threshold,
                min_content_val=settings.detect_min_content_val,
                window_width=settings.detect_window_width,
                min_scene_len=settings.detect_min_scene_len_frames,
            )
        )
        manager.add_detector(
            ThresholdDetector(
                threshold=settings.detect_threshold_value,
                fade_bias=settings.detect_fade_bias,
                min_scene_len=settings.detect_min_scene_len_frames,
            )
        )

        expected_frames = max(1, int(round(float(rate) * info.duration_us / 1_000_000)))

        # Process in slices, continuing from the stream position each time. The
        # detector state lives in the SceneManager, so this is equivalent to one
        # pass -- frame_skip stays 0 and no frame is ever skipped (spec 6.2).
        position = 0
        while True:
            if should_cancel is not None and should_cancel():
                raise DetectorCancelled()

            before = position
            manager.detect_scenes(
                video=video,
                duration=self.SEGMENT_SECONDS,
                frame_skip=0,
                show_progress=False,
            )
            position = int(video.position.frame_num) if video.position else before
            if position <= before:
                break
            if on_progress is not None:
                on_progress(min(0.99, position / expected_frames))

        frame_count = max(position, 1)
        content = _content_series(stats, frame_count)
        luma = _luma_series(stats, frame_count)
        cut_frames = _cut_frames(manager)
        fade_frames = [frame for frame in cut_frames if _is_dark_near(luma, frame)]

        events = tx.classify(
            content,
            luma,
            rate,
            threshold_fade_frames=fade_frames,
            motion_veto=_make_motion_veto(source, settings, frame_count),
        )
        events = _attach_detector_cuts(events, cut_frames, fade_frames)

        shots = tx.shots_from_transitions(events, frame_count, rate)
        if on_progress is not None:
            on_progress(1.0)

        return DetectionResult(
            shots=shots,
            events=events,
            frame_rate=rate,
            frame_count=frame_count,
            detector_config_version=settings.detector_config_version,
            diagnostics={
                "analysedFrames": frame_count,
                "detectorCuts": len(cut_frames),
                "fadeCuts": len(fade_frames),
                "events": len(events),
                "downscale": manager.downscale,
            },
        )


# --- helpers ---------------------------------------------------------------


def _downscale_factor(width: int, target_width: int) -> int:
    if target_width <= 0 or width <= target_width:
        return 1
    return max(1, int(round(width / target_width)))


def _metric_series(
    stats_manager, frame_count: int, key: str, default: float  # noqa: ANN001
) -> list[float]:
    """Per-frame metric, carrying the previous value across any gap."""
    series: list[float] = []
    previous = default
    for frame_num in range(frame_count):
        value = previous
        try:
            if stats_manager.metrics_exist(frame_num, [key]):
                metrics = stats_manager.get_metrics(frame_num, [key])
                if metrics and metrics[0] is not None:
                    value = float(metrics[0])
        except Exception:  # noqa: BLE001 - a missing frame keeps the last value
            value = previous
        series.append(value)
        previous = value
    return series


def _content_series(stats_manager, frame_count: int) -> list[float]:  # noqa: ANN001
    """Per-frame content score. Frame 0 has no predecessor, so it reads zero."""
    # A gap in the content signal means "no change", hence a default of 0.0
    # rather than carrying a previous spike forward.
    series: list[float] = []
    for frame_num in range(frame_count):
        value = 0.0
        try:
            if stats_manager.metrics_exist(frame_num, ["content_val"]):
                metrics = stats_manager.get_metrics(frame_num, ["content_val"])
                if metrics and metrics[0] is not None:
                    value = float(metrics[0])
        except Exception:  # noqa: BLE001
            value = 0.0
        series.append(value)
    return series


def _luma_series(stats_manager, frame_count: int) -> list[float]:  # noqa: ANN001
    """Per-frame average frame intensity on the 8-bit scale.

    ``average_rgb`` is what ThresholdDetector itself uses to find fades, so
    deriving the fade signal from it keeps this classifier consistent with the
    detector the spec mandates.
    """
    return _metric_series(stats_manager, frame_count, "average_rgb", 128.0)


def _cut_frames(manager) -> list[int]:  # noqa: ANN001
    """Cut positions as frame numbers, from the detected scene list."""
    try:
        scenes = manager.get_scene_list()
    except Exception:  # noqa: BLE001
        return []
    cuts: list[int] = []
    for start, _end in scenes[1:]:
        try:
            cuts.append(int(start.frame_num))
        except AttributeError:  # pragma: no cover - defensive
            continue
    return cuts


def _is_dark_near(luma: Sequence[float], frame: int, radius: int = 2) -> bool:
    window = [
        luma[i] for i in range(max(0, frame - radius), min(len(luma), frame + radius + 1))
    ]
    return bool(window) and min(window) <= FADE_LUMA_LEVEL


def _attach_detector_cuts(
    events: list[tx.TransitionEvent], cut_frames: Sequence[int], fade_frames: Sequence[int]
) -> list[tx.TransitionEvent]:
    """Ensure every detector cut is represented, then re-merge."""
    fades = set(fade_frames)
    covered = {
        frame
        for frame in cut_frames
        if any(event.start_frame <= frame < event.end_frame for event in events)
    }
    for frame in cut_frames:
        if frame in covered:
            continue
        events.append(
            tx.TransitionEvent(
                start_frame=max(0, frame),
                end_frame=frame + 1,
                kinds=[tx.FADE if frame in fades else tx.HARD_CUT],
            )
        )
    return tx.merge_events(events)


def _make_motion_veto(
    source: Path, settings: Settings, frame_count: int
) -> Callable[[int, int], bool]:
    """Build the step-4 dissolve veto over a lazily opened capture."""

    def veto(start_frame: int, end_frame: int) -> bool:
        frames = _read_gray_frames(
            source, start_frame, min(end_frame, frame_count), settings.detect_analysis_width
        )
        if len(frames) < 2:
            return False
        ratios: list[float] = []
        residuals: list[float] = []
        raw_differences: list[float] = []
        for previous, current in zip(frames, frames[1:]):
            measurement = _affine_fit(previous, current)
            if measurement is None:
                continue
            ratio, residual, raw = measurement
            ratios.append(ratio)
            residuals.append(residual)
            raw_differences.append(raw)
        if not ratios:
            return False
        return tx.affine_motion_veto(
            statistics.median(ratios),
            statistics.median(residuals),
            statistics.median(raw_differences),
        )

    return veto


def _read_gray_frames(
    source: Path, start_frame: int, end_frame: int, target_width: int
) -> list[np.ndarray]:
    """Decode a bounded grayscale window for motion analysis."""
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return []
    frames: list[np.ndarray] = []
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, start_frame))
        # The run is capped at two seconds, so this stays bounded.
        for _ in range(max(0, end_frame - start_frame)):
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if target_width > 0 and gray.shape[1] > target_width:
                scale = target_width / gray.shape[1]
                gray = cv2.resize(
                    gray,
                    (target_width, max(1, int(round(gray.shape[0] * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            frames.append(gray)
    finally:
        capture.release()
    return frames


def _affine_fit(
    previous: np.ndarray, current: np.ndarray
) -> tuple[float, float, float] | None:
    """Return ``(inlier_ratio, compensated_luma_difference, raw_luma_difference)``."""
    orb = cv2.ORB_create(nfeatures=_ORB_FEATURES)
    keypoints_a, descriptors_a = orb.detectAndCompute(previous, None)
    keypoints_b, descriptors_b = orb.detectAndCompute(current, None)
    if descriptors_a is None or descriptors_b is None:
        return None
    if len(keypoints_a) < _MIN_MATCHES or len(keypoints_b) < _MIN_MATCHES:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = matcher.match(descriptors_a, descriptors_b)
    if len(matches) < _MIN_MATCHES:
        return None

    source_points = np.float32([keypoints_a[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    target_points = np.float32([keypoints_b[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)

    matrix, inliers = cv2.estimateAffinePartial2D(
        source_points, target_points, method=cv2.RANSAC, ransacReprojThreshold=3.0
    )
    if matrix is None or inliers is None:
        return None

    inlier_ratio = float(inliers.sum()) / float(len(matches))
    warped = cv2.warpAffine(
        previous, matrix, (current.shape[1], current.shape[0]), flags=cv2.INTER_LINEAR
    )
    compensated = float(np.median(cv2.absdiff(warped, current)))
    raw = float(np.median(cv2.absdiff(previous, current)))
    return inlier_ratio, compensated, raw


_detector: TransitionDetector | None = None


def get_detector() -> TransitionDetector:
    global _detector
    if _detector is None:
        _detector = PySceneDetectDetector()
    return _detector


def set_detector(detector: TransitionDetector | None) -> None:
    """Test hook: substitute a deterministic detector for synthetic fixtures."""
    global _detector
    _detector = detector
