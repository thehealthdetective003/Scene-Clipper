"""Rational frame-rate and microsecond conversions (spec 12.1)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.media.timebase import (
    MAX_CLIP_US,
    MIN_CLIP_US,
    Interval,
    TimebaseError,
    duration_within_tolerance,
    frame_duration_us_ceil,
    frame_index_ceil,
    frame_index_floor,
    frame_start_us,
    is_valid_clip_duration,
    merge_intervals,
    parse_rational,
    quantize_end_us,
    quantize_start_us,
    subtract_intervals,
)

NTSC = Fraction(30000, 1001)


class TestParseRational:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("30000/1001", Fraction(30000, 1001)),
            ("25", Fraction(25)),
            ("24000/1001", Fraction(24000, 1001)),
            (Fraction(30), Fraction(30)),
            (59.94, Fraction(5994, 100)),
        ],
    )
    def test_parses_supported_forms(self, value, expected):
        assert parse_rational(value) == expected

    @pytest.mark.parametrize("value", [None, "", "0", "0/25", "-30", "abc", "1/0"])
    def test_rejects_unusable_rates(self, value):
        with pytest.raises(TimebaseError):
            parse_rational(value)


class TestFrameConversions:
    def test_ntsc_frame_boundaries_are_exact(self):
        # Frame 30 of 30000/1001 is 30 * 1001/30000 s = 1.001 s exactly.
        assert frame_start_us(30, NTSC, round_up=True) == 1_001_000
        assert frame_start_us(30, NTSC, round_up=False) == 1_001_000

    def test_one_frame_tolerance_is_rounded_up(self):
        assert frame_duration_us_ceil(NTSC) == 33_367
        assert frame_duration_us_ceil(Fraction(25)) == 40_000

    def test_floor_and_ceil_bracket_a_non_boundary_position(self):
        position = 1_000_000  # between frames 29 and 30 at 29.97 fps
        assert frame_index_floor(position, NTSC) == 29
        assert frame_index_ceil(position, NTSC) == 30

    def test_quantization_always_moves_inward(self):
        start = quantize_start_us(1_000_000, NTSC)
        end = quantize_end_us(5_000_000, NTSC)
        assert start >= 1_000_000
        assert end <= 5_000_000
        # ...and lands on real frame boundaries.
        assert start == frame_start_us(frame_index_ceil(1_000_000, NTSC), NTSC)

    def test_quantized_interval_is_a_subset(self):
        interval = Interval(1_000_000, 7_000_000)
        quantized = interval.quantized(NTSC)
        assert quantized is not None
        assert interval.contains(quantized)

    def test_quantization_shrinks_by_at_most_one_frame_per_edge(self):
        # Spec 8.1: quantization "may change the requested duration by no more
        # than one source-frame duration" -- the bound is inclusive, and a
        # start one microsecond past a frame boundary reaches it exactly.
        tolerance = frame_duration_us_ceil(NTSC)
        interval = Interval(1_234_567, 7_654_321)
        quantized = interval.quantized(NTSC)
        assert quantized is not None
        assert quantized.start_us - interval.start_us <= tolerance
        assert interval.end_us - quantized.end_us <= tolerance


class TestClipDurationRules:
    @pytest.mark.parametrize("rate", [Fraction(25), NTSC, Fraction(24000, 1001), Fraction(60)])
    def test_exact_three_and_six_seconds_are_valid(self, rate):
        assert is_valid_clip_duration(MIN_CLIP_US, rate)
        assert is_valid_clip_duration(MAX_CLIP_US, rate)

    @pytest.mark.parametrize("rate", [Fraction(25), NTSC])
    def test_clearly_out_of_range_durations_are_rejected(self, rate):
        assert not is_valid_clip_duration(MIN_CLIP_US - 200_000, rate)
        assert not is_valid_clip_duration(MAX_CLIP_US + 200_000, rate)

    def test_one_frame_tolerance_is_allowed_on_each_side(self):
        tolerance = frame_duration_us_ceil(NTSC)
        assert is_valid_clip_duration(MIN_CLIP_US - tolerance, NTSC)
        assert is_valid_clip_duration(MAX_CLIP_US + tolerance, NTSC)
        assert not is_valid_clip_duration(MIN_CLIP_US - tolerance - 1, NTSC)

    def test_duration_tolerance_helper(self):
        assert duration_within_tolerance(6_000_000, 6_000_000, NTSC)
        assert duration_within_tolerance(6_000_000 - 33_000, 6_000_000, NTSC)
        assert not duration_within_tolerance(6_100_000, 6_000_000, NTSC)


class TestIntervalAlgebra:
    def test_half_open_duration(self):
        assert Interval(100, 400).duration_us == 300

    def test_rejects_inverted_interval(self):
        with pytest.raises(ValueError):
            Interval(400, 100)

    def test_overlap_and_containment(self):
        outer = Interval(0, 1000)
        inner = Interval(200, 800)
        assert outer.contains(inner)
        assert not inner.contains(outer)
        assert outer.overlaps(inner)
        # Half-open intervals that merely touch do not overlap.
        assert not Interval(0, 100).overlaps(Interval(100, 200))

    def test_merge_respects_tolerance(self):
        merged = merge_intervals(
            [Interval(0, 100), Interval(120, 200), Interval(1000, 1100)], tolerance_us=25
        )
        assert [(i.start_us, i.end_us) for i in merged] == [(0, 200), (1000, 1100)]

    def test_merge_without_tolerance_keeps_separate(self):
        merged = merge_intervals([Interval(0, 100), Interval(120, 200)], tolerance_us=0)
        assert len(merged) == 2

    def test_subtract_splits_around_a_hole(self):
        pieces = subtract_intervals(Interval(0, 1000), [Interval(400, 600)])
        assert [(p.start_us, p.end_us) for p in pieces] == [(0, 400), (600, 1000)]
