"""End-to-end scenario and media integration matrix (spec 12.2, 12.7, 13).

These tests run the real local pipeline -- ffprobe, PySceneDetect, FFmpeg -- with
Gemini mocked, which is exactly the CI shape the spec asks for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile

import pytest

from app.media.timebase import MAX_CLIP_US, MIN_CLIP_US, frame_duration_us_ceil, parse_rational
from app.providers.gemini import set_provider
from tests import fixtures_media
from tests.conftest import requires_media
from tests.mock_provider import MockProvider

pytestmark = requires_media

UPLOADS = "/api/v1/uploads"
JOBS = "/api/v1/jobs"


# --- helpers ---------------------------------------------------------------


def upload_file(client, path, *, chunk_size=64 * 1024, interrupt_after=None):
    """Upload through the real chunk API, optionally simulating an interruption."""
    payload = path.read_bytes()
    created = client.post(
        UPLOADS,
        json={
            "fileName": path.name,
            "sizeBytes": len(payload),
            "mimeType": "video/mp4",
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]

    offset = 0
    index = 0
    while offset < len(payload):
        block = payload[offset : offset + chunk_size]
        if interrupt_after is not None and index == interrupt_after:
            # "Interrupt": stop, re-read the verified offset, then resume.
            head = client.head(f"{UPLOADS}/{upload_id}")
            offset = int(head.headers["Upload-Offset"])
            interrupt_after = None
            continue

        response = client.put(
            f"{UPLOADS}/{upload_id}/chunks",
            content=block,
            headers={
                "Upload-Offset": str(offset),
                "Content-Length": str(len(block)),
                "Upload-Checksum": "sha256 "
                + base64.b64encode(hashlib.sha256(block).digest()).decode(),
            },
        )
        assert response.status_code == 204, response.text
        offset = int(response.headers["Upload-Offset"])
        index += 1

    assert client.post(f"{UPLOADS}/{upload_id}/complete").status_code == 202

    from app.workers.tasks import verify_upload

    verify_upload(upload_id)

    state = client.get(f"{UPLOADS}/{upload_id}").json()
    assert state["state"] == "ready", state
    return upload_id


def configure_key(client, provider):
    set_provider(provider)
    response = client.put(
        "/api/v1/settings/gemini",
        json={"apiKey": "AIzaSyFAKEKEYSENTINEL_do_not_log_0000000", "model": "gemini-test-model", "requestCap": 8},
    )
    assert response.status_code == 200, response.text
    return response


def run_job(client, upload_id, *, target=20, prompt=None, use_gemini=True):
    from app.workers.tasks import run_analysis

    body = {"uploadId": upload_id, "targetClipCount": target, "useGemini": use_gemini}
    if prompt:
        body["contentPrompt"] = prompt
    created = client.post(JOBS, json=body)
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]

    run_analysis(job_id)
    return job_id


# --- detection matrix ------------------------------------------------------


class TestDetectionMatrix:
    def test_hard_cuts_produce_one_candidate_per_shot(self, auth_client, tmp_path):
        fixture = fixtures_media.hard_cuts(tmp_path / "media")
        provider = MockProvider()
        configure_key(auth_client, provider)
        upload_id = upload_file(auth_client, fixture.path)
        job_id = run_job(auth_client, upload_id)

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "review-ready", job
        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]

        # Four 5-second shots, each eligible.
        assert len(candidates) == 4

    def test_no_candidate_crosses_an_annotated_transition(self, auth_client, tmp_path):
        fixture = fixtures_media.hard_cuts(tmp_path / "media")
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, fixture.path))

        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
        for candidate in candidates:
            for start, end, _kind in fixture.transitions:
                assert not (
                    candidate["safeStartUs"] < end and start < candidate["safeEndUs"]
                ), f"candidate {candidate['id']} overlaps a transition"

    def test_fade_is_excluded_from_every_safe_interval(self, auth_client, tmp_path):
        fixture = fixtures_media.fade_to_black(tmp_path / "media")
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, fixture.path))

        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
        assert candidates, "a fade fixture must still yield usable shots"
        fade_start, fade_end, _ = fixture.transitions[0]
        for candidate in candidates:
            assert not (
                candidate["safeStartUs"] < fade_end and fade_start < candidate["safeEndUs"]
            )

    def test_continuous_pan_stays_one_eligible_shot(self, auth_client, tmp_path):
        """Motion must never be treated as a transition (spec 2.4, 13)."""
        fixture = fixtures_media.continuous_pan(tmp_path / "media")
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, fixture.path))

        candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
        assert len(candidates) == 1, "a continuous pan must not be split"

    def test_long_take_yields_exactly_one_maximum_length_clip(self, auth_client, tmp_path):
        fixture = fixtures_media.long_take(tmp_path / "media", seconds=30.0)
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, fixture.path), target=20)

        body = auth_client.get(f"{JOBS}/{job_id}/candidates").json()
        assert len(body["candidates"]) == 1
        clip = body["selectedClips"][0]
        tolerance = frame_duration_us_ceil(parse_rational("30000/1001"))
        assert abs(clip["durationUs"] - MAX_CLIP_US) <= tolerance

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        # Fewer eligible shots than requested is a success, not an error.
        assert job["state"] == "review-ready"
        assert job["eligibleCount"] == 1
        assert job["partialResultReason"]

    def test_short_shots_are_all_ineligible(self, auth_client, tmp_path):
        fixture = fixtures_media.short_shots(tmp_path / "media")
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, fixture.path))

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "review-ready"
        assert job["eligibleCount"] == 0
        assert job["selectedCount"] == 0
        assert job["partialResultReason"]

    def test_silent_portrait_source(self, auth_client, tmp_path):
        fixture = fixtures_media.silent_portrait(tmp_path / "media")
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, fixture.path))

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["video"]["hasAudio"] is False
        # Portrait orientation is preserved in the display dimensions.
        assert job["video"]["height"] > job["video"]["width"]


# --- invariants ------------------------------------------------------------


class TestClipInvariants:
    @pytest.fixture
    def analysed(self, auth_client, tmp_path):
        fixture = fixtures_media.hard_cuts(tmp_path / "media")
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, fixture.path))
        return job_id, auth_client.get(f"{JOBS}/{job_id}/candidates").json()

    def test_every_selected_clip_is_within_the_duration_bounds(self, analysed):
        _job_id, body = analysed
        tolerance = frame_duration_us_ceil(parse_rational("30000/1001"))
        for clip in body["selectedClips"]:
            assert MIN_CLIP_US - tolerance <= clip["durationUs"] <= MAX_CLIP_US + tolerance

    def test_every_clip_sits_inside_its_candidate_safe_interval(self, analysed):
        _job_id, body = analysed
        safe = {c["id"]: (c["safeStartUs"], c["safeEndUs"]) for c in body["candidates"]}
        for clip in body["selectedClips"]:
            start, end = safe[clip["candidateId"]]
            assert start <= clip["startUs"] < clip["endUs"] <= end

    def test_each_candidate_contributes_at_most_one_clip(self, analysed):
        _job_id, body = analysed
        ids = [clip["candidateId"] for clip in body["selectedClips"]]
        assert len(ids) == len(set(ids))

    def test_default_selection_order_is_source_chronology(self, analysed):
        _job_id, body = analysed
        starts = [clip["startUs"] for clip in body["selectedClips"]]
        assert starts == sorted(starts)

    def test_candidates_carry_ranking_provenance(self, analysed):
        _job_id, body = analysed
        for candidate in body["candidates"]:
            assert candidate["scoringSource"] in {
                "contact-sheet",
                "proxy-video",
                "local-fallback",
            }
            assert candidate["cacheStatus"] in {"none", "partial", "complete"}
            assert isinstance(candidate["promptRelevanceEvaluated"], bool)
            assert candidate["thumbnailUrl"].startswith("/api/v1/jobs/")
