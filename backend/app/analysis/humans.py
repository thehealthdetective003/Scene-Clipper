"""Local human-presence detection for product-only selection.

When a job names a product, shots containing a person are held back. Gemini
answers that question for every shot it actually ranks, but some shots never
reach it -- the cap runs short, every key is exhausted, or Gemini is off for
the job. This module answers it locally for those shots.

It is deliberately a *screen*, not a classifier: OpenCV's stock HOG pedestrian
detector plus the bundled Haar face cascades catch the obvious cases (a person
in frame, a face, an upper body) and miss the subtle ones (a single finger at
the edge of frame, a reflection). It therefore only ever reports a positive it
is reasonably sure of, and the pipeline reports what it could not verify rather
than implying a shot was cleared. Detected people are real; undetected ones may
still be there.
"""

from __future__ import annotations

import functools
from pathlib import Path

import cv2
import numpy as np

from app.config import Settings
from app.logging_setup import get_logger
from app.media.frames import extract_gray_frames

logger = get_logger("app.analysis.humans")

#: Frames sampled per candidate. Three is enough to catch a person who walks
#: through, without turning a screening pass into a second analysis stage.
SAMPLE_FRAMES = 3

#: Width the detectors run at. HOG's window is 64x128, so a wider frame finds
#: smaller people at proportionally more cost.
DETECT_WIDTH = 480

#: HOG hit threshold. The stock detector is noisy at 0.0; raising it trades a
#: few distant pedestrians for far fewer false alarms on furniture and text.
HOG_HIT_THRESHOLD = 0.6

#: A Haar hit needs corroboration from several scales before it is believed.
HAAR_MIN_NEIGHBORS = 6

#: Below this the frame carries too little detail for either detector to mean
#: anything, so no verdict is reported.
MIN_DETECT_PIXELS = 64


class HumanScreenResult:
    """What the local screen concluded about one candidate."""

    __slots__ = ("present", "assessed", "detector")

    def __init__(self, *, present: bool, assessed: bool, detector: str | None = None) -> None:
        #: True only when a detector fired. Never a claim that nobody is there.
        self.present = present
        #: False when no frame could be examined, which is reported, not hidden.
        self.assessed = assessed
        #: Which detector fired, for the audit trail.
        self.detector = detector

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"HumanScreenResult(present={self.present}, "
            f"assessed={self.assessed}, detector={self.detector!r})"
        )


@functools.lru_cache(maxsize=1)
def _hog() -> cv2.HOGDescriptor:
    descriptor = cv2.HOGDescriptor()
    descriptor.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    return descriptor


@functools.lru_cache(maxsize=1)
def _cascades() -> tuple[cv2.CascadeClassifier, ...]:
    """Frontal face, profile face, and upper body.

    Loaded once; a cascade that fails to load is skipped rather than raising,
    because a missing data file must degrade the screen, not fail the job.
    """
    names = (
        "haarcascade_frontalface_default.xml",
        "haarcascade_profileface.xml",
        "haarcascade_upperbody.xml",
    )
    loaded: list[cv2.CascadeClassifier] = []
    for name in names:
        path = Path(cv2.data.haarcascades) / name
        classifier = cv2.CascadeClassifier(str(path))
        if classifier.empty():
            logger.warning("haar cascade unavailable", extra={"context": {"cascade": name}})
            continue
        loaded.append(classifier)
    return tuple(loaded)


def _prepare(frame: np.ndarray) -> np.ndarray | None:
    """Scale to the detection width and equalize, or ``None`` if unusable."""
    if frame is None or frame.ndim != 2 or min(frame.shape) < 8:
        return None
    height, width = frame.shape
    if width != DETECT_WIDTH:
        scale = DETECT_WIDTH / width
        frame = cv2.resize(
            frame,
            (DETECT_WIDTH, max(8, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
        )
    if min(frame.shape) < MIN_DETECT_PIXELS:
        return None
    # Equalization recovers people in under- and over-exposed frames, which is
    # exactly where the stock detectors are weakest.
    return cv2.equalizeHist(frame)


def detect_in_frame(frame: np.ndarray) -> str | None:
    """The name of the first detector to fire on one grayscale frame."""
    prepared = _prepare(frame)
    if prepared is None:
        return None

    try:
        rects, weights = _hog().detectMultiScale(
            prepared, winStride=(8, 8), padding=(8, 8), scale=1.05
        )
    except cv2.error:  # pragma: no cover - defensive
        rects, weights = (), ()
    for weight in np.asarray(weights).ravel().tolist():
        if weight >= HOG_HIT_THRESHOLD:
            return "hog_person"

    for index, cascade in enumerate(_cascades()):
        try:
            hits = cascade.detectMultiScale(
                prepared, scaleFactor=1.1, minNeighbors=HAAR_MIN_NEIGHBORS, minSize=(24, 24)
            )
        except cv2.error:  # pragma: no cover - defensive
            continue
        if len(hits) > 0:
            return ("haar_frontal_face", "haar_profile_face", "haar_upper_body")[index]

    return None


def screen_candidate(
    source: Path,
    *,
    start_us: int,
    end_us: int,
    source_width: int,
    source_height: int,
    settings: Settings,
    should_cancel=None,  # noqa: ANN001
) -> HumanScreenResult:
    """Sample a candidate's interval and report whether a person was found."""
    duration_us = end_us - start_us
    if duration_us <= 0:
        return HumanScreenResult(present=False, assessed=False)

    # Spread the samples across the whole candidate rather than taking the
    # first few: a person who walks into frame late must still be caught.
    sample_fps = SAMPLE_FRAMES / (duration_us / 1_000_000)

    try:
        frames = extract_gray_frames(
            source,
            start_us=start_us,
            duration_us=duration_us,
            source_width=source_width,
            source_height=source_height,
            fps=sample_fps,
            max_frames=SAMPLE_FRAMES,
            target_width=DETECT_WIDTH,
            settings=settings,
            should_cancel=should_cancel,
        )
    except Exception as exc:  # noqa: BLE001 - a screen must never fail a job
        logger.warning(
            "human screen could not read frames",
            extra={"context": {"error": type(exc).__name__}},
        )
        return HumanScreenResult(present=False, assessed=False)

    if not frames:
        return HumanScreenResult(present=False, assessed=False)

    for frame in frames[:SAMPLE_FRAMES]:
        detector = detect_in_frame(frame)
        if detector is not None:
            return HumanScreenResult(present=True, assessed=True, detector=detector)

    # Every sampled frame came back clean. That is a weak signal, not a
    # guarantee -- `assessed` says the screen ran, not that the shot is clear.
    return HumanScreenResult(present=False, assessed=True)
