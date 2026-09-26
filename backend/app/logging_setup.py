"""Structured logging with hard redaction guarantees.

Spec 11 restricts logs to request IDs, byte counts, status codes, timings,
tool exit codes, and sanitized error categories. Request bodies, media
payloads, provider payloads, environment values, cookies, and authorization
headers must never be recorded.

The :class:`RedactionFilter` is a defence in depth: correct call sites never
pass secrets, and this filter scrubs anything that slips through -- including
tracebacks raised from third-party libraries that may embed a key in a URL.
"""

from __future__ import annotations

import json
import logging
import logging.config
import re
from typing import Any

#: Field names whose values are dropped wholesale.
_SENSITIVE_KEYS = {
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "password_hash",
    "csrf",
    "csrftoken",
    "csrf_token",
    "session",
    "token",
    "secret",
    "app_encryption_key",
    "admin_password_hash",
    "x-goog-api-key",
}

#: Patterns that look like provider credentials wherever they appear in text.
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z\-_]{10,}"),                       # Google API key
    re.compile(r"(?i)\bkey=[^&\s\"']+"),                          # key= query param
    re.compile(r"(?i)\b(x-goog-api-key|authorization)\s*[:=]\s*\S+"),
)

REDACTED = "[redacted]"


def scrub_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def scrub_value(key: str, value: Any) -> Any:
    if key.lower().replace("-", "_") in _SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: scrub_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(key, item) for item in value]
    return value


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: scrub_value(k, v) for k, v in record.args.items()}
            else:
                record.args = tuple(scrub_value("", arg) for arg in record.args)
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            record.context = {k: scrub_value(k, v) for k, v in extra.items()}
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with only allow-listed structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)
        for attr in ("request_id", "job_id", "export_id", "upload_id"):
            value = getattr(record, attr, None)
            if value is not None:
                payload[attr] = value
        if record.exc_info:
            # Type and sanitized message only; no locals, no payload echo.
            exc_type, exc_value, _ = record.exc_info
            payload["errorType"] = getattr(exc_type, "__name__", "Exception")
            payload["errorMessage"] = scrub_text(str(exc_value))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn's access log would record full query strings; the application
    # emits its own sanitized access record instead.
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = False
    for chatty in ("httpx", "httpcore", "google_genai", "urllib3", "pyscenedetect"):
        logging.getLogger(chatty).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
