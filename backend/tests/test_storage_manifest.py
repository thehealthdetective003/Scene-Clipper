"""Path safety, serial numbering, and manifest generation (spec 9, 10.2, 12.1)."""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest

from app.exporting import manifest as manifest_module
from app.services import storage
from app.services.storage import UnsafePathError


class TestPathSafety:
    @pytest.mark.parametrize(
        "component",
        [
            "..",
            ".",
            "../etc",
            "a/b",
            "with space",
            "trailing\x00",
            "CON",
            "con.mp4",
            "PRN",
            "LPT1",
            "nul.txt",
            "",
            "‮exe.mp4",
        ],
    )
    def test_rejects_unsafe_components(self, component):
        with pytest.raises(UnsafePathError):
            storage.validate_component(component)

    @pytest.mark.parametrize(
        "component", ["0001.mp4", "manifest.json", "0192f000-0000-7000-8000-000000000001", "a-b_c.1"]
    )
    def test_accepts_generated_components(self, component):
        assert storage.validate_component(component) == component

    @pytest.mark.parametrize(
        "path",
        [
            "../secrets",
            "uploads/../../etc/passwd",
            "/etc/passwd",
            "uploads\\..\\x",
            "",
            "uploads/./../..",
        ],
    )
    def test_resolve_refuses_traversal(self, path, settings):
        with pytest.raises(UnsafePathError):
            storage.resolve(path, settings)

    def test_resolve_stays_within_the_data_root(self, settings):
        resolved = storage.resolve("uploads/abc/source.mp4", settings)
        assert str(resolved).startswith(str(settings.data_dir.resolve()))

    def test_extension_sanitization(self):
        assert storage.sanitize_extension("holiday.MP4") == "mp4"
        assert storage.sanitize_extension("clip.mov") == "mov"
        assert storage.sanitize_extension("no-extension") == "bin"
        assert storage.sanitize_extension("weird.~/../sh") == "bin"
        assert storage.sanitize_extension("") == "bin"


class TestSerialNaming:
    def test_zero_padded_four_digits(self):
        assert storage.serial_name(1) == "0001.mp4"
        assert storage.serial_name(42) == "0042.mp4"
        assert storage.serial_name(9999) == "9999.mp4"

    @pytest.mark.parametrize("serial", [0, -1, 10000])
    def test_out_of_range_serials_are_refused(self, serial):
        with pytest.raises(UnsafePathError):
            storage.serial_name(serial)

    def test_gapless_sequence_after_deselection(self):
        # Serials are assigned at export from review order, so gaps in the
        # original ranking never reach the archive (spec 5.7).
        review_order = ["cand-c", "cand-a", "cand-f"]
        names = [storage.serial_name(i) for i, _ in enumerate(review_order, start=1)]
        assert names == ["0001.mp4", "0002.mp4", "0003.mp4"]


def build_manifest(reason: str = "A clean wide shot."):
    return manifest_module.build_manifest(
        job_id="job-1",
        export_id="exp-1",
        created_at=dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc),
        source={
            "fileName": "holiday.mp4",
            "sha256": "a" * 64,
            "durationUs": 60_000_000,
            "width": 1920,
            "height": 1080,
            "averageFrameRate": "30000/1001",
            "hasAudio": True,
        },
        resolutions=["original", "max720p"],
        include_audio=True,
        clips=[
            {
                "serial": 1,
                "candidateId": "cand-1",
                "sourceStartUs": 1_001_000,
                "sourceEndUs": 5_005_000,
                "durationUs": 4_004_000,
                "score": 81.25,
                "confidence": 0.82,
                "reason": reason,
                "files": [
                    {
                        "resolution": "original",
                        "path": "original/0001.mp4",
                        "width": 1920,
                        "height": 1080,
                        "sizeBytes": 1234,
                        "sha256": "b" * 64,
                    },
                    {
                        "resolution": "max720p",
                        "path": "720p/0001.mp4",
                        "width": 1280,
                        "height": 720,
                        "sizeBytes": 567,
                        "sha256": "c" * 64,
                    },
                ],
            }
        ],
    )


class TestManifest:
    def test_schema_version_and_shape(self):
        manifest = build_manifest()
        assert manifest["schemaVersion"] == "1.1"
        assert manifest["createdAt"] == "2026-09-14T12:00:00Z"
        assert manifest["options"]["resolutions"] == ["original", "max720p"]

    def test_csv_has_one_row_per_clip_resolution_pair(self):
        manifest = build_manifest()
        rows = list(csv.DictReader(io.StringIO(manifest_module.render_csv(manifest))))
        assert len(rows) == 2
        assert {row["resolution"] for row in rows} == {"original", "max720p"}

    def test_csv_matches_json(self):
        manifest = build_manifest()
        rows = list(csv.DictReader(io.StringIO(manifest_module.render_csv(manifest))))
        by_resolution = {row["resolution"]: row for row in rows}
        for clip in manifest["clips"]:
            for file_entry in clip["files"]:
                row = by_resolution[file_entry["resolution"]]
                assert row["path"] == file_entry["path"]
                assert int(row["sizeBytes"]) == file_entry["sizeBytes"]
                assert row["sha256"] == file_entry["sha256"]
                assert int(row["serial"]) == clip["serial"]

    def test_paths_are_zip_relative(self):
        manifest = build_manifest()
        for clip in manifest["clips"]:
            for file_entry in clip["files"]:
                path = file_entry["path"]
                assert not path.startswith("/")
                assert ":" not in path
                assert ".." not in path


class TestCsvInjection:
    @pytest.mark.parametrize(
        "dangerous",
        [
            "=SUM(A1:A9)",
            "+1+1",
            "-1-1",
            "@import",
            "\tTabbed",
            "\rCarriage",
            '=cmd|\'/c calc\'!A1',
        ],
    )
    def test_formula_cells_are_neutralized(self, dangerous):
        assert manifest_module.neutralize_csv_cell(dangerous).startswith("'")

    @pytest.mark.parametrize("safe", ["A clean shot", "1 + 1", "sunset", "", "0001.mp4"])
    def test_safe_cells_are_untouched(self, safe):
        assert manifest_module.neutralize_csv_cell(safe) == safe

    def test_model_reason_is_neutralized_in_csv_but_plain_in_json(self):
        manifest = build_manifest(reason="=HYPERLINK(\"http://evil\",\"click\")")
        # JSON keeps the original plain string (spec 9).
        assert manifest["clips"][0]["reason"].startswith("=")

        rendered = manifest_module.render_csv(manifest)
        rows = list(csv.DictReader(io.StringIO(rendered)))
        assert all(row["reason"].startswith("'=") for row in rows)

    def test_neutralization_survives_a_csv_round_trip(self):
        manifest = build_manifest(reason="@SUM(1,1)")
        rows = list(csv.DictReader(io.StringIO(manifest_module.render_csv(manifest))))
        assert rows[0]["reason"] == "'@SUM(1,1)"
