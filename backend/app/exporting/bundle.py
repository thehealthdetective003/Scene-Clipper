"""On-demand ZIP of an arbitrary subset of an export's clips.

The full-export archive is built once by the worker and written to disk. This
module answers a different question: "give me these nine clips, now." It builds
nothing on disk -- the archive is assembled and streamed in one pass, so a
selection of any size costs a constant amount of memory and leaves no temporary
file behind to clean up or leak.

The selection travels as a compact ``resolution:serial-ranges`` spec rather than
a list of file ids. Serials are small server-issued integers, so selecting every
clip of a 100-clip export stays inside a few dozen characters of query string,
where 100 UUIDs would not fit at all.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.services.exports import FOLDER_NAMES

#: Hard ceiling on files in one bundle. An export holds at most 100 clips at up
#: to three resolutions, so this cannot be reached by legitimate use; it exists
#: so a hand-written query string cannot ask the server to open 100k handles.
MAX_BUNDLE_FILES = 400

#: Serials are 1-based and gapless within an export.
MAX_SERIAL = 100_000

#: Read size when copying a clip into the archive.
_BLOCK_BYTES = 256 * 1024


class SelectionError(ValueError):
    """The requested selection was malformed or out of bounds."""


@dataclass(frozen=True, slots=True)
class BundleEntry:
    """One file to place in the archive."""

    arcname: str
    path: Path


# --- Selection parsing ------------------------------------------------------


def parse_selection(groups: list[str]) -> dict[str, set[int]]:
    """Parse ``["original:1-5,8", "max720p:2"]`` into ``{resolution: serials}``.

    Every value is validated against the known resolutions and numeric bounds
    before it reaches a database query or the filesystem.
    """
    if not groups:
        raise SelectionError("Select at least one clip to download.")

    selection: dict[str, set[int]] = {}
    total = 0
    for group in groups:
        resolution, separator, ranges = group.partition(":")
        resolution = resolution.strip()
        if not separator or resolution not in FOLDER_NAMES:
            raise SelectionError("The selection names an unknown resolution.")

        serials = _parse_ranges(ranges)
        if not serials:
            raise SelectionError("The selection contains no clips.")

        # Repeating a resolution is a client bug, not an attack; merge it.
        merged = selection.setdefault(resolution, set())
        merged |= serials
        total = sum(len(values) for values in selection.values())
        if total > MAX_BUNDLE_FILES:
            raise SelectionError(f"A download may contain at most {MAX_BUNDLE_FILES} files.")

    return selection


def _parse_ranges(text: str) -> set[int]:
    serials: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        start_text, dash, end_text = part.partition("-")
        try:
            start = int(start_text)
            end = int(end_text) if dash else start
        except ValueError:
            raise SelectionError("The selection contains a non-numeric clip number.") from None

        if start < 1 or end < start or end > MAX_SERIAL:
            raise SelectionError("The selection contains an out-of-range clip number.")
        # Bounded by MAX_SERIAL above, and by MAX_BUNDLE_FILES in the caller.
        if end - start + 1 > MAX_BUNDLE_FILES:
            raise SelectionError(f"A download may contain at most {MAX_BUNDLE_FILES} files.")
        serials.update(range(start, end + 1))
    return serials


def format_selection(pairs: list[tuple[str, int]]) -> list[str]:
    """Inverse of :func:`parse_selection`, used by tests and tooling."""
    by_resolution: dict[str, list[int]] = {}
    for resolution, serial in pairs:
        by_resolution.setdefault(resolution, []).append(serial)

    groups = []
    for resolution, serials in by_resolution.items():
        spans: list[str] = []
        ordered = sorted(set(serials))
        start = previous = ordered[0]
        for serial in ordered[1:]:
            if serial == previous + 1:
                previous = serial
                continue
            spans.append(f"{start}-{previous}" if start != previous else str(start))
            start = previous = serial
        spans.append(f"{start}-{previous}" if start != previous else str(start))
        groups.append(f"{resolution}:{','.join(spans)}")
    return groups


def assert_safe_member(member: str) -> None:
    """Archive paths come only from fixed folders and validated serials.

    They are server-generated, but they are re-checked here because this is the
    one place an archive is built from a client-supplied selection.
    """
    if member.startswith("/") or ".." in member.split("/") or "\\" in member:
        raise SelectionError("Refusing to write an unsafe archive path.")
    folder, _, name = member.partition("/")
    if folder not in set(FOLDER_NAMES.values()) or not name:
        raise SelectionError("Refusing to write an unexpected archive folder.")


# --- Streaming archive ------------------------------------------------------


class _Sink(io.RawIOBase):
    """A write-only, non-seekable file object that hands bytes to a generator.

    ``zipfile`` writes into this; each ``drain()`` yields whatever it produced
    since the last call. Reporting ``seekable() == False`` makes ``zipfile``
    emit data descriptors instead of seeking back to patch local headers, which
    is what allows the archive to stream without ever being fully materialized.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data) -> int:  # noqa: ANN001 - buffer protocol
        chunk = bytes(data)
        self._buffer += chunk
        self._position += len(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self._position

    def drain(self) -> bytes:
        chunk = bytes(self._buffer)
        self._buffer.clear()
        return chunk


def iter_zip(
    entries: list[BundleEntry], extras: list[tuple[str, bytes]] | None = None
) -> Iterator[bytes]:
    """Yield a ZIP containing ``entries``, built as it is sent.

    Clips are stored, not deflated: H.264 in MP4 is already compressed, so
    deflating it burns CPU on every download to save almost nothing.
    """
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for entry in entries:
            assert_safe_member(entry.arcname)
            with archive.open(entry.arcname, "w") as destination:
                with entry.path.open("rb") as source:
                    while True:
                        block = source.read(_BLOCK_BYTES)
                        if not block:
                            break
                        destination.write(block)
                        pending = sink.drain()
                        if pending:
                            yield pending
            pending = sink.drain()
            if pending:
                yield pending

        for name, payload in extras or []:
            # Manifests are small and text, so they are worth deflating.
            archive.writestr(
                zipfile.ZipInfo(name), payload, compress_type=zipfile.ZIP_DEFLATED
            )
            pending = sink.drain()
            if pending:
                yield pending

    # The central directory is written when the ZipFile closes.
    pending = sink.drain()
    if pending:
        yield pending
