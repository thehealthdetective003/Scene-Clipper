"""Authentication routes (spec 8.2).

Login carries no CSRF token -- there is no session to bind one to yet -- so it
is protected by exact same-origin validation, Fetch Metadata checks, and rate
limiting instead (spec 5.1). Request bodies on these routes are never logged.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.deps import CsrfDep, DbDep, RequestIdDep, SettingsDep
from app.api.errors import rate_limited, unauthorized
from app.db import session_scope
from app.schemas import LoginRequest, SessionResponse
from app.security.origin import client_ip, enforce_same_origin
from app.security.passwords import verify_password, verify_username
from app.security.ratelimit import check_rate_limit
from app.security.sessions import (
    SESSION_COOKIE_NAME,
    create_session,
    load_session,
    revoke_session,
    session_cookie_kwargs,
)
from app.services import audit

router = APIRouter(tags=["auth"])


@router.post("/auth/login", status_code=status.HTTP_204_NO_CONTENT)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> Response:
    enforce_same_origin(request, settings.allowed_origin)

    identity = client_ip(request, settings.trust_proxy_headers)
    limit = check_rate_limit("login", identity, settings.login_rate_limit_per_minute)
    if not limit.allowed:
        audit.record(
            db,
            audit.LOGIN_FAILED,
            outcome="throttled",
            request_id=request_id,
            detail={"reason": "rate_limited"},
        )
        raise rate_limited(
            "Too many sign-in attempts. Wait a moment and try again.",
            limit.retry_after_seconds,
        )

    username_ok = verify_username(payload.username, settings.admin_username)
    # Always run the hash comparison so a wrong username and a wrong password
    # cost the same amount of time.
    password_ok = verify_password(payload.password, settings.admin_password_hash)

    if not (username_ok and password_ok):
        audit.record(
            db,
            audit.LOGIN_FAILED,
            outcome="failure",
            request_id=request_id,
            detail={"reason": "invalid_credentials"},
        )
        raise unauthorized("The username or password is incorrect.")

    existing = request.cookies.get(SESSION_COOKIE_NAME)
    token, _session = create_session(
        db, settings, settings.admin_username, replace_token=existing
    )
    audit.record(
        db, audit.LOGIN_SUCCEEDED, subject=settings.admin_username, request_id=request_id
    )

    response.status_code = status.HTTP_204_NO_CONTENT
    response.set_cookie(SESSION_COOKIE_NAME, token, **session_cookie_kwargs(settings))  # type: ignore[arg-type]
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> Response:
    revoke_session(db, auth.token)
    audit.record(db, audit.LOGOUT, subject=auth.username, request_id=request_id)
    response.status_code = status.HTTP_204_NO_CONTENT
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
    )
    return response


@router.get("/session", response_model=SessionResponse, response_model_by_alias=True)
def read_session(request: Request, settings: SettingsDep) -> SessionResponse:
    """Report auth state and hand the UI a CSRF token to keep in memory only."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    # A short independent transaction: this route is polled and must not hold a
    # write lock open behind the request-scoped session.
    with session_scope() as db:
        session = load_session(db, settings, token)
        if session is None:
            return SessionResponse(authenticated=False, csrf_token=None)
        return SessionResponse(authenticated=True, csrf_token=session.csrf_token)
