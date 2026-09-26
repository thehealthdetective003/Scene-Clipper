"""Request-scoped middleware: request IDs, security headers, access logging."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings
from app.logging_setup import get_logger
from app.util.ids import new_id

logger = get_logger("app.access")

#: Applied to every API response. The frontend's own HTML carries the same
#: policy from the reverse proxy (spec 10.1).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "Cache-Control": "no-store",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id and emit one sanitized access record per request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = new_id()
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request failed",
                extra={
                    "request_id": request_id,
                    "context": {
                        "method": request.method,
                        # Path only: query strings can carry user content.
                        "path": request.url.path,
                        "status": 500,
                        "durationMs": duration_ms,
                    },
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "context": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "durationMs": duration_ms,
                    "responseBytes": int(response.headers.get("content-length") or 0),
                },
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN001
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # HSTS only once TLS is actually in use (spec 10.1).
        if self._settings.cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized non-upload bodies before they are buffered.

    Upload chunk routes stream their body and enforce the declared length
    themselves, so they are exempt from this blanket limit.
    """

    def __init__(self, app, max_bytes: int = 1_048_576, exempt_suffixes: tuple[str, ...] = ()) -> None:  # noqa: ANN001
        super().__init__(app)
        self._max_bytes = max_bytes
        self._exempt = exempt_suffixes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not any(request.url.path.endswith(suffix) for suffix in self._exempt):
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > self._max_bytes:
                        from app.api.errors import app_error_handler, payload_too_large

                        return await app_error_handler(
                            request, payload_too_large("The request body is too large.")
                        )
                except ValueError:
                    pass
        return await call_next(request)
