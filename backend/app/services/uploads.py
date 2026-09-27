"""Resumable chunked uploads (spec 5.3, 8.3).

Chunk semantics
---------------
* The normal request offset equals the server's current verified offset.
* A higher offset is ``409 upload_offset_mismatch`` with ``expectedOffset``.
* A lower offset is accepted only when the range and checksum exactly match
  already verified bytes, and then returns the unchanged current offset.
* A mismatched retransmission is ``409 upload_chunk_mismatch``.
* Success is ``204 No Content`` carrying the new ``Upload-Offset``.

Bytes beyond the verified offset are untrusted, so a forward chunk is streamed
straight into the source file at its offset and the verified offset only
advances once the checksum matches. A crash mid-chunk therefore leaves the
verified offset untouched and the client simply resends from it.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import (
    AppError,
    conflict,
    not_found,
    payload_too_large,
    validation_error,
)
from app.config import Settings
from app.models import Upload, utcnow
from app.services import storage

READ_BLOCK_BYTES = 1024 * 1024
ASCII_CONTROL_LIMIT = 32

#: Terminal and transitional upload states (spec 8.3).
UPLOAD_STATES = ("created", "uploading", "downloading", "verifying", "ready", "failed")


@dataclass(frozen=True, slots=True)
class ChunkOutcome:
    offset: int
    duplicate: bool


def decode_checksum_header(value: str | None) -> bytes:
    """Parse ``Upload-Checksum: sha256 {base64Digest}``."""
    if not value:
        raise validation_error(
            "missing_checksum", "The Upload-Checksum header is required for every chunk."
        )
    algorithm, _, digest_b64 = value.strip().partition(" ")
    if algorithm.lower() != "sha256" or not digest_b64:
        raise validation_error(
            "unsupported_checksum", "Only 'sha256 {base64Digest}' checksums are supported."
        )
    try:
        digest = base64.b64decode(digest_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise validation_error(
            "invalid_checksum", "The Upload-Checksum digest is not valid base64."
        ) from exc
    if len(digest) != 32:
        raise validation_error(
            "invalid_checksum", "The Upload-Checksum digest is not a SHA-256 value."
        )
    return digest


def create_upload(
    db: DbSession,
    settings: Settings,
    *,
    file_name: str,
    size_bytes: int,
    mime_type: str | None,
    client_sha256: str | None,
) -> Upload:
    """Reserve an upload and its server-generated storage directory."""
    if size_bytes <= 0:
        raise validation_error("invalid_size", "The declared file size must be positive.")
    if size_bytes > settings.max_upload_bytes:
        raise payload_too_large(
            f"The file exceeds the configured maximum of {settings.max_upload_bytes} bytes."
        )
    if client_sha256 is not None and not _is_hex_sha256(client_sha256):
        raise validation_error("invalid_sha256", "The provided sha256 is not a hex digest.")

    display_name = _sanitize_display_name(file_name)
    upload = Upload(
        file_name=display_name,
        declared_mime_type=(mime_type or None),
        declared_size_bytes=size_bytes,
        verified_offset_bytes=0,
        chunk_size_bytes=settings.upload_chunk_bytes,
        state="created",
        source_kind="file",
        client_sha256=client_sha256.lower() if client_sha256 else None,
        storage_ext=storage.sanitize_extension(file_name),
    )
    db.add(upload)
    db.flush()

    # Pre-create the directory and a sparse file so the first chunk can seek.
    directory = storage.ensure_dir(upload.relative_dir, settings)
    source_path = directory / f"source.{upload.storage_ext}"
    if not source_path.exists():
        source_path.touch()
    return upload


def create_url_upload(db: DbSession, settings: Settings, *, url: str) -> Upload:
    """Create a durable placeholder for a single remote video download."""
    normalized = normalize_video_url(url)
    hostname = urlsplit(normalized).hostname or "video"
    upload = Upload(
        file_name=_sanitize_display_name(f"Video from {hostname}"),
        declared_mime_type=None,
        # yt-dlp may not know the final merged size until transfer time.
        declared_size_bytes=0,
        verified_offset_bytes=0,
        chunk_size_bytes=settings.upload_chunk_bytes,
        state="downloading",
        source_kind="url",
        source_url=normalized,
        storage_ext="bin",
    )
    db.add(upload)
    db.flush()
    storage.ensure_dir(upload.relative_dir, settings)
    return upload


def get_upload(db: DbSession, upload_id: str, *, include_deleted: bool = False) -> Upload:
    upload = db.get(Upload, upload_id)
    if upload is None or (upload.deleted_at is not None and not include_deleted):
        raise not_found("upload")
    return upload


def source_path(upload: Upload, settings: Settings) -> Path:
    return storage.resolve(upload.relative_source_path, settings)


def validate_chunk_request(
    upload: Upload, settings: Settings, *, offset: int, declared_length: int
) -> None:
    """Reject a chunk before any bytes are read (spec 5.3)."""
    if upload.state in ("downloading", "verifying", "ready"):
        raise conflict(
            "upload_already_complete", "This upload has already been completed."
        )
    if upload.state == "failed":
        raise conflict("upload_failed", "This upload failed and cannot accept more data.")
    if offset < 0:
        raise validation_error("invalid_offset", "Upload-Offset must not be negative.")
    if declared_length <= 0:
        raise validation_error("invalid_length", "Content-Length must be a positive value.")
    if declared_length > upload.chunk_size_bytes:
        raise payload_too_large(
            f"A chunk may not exceed {upload.chunk_size_bytes} bytes."
        )
    if offset + declared_length > upload.declared_size_bytes:
        raise payload_too_large(
            "The chunk would extend past the declared file size."
        )


def classify_offset(upload: Upload, offset: int) -> str:
    """``forward`` | ``replay`` | ``ahead`` relative to the verified offset."""
    if offset == upload.verified_offset_bytes:
        return "forward"
    if offset > upload.verified_offset_bytes:
        return "ahead"
    return "replay"


def offset_mismatch_error(upload: Upload) -> AppError:
    return conflict(
        "upload_offset_mismatch",
        "The chunk offset does not match the server's verified offset.",
        {"expectedOffset": upload.verified_offset_bytes},
    )


def verify_replayed_range(
    upload: Upload, settings: Settings, *, offset: int, length: int, digest: bytes
) -> ChunkOutcome:
    """Accept an identical retransmission of already-verified bytes."""
    if offset + length > upload.verified_offset_bytes:
        # Partially overlaps unverified territory: not a pure replay.
        raise conflict(
            "upload_chunk_mismatch",
            "The retransmitted range does not align with verified bytes.",
            {"expectedOffset": upload.verified_offset_bytes},
        )

    hasher = hashlib.sha256()
    with source_path(upload, settings).open("rb") as handle:
        handle.seek(offset)
        remaining = length
        while remaining > 0:
            block = handle.read(min(READ_BLOCK_BYTES, remaining))
            if not block:
                break
            remaining -= len(block)
            hasher.update(block)
    if remaining != 0 or hasher.digest() != digest:
        raise conflict(
            "upload_chunk_mismatch",
            "The retransmitted bytes differ from the bytes already stored.",
            {"expectedOffset": upload.verified_offset_bytes},
        )
    return ChunkOutcome(offset=upload.verified_offset_bytes, duplicate=True)


class ChunkWriter:
    """Streams one forward chunk into the source file at its offset."""

    def __init__(self, upload: Upload, settings: Settings, *, offset: int, declared_length: int):
        self._path = source_path(upload, settings)
        self._offset = offset
        self._declared_length = declared_length
        self._hasher = hashlib.sha256()
        self._written = 0
        self._handle = None

    def __enter__(self) -> "ChunkWriter":
        self._handle = self._path.open("r+b")
        self._handle.seek(self._offset)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None

    def write(self, block: bytes) -> None:
        if not block:
            return
        self._written += len(block)
        if self._written > self._declared_length:
            raise payload_too_large("The chunk body exceeded its declared Content-Length.")
        self._hasher.update(block)
        assert self._handle is not None
        self._handle.write(block)

    def finish(self, expected_digest: bytes) -> int:
        """Return the number of accepted bytes, or raise on any mismatch."""
        if self._written != self._declared_length:
            raise validation_error(
                "incomplete_chunk",
                "The chunk body was shorter than its declared Content-Length.",
            )
        if self._hasher.digest() != expected_digest:
            raise conflict(
                "upload_chunk_mismatch",
                "The chunk checksum does not match the transmitted bytes.",
                {"expectedOffset": self._offset},
            )
        return self._written


def commit_chunk(db: DbSession, upload: Upload, *, accepted_bytes: int) -> ChunkOutcome:
    """Advance the verified offset after a validated forward chunk."""
    upload.verified_offset_bytes += accepted_bytes
    upload.state = "uploading"
    upload.updated_at = utcnow()
    db.flush()
    return ChunkOutcome(offset=upload.verified_offset_bytes, duplicate=False)


def truncate_to_verified(upload: Upload, settings: Settings) -> None:
    """Discard unverified trailing bytes left by a rejected or partial chunk."""
    path = source_path(upload, settings)
    if path.exists() and path.stat().st_size > upload.verified_offset_bytes:
        with path.open("r+b") as handle:
            handle.truncate(upload.verified_offset_bytes)


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(READ_BLOCK_BYTES), b""):
            hasher.update(block)
    return hasher.hexdigest()


def begin_completion(db: DbSession, upload: Upload, settings: Settings) -> Upload:
    """Validate transferred length and move the upload into verification."""
    if upload.state == "ready":
        return upload
    if upload.state == "verifying":
        return upload
    if upload.state == "failed":
        raise conflict("upload_failed", "This upload failed and cannot be completed.")
    if upload.verified_offset_bytes != upload.declared_size_bytes:
        raise conflict(
            "upload_incomplete",
            "The upload is not finished; more bytes are expected.",
            {"expectedOffset": upload.verified_offset_bytes},
        )

    path = source_path(upload, settings)
    if not path.exists() or path.stat().st_size != upload.declared_size_bytes:
        upload.state = "failed"
        upload.error = _error_payload(
            "upload_size_mismatch", "The stored file size does not match the declared size."
        )
        db.flush()
        raise conflict("upload_size_mismatch", "The stored file does not match the declared size.")

    upload.state = "verifying"
    upload.error = None
    upload.updated_at = utcnow()
    db.flush()
    return upload


def fail_upload(
    db: DbSession,
    upload: Upload,
    code: str,
    message: str,
    *,
    phase: str = "verifying",
    retryable: bool = False,
) -> None:
    upload.state = "failed"
    upload.error = _error_payload(code, message, phase=phase, retryable=retryable)
    upload.updated_at = utcnow()
    db.flush()


def update_download_progress(
    db: DbSession,
    upload: Upload,
    *,
    downloaded_bytes: int,
    total_bytes: int | None,
) -> None:
    """Persist throttled yt-dlp progress without ever trusting it as a path."""
    if upload.state != "downloading":
        return
    safe_downloaded = max(0, int(downloaded_bytes))
    # Separate video/audio streams report their own byte counters. Keep the UI
    # monotonic instead of jumping backwards when yt-dlp starts stream two.
    upload.verified_offset_bytes = max(upload.verified_offset_bytes, safe_downloaded)
    if total_bytes is not None and total_bytes > 0:
        upload.declared_size_bytes = max(
            upload.declared_size_bytes,
            upload.verified_offset_bytes,
            int(total_bytes),
        )
    upload.updated_at = utcnow()
    db.flush()


def finish_url_download(
    db: DbSession,
    upload: Upload,
    *,
    title: str | None,
    extension: str,
    size_bytes: int,
) -> None:
    """Move a completed URL import into the ordinary verification lifecycle."""
    safe_extension = storage.sanitize_extension(f"source.{extension}", default="bin")
    display_title = _sanitize_display_name(title or "Downloaded video")
    upload.file_name = _sanitize_display_name(f"{display_title}.{safe_extension}")
    upload.storage_ext = safe_extension
    upload.declared_size_bytes = size_bytes
    upload.verified_offset_bytes = size_bytes
    upload.state = "verifying"
    upload.error = None
    upload.updated_at = utcnow()
    db.flush()


def mark_ready(db: DbSession, upload: Upload, *, sha256: str, probe: dict[str, Any]) -> None:
    upload.state = "ready"
    upload.sha256 = sha256
    upload.probe = probe
    upload.error = None
    upload.updated_at = utcnow()
    db.flush()


def delete_upload(db: DbSession, upload: Upload, settings: Settings) -> None:
    """Remove an incomplete or unreferenced upload (spec 8.3)."""
    if upload.reference_count > 0:
        raise conflict(
            "upload_referenced", "This upload is used by a job and cannot be deleted."
        )
    upload.deleted_at = utcnow()
    db.flush()
    storage.remove_tree(upload.relative_dir, settings)
    db.delete(upload)
    db.flush()


def list_uploads(db: DbSession, limit: int = 50) -> list[Upload]:
    return list(
        db.execute(
            select(Upload)
            .where(Upload.deleted_at.is_(None))
            .order_by(Upload.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def progress_percent(upload: Upload) -> float:
    if upload.declared_size_bytes <= 0:
        return 0.0
    if upload.state == "ready":
        return 100.0
    return round(
        min(100.0, upload.verified_offset_bytes / upload.declared_size_bytes * 100.0), 2
    )


# --- helpers ---------------------------------------------------------------


def _is_hex_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def _sanitize_display_name(file_name: str) -> str:
    """Keep a readable label; it is metadata only and never touches a path."""
    cleaned = "".join(ch for ch in (file_name or "") if ch.isprintable() and ch not in "\r\n\t")
    cleaned = cleaned.replace("\\", "/").split("/")[-1].strip()
    return cleaned[:255] or "video"


def normalize_video_url(value: str) -> str:
    """Accept only public-looking HTTP(S) URLs and strip non-request fragments.

    The worker resolves the hostname again immediately before yt-dlp runs.  The
    duplicate check prevents a DNS change between API validation and queue
    execution from turning this feature into a private-network fetch primitive.
    """
    cleaned = (value or "").strip()
    if any(ord(char) < ASCII_CONTROL_LIMIT for char in cleaned):
        raise validation_error(
            "invalid_video_url", "The video URL contains invalid characters."
        )
    try:
        parts = urlsplit(cleaned)
        # Accessing port performs urllib's port-range validation.
        _ = parts.port
    except ValueError as exc:
        raise validation_error("invalid_video_url", "Enter a valid video URL.") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise validation_error(
            "invalid_video_url", "Enter a full http:// or https:// video URL."
        )
    if parts.username is not None or parts.password is not None:
        raise validation_error(
            "invalid_video_url", "Video URLs containing embedded credentials are not accepted."
        )

    hostname = parts.hostname.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith((".localhost", ".local"))
        or "." not in hostname
    ):
        raise validation_error(
            "private_video_url", "Private-network and local video URLs are not accepted."
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise validation_error(
            "private_video_url", "Private-network and local video URLs are not accepted."
        )

    # Fragments are browser-side navigation and can accidentally contain
    # tokens; yt-dlp never needs them for the HTTP request.
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))


def _error_payload(
    code: str,
    message: str,
    *,
    phase: str = "verifying",
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "code": code,
        "message": message,
        "retryable": retryable,
        "occurredAt": utcnow().isoformat().replace("+00:00", "Z"),
    }


def isoformat(value: dt.datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None
