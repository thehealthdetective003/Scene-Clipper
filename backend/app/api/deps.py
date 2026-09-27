"""FastAPI dependencies for authentication, CSRF, and request context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DbSession

from app.api.errors import forbidden, unauthorized
from app.config import Settings, get_settings
from app.db import db_session
from app.models import Session as SessionRow
from app.security.origin import enforce_same_origin
from app.security.sessions import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    csrf_token_matches,
    load_session,
)

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[DbSession, Depends(db_session)]

#: Methods that mutate state and therefore require CSRF + origin validation.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(slots=True)
class AuthContext:
    session: SessionRow
    username: str
    token: str


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def require_session(request: Request, db: DbDep, settings: SettingsDep) -> AuthContext:
    """Authenticate the caller. Every non-auth route depends on this."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = load_session(db, settings, token)
    if session is None or token is None:
        raise unauthorized("Reload the app to start a new local session.")
    return AuthContext(session=session, username=session.username, token=token)


def require_csrf(request: Request, auth: Annotated[AuthContext, Depends(require_session)], settings: SettingsDep) -> AuthContext:
    """Authenticated state-changing requests need CSRF + exact same-origin.

    Spec 5.1: "Every authenticated state-changing request MUST require a CSRF
    token and exact same-origin validation."
    """
    if request.method.upper() not in _UNSAFE_METHODS:
        return auth

    enforce_same_origin(request, settings.allowed_origin)

    provided = request.headers.get(CSRF_HEADER_NAME)
    if not csrf_token_matches(auth.session.csrf_token, provided):
        raise forbidden(
            "invalid_csrf_token",
            "The CSRF token is missing or no longer valid. Reload the page and retry.",
        )
    return auth


AuthDep = Annotated[AuthContext, Depends(require_session)]
CsrfDep = Annotated[AuthContext, Depends(require_csrf)]
RequestIdDep = Annotated[str, Depends(get_request_id)]
