"""The local human screen (``app.analysis.humans``).

It is a screen, not a classifier: it must never turn "nobody looked" into
"nobody is there", and it must never fail a job.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.analysis import humans, scoring
from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import JOBS, run_job, upload_file
from tests.mock_provider import MockProvider

PRODUCT = "the matte black espresso machine"


class TestFrameDetection:
    def test_flat_noise_is_not_a_person(self):
        rng = np.random.default_rng(1234)
        frame = rng.integers(0, 255, size=(270, 480), dtype=np.uint8)
        # Random noise is the classic HOG false-positive trap. Not a person.
        assert humans.detect_in_frame(frame) is None

    def test_a_blank_frame_is_not_a_person(self):
        assert humans.detect_in_frame(np.zeros((270, 480), dtype=np.uint8)) is None
        assert humans.detect_in_frame(np.full((270, 480), 255, dtype=np.uint8)) is None

    def test_an_unusable_frame_returns_no_verdict(self):
        assert humans.detect_in_frame(np.zeros((4, 4), dtype=np.uint8)) is None
        assert humans.detect_in_frame(np.zeros((0, 0), dtype=np.uint8)) is None

    def test_a_colour_frame_is_rejected_rather_than_misread(self):
        # The detectors expect single-channel input; anything else is refused
        # instead of being silently reinterpreted.
        assert humans.detect_in_frame(np.zeros((270, 480, 3), dtype=np.uint8)) is None

    def test_the_detectors_load(self):
        assert humans._hog() is not None
        # At least the frontal face cascade must be present for the screen to
        # be worth running at all.
        assert len(humans._cascades()) >= 1


class TestScreenDegradesSafely:
    def test_a_zero_length_interval_is_not_assessed(self, settings, tmp_path):
        result = humans.screen_candidate(
            tmp_path / "missing.mp4",
            start_us=0,
            end_us=0,
            source_width=320,
            source_height=180,
            settings=settings,
        )
        assert result.assessed is False
        assert result.present is False

    def test_an_unreadable_source_is_not_assessed(self, settings, tmp_path):
        missing = tmp_path / "missing.mp4"
        result = humans.screen_candidate(
            missing,
            start_us=0,
            end_us=2_000_000,
            source_width=320,
            source_height=180,
            settings=settings,
        )
        # No exception: a failed screen reports "not assessed" so the caller
        # can warn, rather than bringing the job down.
        assert result.assessed is False
        assert result.present is False


@requires_media
class TestScreenOnRealMedia:
    def test_synthetic_colour_bars_contain_no_person(self, settings, tmp_path):
        fixture = fixtures_media.hard_cuts(tmp_path / "media")
        result = humans.screen_candidate(
            fixture.path,
            start_us=0,
            end_us=1_500_000,
            source_width=320,
            source_height=180,
            settings=settings,
        )
        assert result.assessed is True
        assert result.present is False


@requires_media
class TestScreenInThePipeline:
    @pytest.fixture
    def source(self, tmp_path):
        return fixtures_media.hard_cuts(tmp_path / "media").path

    def test_a_local_only_job_with_a_product_is_screened(self, auth_client, source):
        # No key configured, so Gemini never assesses anything and the local
        # screen is the only thing standing between a person and the export.
        from app.providers.gemini import set_provider

        set_provider(MockProvider())
        job_id = run_job(
            auth_client, upload_file(auth_client, source), prompt=PRODUCT, target=4
        )

        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
        assert candidates
        for candidate in candidates:
            # The screen ran and found nobody in these synthetic frames.
            assert candidate["humanPresent"] is False
            assert candidate["humanSource"] == "local"
            # It says nothing about the product, which only Gemini can judge.
            assert candidate["productVisible"] is None
            assert candidate["excludedReason"] is None

    def test_a_local_only_job_without_a_product_is_not_screened(self, auth_client, source):
        from app.providers.gemini import set_provider

        set_provider(MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, source), target=4)

        for candidate in auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]:
            assert candidate["humanPresent"] is None
            assert candidate["humanSource"] is None

    def test_a_detected_person_excludes_the_shot_and_warns(self, auth_client, source, monkeypatch):
        from app.providers.gemini import set_provider

        set_provider(MockProvider())
        monkeypatch.setattr(
            humans,
            "screen_candidate",
            lambda *a, **k: humans.HumanScreenResult(
                present=True, assessed=True, detector="haar_frontal_face"
            ),
        )

        job_id = run_job(
            auth_client, upload_file(auth_client, source), prompt=PRODUCT, target=4
        )

        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
        assert all(c["excludedReason"] == "human_present" for c in candidates)
        assert all(c["humanSource"] == "local" for c in candidates)
        assert all("detected locally" in c["reason"] for c in candidates)

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert "human_present_shots_excluded" in (job["warnings"] or [])

    def test_an_unassessable_shot_is_reported_not_cleared(
        self, auth_client, source, monkeypatch
    ):
        from app.providers.gemini import set_provider

        set_provider(MockProvider())
        monkeypatch.setattr(
            humans,
            "screen_candidate",
            lambda *a, **k: humans.HumanScreenResult(present=False, assessed=False),
        )

        job_id = run_job(
            auth_client, upload_file(auth_client, source), prompt=PRODUCT, target=4
        )

        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
        # Unverified, so left as unknown rather than recorded as people-free.
        assert all(c["humanPresent"] is None for c in candidates)
        assert all(c["excludedReason"] is None for c in candidates)

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert "human_check_incomplete" in (job["warnings"] or [])

    def test_the_model_verdict_is_not_overwritten_by_the_screen(
        self, auth_client, source, monkeypatch
    ):
        from tests.helpers import configure_key

        provider = MockProvider()
        configure_key(auth_client, provider)

        # The screen would claim a person, but Gemini already said otherwise
        # and it is the better-informed judge.
        called = []
        monkeypatch.setattr(
            humans,
            "screen_candidate",
            lambda *a, **k: called.append(1)
            or humans.HumanScreenResult(present=True, assessed=True, detector="hog_person"),
        )

        job_id = run_job(
            auth_client, upload_file(auth_client, source), prompt=PRODUCT, target=4
        )

        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
        assert all(c["humanSource"] == "model" for c in candidates)
        assert all(c["humanPresent"] is False for c in candidates)
        assert not called, "the screen must skip candidates the model already judged"


class TestScoringSourceConstants:
    def test_sources_are_distinct(self):
        assert scoring.HUMAN_SOURCE_MODEL != scoring.HUMAN_SOURCE_LOCAL
