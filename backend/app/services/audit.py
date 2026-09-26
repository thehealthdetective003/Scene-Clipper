"""Audit trail for security-relevant actions (spec 10.1).

Recorded: login success/failure, key replacement/deletion, job deletion, and
export creation. Details are limited to identifiers and counts -- never
secrets, request bodies, or media payloads.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.logging_setup import scrub_value
from app.models import AuditEvent

LOGIN_SUCCEEDED = "auth.login.succeeded"
LOGIN_FAILED = "auth.login.failed"
LOGOUT = "auth.logout"
GEMINI_KEY_REPLACED = "settings.gemini_key.replaced"
GEMINI_KEY_ADDED = "settings.gemini_key.added"
GEMINI_KEY_UPDATED = "settings.gemini_key.updated"
GEMINI_KEY_DELETED = "settings.gemini_key.deleted"
GEMINI_KEY_TESTED = "settings.gemini_key.tested"
GEMINI_PREFERENCES_UPDATED = "settings.gemini.preferences_updated"
SOURCE_LABEL_SETTINGS_UPDATED = "settings.source_label.updated"
JOB_CREATED = "job.created"
JOB_DELETED = "job.deleted"
EXPORT_CREATED = "export.created"


def record(
    db: DbSession,
    event_type: str,
    *,
    outcome: str = "success",
    subject: str | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    safe_detail = {key: scrub_value(key, value) for key, value in (detail or {}).items()}
    db.add(
        AuditEvent(
            event_type=event_type,
            outcome=outcome,
            subject=subject,
            request_id=request_id,
            detail=safe_detail,
        )
    )
