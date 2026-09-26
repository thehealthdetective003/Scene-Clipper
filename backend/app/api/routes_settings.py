"""Gemini key-pool management routes (spec 8.2).

Up to :data:`MAX_GEMINI_KEYS` credentials are held so a run survives one key
hitting its quota. A stored key is never returned, not even masked; only its
label and health are exposed. Request bodies on these routes are excluded from
logging and tracing (spec 5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.deps import CsrfDep, DbDep, RequestIdDep, SettingsDep
from app.api.errors import forbidden, rate_limited, validation_error
from app.api.serializers import gemini_key_model, iso
from app.models import MAX_GEMINI_KEYS
from app.providers.gemini import get_provider
from app.schemas import (
    GeminiKeyPatch,
    GeminiKeyTestRequest,
    GeminiKeyTestResponse,
    GeminiPreferencesUpdate,
    GeminiSettingsResponse,
    GeminiSettingsUpdate,
    SourceLabelSettingsResponse,
    SourceLabelSettingsUpdate,
)
from app.security.crypto import DecryptionError
from app.security.origin import client_ip, is_loopback_origin
from app.security.ratelimit import check_rate_limit
from app.services import audit, settings_service

router = APIRouter(tags=["settings"])

#: Sanitized test outcomes mapped onto the provider error codes that
#: ``settings_service.mark_failure`` understands.
_TEST_ERROR_CODES = {
    "invalid_key": "provider_auth_failed",
    "quota": "provider_quota_exceeded",
    "unavailable": "provider_unavailable",
    "unsupported_model": "provider_rejected_request",
}


def _require_secure_transport(request: Request, settings) -> None:  # noqa: ANN001
    """The key may only cross an HTTPS connection, or a loopback origin.

    Spec 5.2 permits ``http://localhost`` and loopback IPs for same-machine
    development and self-hosting.
    """
    forwarded_proto = (
        request.headers.get("x-forwarded-proto") if settings.trust_proxy_headers else None
    )
    scheme = forwarded_proto or request.url.scheme
    if scheme == "https":
        return
    origin = request.headers.get("origin") or str(request.base_url)
    if is_loopback_origin(origin) or settings.is_loopback:
        return
    raise forbidden(
        "insecure_transport",
        "An API key may only be submitted over HTTPS.",
    )


def _settings_response(db, settings) -> GeminiSettingsResponse:  # noqa: ANN001
    row = settings_service.get_or_create(db, settings)
    keys = settings_service.list_keys(db)
    return GeminiSettingsResponse(
        configured=bool(keys),
        model=row.gemini_model,
        request_cap=row.gemini_request_cap,
        updated_at=iso(row.key_updated_at),
        keys=[gemini_key_model(key) for key in keys],
        max_keys=MAX_GEMINI_KEYS,
        any_available=any(key.is_available() for key in keys),
    )


def _source_label_response(row) -> SourceLabelSettingsResponse:  # noqa: ANN001
    style = settings_service.source_label_style(row)
    return SourceLabelSettingsResponse(
        font_preset=style["fontPreset"],  # type: ignore[arg-type]
        fill_color=str(style["fillColor"]),
        outline_color=str(style["outlineColor"]),
        size_percent=float(style["sizePercent"]),
        updated_at=iso(row.updated_at),
    )


@router.get(
    "/settings/source-label",
    response_model=SourceLabelSettingsResponse,
    response_model_by_alias=True,
)
def read_source_label_settings(
    db: DbDep, settings: SettingsDep, auth: CsrfDep
) -> SourceLabelSettingsResponse:
    return _source_label_response(settings_service.get_or_create(db, settings))


@router.put(
    "/settings/source-label",
    response_model=SourceLabelSettingsResponse,
    response_model_by_alias=True,
)
def replace_source_label_settings(
    payload: SourceLabelSettingsUpdate,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> SourceLabelSettingsResponse:
    row = settings_service.update_source_label_style(
        db,
        settings,
        font_preset=payload.font_preset,
        fill_color=payload.fill_color,
        outline_color=payload.outline_color,
        size_percent=payload.size_percent,
    )
    audit.record(
        db,
        audit.SOURCE_LABEL_SETTINGS_UPDATED,
        subject=auth.username,
        request_id=request_id,
        detail={
            "fontPreset": row.source_label_font_preset,
            "fillColor": row.source_label_fill_color,
            "outlineColor": row.source_label_outline_color,
            "sizePercent": row.source_label_size_percent,
        },
    )
    return _source_label_response(row)


def _enforce_test_rate_limit(request: Request, settings) -> None:  # noqa: ANN001
    identity = client_ip(request, settings.trust_proxy_headers)
    limit = check_rate_limit("gemini_key_test", identity, settings.key_test_rate_limit_per_minute)
    if not limit.allowed:
        raise rate_limited(
            "Too many key tests. Wait a moment and try again.", limit.retry_after_seconds
        )


@router.get(
    "/settings/gemini", response_model=GeminiSettingsResponse, response_model_by_alias=True
)
def read_gemini_settings(db: DbDep, settings: SettingsDep, auth: CsrfDep) -> GeminiSettingsResponse:
    return _settings_response(db, settings)


@router.post(
    "/settings/gemini/test",
    response_model=GeminiKeyTestResponse,
    response_model_by_alias=True,
)
def test_gemini_key(
    payload: GeminiKeyTestRequest,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> GeminiKeyTestResponse:
    """Validate a key without persisting it (spec 8.2)."""
    _require_secure_transport(request, settings)
    _enforce_test_rate_limit(request, settings)

    model = payload.model or settings.gemini_model
    result = get_provider(settings.gemini_timeout_seconds).test_key(payload.api_key, model)
    audit.record(
        db,
        audit.GEMINI_KEY_TESTED,
        outcome="success" if result.valid else "failure",
        subject=auth.username,
        request_id=request_id,
        detail={"model": model, "result": result.result},
    )
    return GeminiKeyTestResponse(valid=result.valid, result=result.result, message=result.message)


@router.put(
    "/settings/gemini", response_model=GeminiSettingsResponse, response_model_by_alias=True
)
def add_gemini_key(
    payload: GeminiSettingsUpdate,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> GeminiSettingsResponse:
    """Append a credential to the failover pool.

    The key is validated against the provider before it is stored, so a typo
    never sits silently in the rotation waiting to burn a request mid-job.
    """
    _require_secure_transport(request, settings)

    model = payload.model or settings.gemini_model
    result = get_provider(settings.gemini_timeout_seconds).test_key(payload.api_key, model)
    if not result.valid:
        audit.record(
            db,
            audit.GEMINI_KEY_ADDED,
            outcome="failure",
            subject=auth.username,
            request_id=request_id,
            detail={"model": model, "result": result.result},
        )
        # 422: the submitted value failed validation. The provider's own
        # message is not echoed.
        raise validation_error("gemini_key_invalid", result.message, {"result": result.result})

    row = settings_service.add_key(db, settings, api_key=payload.api_key, label=payload.label)
    settings_service.update_preferences(
        db, settings, model=model, request_cap=payload.request_cap
    )
    audit.record(
        db,
        audit.GEMINI_KEY_ADDED,
        subject=auth.username,
        request_id=request_id,
        detail={
            "model": model,
            "requestCap": payload.request_cap,
            "keyId": row.id,
            "position": row.position,
        },
    )
    return _settings_response(db, settings)


@router.patch(
    "/settings/gemini", response_model=GeminiSettingsResponse, response_model_by_alias=True
)
def update_gemini_preferences(
    payload: GeminiPreferencesUpdate,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> GeminiSettingsResponse:
    """Change model or request cap without touching the stored keys."""
    settings_service.update_preferences(
        db, settings, model=payload.model, request_cap=payload.request_cap
    )
    audit.record(
        db,
        audit.GEMINI_PREFERENCES_UPDATED,
        subject=auth.username,
        request_id=request_id,
        detail={"model": payload.model, "requestCap": payload.request_cap},
    )
    return _settings_response(db, settings)


@router.patch(
    "/settings/gemini/keys/{key_id}",
    response_model=GeminiSettingsResponse,
    response_model_by_alias=True,
)
def patch_gemini_key(
    key_id: str,
    payload: GeminiKeyPatch,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> GeminiSettingsResponse:
    """Rename a key, or take it in or out of the rotation.

    Re-enabling also clears the health state, which is how the operator says
    "the quota window has reset, try this one again" without waiting out the
    cooldown.
    """
    row = settings_service.get_key(db, key_id)
    if payload.label is not None:
        settings_service.rename_key(db, key_id, payload.label)
    if payload.enabled is not None:
        settings_service.set_enabled(db, key_id, payload.enabled)
    audit.record(
        db,
        audit.GEMINI_KEY_UPDATED,
        subject=auth.username,
        request_id=request_id,
        detail={"keyId": row.id, "enabled": payload.enabled, "renamed": payload.label is not None},
    )
    return _settings_response(db, settings)


@router.post(
    "/settings/gemini/keys/{key_id}/test",
    response_model=GeminiKeyTestResponse,
    response_model_by_alias=True,
)
def test_stored_gemini_key(
    key_id: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> GeminiKeyTestResponse:
    """Re-check a stored key and update its health from the result.

    This is the manual counterpart to failover: it tells the operator whether a
    key shown in red has recovered, and costs exactly one provider request.
    """
    _enforce_test_rate_limit(request, settings)

    row = settings_service.get_key(db, key_id)
    app_row = settings_service.get_or_create(db, settings)
    model = app_row.gemini_model or settings.gemini_model
    try:
        api_key = settings_service.reveal_key(db, settings, row)
    except DecryptionError:
        settings_service.mark_failure(db, row.id, "provider_auth_failed")
        raise validation_error(
            "gemini_key_unreadable",
            "This key could not be decrypted. Delete it and add it again.",
            {"keyId": row.id},
        ) from None

    result = get_provider(settings.gemini_timeout_seconds).test_key(api_key, model)
    del api_key
    if result.valid:
        settings_service.mark_success(db, row.id)
    else:
        settings_service.mark_failure(
            db, row.id, _TEST_ERROR_CODES.get(result.result, "provider_rejected_request")
        )
    audit.record(
        db,
        audit.GEMINI_KEY_TESTED,
        outcome="success" if result.valid else "failure",
        subject=auth.username,
        request_id=request_id,
        detail={"model": model, "result": result.result, "keyId": row.id},
    )
    return GeminiKeyTestResponse(valid=result.valid, result=result.result, message=result.message)


@router.delete(
    "/settings/gemini/keys/{key_id}",
    response_model=GeminiSettingsResponse,
    response_model_by_alias=True,
)
def delete_gemini_key(
    key_id: str,
    db: DbDep,
    settings: SettingsDep,
    auth: CsrfDep,
    request_id: RequestIdDep,
) -> GeminiSettingsResponse:
    settings_service.get_key(db, key_id)
    settings_service.delete_key(db, key_id)
    audit.record(
        db,
        audit.GEMINI_KEY_DELETED,
        subject=auth.username,
        request_id=request_id,
        detail={"keyId": key_id},
    )
    return _settings_response(db, settings)


@router.delete("/settings/gemini", status_code=status.HTTP_204_NO_CONTENT)
def delete_all_gemini_keys(
    db: DbDep, settings: SettingsDep, auth: CsrfDep, request_id: RequestIdDep
) -> Response:
    removed = settings_service.delete_all_keys(db)
    audit.record(
        db,
        audit.GEMINI_KEY_DELETED,
        subject=auth.username,
        request_id=request_id,
        detail={"removed": removed},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
