"""Contact sheets and storyboards (spec 6.5, 6.8).

A sheet carries, for each candidate, its opaque id, source timecode, duration,
and representative frames. Constraints:

* at most 12 candidates per sheet;
* the sheet fits inside 2048x2048;
* one coarse request carries at most 8 sheets (96 candidates);
* the assembled inline request stays below the configured payload limit;
* source filenames and image metadata are removed -- sheets are drawn onto a
  fresh canvas and saved without EXIF.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.media.timebase import US_PER_SECOND

MAX_CANDIDATES_PER_SHEET = 12
MAX_SHEETS_PER_REQUEST = 8
MAX_CANDIDATES_PER_REQUEST = MAX_CANDIDATES_PER_SHEET * MAX_SHEETS_PER_REQUEST
SHEET_MAX_PIXELS = 2048
JPEG_QUALITY = 82

_MARGIN = 10
_GAP = 6
_LABEL_HEIGHT = 20
_BACKGROUND = (18, 18, 20)
_LABEL_COLOR = (240, 240, 245)
_RULE_COLOR = (70, 70, 80)


@dataclass(frozen=True, slots=True)
class SheetEntry:
    """One candidate's contribution to a sheet."""

    candidate_id: str
    start_us: int
    end_us: int
    frame_paths: list[Path]

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


def format_timecode(position_us: int) -> str:
    """``HH:MM:SS.mmm`` -- unambiguous for an editor reading the sheet."""
    total_ms = position_us // 1000
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _font(size: int = 13) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def chunk_candidates(entries: list[SheetEntry]) -> list[list[SheetEntry]]:
    return [
        entries[index : index + MAX_CANDIDATES_PER_SHEET]
        for index in range(0, len(entries), MAX_CANDIDATES_PER_SHEET)
    ]


def _tile_geometry(entries: list[SheetEntry]) -> tuple[int, int, int]:
    """Return ``(columns, tile_width, tile_height)`` fitting the sheet budget."""
    columns = max(1, max(len(entry.frame_paths) for entry in entries))
    rows = len(entries)

    tile_width = (SHEET_MAX_PIXELS - 2 * _MARGIN - _GAP * (columns - 1)) // columns
    tile_width = max(48, tile_width)

    # Derive height from the first available frame's aspect ratio.
    aspect = 9 / 16
    for entry in entries:
        for path in entry.frame_paths:
            try:
                with Image.open(path) as probe:
                    if probe.width:
                        aspect = probe.height / probe.width
                    break
            except OSError:
                continue
        else:
            continue
        break

    tile_height = max(36, int(round(tile_width * aspect)))
    row_height = _LABEL_HEIGHT + tile_height + _GAP
    total_height = 2 * _MARGIN + rows * row_height

    if total_height > SHEET_MAX_PIXELS:
        # Shrink tiles uniformly until the whole sheet fits the budget.
        available = SHEET_MAX_PIXELS - 2 * _MARGIN - rows * (_LABEL_HEIGHT + _GAP)
        tile_height = max(24, available // rows)
        tile_width = max(32, int(round(tile_height / aspect)))

    return columns, tile_width, tile_height


def render_sheet(entries: list[SheetEntry], destination: Path) -> Path:
    """Draw one contact sheet and write it as a metadata-free JPEG."""
    if not entries:
        raise ValueError("A contact sheet needs at least one candidate.")
    if len(entries) > MAX_CANDIDATES_PER_SHEET:
        raise ValueError("A contact sheet holds at most 12 candidates.")

    columns, tile_width, tile_height = _tile_geometry(entries)
    row_height = _LABEL_HEIGHT + tile_height + _GAP
    width = min(SHEET_MAX_PIXELS, 2 * _MARGIN + columns * tile_width + _GAP * (columns - 1))
    height = min(SHEET_MAX_PIXELS, 2 * _MARGIN + len(entries) * row_height)

    sheet = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    font = _font()

    for row, entry in enumerate(entries):
        top = _MARGIN + row * row_height
        label = (
            f"candidateId={entry.candidate_id}  "
            f"source={format_timecode(entry.start_us)}-{format_timecode(entry.end_us)}  "
            f"duration={entry.duration_us / US_PER_SECOND:.2f}s"
        )
        draw.text((_MARGIN, top + 3), label, fill=_LABEL_COLOR, font=font)
        draw.line(
            [(_MARGIN, top + _LABEL_HEIGHT - 2), (width - _MARGIN, top + _LABEL_HEIGHT - 2)],
            fill=_RULE_COLOR,
            width=1,
        )

        for column, frame_path in enumerate(entry.frame_paths[:columns]):
            left = _MARGIN + column * (tile_width + _GAP)
            try:
                with Image.open(frame_path) as frame:
                    tile = frame.convert("RGB").resize(
                        (tile_width, tile_height), Image.LANCZOS
                    )
                    sheet.paste(tile, (left, top + _LABEL_HEIGHT))
            except OSError:
                draw.rectangle(
                    [left, top + _LABEL_HEIGHT, left + tile_width, top + _LABEL_HEIGHT + tile_height],
                    outline=_RULE_COLOR,
                )

    destination.parent.mkdir(parents=True, exist_ok=True)
    # A fresh canvas carries no source EXIF; `optimize` keeps the payload small.
    sheet.save(destination, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return destination


def render_window_storyboard(
    candidate_id: str,
    windows: list[tuple[str, int, int, list[Path]]],
    destination: Path,
) -> Path:
    """Stage H storyboard: each row is a server-issued window id.

    ``windows`` is ``(window_id, start_us, end_us, frame_paths)``. The model
    must return one of these ids and may not invent a timestamp (spec 6.8).
    """
    entries = [
        SheetEntry(
            candidate_id=f"{candidate_id} windowId={window_id}",
            start_us=start_us,
            end_us=end_us,
            frame_paths=frames,
        )
        for window_id, start_us, end_us, frames in windows
    ]
    return render_sheet(entries[:MAX_CANDIDATES_PER_SHEET], destination)


def total_bytes(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths if path.exists())


def pack_sheets_into_requests(
    sheet_paths: list[Path], *, payload_limit_bytes: int
) -> list[list[Path]]:
    """Group sheets into requests under the sheet-count and byte budgets."""
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_bytes = 0

    for path in sheet_paths:
        size = path.stat().st_size if path.exists() else 0
        would_exceed_bytes = current and (current_bytes + size) > payload_limit_bytes
        would_exceed_count = len(current) >= MAX_SHEETS_PER_REQUEST
        if would_exceed_bytes or would_exceed_count:
            batches.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += size

    if current:
        batches.append(current)
    return batches
