"""The Gemini key pool: storage, health, and failover (spec 5.2 extension).

Up to five credentials are stored. When one is rejected or runs out of quota
the run continues on the next, and the failed key is left in a state the
Settings screen shows in red.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db import session_scope
from app.models import MAX_GEMINI_KEYS, GeminiKey, utcnow
from app.services import settings_service
from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import (
    SENTINEL_KEY,
    JOBS,
    configure_key,
    configure_keys,
    key_state,
    run_job,
    upload_file,
)
from tests.mock_provider import MockProvider

SETTINGS = "/api/v1/settings/gemini"


def key_for(index: int) -> str:
    """The sentinel plaintext ``configure_keys`` stores at slot ``index``."""
    return f"{SENTINEL_KEY[:-1]}{index}"


# --- Storage ----------------------------------------------------------------


class TestKeyPoolStorage:
    def test_up_to_five_keys_can_be_stored(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one", "two", "three", "four", "five"])
        assert [key["label"] for key in keys] == ["one", "two", "three", "four", "five"]
        assert [key["position"] for key in keys] == [1, 2, 3, 4, 5]
        assert all(key["status"] == "active" for key in keys)

    def test_a_sixth_key_is_refused(self, auth_client):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["1", "2", "3", "4", "5"])
        response = auth_client.put(
            SETTINGS, json={"apiKey": key_for(9), "model": "gemini-test-model"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "key_pool_full"
        assert response.json()["error"]["details"]["maxKeys"] == MAX_GEMINI_KEYS

    def test_no_response_exposes_key_material(self, auth_client):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["one", "two"])
        body = auth_client.get(SETTINGS).text
        for index in (0, 1):
            assert key_for(index) not in body
        assert "AIza" not in body

    def test_an_invalid_key_is_never_stored(self, auth_client):
        provider = MockProvider(auth_keys={key_for(0)})
        from app.providers.gemini import set_provider

        set_provider(provider)
        response = auth_client.put(
            SETTINGS, json={"apiKey": key_for(0), "model": "gemini-test-model"}
        )
        assert response.status_code == 422
        assert auth_client.get(SETTINGS).json()["keys"] == []

    def test_a_key_can_be_deleted_without_disturbing_the_others(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one", "two", "three"])
        response = auth_client.delete(f"{SETTINGS}/keys/{keys[1]['id']}")
        assert response.status_code == 200
        assert [key["label"] for key in response.json()["keys"]] == ["one", "three"]

    def test_deleting_every_key_clears_the_pool(self, auth_client):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["one", "two"])
        assert auth_client.delete(SETTINGS).status_code == 204
        body = auth_client.get(SETTINGS).json()
        assert body["keys"] == []
        assert body["configured"] is False

    def test_deleting_an_unknown_key_is_a_404(self, auth_client):
        assert auth_client.delete(f"{SETTINGS}/keys/does-not-exist").status_code == 404

    def test_each_key_gets_its_own_envelope(self, auth_client, settings):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["one", "two"])
        with session_scope() as db:
            rows = settings_service.list_keys(db)
            # Distinct nonces, and each decrypts to its own plaintext: the AAD
            # binds every ciphertext to its own row id.
            assert rows[0].key_nonce != rows[1].key_nonce
            assert settings_service.reveal_key(db, settings, rows[0]) == key_for(0)
            assert settings_service.reveal_key(db, settings, rows[1]) == key_for(1)


# --- Health and manual control ----------------------------------------------


class TestKeyHealth:
    def test_a_key_can_be_disabled_and_re_enabled(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one", "two"])
        body = auth_client.patch(
            f"{SETTINGS}/keys/{keys[0]['id']}", json={"enabled": False}
        ).json()
        assert body["keys"][0]["status"] == "disabled"
        assert body["keys"][0]["available"] is False

        body = auth_client.patch(
            f"{SETTINGS}/keys/{keys[0]['id']}", json={"enabled": True}
        ).json()
        assert body["keys"][0]["status"] == "active"
        assert body["keys"][0]["available"] is True

    def test_a_key_can_be_renamed(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one"])
        body = auth_client.patch(
            f"{SETTINGS}/keys/{keys[0]['id']}", json={"label": "personal account"}
        ).json()
        assert body["keys"][0]["label"] == "personal account"

    def test_testing_a_stored_key_records_the_outcome(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one"])
        provider.quota_keys = {key_for(0)}

        result = auth_client.post(f"{SETTINGS}/keys/{keys[0]['id']}/test").json()
        assert result["valid"] is False
        assert result["result"] == "quota"
        assert key_state(auth_client) == {"one": "exhausted"}

        # And recovery is visible the same way.
        provider.quota_keys = set()
        assert auth_client.post(f"{SETTINGS}/keys/{keys[0]['id']}/test").json()["valid"] is True
        assert key_state(auth_client) == {"one": "active"}

    def test_re_enabling_clears_a_cooldown(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one"])
        with session_scope() as db:
            settings_service.mark_failure(db, keys[0]["id"], "provider_quota_exceeded")

        assert key_state(auth_client) == {"one": "exhausted"}
        body = auth_client.patch(
            f"{SETTINGS}/keys/{keys[0]['id']}", json={"enabled": True}
        ).json()
        assert body["keys"][0]["status"] == "active"
        assert body["keys"][0]["cooldownUntil"] is None

    def test_an_exhausted_key_returns_on_its_own_once_the_cooldown_lapses(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one"])
        with session_scope() as db:
            settings_service.mark_failure(db, keys[0]["id"], "provider_quota_exceeded")
            row = db.get(GeminiKey, keys[0]["id"])
            assert row.cooldown_until is not None
            assert not row.is_available()
            # Quota windows reopen; an invalid credential never would.
            assert row.is_available(row.cooldown_until + dt.timedelta(seconds=1))

    def test_an_invalid_key_never_returns_on_its_own(self, auth_client):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one"])
        with session_scope() as db:
            settings_service.mark_failure(db, keys[0]["id"], "provider_auth_failed")
            row = db.get(GeminiKey, keys[0]["id"])
            assert row.status == "invalid"
            assert row.cooldown_until is None
            assert not row.is_available(utcnow() + dt.timedelta(days=365))

    @pytest.mark.parametrize(
        "error_code,expected",
        [
            ("provider_quota_exceeded", "exhausted"),
            ("provider_auth_failed", "invalid"),
            ("provider_unavailable", "unavailable"),
        ],
    )
    def test_failure_codes_map_onto_visible_states(self, auth_client, error_code, expected):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["one"])
        with session_scope() as db:
            settings_service.mark_failure(db, keys[0]["id"], error_code)
        assert key_state(auth_client) == {"one": expected}


# --- Failover ----------------------------------------------------------------


@requires_media
class TestFailover:
    @pytest.fixture
    def source(self, tmp_path):
        return fixtures_media.hard_cuts(tmp_path / "media").path

    def test_a_quota_error_moves_the_run_to_the_next_key(self, auth_client, source):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["first", "second"])
        # Armed only after storage: a key is validated before it is accepted,
        # so it has to be healthy at the moment it is added.
        provider.quota_keys = {key_for(0)}
        job_id = run_job(auth_client, upload_file(auth_client, source))

        # The first key was tried, rejected, and the same request re-sent on
        # the second -- which answered, so no local fallback was needed.
        inference_keys = [
            key for key in provider.keys_seen if key in {key_for(0), key_for(1)}
        ][-2:]
        assert inference_keys == [key_for(0), key_for(1)]
        assert key_state(auth_client) == {"first": "exhausted", "second": "active"}

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "review-ready"
        assert job["usage"]["localFallbackUsed"] is False

    def test_an_auth_error_moves_the_run_to_the_next_key(self, auth_client, source):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["first", "second"])
        provider.auth_keys = {key_for(0)}
        job_id = run_job(auth_client, upload_file(auth_client, source))

        assert key_state(auth_client) == {"first": "invalid", "second": "active"}
        assert auth_client.get(f"{JOBS}/{job_id}").json()["usage"]["localFallbackUsed"] is False

    def test_failover_walks_the_whole_pool_in_order(self, auth_client, source):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["one", "two", "three", "four"])
        provider.quota_keys = {key_for(0), key_for(1), key_for(2)}
        run_job(auth_client, upload_file(auth_client, source))

        assert key_state(auth_client) == {
            "one": "exhausted",
            "two": "exhausted",
            "three": "exhausted",
            "four": "active",
        }

    def test_exhausting_every_key_falls_back_locally(self, auth_client, source):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["one", "two"])
        provider.quota_keys = {key_for(0), key_for(1)}
        job_id = run_job(auth_client, upload_file(auth_client, source))

        job = auth_client.get(f"{JOBS}/{job_id}").json()
        # Still a usable result: local scoring carries the run.
        assert job["state"] == "review-ready"
        assert job["eligibleCount"] == 4
        assert job["usage"]["localFallbackUsed"] is True
        assert key_state(auth_client) == {"one": "exhausted", "two": "exhausted"}

    def test_a_disabled_key_is_skipped(self, auth_client, source):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["first", "second"])
        auth_client.patch(f"{SETTINGS}/keys/{keys[0]['id']}", json={"enabled": False})

        run_job(auth_client, upload_file(auth_client, source))
        assert key_for(0) not in provider.keys_seen[1:]

    def test_a_key_already_in_cooldown_is_skipped(self, auth_client, source):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["first", "second"])
        with session_scope() as db:
            settings_service.mark_failure(db, keys[0]["id"], "provider_quota_exceeded")

        run_job(auth_client, upload_file(auth_client, source))
        # Ranking ran on the second key only; the first is still resting.
        assert key_for(0) not in provider.keys_seen[2:]
        assert key_state(auth_client) == {"first": "exhausted", "second": "active"}

    def test_a_successful_call_clears_an_earlier_failure(self, auth_client, source):
        provider = MockProvider()
        keys = configure_keys(auth_client, provider, ["only"])
        with session_scope() as db:
            row = db.get(GeminiKey, keys[0]["id"])
            row.status = "unavailable"
            row.last_error_code = "provider_unavailable"
            row.consecutive_failures = 3

        run_job(auth_client, upload_file(auth_client, source))
        body = auth_client.get(SETTINGS).json()["keys"][0]
        assert body["status"] == "active"
        assert body["lastErrorCode"] is None
        assert body["requestsSucceeded"] >= 1

    def test_no_key_at_all_runs_locally(self, auth_client, source):
        from app.providers.gemini import set_provider

        set_provider(MockProvider())
        job_id = run_job(
            auth_client, upload_file(auth_client, source), use_gemini=True
        )
        job = auth_client.get(f"{JOBS}/{job_id}").json()
        assert job["state"] == "review-ready"
        assert job["usage"]["localFallbackUsed"] is True

    def test_failover_requests_are_counted_against_the_cap(self, auth_client, source):
        # Both the rejected call and the one that replaced it were real
        # outbound requests, so both are charged (spec 6.7).
        provider = MockProvider()
        configure_keys(auth_client, provider, ["first", "second"], request_cap=8)
        provider.quota_keys = {key_for(0)}
        job_id = run_job(auth_client, upload_file(auth_client, source))

        usage = auth_client.get(f"{JOBS}/{job_id}").json()["usage"]
        assert usage["requestsUsed"] >= 2

    def test_failover_stops_when_the_cap_runs_out(self, auth_client, source):
        provider = MockProvider()
        configure_keys(auth_client, provider, ["first", "second"], request_cap=1)
        provider.quota_keys = {key_for(0)}
        job_id = run_job(auth_client, upload_file(auth_client, source))

        usage = auth_client.get(f"{JOBS}/{job_id}").json()["usage"]
        assert usage["requestsUsed"] == 1
        assert usage["localFallbackUsed"] is True
        # The cap, not the key pool, is what stopped the run.
        assert key_state(auth_client) == {"first": "exhausted", "second": "active"}


# --- Legacy single-key compatibility ----------------------------------------


class TestSingleKeyBehaviourIsPreserved:
    def test_one_key_still_configures_and_reports(self, auth_client):
        provider = MockProvider()
        configure_key(auth_client, provider, request_cap=10)
        body = auth_client.get(SETTINGS).json()
        assert body["configured"] is True
        assert body["requestCap"] == 10
        assert body["model"] == "gemini-test-model"
        assert len(body["keys"]) == 1
        assert body["anyAvailable"] is True

    def test_preferences_can_change_without_re_entering_a_key(self, auth_client):
        provider = MockProvider()
        configure_key(auth_client, provider, request_cap=10)
        body = auth_client.patch(SETTINGS, json={"requestCap": 3}).json()
        assert body["requestCap"] == 3
        assert len(body["keys"]) == 1
        assert body["configured"] is True
