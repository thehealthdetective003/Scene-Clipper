"""Stage C and D: safe intervals and recommended clips (spec 6.3, 6.4).

Safe interval construction:

1. start from the source shot interval;
2. remove any detected fade/dissolve interval at its edges;
3. add an inward guard of two source frames after every *internal* transition
   interval, of any kind;
4. add no artificial guard at the start or end of the source unless a
   transition actually exists there;
5. quantize to valid source-frame timestamps;
6. derive the usable duration from those timestamps.

A shot whose usable duration falls below the minimum clip length is ineligible
with reason ``too_short_after_transition_guard``. It is never merged with a
neighbour to manufacture a longer clip.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from app.analysis.transitions import DISSOLVE, FADE, TransitionEvent
from app.media.timebase import (
    LONG_SHOT_WINDOW_US,
    MAX_CLIP_US,
    MIN_CLIP_US,
    Interval,
    frame_start_us,
    quantize_end_us,
    quantize_start_us,
)

#: Inward guard applied after every internal transition (spec 6.3 step 3).
GUARD_FRAMES = 2

REASON_TOO_SHORT = "too_short_after_transition_guard"
REASON_EMPTY_AFTER_GUARD = "too_short_after_transition_guard"


@dataclass(frozen=True, slots=True)
class SafeShot:
    """A detected shot with its guarded, frame-quantized safe interval."""

    shot_number: int
    source: Interval
    safe: Interval | None
    incoming: dict[str, object] | None
    outgoing: dict[str, object] | None
    eligible: bool
    ineligible_reason: str | None

    @property
    def usable_duration_us(self) -> int:
        return self.safe.duration_us if self.safe else 0

    @property
    def is_long_shot(self) -> bool:
        return self.usable_duration_us > MAX_CLIP_US


def _guard_us(rate: Fraction) -> int:
    """Two source frames expressed in microseconds."""
    return frame_start_us(GUARD_FRAMES, rate, round_up=True)


def _touching(event_interval: Interval, position_us: int, tolerance_us: int) -> bool:
    return (
        event_interval.start_us - tolerance_us
        <= position_us
        <= event_interval.end_us + tolerance_us
    )


def build_safe_shot(
    shot_number: int,
    shot: Interval,
    events: list[TransitionEvent],
    rate: Fraction,
    *,
    source_duration_us: int,
) -> SafeShot:
    """Apply steps 1-6 to one detected shot."""
    one_frame_us = frame_start_us(1, rate, round_up=True)
    guard = _guard_us(rate)

    incoming_event: TransitionEvent | None = None
    outgoing_event: TransitionEvent | None = None
    for event in events:
        interval = event.to_interval(rate)
        if _touching(interval, shot.start_us, one_frame_us) and interval.end_us <= shot.end_us:
            incoming_event = event
        if _touching(interval, shot.end_us, one_frame_us) and interval.start_us >= shot.start_us:
            outgoing_event = event

    start_us = shot.start_us
    end_us = shot.end_us

    # Step 2: a fade or dissolve interval overlapping an edge is removed
    # entirely, not merely guarded against.
    for event, at_start in ((incoming_event, True), (outgoing_event, False)):
        if event is None or not ({FADE, DISSOLVE} & set(event.kinds)):
            continue
        interval = event.to_interval(rate)
        if at_start:
            start_us = max(start_us, interval.end_us)
        else:
            end_us = min(end_us, interval.start_us)

    # Steps 3 and 4: guard only where a transition actually abuts the shot.
    at_source_start = shot.start_us <= 0
    at_source_end = shot.end_us >= source_duration_us - one_frame_us

    if incoming_event is not None or not at_source_start:
        if incoming_event is not None:
            start_us += guard
    if outgoing_event is not None or not at_source_end:
        if outgoing_event is not None:
            end_us -= guard

    if end_us <= start_us:
        return SafeShot(
            shot_number=shot_number,
            source=shot,
            safe=None,
            incoming=incoming_event.to_json(rate) if incoming_event else None,
            outgoing=outgoing_event.to_json(rate) if outgoing_event else None,
            eligible=False,
            ineligible_reason=REASON_EMPTY_AFTER_GUARD,
        )

    # Step 5: quantize inward to real source-frame timestamps.
    safe_start = quantize_start_us(start_us, rate)
    safe_end = quantize_end_us(end_us, rate)

    if safe_end <= safe_start:
        return SafeShot(
            shot_number=shot_number,
            source=shot,
            safe=None,
            incoming=incoming_event.to_json(rate) if incoming_event else None,
            outgoing=outgoing_event.to_json(rate) if outgoing_event else None,
            eligible=False,
            ineligible_reason=REASON_EMPTY_AFTER_GUARD,
        )

    safe = Interval(safe_start, safe_end)
    # Step 6 plus the eligibility gate. The comparison is exact: a shot exactly
    # at the minimum, to the microsecond, is eligible.
    eligible = safe.duration_us >= MIN_CLIP_US

    return SafeShot(
        shot_number=shot_number,
        source=shot,
        safe=safe,
        incoming=incoming_event.to_json(rate) if incoming_event else None,
        outgoing=outgoing_event.to_json(rate) if outgoing_event else None,
        eligible=eligible,
        ineligible_reason=None if eligible else REASON_TOO_SHORT,
    )


def build_safe_shots(
    shots: list[Interval],
    events: list[TransitionEvent],
    rate: Fraction,
    *,
    source_duration_us: int,
) -> list[SafeShot]:
    return [
        build_safe_shot(
            number, shot, events, rate, source_duration_us=source_duration_us
        )
        for number, shot in enumerate(shots, start=1)
    ]


def recommend_interval(safe: Interval, rate: Fraction) -> Interval:
    """Stage D: the interval proposed for one candidate.

    A safe interval within the clip bounds is recommended whole. A longer shot
    yields exactly one window of the maximum length; Stage H may move that
    window, but never produces a second clip from the same shot (spec 6.4).
    """
    if safe.duration_us <= MAX_CLIP_US:
        return safe

    # Provisional window: centred, so a later fine pass has room on both sides.
    centre = safe.start_us + (safe.duration_us - LONG_SHOT_WINDOW_US) // 2
    return clamp_window(safe, centre, rate)


def clamp_window(safe: Interval, desired_start_us: int, rate: Fraction) -> Interval:
    """Place a maximum-length window at ``desired_start_us``, kept inside ``safe``."""
    latest_start = safe.end_us - LONG_SHOT_WINDOW_US
    start = max(safe.start_us, min(desired_start_us, latest_start))

    quantized_start = max(quantize_start_us(start, rate), safe.start_us)
    quantized_end = min(quantize_end_us(quantized_start + LONG_SHOT_WINDOW_US, rate), safe.end_us)

    if quantized_end - quantized_start < MIN_CLIP_US:
        # Degenerate only if the safe interval itself is too small, which the
        # eligibility gate has already excluded; clamp defensively anyway.
        return Interval(safe.start_us, min(safe.end_us, safe.start_us + LONG_SHOT_WINDOW_US))
    return Interval(quantized_start, quantized_end)


def long_shot_windows(safe: Interval, rate: Fraction, *, step_us: int = 1_000_000) -> list[Interval]:
    """Stage H step 1-2: windows every second, plus one anchored to the end.

    Each window is the maximum clip length; the last one is anchored to the end
    of the safe interval so the tail of a long shot is always reachable.
    """
    windows: list[Interval] = []
    latest_start = safe.end_us - LONG_SHOT_WINDOW_US
    if latest_start < safe.start_us:
        return [safe]

    start = safe.start_us
    while start <= latest_start:
        windows.append(clamp_window(safe, start, rate))
        start += step_us

    tail = clamp_window(safe, latest_start, rate)
    if not windows or windows[-1].start_us != tail.start_us:
        windows.append(tail)

    # Deduplicate while preserving order.
    seen: set[tuple[int, int]] = set()
    unique: list[Interval] = []
    for window in windows:
        key = (window.start_us, window.end_us)
        if key not in seen:
            seen.add(key)
            unique.append(window)
    return unique
