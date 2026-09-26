"""Server-side session lifecycle (spec 5.1).

The cookie carries a high-entropy opaque token; only its SHA-256 is persisted,
so a database read cannot reconstruct a usable cookie. Login always mints a new
session row and deletes the prior one, which rotates the session identifier.
Sessions expire on both an idle timeout and an absolute lifetime.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.config import Settings
from app.models import Session as SessionRow
from app.models import utcnow

SESSION_COOKIE_NAME = "clipper_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
_TOKEN_BYTES = 32


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    db: DbSession, settings: Settings, username: str, *, replace_token: str | None = None
) -> tuple[str, SessionRow]:
    """Mint a new session, rotating away from ``replace_token`` if given."""
    if replace_token:
        db.execute(delete(SessionRow).where(SessionRow.token_hash == hash_token(replace_token)))

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = utcnow()
    row = SessionRow(
        token_hash=hash_token(token),
        csrf_token=secrets.token_urlsafe(_TOKEN_BYTES),
        username=username,
        created_at=now,
        last_seen_at=now,
        idle_expires_at=now + dt.timedelta(minutes=settings.session_idle_minutes),
        absolute_expires_at=now + dt.timedelta(hours=settings.session_absolute_hours),
    )
    db.add(row)
    db.flush()
    return token, row


def load_session(db: DbSession, settings: Settings, token: str | None) -> SessionRow | None:
    """Resolve a cookie token to a live session, sliding the idle window."""
    if not token:
        return None
    row = db.execute(
        select(SessionRow).where(SessionRow.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if row is None:
        return None

    now = utcnow()
    if row.revoked_at is not None or now >= row.idle_expires_at or now >= row.absolute_expires_at:
        db.delete(row)
        return None

    row.last_seen_at = now
    row.idle_expires_at = min(
        now + dt.timedelta(minutes=settings.session_idle_minutes), row.absolute_expires_at
    )
    return row


def revoke_session(db: DbSession, token: str | None) -> None:
    """Invalidate immediately; the row is removed rather than flagged."""
    if not token:
        return
    db.execute(delete(SessionRow).where(SessionRow.token_hash == hash_token(token)))


def purge_expired_sessions(db: DbSession) -> int:
    now = utcnow()
    result = db.execute(
        delete(SessionRow).where(
            (SessionRow.absolute_expires_at <= now) | (SessionRow.idle_expires_at <= now)
        )
    )
    return int(result.rowcount or 0)


def session_cookie_kwargs(settings: Settings) -> dict[str, object]:
    """Cookie attributes required by spec 5.1."""
    return {
        "httponly": True,
        "samesite": "strict",
        "secure": settings.cookie_secure,
        "path": "/",
        # No Max-Age: the cookie is a session cookie; the server holds the
        # authoritative idle and absolute expiry.
    }


def csrf_token_matches(expected: str, provided: str | None) -> bool:
    if not provided:
        return False
    return secrets.compare_digest(expected, provided)
