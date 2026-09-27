"""Automatic local-session bootstrap.

Scene Clipper is a single-user, self-hosted tool and does not present a login
screen. The browser still receives a short-lived, server-side session so CSRF
tokens and same-origin checks continue to protect every state-changing route.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.deps import SettingsDep
from app.db import session_scope
from app.schemas import SessionResponse
from app.security.sessions import (
    SESSION_COOKIE_NAME,
    create_session,
    load_session,
    session_cookie_kwargs,
)

router = APIRouter(tags=["session"])

_LOCAL_PRINCIPAL = "local-user"


@router.get("/session", response_model=SessionResponse, response_model_by_alias=True)
def read_session(
    request: Request, response: Response, settings: SettingsDep
) -> SessionResponse:
    """Return the current session, creating one automatically when needed."""
    token = request.cookies.get(SESSION_COOKIE_NAME)

    # A short independent transaction: this route is called during UI startup
    # and must not hold a write lock behind the request-scoped dependencies.
    with session_scope() as db:
        session = load_session(db, settings, token)
        if session is None:
            token, session = create_session(
                db, settings, _LOCAL_PRINCIPAL, replace_token=token
            )
            response.set_cookie(
                SESSION_COOKIE_NAME,
                token,
                **session_cookie_kwargs(settings),  # type: ignore[arg-type]
            )

        return SessionResponse(authenticated=True, csrf_token=session.csrf_token)
