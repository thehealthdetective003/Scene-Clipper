"""Authenticated, range-enabled file streaming.

``/data`` is never exposed as a static directory (spec 10.2); previews,
thumbnails, and export archives are streamed through authenticated routes that
resolve a server-generated relative path and honour HTTP range requests so the
browser can scrub a preview without downloading it whole.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from starlette.responses import Response, StreamingResponse

from app.api.errors import AppError, not_found

_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
_BLOCK_BYTES = 256 * 1024


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return an inclusive ``(start, end)`` byte range, or ``None`` for the whole file."""
    if not header:
        return None
    match = _RANGE_PATTERN.match(header.strip())
    if not match:
        return None
    start_text, end_text = match.groups()

    if start_text == "" and end_text == "":
        return None
    if start_text == "":
        # Suffix range: the last N bytes.
        length = int(end_text)
        if length <= 0:
            raise _unsatisfiable(size)
        start = max(0, size - length)
        return start, size - 1

    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise _unsatisfiable(size)
    return start, min(end, size - 1)


def _unsatisfiable(size: int) -> AppError:
    return AppError(
        "range_not_satisfiable",
        "The requested byte range is outside the file.",
        status_code=416,
        headers={"Content-Range": f"bytes */{size}"},
    )


def _iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            block = handle.read(min(_BLOCK_BYTES, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def stream_file(
    path: Path,
    *,
    media_type: str,
    range_header: str | None = None,
    download_name: str | None = None,
    allow_range: bool = True,
) -> Response:
    """Stream a file, honouring ``Range`` when the client sends one."""
    if not path.is_file():
        raise not_found("file")
    size = path.stat().st_size

    headers = {
        "Accept-Ranges": "bytes" if allow_range else "none",
        # These are private media files; never let a shared cache hold them.
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if download_name:
        # The name is built from server-side identifiers only.
        headers["Content-Disposition"] = f'attachment; filename="{download_name}"'

    byte_range = _parse_range(range_header, size) if allow_range else None
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(
            _iter_file(path, 0, size), media_type=media_type, headers=headers, status_code=200
        )

    start, end = byte_range
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _iter_file(path, start, length), media_type=media_type, headers=headers, status_code=206
    )
