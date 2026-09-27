"""Standard error envelope and the stable error codes used across the API.

Every failure response has the shape defined in spec 8.1::

    {"error": {"code", "message", "retryable", "requestId", "details"}}

Messages are actionable but never disclose internal paths, provider payloads,
or secret material (spec 11).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """An error with a stable, user-facing code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers or {}

    def to_payload(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "requestId": request_id,
                "details": jsonable_encoder(self.details),
            }
        }


# --- Common constructors ---------------------------------------------------


def unauthorized(message: str = "Authentication is required.") -> AppError:
    return AppError("unauthorized", message, status_code=status.HTTP_401_UNAUTHORIZED)


def forbidden(code: str, message: str) -> AppError:
    return AppError(code, message, status_code=status.HTTP_403_FORBIDDEN)


def not_found(resource: str) -> AppError:
    return AppError(
        "not_found", f"The requested {resource} does not exist.", status_code=status.HTTP_404_NOT_FOUND
    )


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(code, message, status_code=status.HTTP_409_CONFLICT, details=details)


def validation_error(code: str, message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(
        # Literal 422: the Starlette constant was renamed between versions.
        code, message, status_code=422, details=details
    )


def unsupported_media(code: str, message: str) -> AppError:
    return AppError(code, message, status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)


def payload_too_large(message: str) -> AppError:
    return AppError(
        "upload_too_large", message, status_code=status.HTTP_413_CONTENT_TOO_LARGE
    )


def rate_limited(message: str, retry_after_seconds: int) -> AppError:
    return AppError(
        "rate_limited",
        message,
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        retryable=True,
        headers={"Retry-After": str(retry_after_seconds)},
    )


def dependency_unavailable(code: str, message: str) -> AppError:
    return AppError(
        code, message, status_code=status.HTTP_503_SERVICE_UNAVAILABLE, retryable=True
    )


def insufficient_storage(message: str = "Not enough disk space to continue.") -> AppError:
    return AppError(
        "insufficient_storage",
        message,
        status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
        retryable=True,
    )


def invalid_job_state(message: str, details: dict[str, Any] | None = None) -> AppError:
    return conflict("invalid_job_state", message, details)


# --- Handlers --------------------------------------------------------------


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(_request_id(request)),
        headers=exc.headers or None,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "upload_too_large",
        415: "unsupported_media_type",
        429: "rate_limited",
        503: "dependency_unavailable",
    }.get(exc.status_code, "request_failed")
    error = AppError(
        code,
        str(exc.detail) if exc.detail else "The request could not be completed.",
        status_code=exc.status_code,
        retryable=exc.status_code in (429, 503),
    )
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_payload(_request_id(request)),
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Echo only field locations and messages; never the submitted values, which
    # may contain a provider key (spec 5.2).
    fields = [
        {"field": ".".join(str(part) for part in err.get("loc", ())), "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    error = validation_error(
        "invalid_request", "The request failed validation.", {"fields": fields}
    )
    return JSONResponse(
        status_code=error.status_code, content=error.to_payload(_request_id(request))
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Deliberately opaque: the detail is available in the server log under the
    # same request id, never in the response body.
    error = AppError(
        "internal_error",
        "The server could not complete the request.",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        retryable=True,
    )
    return JSONResponse(
        status_code=error.status_code, content=error.to_payload(_request_id(request))
    )
