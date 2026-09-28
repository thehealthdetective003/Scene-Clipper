"""Source-label typography defaults, validation, and bundled font lookup.

The values in this module are shared by API validation, settings persistence,
job snapshots, preview rendering, and final export rendering.  Keeping one
canonical representation prevents a settings value from looking valid in the
browser but failing later in a worker.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

SOURCE_NAME_MAX_CHARS = 48
SOURCE_LABEL_SIZE_MIN = 2.5
SOURCE_LABEL_SIZE_MAX = 8.0
SOURCE_LABEL_SIZE_STEP = 0.25

DEFAULT_FONT_PRESET = "bebas-neue"
DEFAULT_FILL_COLOR = "#FFFFFF"
DEFAULT_OUTLINE_COLOR = "#000000"
DEFAULT_SIZE_PERCENT = 4.0

# The same font binaries are shipped to the browser and the media worker.  The
# filenames are deliberately server-owned; API clients only ever select an
# opaque preset id.
FONT_PRESETS: dict[str, str] = {
    "bebas-neue": "BebasNeue-Regular.ttf",
    "anton": "Anton-Regular.ttf",
    "oswald-semibold": "Oswald-SemiBold.ttf",
    "roboto-condensed-bold": "RobotoCondensed-Bold.ttf",
}

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_EXTRA_SYMBOLS = frozenset("&+@#%")
_SIZE_GRID_TOLERANCE = 1e-9
_COMBINING_MARK_RANGES = (
    (0x0300, 0x036F),
    (0x1AB0, 0x1AFF),
    (0x1DC0, 0x1DFF),
    (0x20D0, 0x20FF),
    (0xFE20, 0xFE2F),
)
_CJK_NAME_MARKERS = (
    "BOPOMOFO",
    "CJK",
    "HANGUL",
    "HIRAGANA",
    "IDEOGRAPHIC",
    "KATAKANA",
)

# The application image installs fonts-noto-cjk. The extra locations make
# direct development on Windows and macOS behave like the container without
# requiring a second copy of the large font in the repository.
CJK_FONT_CANDIDATES = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/malgunbd.ttf"),
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)


def default_style() -> dict[str, Any]:
    """Return a fresh JSON-safe copy of the deployment defaults."""
    return {
        "fontPreset": DEFAULT_FONT_PRESET,
        "fillColor": DEFAULT_FILL_COLOR,
        "outlineColor": DEFAULT_OUTLINE_COLOR,
        "sizePercent": DEFAULT_SIZE_PERCENT,
    }


def normalize_color(value: str) -> str:
    """Validate and canonicalize an opaque six-digit RGB color."""
    cleaned = value.strip()
    if not _HEX_COLOR.fullmatch(cleaned):
        raise ValueError("Use a six-digit color such as #FFFFFF.")
    return cleaned.upper()


def normalize_size(value: float) -> float:
    """Validate the responsive font size and its quarter-percent grid."""
    number = float(value)
    if not SOURCE_LABEL_SIZE_MIN <= number <= SOURCE_LABEL_SIZE_MAX:
        raise ValueError(
            f"Size must be between {SOURCE_LABEL_SIZE_MIN:g}% and "
            f"{SOURCE_LABEL_SIZE_MAX:g}%."
        )
    scaled = number / SOURCE_LABEL_SIZE_STEP
    if abs(scaled - round(scaled)) > _SIZE_GRID_TOLERANCE:
        raise ValueError("Size must use 0.25% increments.")
    return round(number, 2)


def normalize_font_preset(value: str) -> str:
    cleaned = value.strip().lower()
    if cleaned not in FONT_PRESETS:
        raise ValueError("Choose one of the supported source-label fonts.")
    return cleaned


def normalize_style(style: dict[str, Any]) -> dict[str, Any]:
    """Return a validated style in the stable job-snapshot wire shape."""
    return {
        "fontPreset": normalize_font_preset(str(style.get("fontPreset", ""))),
        "fillColor": normalize_color(str(style.get("fillColor", ""))),
        "outlineColor": normalize_color(str(style.get("outlineColor", ""))),
        "sizePercent": normalize_size(float(style.get("sizePercent", 0))),
    }


def normalize_source_name(value: str | None) -> str | None:
    """Normalize an optional source name to the exact rendered text.

    Latin and CJK names are supported by the fonts shipped in the application
    image. Numbers, punctuation, combining marks, spaces, and a few common
    source-name symbols remain available.
    """
    if value is None:
        return None

    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        return None

    # The input is rendered on one line.  Reject controls rather than silently
    # changing what an API client submitted; the browser already uses a
    # single-line input.
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C"):
            raise ValueError("Source names cannot contain control characters or line breaks.")

    text = re.sub(r"\s+", " ", text).upper()
    if len(text) > SOURCE_NAME_MAX_CHARS:
        raise ValueError(f"Source names may contain at most {SOURCE_NAME_MAX_CHARS} characters.")

    for char in text:
        if char == " ":
            continue
        category = unicodedata.category(char)
        if category.startswith("L"):
            name = unicodedata.name(char, "")
            if "LATIN" not in name and not any(marker in name for marker in _CJK_NAME_MARKERS):
                raise ValueError("Source names support Latin, Chinese, Japanese, and Korean text.")
            continue
        if category.startswith("M"):
            codepoint = ord(char)
            if any(start <= codepoint <= end for start, end in _COMBINING_MARK_RANGES):
                continue
            raise ValueError("Source names support Latin, Chinese, Japanese, and Korean text.")
        if category[0] in {"N", "P"} or char in _EXTRA_SYMBOLS:
            continue
        raise ValueError("The source name contains an unsupported character.")

    return text


def font_path(font_preset: str) -> Path:
    """Resolve a validated preset to a packaged font file."""
    preset = normalize_font_preset(font_preset)
    return Path(__file__).resolve().parent / "assets" / "fonts" / FONT_PRESETS[preset]


def font_path_for_text(font_preset: str, text: str) -> Path:
    """Choose the configured display font or the CJK glyph fallback."""
    if not any(
        unicodedata.category(char).startswith("L")
        and "LATIN" not in unicodedata.name(char, "")
        for char in text
    ):
        return font_path(font_preset)

    for candidate in CJK_FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("The Noto CJK source-label font is not installed.")

