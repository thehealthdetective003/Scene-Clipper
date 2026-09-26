"""Exact same-origin and Fetch Metadata validation (spec 5.1, 10.1).

Two independent checks guard every state-changing request:

1. ``Origin`` (falling back to ``Referer``) must equal the configured origin
   exactly -- scheme, host, and port. No suffix or prefix matching.
2. Fetch Metadata: ``Sec-Fetch-Site`` must be ``same-origin`` and the request
   must not be a navigation initiated by another document.

Login is additionally covered by these checks even though it carries no CSRF
token, because no session exists yet to bind one to.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import Request

from app.api.errors import forbidden

#: Browsers that predate Fetch Metadata send no Sec-Fetch-* headers. A request
#: with none of them present is allowed through to the Origin check alone.
_SAFE_FETCH_SITES = {"same-origin"}
_ALLOWED_FETCH_MODES = {"same-origin", "cors", "no-cors"}


def _origin_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def enforce_same_origin(request: Request, allowed_origin: str) -> None:
    """Raise ``403`` unless the request provably originates from this app."""
    origin_header = request.headers.get("origin")
    referer_header = request.headers.get("referer")

    candidate = _origin_of(origin_header) or _origin_of(referer_header)
    if candidate is None:
        raise forbidden(
            "missing_origin",
            "The request must include an Origin or Referer header from this application.",
        )
    if candidate != allowed_origin:
        raise forbidden("cross_origin_blocked", "Cross-origin requests are not permitted.")

    enforce_fetch_metadata(request)


def enforce_fetch_metadata(request: Request) -> None:
    site = request.headers.get("sec-fetch-site")
    if site is None:
        # Header absent: nothing to validate, the Origin check already ran.
        return
    if site not in _SAFE_FETCH_SITES:
        raise forbidden("cross_origin_blocked", "Cross-site requests are not permitted.")

    mode = request.headers.get("sec-fetch-mode")
    if mode is not None and mode not in _ALLOWED_FETCH_MODES:
        raise forbidden(
            "unsupported_fetch_mode",
            "This endpoint does not accept top-level navigation requests.",
        )

    dest = request.headers.get("sec-fetch-dest")
    if dest is not None and dest == "document":
        raise forbidden(
            "unsupported_fetch_dest",
            "This endpoint does not accept top-level navigation requests.",
        )


def is_loopback_origin(origin: str | None) -> bool:
    """``http://localhost`` and loopback IPs are permitted for self-hosting."""
    host = urlsplit(origin or "").hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def client_ip(request: Request, trust_proxy_headers: bool) -> str:
    """Resolve the client address, trusting forwarding headers only when told to."""
    if trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most entry is the original client as recorded by our proxy.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
