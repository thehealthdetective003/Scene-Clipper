"""Gradual-transition classification and boundary merging (spec 6.2, 12.1)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.analysis import transitions as tx

NTSC = Fraction(30000, 1001)
FPS30 = Fraction(30)


def quiet(length: int = 120, level: float = 2.0) -> list[float]:
    return [level] * length


class TestNeighborhoodMedian:
    def test_excludes_the_central_half_second(self):
        scores = quiet(120)
        # A tall spike inside the excluded centre must not raise the median.
        for index in range(58, 63):
            scores[index] = 90.0
        assert tx.neighborhood_median(scores, 60, FPS30) == pytest.approx(2.0)

    def test_uses_the_surrounding_two_second_window(self):
        scores = quiet(120, level=2.0)
        for index in range(30, 46):
            scores[index] = 10.0
        median = tx.neighborhood_median(scores, 60, FPS30)
        assert median >= 2.0

    def test_falls_back_near_the_series_edge(self):
        scores = quiet(10)
        assert tx.neighborhood_median(scores, 0, FPS30) == pytest.approx(2.0)


class TestElevationThreshold:
    def test_never_drops_below_the_absolute_floor(self):
        # A silent neighbourhood must not make noise look elevated.
        assert tx.elevation_threshold(quiet(120, level=0.0), 60, FPS30) == 8.0

    def test_scales_with_a_busy_neighbourhood(self):
        scores = quiet(120, level=20.0)
        assert tx.elevation_threshold(scores, 60, FPS30) == pytest.approx(30.0)


class TestRunDetection:
    def test_single_frame_peak_is_a_hard_cut(self):
        scores = quiet()
        scores[60] = 60.0
        events = tx.classify(scores, [120.0] * 120, FPS30, motion_veto=lambda a, b: False)
        assert len(events) == 1
        assert events[0].kinds == [tx.HARD_CUT]
        assert (events[0].start_frame, events[0].end_frame) == (60, 61)

    def test_two_frame_peak_is_still_a_hard_cut(self):
        scores = quiet()
        scores[60] = scores[61] = 60.0
        events = tx.classify(scores, [120.0] * 120, FPS30, motion_veto=lambda a, b: False)
        assert events[0].kinds == [tx.HARD_CUT]

    def test_three_frame_run_becomes_a_dissolve(self):
        scores = quiet()
        for index in range(60, 63):
            scores[index] = 40.0
        events = tx.classify(scores, [120.0] * 120, FPS30, motion_veto=lambda a, b: False)
        assert events[0].kinds == [tx.DISSOLVE]

    def test_one_below_threshold_frame_is_tolerated_inside_a_run(self):
        scores = quiet()
        for index in (60, 61, 63, 64):
            scores[index] = 40.0
        runs = tx.find_runs(scores, FPS30)
        assert len(runs) == 1
        assert runs[0].start_frame == 60
        assert runs[0].end_frame == 64

    def test_two_consecutive_gaps_split_the_run(self):
        scores = quiet()
        for index in (60, 61, 64, 65):
            scores[index] = 40.0
        runs = tx.find_runs(scores, FPS30)
        assert len(runs) == 2


class TestGradualCap:
    def test_run_longer_than_two_seconds_is_capped_around_the_peak(self):
        # A run that stays elevated for 140 frames is capped to the two-second
        # region centred on its highest score (spec 6.2 step 5).
        run = tx.Run(start_frame=60, end_frame=199, peak_frame=150, peak_score=99.0)
        start, end = tx.cap_gradual_run(run, FPS30)
        assert end - start == tx.frames_for_seconds(tx.MAX_GRADUAL_SECONDS, FPS30)
        assert start <= 150 < end

    def test_short_run_is_not_capped(self):
        run = tx.Run(start_frame=10, end_frame=20, peak_frame=15, peak_score=40.0)
        assert tx.cap_gradual_run(run, FPS30) == (10, 21)

    def test_classify_never_emits_an_event_longer_than_the_cap(self):
        scores = quiet(400)
        for index in range(100, 260):
            scores[index] = 40.0
        events = tx.classify(scores, [120.0] * 400, FPS30, motion_veto=lambda a, b: False)
        cap = tx.frames_for_seconds(tx.MAX_GRADUAL_SECONDS, FPS30)
        for event in events:
            assert event.end_frame - event.start_frame <= cap

    def test_a_sustained_plateau_stops_being_elevated(self):
        # Elevation is relative to the local neighbourhood, so a long plateau
        # becomes the new baseline rather than one enormous transition.
        scores = quiet(300)
        for index in range(60, 200):
            scores[index] = 40.0
        runs = tx.find_runs(scores, FPS30)
        assert runs
        assert runs[0].length_frames < 140


class TestMotionVeto:
    def test_coherent_camera_motion_vetoes_a_dissolve(self):
        scores = quiet()
        for index in range(60, 70):
            scores[index] = 40.0
        events = tx.classify(scores, [120.0] * 120, FPS30, motion_veto=lambda a, b: True)
        assert events == []

    def test_decision_rule_thresholds(self):
        assert tx.affine_motion_veto(0.70, 11.9) is True
        assert tx.affine_motion_veto(0.69, 5.0) is False
        assert tx.affine_motion_veto(0.95, 12.0) is False

    def test_veto_requires_compensation_to_explain_the_change(self):
        # A camera pan: compensation removes the difference entirely.
        assert tx.affine_motion_veto(0.92, 0.0, 8.0) is True
        # A cross-dissolve under a still camera: compensation changes nothing,
        # so the run is a real transition even though both other tests pass.
        assert tx.affine_motion_veto(0.88, 7.0, 7.0) is False
        # A partial improvement that does not meet the ratio still fails.
        assert tx.affine_motion_veto(0.90, 6.0, 8.0) is False
        # A clear improvement passes.
        assert tx.affine_motion_veto(0.90, 2.0, 8.0) is True

    def test_a_static_scene_is_vetoed_rather_than_called_a_transition(self):
        assert tx.affine_motion_veto(0.95, 0.0, 0.0) is True


class TestFadeHandling:
    def test_fade_expands_until_luma_settles(self):
        luma = [120.0] * 200
        # Ramp down to black across frames 60-80, then hold black.
        for offset, index in enumerate(range(60, 81)):
            luma[index] = max(0.0, 120.0 - offset * 6.0)
        for index in range(81, 100):
            luma[index] = 0.0

        # The content score is elevated for the whole ramp, which is what makes
        # the run reach near-black and classify as a fade rather than a dissolve.
        scores = quiet(200)
        for index in range(60, 81):
            scores[index] = 40.0

        events = tx.classify(scores, luma, FPS30, motion_veto=lambda a, b: False)
        assert events, "a fade must produce a transition"
        assert tx.FADE in events[0].kinds
        # The transition is an interval, never a single unsafe frame.
        assert events[0].end_frame - events[0].start_frame > 1

    def test_threshold_detector_fades_are_honoured_outside_any_run(self):
        luma = [120.0] * 120
        for index in range(50, 70):
            luma[index] = 4.0
        events = tx.classify(
            quiet(120), luma, FPS30, threshold_fade_frames=[60], motion_veto=lambda a, b: False
        )
        assert events and tx.FADE in events[0].kinds


class TestMerging:
    def test_events_within_three_frames_merge_and_keep_all_labels(self):
        merged = tx.merge_events(
            [
                tx.TransitionEvent(10, 12, [tx.HARD_CUT]),
                tx.TransitionEvent(14, 20, [tx.DISSOLVE]),
            ]
        )
        assert len(merged) == 1
        assert merged[0].start_frame == 10
        assert merged[0].end_frame == 20
        assert merged[0].kinds == [tx.DISSOLVE, tx.HARD_CUT]

    def test_events_beyond_the_tolerance_stay_separate(self):
        merged = tx.merge_events(
            [tx.TransitionEvent(10, 12, [tx.HARD_CUT]), tx.TransitionEvent(16, 20, [tx.FADE])]
        )
        assert len(merged) == 2

    def test_overlapping_events_merge(self):
        merged = tx.merge_events(
            [tx.TransitionEvent(10, 30, [tx.FADE]), tx.TransitionEvent(20, 40, [tx.DISSOLVE])]
        )
        assert len(merged) == 1
        assert (merged[0].start_frame, merged[0].end_frame) == (10, 40)


class TestShotDerivation:
    def test_shots_are_the_gaps_between_transitions(self):
        events = [tx.TransitionEvent(30, 32, [tx.HARD_CUT])]
        shots = tx.shots_from_transitions(events, 60, FPS30)
        assert len(shots) == 2
        assert shots[0].start_us == 0
        assert shots[1].end_us == tx.frame_start_us(60, FPS30, round_up=False)

    def test_no_transitions_yields_one_shot(self):
        shots = tx.shots_from_transitions([], 300, NTSC)
        assert len(shots) == 1
        assert shots[0].start_us == 0

    def test_ntsc_boundaries_are_exact(self):
        events = [tx.TransitionEvent(30, 31, [tx.HARD_CUT])]
        shots = tx.shots_from_transitions(events, 60, NTSC)
        assert shots[0].end_us == 1_001_000
