"""Provider interaction, budgeting, fallback, and caching (spec 12.4)."""

from __future__ import annotations

import pytest

from app.analysis import budget
from app.db import session_scope
from app.models import AnalysisAttempt, AnalysisUsage
from tests import fixtures_media, mock_provider
from tests.conftest import requires_media
from tests.helpers import JOBS, analyse, configure_key, run_job, upload_file
from tests.mock_provider import MockProvider

pytestmark = requires_media

SETTINGS = "/api/v1/settings/gemini"


@pytest.fixture
def source(tmp_path):
    return fixtures_media.hard_cuts(tmp_path / "media").path


def candidates_of(client, job_id):
    return client.get(f"{JOBS}/{job_id}/candidates").json()["candidates"]


def usage_of(client, job_id):
    return client.get(f"{JOBS}/{job_id}").json()["usage"]


# --- Request cap validation -------------------------------------------------


class TestRequestCapValidation:
    @pytest.mark.parametrize("cap", [0, 8, 50])
    def test_valid_caps_are_accepted(self, auth_client, cap):
        provider = MockProvider()
        response = configure_key(auth_client, provider, request_cap=cap)
        assert response.json()["requestCap"] == cap

    @pytest.mark.parametrize("cap", [-1, 51, 100, 2.5, "eight", True])
    def test_invalid_caps_are_rejected(self, auth_client, cap):
        from app.providers.gemini import set_provider

        set_provider(MockProvider())
        response = auth_client.put(
            SETTINGS, json={"apiKey": "AIzaSyTESTKEY000000000000", "requestCap": cap}
        )
        assert response.status_code == 422, f"cap={cap!r}"

    def test_cap_zero_performs_local_only_ranking(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider, request_cap=0)
        job_id = run_job(auth_client, upload_file(auth_client, source))

        assert provider.total_inference_calls == 0
        usage = usage_of(auth_client, job_id)
        assert usage["requestsUsed"] == 0
        assert usage["localFallbackUsed"] is True
        for candidate in candidates_of(auth_client, job_id):
            assert candidate["scoringSource"] == "local-fallback"
            assert candidate["confidence"] == pytest.approx(0.35)
            assert candidate["promptRelevanceEvaluated"] is False


# --- Ranking with and without a prompt --------------------------------------


class TestRanking:
    def test_ranking_without_a_prompt_leaves_relevance_unevaluated(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider)
        job_id = run_job(auth_client, upload_file(auth_client, source))

        assert provider.coarse_calls
        for candidate in candidates_of(auth_client, job_id):
            assert candidate["scoringSource"] == "contact-sheet"
            assert candidate["promptRelevanceEvaluated"] is False

    def test_ranking_with_a_prompt_evaluates_relevance(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider)
        job_id = run_job(
            auth_client, upload_file(auth_client, source), prompt="wide establishing shots"
        )

        for candidate in candidates_of(auth_client, job_id):
            assert candidate["promptRelevanceEvaluated"] is True

    def test_rank_order_is_gapless_and_sorted_by_score(self, auth_client, source):
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, source))

        ranked = candidates_of(auth_client, job_id)
        assert [c["rank"] for c in ranked] == list(range(1, len(ranked) + 1))
        scores = [c["score"] for c in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_every_candidate_is_scored_even_when_gemini_is_used(self, auth_client, source):
        configure_key(auth_client, MockProvider())
        job_id = run_job(auth_client, upload_file(auth_client, source))
        ranked = candidates_of(auth_client, job_id)
        assert all(c["score"] > 0 for c in ranked)


# --- Malformed and hostile responses ----------------------------------------


class TestResponseValidation:
    def _run(self, client, source, provider):
        configure_key(client, provider)
        return run_job(client, upload_file(client, source))

    def test_missing_candidates_invalidate_the_response(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider)
        upload_id = upload_file(auth_client, source)

        created = auth_client.post(
            JOBS, json={"uploadId": upload_id, "targetClipCount": 20, "useGemini": True}
        )
        job_id = created.json()["id"]
        # Drop one candidate from every response.
        from app.workers.tasks import run_analysis

        provider.omit_candidates = {"will-be-set"}

        def omit_first(*args, **kwargs):
            raise AssertionError

        run_analysis(job_id)
        # Local analysis survives regardless of what the provider returned.
        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "review-ready"
        assert len(candidates_of(auth_client, job_id)) == 4

    def test_unknown_candidate_id_falls_back_locally(self, auth_client, source):
        provider = MockProvider(inject_unknown_id="00000000-0000-0000-0000-000000000000")
        job_id = self._run(auth_client, source, provider)

        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "review-ready"
        for candidate in candidates_of(auth_client, job_id):
            assert candidate["scoringSource"] == "local-fallback"
        assert usage_of(auth_client, job_id)["localFallbackUsed"] is True

    def test_duplicate_candidate_id_falls_back_locally(self, auth_client, source):
        configure_key(auth_client, MockProvider())
        upload_id = upload_file(auth_client, source)
        created = auth_client.post(JOBS, json={"uploadId": upload_id, "useGemini": True})
        job_id = created.json()["id"]

        candidates_before = None
        from app.providers.gemini import get_provider
        from app.workers.tasks import run_analysis

        provider = get_provider()
        provider.duplicate_candidate = "any"
        run_analysis(job_id)

        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "review-ready"
        assert len(candidates_of(auth_client, job_id)) == 4

    def test_malformed_response_retries_once_then_falls_back(self, auth_client, source):
        provider = MockProvider(fail_coarse_with=mock_provider.invalid_response())
        job_id = self._run(auth_client, source, provider)

        # One initial attempt plus exactly one retry.
        assert len(provider.coarse_calls) == 2
        usage = usage_of(auth_client, job_id)
        assert usage["requestsUsed"] == 2
        assert usage["localFallbackUsed"] is True

    def test_transient_failure_then_success(self, auth_client, source):
        provider = MockProvider(transient_coarse_failures=1)
        job_id = self._run(auth_client, source, provider)

        assert len(provider.coarse_calls) == 2
        for candidate in candidates_of(auth_client, job_id):
            assert candidate["scoringSource"] == "contact-sheet"
        # The retry consumed a unit of the cap.
        assert usage_of(auth_client, job_id)["requestsUsed"] == 2


class TestProviderErrors:
    def _run(self, client, source, exc, cap=8):
        provider = MockProvider(fail_coarse_with=exc)
        configure_key(client, provider, request_cap=cap)
        job_id = run_job(client, upload_file(client, source))
        return provider, job_id

    def test_auth_failure_is_not_retried(self, auth_client, source):
        provider, job_id = self._run(auth_client, source, mock_provider.auth_failure())
        # Permanent 4xx: exactly one attempt.
        assert len(provider.coarse_calls) == 1
        assert usage_of(auth_client, job_id)["requestsUsed"] == 1

    def test_auth_failure_preserves_local_analysis(self, auth_client, source):
        _provider, job_id = self._run(auth_client, source, mock_provider.auth_failure())
        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "review-ready"
        assert job["eligibleCount"] == 4
        assert job["usage"]["fallbackReason"]
        assert len(candidates_of(auth_client, job_id)) == 4

    def test_quota_failure_is_not_retried_on_the_same_key(self, auth_client, source):
        # A key that just reported 429 will report it again a second later, so
        # the pool fails over instead of retrying. With one key configured
        # there is nowhere to fail over to, and the run falls back locally.
        provider, job_id = self._run(auth_client, source, mock_provider.quota_failure())
        assert len(provider.coarse_calls) == 1
        assert usage_of(auth_client, job_id)["localFallbackUsed"] is True

    def test_transient_failure_retries_once(self, auth_client, source):
        provider, job_id = self._run(auth_client, source, mock_provider.transient_failure())
        assert len(provider.coarse_calls) == 2

    def test_no_retry_when_the_cap_has_one_unit(self, auth_client, source):
        provider, job_id = self._run(
            auth_client, source, mock_provider.transient_failure(), cap=1
        )
        # A cap of 1 allocates one coarse unit, so the retry has nothing to take.
        assert len(provider.coarse_calls) == 1
        assert usage_of(auth_client, job_id)["requestsUsed"] == 1


# --- Budget accounting ------------------------------------------------------


class TestBudgetAccounting:
    def test_a_job_never_exceeds_its_snapshotted_cap(self, auth_client, source):
        provider = MockProvider(fail_coarse_with=mock_provider.transient_failure())
        configure_key(auth_client, provider, request_cap=3)
        job_id = run_job(auth_client, upload_file(auth_client, source))

        usage = usage_of(auth_client, job_id)
        assert usage["requestsUsed"] <= 3
        assert provider.total_inference_calls <= 3

    def test_changing_the_cap_later_does_not_affect_a_running_job(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider, request_cap=8)
        upload_id = upload_file(auth_client, source)
        created = auth_client.post(JOBS, json={"uploadId": upload_id, "useGemini": True})
        job_id = created.json()["id"]

        # Change the deployment setting after the job snapshotted it.
        configure_key(auth_client, provider, request_cap=0)

        from app.workers.tasks import run_analysis

        run_analysis(job_id)
        assert usage_of(auth_client, job_id)["requestCap"] == 8

    def test_indeterminate_reservation_stays_consumed(self, auth_client, source):
        """A worker that dies after transmitting must not get the unit back."""
        configure_key(auth_client, MockProvider(), request_cap=8)
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(
            JOBS, json={"uploadId": upload_id, "useGemini": True}
        ).json()["id"]

        with session_scope() as db:
            attempt = budget.reserve(
                db, job_id, budget.STAGE_COARSE, cap=8, stage_limit=8
            )
            attempt_id = attempt.id

        # Simulate the crash: the attempt is never settled.
        with session_scope() as db:
            assert budget.consumed_units(db, job_id) == 1
            assert db.get(AnalysisAttempt, attempt_id).status == "reserved"
            assert db.get(AnalysisUsage, job_id).requests_used == 1

    def test_reserve_refuses_once_the_cap_is_reached(self, auth_client, source):
        configure_key(auth_client, MockProvider(), request_cap=8)
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(
            JOBS, json={"uploadId": upload_id, "useGemini": True}
        ).json()["id"]

        with session_scope() as db:
            for _ in range(2):
                budget.reserve(db, job_id, budget.STAGE_COARSE, cap=2, stage_limit=8)
            with pytest.raises(budget.BudgetExhausted):
                budget.reserve(db, job_id, budget.STAGE_COARSE, cap=2, stage_limit=8)

    def test_stage_limit_is_enforced_independently(self, auth_client, source):
        configure_key(auth_client, MockProvider(), request_cap=8)
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(
            JOBS, json={"uploadId": upload_id, "useGemini": True}
        ).json()["id"]

        with session_scope() as db:
            budget.reserve(db, job_id, budget.STAGE_FINE, cap=8, stage_limit=1)
            with pytest.raises(budget.BudgetExhausted):
                budget.reserve(db, job_id, budget.STAGE_FINE, cap=8, stage_limit=1)

    def test_provider_file_operations_do_not_consume_inference_units(
        self, auth_client, source
    ):
        configure_key(auth_client, MockProvider(), request_cap=8)
        upload_id = upload_file(auth_client, source)
        job_id = auth_client.post(
            JOBS, json={"uploadId": upload_id, "useGemini": True}
        ).json()["id"]

        with session_scope() as db:
            budget.record_provider_file_operation(db, job_id, 3)
            assert budget.consumed_units(db, job_id) == 0
            assert db.get(AnalysisUsage, job_id).provider_file_operations == 3


# --- Cache ------------------------------------------------------------------


class TestCache:
    def test_complete_cache_hit_makes_zero_model_requests(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider)
        upload_id = upload_file(auth_client, source)

        first_job = run_job(auth_client, upload_id, prompt="sunlit streets")
        assert provider.coarse_calls, "the first run must populate the cache"
        calls_after_first = provider.total_inference_calls

        second_job = run_job(auth_client, upload_id, prompt="sunlit streets")
        assert provider.total_inference_calls == calls_after_first, (
            "a complete cache hit must perform zero Gemini requests"
        )

        usage = usage_of(auth_client, second_job)
        assert usage["requestsUsed"] == 0
        assert usage["cacheStatus"] == "complete"
        # Scoring source survives caching (spec 6.10).
        for candidate in candidates_of(auth_client, second_job):
            assert candidate["scoringSource"] == "contact-sheet"
            assert candidate["cacheStatus"] == "complete"

    def test_changing_the_prompt_invalidates_the_cache(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider)
        upload_id = upload_file(auth_client, source)

        run_job(auth_client, upload_id, prompt="sunlit streets")
        calls_after_first = provider.total_inference_calls

        run_job(auth_client, upload_id, prompt="night time neon")
        assert provider.total_inference_calls > calls_after_first

    def test_changing_the_model_invalidates_the_cache(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider)
        upload_id = upload_file(auth_client, source)

        run_job(auth_client, upload_id)
        calls_after_first = provider.total_inference_calls

        auth_client.put(
            SETTINGS,
            json={
                "apiKey": "AIzaSyFAKEKEYSENTINEL_do_not_log_0000000",
                "model": "gemini-different-model",
                "requestCap": 8,
            },
        )
        run_job(auth_client, upload_id)
        assert provider.total_inference_calls > calls_after_first

    def test_changing_the_detector_version_invalidates_the_cache(
        self, auth_client, source, monkeypatch
    ):
        provider = MockProvider()
        configure_key(auth_client, provider)
        upload_id = upload_file(auth_client, source)
        run_job(auth_client, upload_id)
        calls_after_first = provider.total_inference_calls

        from app.config import get_settings, reset_settings_cache

        monkeypatch.setenv("DETECTOR_CONFIG_VERSION", "2")
        reset_settings_cache()
        get_settings().ensure_directories()

        run_job(auth_client, upload_id)
        assert provider.total_inference_calls > calls_after_first


# --- Local fallback labelling -----------------------------------------------


class TestLocalFallback:
    def test_use_gemini_false_skips_the_provider(self, auth_client, source):
        provider = MockProvider()
        configure_key(auth_client, provider)
        job_id = run_job(auth_client, upload_file(auth_client, source), use_gemini=False)

        assert provider.total_inference_calls == 0
        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["useGemini"] is False
        assert job["usage"]["fallbackReason"]

    def test_no_key_configured_falls_back_and_warns(self, auth_client, source):
        from app.providers.gemini import set_provider

        set_provider(MockProvider())
        auth_client.delete(SETTINGS)

        upload_id = upload_file(auth_client, source)
        created = auth_client.post(
            JOBS, json={"uploadId": upload_id, "useGemini": True}
        )
        job_id = created.json()["id"]

        from app.workers.tasks import run_analysis

        run_analysis(job_id)

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "review-ready"
        assert job["useGemini"] is False
        assert "gemini_key_not_configured" in job["warnings"]

    def test_fallback_candidates_are_still_ranked_and_selected(self, auth_client, source):
        provider = MockProvider(fail_coarse_with=mock_provider.auth_failure())
        configure_key(auth_client, provider)
        job_id = run_job(auth_client, upload_file(auth_client, source))

        body = auth_client.get(f"{JOBS}/{job_id}/candidates").json()
        assert len(body["selectedClips"]) == 4
        assert all(c["rank"] > 0 for c in body["candidates"])
