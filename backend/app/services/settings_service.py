"""Gemini key pool, failover order, and per-key health (spec 5.2).

Up to :data:`MAX_GEMINI_KEYS` credentials are stored so a job keeps running when
one hits its quota. A plaintext key exists in memory only twice: while a save
request is being encrypted, and immediately before an outbound provider call. It
is never returned by the API (not even masked), never placed in a queue payload,
and never written anywhere but as AES-256-GCM ciphertext.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import conflict, not_found, validation_error
from app.config import Settings as AppSettings
from app.models import (
    KEY_ACTIVE,
    KEY_DISABLED,
    KEY_EXHAUSTED,
    KEY_INVALID,
    KEY_UNAVAILABLE,
    MAX_GEMINI_KEYS,
    GeminiKey,
    utcnow,
)
from app.models import (
    Settings as SettingsRow,
)
from app.security.crypto import DecryptionError, EncryptedSecret, decrypt_secret, encrypt_secret
from app.source_labels import normalize_style

#: How long a key rests after each failure class before it is retried.
#: A free-tier quota typically resets on a daily boundary, but retrying sooner
#: costs only one request and recovers much faster when the limit was per-minute.
COOLDOWN = {
    KEY_EXHAUSTED: dt.timedelta(minutes=15),
    KEY_UNAVAILABLE: dt.timedelta(minutes=2),
}

#: Provider error codes mapped onto a key health state.
ERROR_STATUS = {
    "provider_auth_failed": KEY_INVALID,
    "provider_quota_exceeded": KEY_EXHAUSTED,
    "provider_unavailable": KEY_UNAVAILABLE,
    "provider_rejected_request": KEY_UNAVAILABLE,
}


# --- Deployment settings row ------------------------------------------------


def get_or_create(db: DbSession, app_settings: AppSettings) -> SettingsRow:
    """The deployment holds exactly one settings row."""
    row = db.execute(select(SettingsRow).limit(1)).scalar_one_or_none()
    if row is None:
        row = SettingsRow(
            gemini_model=app_settings.gemini_model,
            gemini_request_cap=app_settings.gemini_request_cap,
        )
        db.add(row)
        db.flush()
    return row


def update_preferences(
    db: DbSession, app_settings: AppSettings, *, model: str | None, request_cap: int | None
) -> SettingsRow:
    row = get_or_create(db, app_settings)
    if model:
        row.gemini_model = model
    if request_cap is not None:
        row.gemini_request_cap = request_cap
    db.flush()
    return row


def source_label_style(row: SettingsRow) -> dict[str, object]:
    """Return the canonical API/job-snapshot representation."""
    return normalize_style(
        {
            "fontPreset": row.source_label_font_preset,
            "fillColor": row.source_label_fill_color,
            "outlineColor": row.source_label_outline_color,
            "sizePercent": row.source_label_size_percent,
        }
    )


def update_source_label_style(
    db: DbSession,
    app_settings: AppSettings,
    *,
    font_preset: str,
    fill_color: str,
    outline_color: str,
    size_percent: float,
) -> SettingsRow:
    """Replace all source-label defaults atomically."""
    style = normalize_style(
        {
            "fontPreset": font_preset,
            "fillColor": fill_color,
            "outlineColor": outline_color,
            "sizePercent": size_percent,
        }
    )
    row = get_or_create(db, app_settings)
    row.source_label_font_preset = str(style["fontPreset"])
    row.source_label_fill_color = str(style["fillColor"])
    row.source_label_outline_color = str(style["outlineColor"])
    row.source_label_size_percent = float(style["sizePercent"])
    db.flush()
    return row


# --- Key pool ---------------------------------------------------------------


def list_keys(db: DbSession) -> list[GeminiKey]:
    return list(
        db.execute(select(GeminiKey).order_by(GeminiKey.position.asc())).scalars()
    )


def get_key(db: DbSession, key_id: str) -> GeminiKey:
    row = db.get(GeminiKey, key_id)
    if row is None:
        raise not_found("API key")
    return row


def count_keys(db: DbSession) -> int:
    return int(db.execute(select(func.count()).select_from(GeminiKey)).scalar_one())


def add_key(
    db: DbSession, app_settings: AppSettings, *, api_key: str, label: str | None = None
) -> GeminiKey:
    """Encrypt and append a key to the failover pool."""
    if count_keys(db) >= MAX_GEMINI_KEYS:
        raise conflict(
            "key_pool_full",
            f"At most {MAX_GEMINI_KEYS} API keys can be stored. Delete one first.",
            {"maxKeys": MAX_GEMINI_KEYS},
        )
    if not api_key.strip():
        raise validation_error("invalid_api_key", "The API key is empty.")

    next_position = int(
        db.execute(select(func.coalesce(func.max(GeminiKey.position), 0))).scalar_one()
    ) + 1

    row = GeminiKey(
        label=(label or "").strip()[:64] or f"Key {next_position}",
        position=next_position,
        # Placeholders replaced immediately below, once the row has an id to
        # bind the ciphertext to.
        key_version=0,
        key_nonce=b"",
        key_ciphertext=b"",
        key_tag=b"",
        status=KEY_ACTIVE,
    )
    db.add(row)
    db.flush()

    sealed = encrypt_secret(
        api_key, master_key=app_settings.encryption_key_bytes, record_id=row.id
    )
    row.key_version = sealed.version
    row.key_nonce = sealed.nonce
    row.key_ciphertext = sealed.ciphertext
    row.key_tag = sealed.tag
    db.flush()
    return row


def delete_key(db: DbSession, key_id: str) -> None:
    db.delete(get_key(db, key_id))
    db.flush()


def delete_all_keys(db: DbSession) -> int:
    rows = list_keys(db)
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)


def rename_key(db: DbSession, key_id: str, label: str) -> GeminiKey:
    row = get_key(db, key_id)
    row.label = label.strip()[:64] or f"Key {row.position}"
    db.flush()
    return row


def set_enabled(db: DbSession, key_id: str, enabled: bool) -> GeminiKey:
    row = get_key(db, key_id)
    if enabled:
        row.status = KEY_ACTIVE
        row.cooldown_until = None
        row.consecutive_failures = 0
        row.last_error_code = None
    else:
        row.status = KEY_DISABLED
    db.flush()
    return row


def reveal_key(db: DbSession, app_settings: AppSettings, row: GeminiKey) -> str:
    """Decrypt one key for a single immediate outbound call.

    Callers must not cache, log, or enqueue the result.
    """
    sealed = EncryptedSecret(
        version=int(row.key_version),
        nonce=bytes(row.key_nonce),
        ciphertext=bytes(row.key_ciphertext),
        tag=bytes(row.key_tag),
    )
    return decrypt_secret(
        sealed, master_key=app_settings.encryption_key_bytes, record_id=row.id
    )


# --- Failover ---------------------------------------------------------------


def available_keys(db: DbSession) -> list[GeminiKey]:
    """Keys usable right now, in failover order."""
    now = utcnow()
    return [row for row in list_keys(db) if row.is_available(now)]


def next_key(db: DbSession, *, skip_ids: set[str] | None = None) -> GeminiKey | None:
    """The highest-priority key not already tried in this attempt."""
    skip = skip_ids or set()
    for row in available_keys(db):
        if row.id not in skip:
            return row
    return None


def mark_success(db: DbSession, key_id: str) -> None:
    row = db.get(GeminiKey, key_id)
    if row is None:
        return
    row.status = KEY_ACTIVE
    row.cooldown_until = None
    row.consecutive_failures = 0
    row.last_error_code = None
    row.last_success_at = utcnow()
    row.requests_succeeded += 1
    db.flush()


def mark_failure(db: DbSession, key_id: str, error_code: str) -> str:
    """Record a failed call and move the key into the matching health state.

    Returns the new status so the caller can decide whether to fail over.
    """
    row = db.get(GeminiKey, key_id)
    if row is None:
        return KEY_ACTIVE

    status = ERROR_STATUS.get(error_code, KEY_UNAVAILABLE)
    row.status = status
    row.last_error_code = error_code
    row.last_error_at = utcnow()
    row.consecutive_failures += 1
    row.requests_failed += 1

    cooldown = COOLDOWN.get(status)
    # An invalid credential never self-heals; it waits for the operator.
    row.cooldown_until = (utcnow() + cooldown) if cooldown else None
    db.flush()
    return status


def any_key_configured(db: DbSession) -> bool:
    return count_keys(db) > 0


def any_key_usable(db: DbSession, app_settings: AppSettings) -> bool:
    """True when at least one stored key both decrypts and is available."""
    for row in available_keys(db):
        try:
            if reveal_key(db, app_settings, row):
                return True
        except DecryptionError:
            continue
    return False
