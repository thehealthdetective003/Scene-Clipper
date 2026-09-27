"""Export manifests (spec 9).

``manifest.json`` and ``manifest.csv`` describe the same files: the CSV has one
row per clip-resolution pair. Neither may contain secrets, session information,
raw model output, internal filesystem paths, or protected diagnostics -- every
path is ZIP-relative.

CSV injection defence: a cell beginning with ``=``, ``+``, ``-``, ``@``, tab, or
carriage return is prefixed with a single quote so a spreadsheet cannot execute
it. The JSON keeps the original plain string (spec 9).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

from app.versions import MANIFEST_SCHEMA_VERSION

#: Leading characters a spreadsheet may interpret as a formula.
_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

CSV_COLUMNS = [
    "serial",
    "resolution",
    "path",
    "candidateId",
    "sourceId",
    "sourceFileName",
    "sourceName",
    "sourceStartUs",
    "sourceEndUs",
    "durationUs",
    "width",
    "height",
    "sizeBytes",
    "sha256",
    "score",
    "confidence",
    "reason",
]


def neutralize_csv_cell(value: Any) -> str:
    """Prefix a formula-looking cell with a single quote."""
    text = "" if value is None else str(value)
    if text.startswith(_DANGEROUS_PREFIXES):
        return "'" + text
    return text


def build_manifest(
    *,
    job_id: str,
    export_id: str,
    created_at: dt.datetime,
    source: dict[str, Any],
    resolutions: list[str],
    include_audio: bool,
    clips: list[dict[str, Any]],
    source_label: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "jobId": job_id,
        "exportId": export_id,
        "createdAt": created_at.astimezone(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": source,
        "options": {
            "resolutions": resolutions,
            "includeAudio": include_audio,
            "sourceLabel": source_label,
        },
        "clips": clips,
    }


def write_json(manifest: dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return destination


def manifest_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten to one row per clip-resolution pair."""
    rows: list[dict[str, Any]] = []
    for clip in manifest["clips"]:
        for file_entry in clip["files"]:
            rows.append(
                {
                    "serial": clip["serial"],
                    "resolution": file_entry["resolution"],
                    "path": file_entry["path"],
                    "candidateId": clip["candidateId"],
                    "sourceId": clip.get("sourceId", ""),
                    "sourceFileName": clip.get("sourceFileName", ""),
                    "sourceName": clip.get("sourceName", ""),
                    "sourceStartUs": clip["sourceStartUs"],
                    "sourceEndUs": clip["sourceEndUs"],
                    "durationUs": clip["durationUs"],
                    "width": file_entry["width"],
                    "height": file_entry["height"],
                    "sizeBytes": file_entry["sizeBytes"],
                    "sha256": file_entry["sha256"],
                    "score": clip["score"],
                    "confidence": clip["confidence"],
                    "reason": clip["reason"],
                }
            )
    return rows


def render_csv(manifest: dict[str, Any]) -> str:
    buffer = io.StringIO()
    # QUOTE_ALL keeps embedded separators and newlines from breaking a row.
    writer = csv.DictWriter(
        buffer, fieldnames=CSV_COLUMNS, lineterminator="\r\n", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    for row in manifest_rows(manifest):
        writer.writerow({key: neutralize_csv_cell(row[key]) for key in CSV_COLUMNS})
    return buffer.getvalue()


def write_csv(manifest: dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so spreadsheets detect the encoding without mangling accents.
    destination.write_text(render_csv(manifest), encoding="utf-8-sig")
    return destination
