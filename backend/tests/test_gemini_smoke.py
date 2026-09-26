"""Opt-in credentialed smoke test (spec 12).

This is the *only* test that talks to the real Gemini API. It is skipped unless
``GEMINI_SMOKE_KEY`` is set, uses a tiny fixture, and pins a strict request cap
so an accidental run cannot burn quota.

    GEMINI_SMOKE_KEY=... pytest -m credentialed tests/test_gemini_smoke.py
"""

from __future__ import annotations

import os

import pytest

from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import JOBS, run_job, upload_file

SMOKE_KEY = os.environ.get("GEMINI_SMOKE_KEY", "")
SMOKE_MODEL = os.environ.get("GEMINI_SMOKE_MODEL", "gemini-3.8-flash")

#: Deliberately tiny: one coarse request is enough to prove the wiring.
SMOKE_REQUEST_CAP = 1

pytestmark = [
    pytest.mark.credentialed,
    requires_media,
    pytest.mark.skipif(not SMOKE_KEY, reason="Set GEMINI_SMOKE_KEY to run the credentialed smoke test."),
]


@pytest.fixture
def real_provider():
    """Use the genuine adapter rather than the mock."""
    from app.providers.gemini import GeminiProvider, set_provider

    set_provider(GeminiProvider(timeout_seconds=60))
    yield
    set_provider(None)


def test_key_validates_against_the_real_api(auth_client, real_provider):
    response = auth_client.post(
        "/api/v1/settings/gemini/test", json={"apiKey": SMOKE_KEY, "model": SMOKE_MODEL}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True, body
    # Even here, the key must not come back.
    assert SMOKE_KEY not in response.text


def test_end_to_end_ranking_within_a_strict_cap(auth_client, tmp_path, real_provider):
    saved = auth_client.put(
        "/api/v1/settings/gemini",
        json={"apiKey": SMOKE_KEY, "model": SMOKE_MODEL, "requestCap": SMOKE_REQUEST_CAP},
    )
    assert saved.status_code == 200, saved.text

    fixture = fixtures_media.hard_cuts(tmp_path / "media")
    job_id = run_job(auth_client, upload_file(auth_client, fixture.path), target=2)

    job = auth_client.get(f"{JOBS}/{job_id}").json()
    assert job["state"] == "review-ready", job
    assert job["usage"]["requestsUsed"] <= SMOKE_REQUEST_CAP

    candidates = auth_client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]
    assert candidates
    # Whether the provider answered or not, every candidate is ranked.
    assert all(candidate["rank"] > 0 for candidate in candidates)
    assert SMOKE_KEY not in auth_client.get(f"{JOBS}/{job_id}").text
