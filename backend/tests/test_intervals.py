"""Safe intervals, guards, and recommended clips (spec 6.3, 6.4, 12.1)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.analysis import transitions as tx
from app.analysis.intervals import (
    GUARD_FRAMES,
    REASON_TOO_SHORT,
    build_safe_shot,
    build_safe_shots,
    clamp_window,
    recommend_interval,
    long_shot_windows,
)
from app.media.timebase import (
    LONG_SHOT_WINDOW_US,
    MAX_CLIP_US,
    MIN_CLIP_US,
    Interval,
    frame_start_us,
)

FPS25 = Fraction(25)
NTSC = Fraction(30000, 1001)
SOURCE_US = 60_000_000


def guard_us(rate: Fraction) -> int:
    return frame_start_us(GUARD_FRAMES, rate, round_up=True)


def cut_at(frame: int) -> tx.TransitionEvent:
    return tx.TransitionEvent(start_frame=frame, end_frame=frame + 1, kinds=[tx.HARD_CUT])


class TestGuards:
    def test_two_frame_guard_after_an_internal_cut(self):
        # Shot runs from the cut at frame 25 (1.0s) to 11.0s.
        events = [cut_at(25)]
        shot = Interval(frame_start_us(26, FPS25, round_up=True), 11_000_000)
        result = build_safe_shot(1, shot, events, FPS25, source_duration_us=SOURCE_US)

        assert result.eligible
        assert result.safe is not None
        assert result.safe.start_us >= shot.start_us + guard_us(FPS25)

    def test_no_artificial_guard_at_the_source_start(self):
        shot = Interval(0, 10_000_000)
        result = build_safe_shot(1, shot, [], FPS25, source_duration_us=SOURCE_US)
        assert result.safe is not None
        # Spec 6.3 step 4: no guard unless a transition actually exists there.
        assert result.safe.start_us == 0

    def test_no_artificial_guard_at_the_source_end(self):
        shot = Interval(50_000_000, SOURCE_US)
        result = build_safe_shot(1, shot, [], FPS25, source_duration_us=SOURCE_US)
        assert result.safe is not None
        assert result.safe.end_us == SOURCE_US

    def test_guard_applied_at_both_edges_when_transitions_abut(self):
        events = [cut_at(25), cut_at(275)]
        shot = Interval(
            frame_start_us(26, FPS25, round_up=True), frame_start_us(275, FPS25, round_up=False)
        )
        result = build_safe_shot(1, shot, events, FPS25, source_duration_us=SOURCE_US)
        assert result.safe is not None
        assert result.safe.start_us > shot.start_us
        assert result.safe.end_us < shot.end_us


class TestFadeAndDissolveRemoval:
    def test_fade_at_an_edge_is_removed_entirely(self):
        fade = tx.TransitionEvent(start_frame=25, end_frame=100, kinds=[tx.FADE])
        shot = Interval(frame_start_us(25, FPS25, round_up=True), 20_000_000)
        result = build_safe_shot(1, shot, [fade], FPS25, source_duration_us=SOURCE_US)

        assert result.safe is not None
        fade_end = fade.to_interval(FPS25).end_us
        # The whole fade interval is gone, plus the two-frame guard.
        assert result.safe.start_us >= fade_end
        assert result.incoming is not None
        assert result.incoming["kinds"] == [tx.FADE]

    def test_dissolve_at_the_trailing_edge_is_removed(self):
        dissolve = tx.TransitionEvent(start_frame=200, end_frame=250, kinds=[tx.DISSOLVE])
        shot = Interval(1_000_000, frame_start_us(250, FPS25, round_up=False))
        result = build_safe_shot(1, shot, [dissolve], FPS25, source_duration_us=SOURCE_US)
        assert result.safe is not None
        assert result.safe.end_us <= dissolve.to_interval(FPS25).start_us


class TestEligibilityGate:
    def test_exactly_the_minimum_is_eligible(self):
        shot = Interval(0, MIN_CLIP_US)
        result = build_safe_shot(1, shot, [], FPS25, source_duration_us=SOURCE_US)
        assert result.eligible
        assert result.usable_duration_us == MIN_CLIP_US

    def test_just_under_the_minimum_is_ineligible(self):
        shot = Interval(0, MIN_CLIP_US - 40_000)
        result = build_safe_shot(1, shot, [], FPS25, source_duration_us=SOURCE_US)
        assert not result.eligible
        assert result.ineligible_reason == REASON_TOO_SHORT

    def test_shot_that_guards_below_the_minimum_is_ineligible(self):
        # A shot just over the floor before guards, and just under it after.
        last_frame = 25 + int(MIN_CLIP_US / 1_000_000 * 25) + 1
        events = [cut_at(25), cut_at(last_frame)]
        shot = Interval(
            frame_start_us(26, FPS25, round_up=True),
            frame_start_us(last_frame, FPS25, round_up=False),
        )
        result = build_safe_shot(1, shot, events, FPS25, source_duration_us=SOURCE_US)
        assert not result.eligible
        assert result.ineligible_reason == REASON_TOO_SHORT

    def test_short_shots_are_never_merged_with_neighbours(self):
        # Five shots that are each below the floor must stay five ineligible
        # shots -- merging them to reach the minimum is forbidden.
        span = max(2, int(MIN_CLIP_US / 1_000_000 * 25) // 2)
        events = [cut_at(span * i) for i in range(1, 5)]
        shots = [
            Interval(frame_start_us(span * i, FPS25, round_up=True),
                     frame_start_us(span * (i + 1), FPS25, round_up=False))
            for i in range(5)
        ]
        results = build_safe_shots(
            shots, events, FPS25, source_duration_us=frame_start_us(span * 5, FPS25)
        )
        assert len(results) == 5
        assert all(not r.eligible for r in results)


class TestRecommendedInterval:
    def test_in_range_shot_is_recommended_whole(self):
        safe = Interval(1_000_000, 1_000_000 + (MIN_CLIP_US + MAX_CLIP_US) // 2)
        assert recommend_interval(safe, FPS25) == safe

    def test_exactly_the_maximum_is_recommended_whole(self):
        safe = Interval(0, MAX_CLIP_US)
        assert recommend_interval(safe, FPS25) == safe

    def test_long_shot_yields_exactly_one_maximum_length_window(self):
        safe = Interval(0, 30_000_000)
        recommended = recommend_interval(safe, NTSC)
        assert safe.contains(recommended)
        assert abs(recommended.duration_us - LONG_SHOT_WINDOW_US) <= 40_000

    def test_recommended_interval_always_stays_inside_safe(self):
        safe = Interval(2_345_678, 41_234_567)
        recommended = recommend_interval(safe, NTSC)
        assert safe.contains(recommended)


class TestWindowGeneration:
    def test_clamp_keeps_window_inside_safe(self):
        safe = Interval(1_000_000, 20_000_000)
        window = clamp_window(safe, 19_500_000, FPS25)
        assert safe.contains(window)

    def test_windows_step_every_second_plus_a_tail_anchor(self):
        safe = Interval(0, 12_000_000)
        windows = long_shot_windows(safe, FPS25)
        assert all(safe.contains(w) for w in windows)
        # One window per second up to the last legal start, plus a tail
        # anchor so the end of the shot is always reachable.
        latest_start = safe.duration_us - MAX_CLIP_US
        assert len(windows) == latest_start // 1_000_000 + 1
        assert windows[-1].end_us == 12_000_000

    def test_windows_deduplicate(self):
        safe = Interval(0, MAX_CLIP_US + 1_500_000)
        windows = long_shot_windows(safe, FPS25)
        starts = [w.start_us for w in windows]
        assert len(starts) == len(set(starts))

    def test_safe_interval_shorter_than_the_maximum_yields_itself(self):
        safe = Interval(0, MAX_CLIP_US - 1_000_000)
        assert long_shot_windows(safe, FPS25) == [safe]


class TestLongShotFlag:
    @pytest.mark.parametrize(
        ("duration", "expected"),
        [(MIN_CLIP_US, False), (MAX_CLIP_US, False), (MAX_CLIP_US + 1_000_000, True)],
    )
    def test_long_shot_detection(self, duration, expected):
        shot = Interval(0, duration)
        result = build_safe_shot(1, shot, [], FPS25, source_duration_us=SOURCE_US)
        assert result.is_long_shot is expected
