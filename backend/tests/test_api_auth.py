"""Automatic sessions, CSRF, origin checks, and security headers."""

from __future__ import annotations

import json

import pytest

SESSION = "/api/v1/session"


class TestAutomaticSession:
    def test_first_visit_creates_a_ready_session(self, client):
        response = client.get(SESSION)
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert response.json()["csrfToken"]
        assert client.cookies.get("clipper_session")

    def test_existing_session_is_reused(self, client):
        first = client.get(SESSION)
        cookie = client.cookies.get("clipper_session")
        csrf = first.json()["csrfToken"]

        second = client.get(SESSION)
        assert client.cookies.get("clipper_session") == cookie
        assert second.json()["csrfToken"] == csrf
        assert "set-cookie" not in second.headers

    def test_tampered_cookie_is_replaced_on_bootstrap(self, client):
        client.cookies.set(
            "clipper_session", "forged-token-value", domain="clips.test.internal", path="/"
        )
        response = client.get(SESSION)
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert client.cookies.get("clipper_session") != "forged-token-value"

    def test_cookie_attributes(self, client):
        header = client.get(SESSION).headers["set-cookie"]
        assert "HttpOnly" in header
        assert "SameSite=strict" in header or "SameSite=Strict" in header
        assert "Secure" in header
        assert "Path=/" in header

    @pytest.mark.parametrize("path", ["/api/v1/auth/login", "/api/v1/auth/logout"])
    def test_credential_routes_no_longer_exist(self, client, path):
        response = client.post(
            path, json={"username": "admin", "password": "secret"}
        )
        assert response.status_code == 404


class TestOriginAndCsrf:
    def test_state_changing_request_requires_a_token(self, client):
        client.get(SESSION)
        response = client.delete("/api/v1/settings/gemini")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "invalid_csrf_token"

    def test_wrong_token_is_rejected(self, client):
        client.get(SESSION)
        response = client.delete(
            "/api/v1/settings/gemini", headers={"X-CSRF-Token": "not-the-token"}
        )
        assert response.status_code == 403

    def test_correct_token_is_accepted(self, auth_client):
        assert auth_client.delete("/api/v1/settings/gemini").status_code == 204

    def test_cross_origin_mutation_with_a_valid_token_is_blocked(self, auth_client):
        response = auth_client.delete(
            "/api/v1/settings/gemini",
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cross_origin_blocked"

    def test_missing_origin_is_blocked(self, auth_client):
        auth_client.headers.pop("Origin", None)
        response = auth_client.delete("/api/v1/settings/gemini")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "missing_origin"

    def test_cross_site_fetch_metadata_is_blocked(self, auth_client):
        response = auth_client.delete(
            "/api/v1/settings/gemini", headers={"Sec-Fetch-Site": "same-site"}
        )
        assert response.status_code == 403

    def test_top_level_navigation_is_blocked(self, auth_client):
        response = auth_client.delete(
            "/api/v1/settings/gemini",
            headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "document"},
        )
        assert response.status_code == 403

    def test_safe_requests_need_no_token(self, client):
        client.get(SESSION)
        assert client.get("/api/v1/jobs").status_code == 200

    def test_csrf_token_is_not_a_cookie(self, auth_client):
        assert "csrf" not in "".join(auth_client.cookies.keys()).lower()


class TestSessionLifecycle:
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
    def test_protected_routes_require_bootstrap(self, client, method, path):
        send = getattr(client, method)
        response = send(path) if method == "get" else send(path, json={})
        assert response.status_code == 401, f"{method} {path}"

    def test_bootstrap_unlocks_protected_routes(self, client):
        client.get(SESSION)
        assert client.get("/api/v1/jobs").status_code == 200

    def test_tampered_cookie_is_rejected_until_rebootstrapped(self, auth_client):
        auth_client.cookies.set(
            "clipper_session", "forged-token-value", domain="clips.test.internal", path="/"
        )
        assert auth_client.get("/api/v1/jobs").status_code == 401
        assert auth_client.get(SESSION).status_code == 200
        assert auth_client.get("/api/v1/jobs").status_code == 200


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
        error = response.json()["error"]
        assert "-5" not in error["message"]
        assert "-5" not in json.dumps(error["details"] or {})
