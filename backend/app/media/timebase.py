"""Frame/microsecond conversions using exact rational arithmetic.

Spec 3: "All media positions in APIs and persistence MUST use integer
microseconds. Frame boundaries MUST be calculated using the source time base
rather than floating-point seconds."

Every interval in this codebase is half-open ``[start_us, end_us)`` with
duration exactly ``end_us - start_us`` (spec 8.1). Quantization always moves
bounds *inward*, so a quantized interval is always a subset of its input and
shrinks by at most one frame duration on each edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

US_PER_SECOND = 1_000_000

#: Hard product invariants. These are the single source of truth for the clip
#: duration rule -- everything else (eligibility, recommended windows, review
#: validation, the UI handles, the provider prompts) derives from them.
MIN_CLIP_US = 1_000_000
MAX_CLIP_US = 5_000_000

#: A shot longer than the maximum collapses to exactly one window of that
#: maximum length (spec 6.4). Tied to MAX_CLIP_US so the two cannot drift apart.
LONG_SHOT_WINDOW_US = MAX_CLIP_US

#: Human-readable forms, used in messages and model prompts so they never go
#: stale when the bounds change.
MIN_CLIP_SECONDS = MIN_CLIP_US / US_PER_SECOND
MAX_CLIP_SECONDS = MAX_CLIP_US / US_PER_SECOND


class TimebaseError(ValueError):
    """Raised for an unusable or absent frame rate."""


def parse_rational(value: str | float | int | Fraction | None) -> Fraction:
    """Parse an ffprobe rational such as ``"30000/1001"`` or ``"25"``."""
    if value is None:
        raise TimebaseError("Missing frame rate.")
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, (int, float)):
        rate = Fraction(value).limit_denominator(1_000_000)
    else:
        text = value.strip()
        if not text:
            raise TimebaseError("Empty frame rate.")
        if "/" in text:
            num_text, _, den_text = text.partition("/")
            try:
                num, den = int(num_text), int(den_text)
            except ValueError as exc:
                raise TimebaseError(f"Unparsable frame rate {value!r}.") from exc
            if den == 0:
                raise TimebaseError(f"Frame rate {value!r} has a zero denominator.")
            rate = Fraction(num, den)
        else:
            try:
                rate = Fraction(text)
            except (ValueError, ZeroDivisionError) as exc:
                raise TimebaseError(f"Unparsable frame rate {value!r}.") from exc
    if rate <= 0:
        raise TimebaseError(f"Frame rate {value!r} is not positive.")
    return rate


def format_rational(rate: Fraction) -> str:
    """Render a frame rate the way ffprobe does, e.g. ``30000/1001``."""
    return f"{rate.numerator}/{rate.denominator}"


def frame_duration_us(rate: Fraction) -> Fraction:
    """Exact duration of one source frame, in microseconds."""
    return Fraction(US_PER_SECOND) / rate


def frame_duration_us_ceil(rate: Fraction) -> int:
    """One-frame tolerance as a whole number of microseconds, rounded up."""
    exact = frame_duration_us(rate)
    return -((-exact.numerator) // exact.denominator)


def frame_index_floor(position_us: int, rate: Fraction) -> int:
    """Index of the frame displayed at ``position_us`` (the frame at or before)."""
    value = Fraction(position_us) * rate / US_PER_SECOND
    return value.numerator // value.denominator


def frame_index_ceil(position_us: int, rate: Fraction) -> int:
    """Index of the first frame boundary at or after ``position_us``."""
    value = Fraction(position_us) * rate / US_PER_SECOND
    return -((-value.numerator) // value.denominator)


def frame_start_us(frame_index: int, rate: Fraction, *, round_up: bool = True) -> int:
    """Presentation time of ``frame_index`` as integer microseconds.

    ``round_up`` selects the sub-microsecond rounding direction; callers pass
    ``True`` for interval starts and ``False`` for interval ends so that the
    resulting interval never grows.
    """
    exact = Fraction(frame_index) * US_PER_SECOND / rate
    if round_up:
        return -((-exact.numerator) // exact.denominator)
    return exact.numerator // exact.denominator


def quantize_start_us(position_us: int, rate: Fraction) -> int:
    """Move an interval start inward to the next source-frame boundary."""
    return frame_start_us(frame_index_ceil(position_us, rate), rate, round_up=True)


def quantize_end_us(position_us: int, rate: Fraction) -> int:
    """Move an interval end inward to the previous source-frame boundary."""
    return frame_start_us(frame_index_floor(position_us, rate), rate, round_up=False)


@dataclass(frozen=True, slots=True)
class Interval:
    """A half-open media interval ``[start_us, end_us)``."""

    start_us: int
    end_us: int

    def __post_init__(self) -> None:
        if self.start_us < 0:
            raise ValueError("Interval start must not be negative.")
        if self.end_us < self.start_us:
            raise ValueError("Interval end must not precede its start.")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def overlaps(self, other: "Interval") -> bool:
        return self.start_us < other.end_us and other.start_us < self.end_us

    def contains(self, other: "Interval") -> bool:
        return self.start_us <= other.start_us and other.end_us <= self.end_us

    def gap_to(self, other: "Interval") -> int:
        """Distance between two intervals; ``0`` when they touch or overlap."""
        if self.overlaps(other):
            return 0
        if self.end_us <= other.start_us:
            return other.start_us - self.end_us
        return self.start_us - other.end_us

    def union(self, other: "Interval") -> "Interval":
        return Interval(min(self.start_us, other.start_us), max(self.end_us, other.end_us))

    def intersect(self, other: "Interval") -> "Interval | None":
        start = max(self.start_us, other.start_us)
        end = min(self.end_us, other.end_us)
        if end <= start:
            return None
        return Interval(start, end)

    def clamp_to(self, bounds: "Interval") -> "Interval | None":
        return self.intersect(bounds)

    def quantized(self, rate: Fraction) -> "Interval | None":
        """Snap both bounds inward to source-frame boundaries."""
        start = quantize_start_us(self.start_us, rate)
        end = quantize_end_us(self.end_us, rate)
        if end <= start:
            return None
        return Interval(start, end)


def duration_within_tolerance(actual_us: int, target_us: int, rate: Fraction) -> bool:
    """True when ``actual_us`` differs from ``target_us`` by at most one frame."""
    return abs(actual_us - target_us) <= frame_duration_us_ceil(rate)


def is_valid_clip_duration(duration_us: int, rate: Fraction) -> bool:
    """Enforce the clip duration rule within one source-frame tolerance."""
    tolerance = frame_duration_us_ceil(rate)
    return (MIN_CLIP_US - tolerance) <= duration_us <= (MAX_CLIP_US + tolerance)


def merge_intervals(intervals: list[Interval], *, tolerance_us: int = 0) -> list[Interval]:
    """Merge overlapping intervals, and those separated by <= ``tolerance_us``."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: (item.start_us, item.end_us))
    merged = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if current.start_us - last.end_us <= tolerance_us:
            merged[-1] = Interval(last.start_us, max(last.end_us, current.end_us))
        else:
            merged.append(current)
    return merged


def subtract_intervals(base: Interval, holes: list[Interval]) -> list[Interval]:
    """Remove ``holes`` from ``base``, returning the remaining sub-intervals."""
    remaining = [base]
    for hole in sorted(holes, key=lambda item: item.start_us):
        next_remaining: list[Interval] = []
        for piece in remaining:
            if not piece.overlaps(hole):
                next_remaining.append(piece)
                continue
            if piece.start_us < hole.start_us:
                next_remaining.append(Interval(piece.start_us, hole.start_us))
            if hole.end_us < piece.end_us:
                next_remaining.append(Interval(hole.end_us, piece.end_us))
        remaining = next_remaining
    return [piece for piece in remaining if piece.duration_us > 0]
