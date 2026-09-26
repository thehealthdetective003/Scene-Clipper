"""Authentication, CSRF, origin, and security headers (spec 5.1, 10.1, 12.6)."""

from __future__ import annotations

import json

import pytest

from tests.conftest import TEST_PASSWORD

LOGIN = "/api/v1/auth/login"
SESSION = "/api/v1/session"
LOGOUT = "/api/v1/auth/logout"


def login(client, password=TEST_PASSWORD, username="admin"):
    return client.post(LOGIN, json={"username": username, "password": password})


class TestLogin:
    def test_correct_credentials_create_a_session(self, client):
        assert login(client).status_code == 204
        assert client.get(SESSION).json()["authenticated"] is True

    @pytest.mark.parametrize(
        ("username", "password"),
        [("admin", "wrong"), ("wrong", TEST_PASSWORD), ("", ""), ("Admin", TEST_PASSWORD)],
    )
    def test_incorrect_credentials_are_rejected(self, client, username, password):
        response = client.post(LOGIN, json={"username": username, "password": password})
        assert response.status_code in (401, 422)
        if response.status_code == 401:
            assert response.json()["error"]["code"] == "unauthorized"

    def test_failure_message_does_not_say_which_field_was_wrong(self, client):
        message = login(client, password="wrong").json()["error"]["message"]
        assert "username or password" in message.lower()

    def test_session_identifier_rotates_on_login(self, client):
        login(client)
        first = client.cookies.get("clipper_session")
        login(client)
        second = client.cookies.get("clipper_session")
        assert first and second and first != second

    def test_cookie_attributes(self, client):
        response = login(client)
        header = response.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=strict" in header or "SameSite=Strict" in header
        assert "Secure" in header  # HTTPS_ENABLED with a non-loopback base URL
        assert "Path=/" in header

    def test_rate_limiting_after_repeated_failures(self, client):
        codes = [login(client, password="wrong").status_code for _ in range(8)]
        assert 429 in codes
        throttled = login(client, password="wrong")
        if throttled.status_code == 429:
            assert throttled.headers.get("Retry-After")
            assert throttled.json()["error"]["retryable"] is True

    def test_password_is_never_echoed(self, client):
        response = client.post(
            LOGIN, json={"username": "admin", "password": "a-distinctive-wrong-password"}
        )
        assert "a-distinctive-wrong-password" not in response.text
        assert TEST_PASSWORD not in response.text


class TestOriginAndFetchMetadata:
    def test_cross_origin_login_is_blocked(self, client):
        response = client.post(
            LOGIN,
            json={"username": "admin", "password": TEST_PASSWORD},
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cross_origin_blocked"

    def test_missing_origin_is_blocked(self, client):
        client.headers.pop("Origin", None)
        response = client.post(
            LOGIN, json={"username": "admin", "password": TEST_PASSWORD}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "missing_origin"

    def test_referer_fallback_is_accepted(self, client):
        client.headers.pop("Origin", None)
        response = client.post(
            LOGIN,
            json={"username": "admin", "password": TEST_PASSWORD},
            headers={"Referer": "https://clips.test.internal/login"},
        )
        assert response.status_code == 204

    def test_cross_site_fetch_metadata_is_blocked(self, client):
        response = client.post(
            LOGIN,
            json={"username": "admin", "password": TEST_PASSWORD},
            headers={"Sec-Fetch-Site": "same-site"},
        )
        assert response.status_code == 403

    def test_top_level_navigation_is_blocked(self, client):
        response = client.post(
            LOGIN,
            json={"username": "admin", "password": TEST_PASSWORD},
            headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "document"},
        )
        assert response.status_code == 403


class TestCsrf:
    def test_state_changing_request_requires_a_token(self, client):
        login(client)
        response = client.delete("/api/v1/settings/gemini")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_csrf_token"

    def test_wrong_token_is_rejected(self, client):
        login(client)
        response = client.delete(
            "/api/v1/settings/gemini", headers={"X-CSRF-Token": "not-the-token"}
        )
        assert response.status_code == 403

    def test_correct_token_is_accepted(self, auth_client):
        assert auth_client.delete("/api/v1/settings/gemini").status_code == 204

    def test_cross_origin_mutation_with_a_valid_token_is_still_blocked(self, auth_client):
        response = auth_client.delete(
            "/api/v1/settings/gemini", headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cross_origin_blocked"

    def test_safe_requests_need_no_token(self, client):
        login(client)
        assert client.get("/api/v1/jobs").status_code == 200

    def test_csrf_token_is_not_a_cookie(self, auth_client):
        # The UI keeps it in memory only (spec 8.2).
        assert "csrf" not in "".join(auth_client.cookies.keys()).lower()


class TestSessionLifecycle:
    def test_unauthenticated_session_reports_false(self, client):
        body = client.get(SESSION).json()
        assert body["authenticated"] is False
        assert body["csrfToken"] is None

    def test_logout_invalidates_immediately(self, auth_client):
        assert auth_client.post(LOGOUT).status_code == 204
        assert auth_client.get(SESSION).json()["authenticated"] is False
        assert auth_client.get("/api/v1/jobs").status_code == 401

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/jobs"),
            ("get", "/api/v1/settings/gemini"),
            ("post", "/api/v1/uploads"),
            ("get", "/api/v1/jobs/some-id"),
            ("get", "/api/v1/jobs/some-id/candidates"),
            ("get", "/api/v1/jobs/some-id/exports/other-id/download"),
            ("put", "/api/v1/jobs/some-id/review"),
        ],
    )
    def test_protected_routes_require_authentication(self, client, method, path):
        send = getattr(client, method)
        response = send(path) if method == "get" else send(path, json={})
        assert response.status_code == 401, f"{method} {path}"

    def test_tampered_cookie_is_rejected(self, auth_client):
        auth_client.cookies.set("clipper_session", "forged-token-value")
        assert auth_client.get("/api/v1/jobs").status_code == 401


class TestSecurityHeaders:
    def test_standard_headers_are_present(self, client):
        headers = client.get(SESSION).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        assert "camera=()" in headers["Permissions-Policy"]

    def test_hsts_present_when_tls_is_enabled(self, client):
        assert "Strict-Transport-Security" in client.get(SESSION).headers

    def test_no_permissive_cors(self, client):
        assert "access-control-allow-origin" not in client.get(SESSION).headers

    def test_request_id_is_returned(self, client):
        assert client.get(SESSION).headers.get("X-Request-Id")


class TestErrorEnvelope:
    def test_unknown_route_uses_the_standard_shape(self, auth_client):
        body = auth_client.get("/api/v1/jobs/does-not-exist").json()
        assert set(body["error"]) == {"code", "message", "retryable", "requestId", "details"}
        assert body["error"]["code"] == "not_found"

    def test_validation_errors_do_not_echo_submitted_values(self, auth_client):
        response = auth_client.post(
            "/api/v1/uploads",
            json={"fileName": "x.mp4", "sizeBytes": -5, "mimeType": "video/mp4"},
        )
        assert response.status_code == 422
        # The submitted value must not be echoed back. Checked field by field
        # rather than over the whole body, because `requestId` is a random UUID
        # that can legitimately contain "-5".
        error = response.json()["error"]
        assert "-5" not in error["message"]
        assert "-5" not in json.dumps(error["details"] or {})
