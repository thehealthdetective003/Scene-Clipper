"""Downloading a chosen subset of an export as one ZIP.

The bundle is assembled and streamed in a single pass, so these tests care
about two things: that the archive is a real, intact ZIP holding exactly the
clips asked for, and that a malformed or unfulfillable selection is refused
rather than partly honoured.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from app.db import session_scope
from app.exporting import bundle
from app.models import ExportFile
from app.services import storage
from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import JOBS, analyse, run_export
from tests.mock_provider import MockProvider


def bundle_url(job_id: str, export_id: str) -> str:
    return f"{JOBS}/{job_id}/exports/{export_id}/bundle"


# --- Selection parsing (no media needed) ------------------------------------


class TestSelectionParsing:
    def test_ranges_and_singles_expand(self):
        assert bundle.parse_selection(["original:1-3,7"]) == {"original": {1, 2, 3, 7}}

    def test_several_resolutions_are_kept_apart(self):
        assert bundle.parse_selection(["original:1", "max720p:2-3"]) == {
            "original": {1},
            "max720p": {2, 3},
        }

    def test_a_repeated_resolution_is_merged(self):
        assert bundle.parse_selection(["original:1", "original:2"]) == {"original": {1, 2}}

    @pytest.mark.parametrize(
        "groups",
        [
            [],
            ["original"],
            ["original:"],
            ["nope:1"],
            ["../etc:1"],
            ["original:0"],
            ["original:-1"],
            ["original:abc"],
            ["original:5-1"],
            ["original:1-999999"],
            ["original:1e5"],
        ],
    )
    def test_malformed_selections_are_refused(self, groups):
        with pytest.raises(bundle.SelectionError):
            bundle.parse_selection(groups)

    def test_the_file_count_is_capped(self):
        with pytest.raises(bundle.SelectionError):
            bundle.parse_selection([f"original:1-{bundle.MAX_BUNDLE_FILES + 1}"])

    def test_the_cap_counts_across_resolutions(self):
        half = bundle.MAX_BUNDLE_FILES // 2 + 10
        with pytest.raises(bundle.SelectionError):
            bundle.parse_selection([f"original:1-{half}", f"max720p:1-{half}"])

    def test_encoding_round_trips(self):
        pairs = [("original", s) for s in (1, 2, 3, 7, 9, 10)]
        groups = bundle.format_selection(pairs)
        assert groups == ["original:1-3,7,9-10"]
        assert bundle.parse_selection(groups) == {"original": {1, 2, 3, 7, 9, 10}}

    @pytest.mark.parametrize(
        "member",
        ["../evil.mp4", "/abs/evil.mp4", "original/../../x", "unknown/0001.mp4", "original/"],
    )
    def test_unsafe_archive_members_are_refused(self, member):
        with pytest.raises(bundle.SelectionError):
            bundle.assert_safe_member(member)


class TestStreamingArchive:
    def test_the_stream_is_a_valid_zip_with_intact_bytes(self, tmp_path):
        entries, digests = [], {}
        for serial in (1, 2, 3):
            path = tmp_path / f"{serial:04d}.mp4"
            # Larger than one read block, so the multi-chunk path is exercised.
            payload = bytes(range(256)) * 3000 + b"tail"
            path.write_bytes(payload)
            arcname = f"original/{serial:04d}.mp4"
            digests[arcname] = hashlib.sha256(payload).hexdigest()
            entries.append(bundle.BundleEntry(arcname=arcname, path=path))

        blob = b"".join(bundle.iter_zip(entries, [("manifest.json", b"{}")]))
        archive = zipfile.ZipFile(io.BytesIO(blob))

        assert archive.testzip() is None
        assert sorted(archive.namelist()) == [
            "manifest.json",
            "original/0001.mp4",
            "original/0002.mp4",
            "original/0003.mp4",
        ]
        for arcname, digest in digests.items():
            assert hashlib.sha256(archive.read(arcname)).hexdigest() == digest

    def test_clips_are_stored_and_manifests_deflated(self, tmp_path):
        path = tmp_path / "0001.mp4"
        path.write_bytes(b"\x00" * 5000)
        blob = b"".join(
            bundle.iter_zip(
                [bundle.BundleEntry(arcname="original/0001.mp4", path=path)],
                [("manifest.json", b"{}" * 500)],
            )
        )
        by_name = {i.filename: i.compress_type for i in zipfile.ZipFile(io.BytesIO(blob)).infolist()}
        # H.264 does not deflate, so spending CPU on it every download is waste.
        assert by_name["original/0001.mp4"] == zipfile.ZIP_STORED
        assert by_name["manifest.json"] == zipfile.ZIP_DEFLATED

    def test_an_unsafe_member_stops_the_stream(self, tmp_path):
        path = tmp_path / "x.mp4"
        path.write_bytes(b"data")
        with pytest.raises(bundle.SelectionError):
            b"".join(bundle.iter_zip([bundle.BundleEntry(arcname="../x.mp4", path=path)]))


# --- End to end --------------------------------------------------------------


@requires_media
class TestBundleEndpoint:
    @pytest.fixture
    def exported(self, auth_client, tmp_path):
        fixture = fixtures_media.hard_cuts(tmp_path / "media")
        job_id = analyse(auth_client, fixture.path, MockProvider())
        record = run_export(
            auth_client, job_id, resolutions=["original", "max720p"], include_audio=True
        )
        assert record["state"] == "complete", record
        return job_id, record["id"]

    def _files(self, client, job_id, export_id):
        return client.get(f"{JOBS}/{job_id}/exports/{export_id}/files").json()["files"]

    def test_a_selection_downloads_as_one_zip(self, auth_client, exported):
        job_id, export_id = exported
        response = auth_client.get(
            bundle_url(job_id, export_id), params={"group": ["original:1-2"]}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert archive.testzip() is None
        clips = [n for n in archive.namelist() if n.endswith(".mp4")]
        assert sorted(clips) == ["original/0001.mp4", "original/0002.mp4"]

    def test_the_bundled_clips_are_byte_identical_to_the_single_downloads(
        self, auth_client, exported
    ):
        job_id, export_id = exported
        files = [f for f in self._files(auth_client, job_id, export_id) if f["resolution"] == "original"]
        chosen = sorted(files, key=lambda f: f["serial"])[:2]

        response = auth_client.get(
            bundle_url(job_id, export_id),
            params={"group": [f"original:{chosen[0]['serial']},{chosen[1]['serial']}"]},
        )
        archive = zipfile.ZipFile(io.BytesIO(response.content))

        for entry in chosen:
            single = auth_client.get(entry["downloadUrl"])
            assert single.status_code == 200
            member = f"original/{entry['serial']:04d}.mp4"
            assert archive.read(member) == single.content
            # And it matches the hash the export itself recorded.
            assert hashlib.sha256(archive.read(member)).hexdigest() == entry["sha256"]

    def test_a_selection_can_span_resolutions(self, auth_client, exported):
        job_id, export_id = exported
        response = auth_client.get(
            bundle_url(job_id, export_id),
            params={"group": ["original:1", "max720p:2-3"]},
        )
        assert response.status_code == 200
        names = sorted(
            n for n in zipfile.ZipFile(io.BytesIO(response.content)).namelist() if n.endswith(".mp4")
        )
        assert names == ["720p/0002.mp4", "720p/0003.mp4", "original/0001.mp4"]

    def test_every_clip_can_be_taken_at_once(self, auth_client, exported):
        job_id, export_id = exported
        files = self._files(auth_client, job_id, export_id)
        groups = bundle.format_selection([(f["resolution"], f["serial"]) for f in files])

        response = auth_client.get(bundle_url(job_id, export_id), params={"group": groups})
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert len([n for n in archive.namelist() if n.endswith(".mp4")]) == len(files)

    def test_the_bundle_carries_manifests_for_just_the_selection(self, auth_client, exported):
        import json

        job_id, export_id = exported
        response = auth_client.get(
            bundle_url(job_id, export_id), params={"group": ["original:1"]}
        )
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert "manifest.json" in archive.namelist()
        assert "manifest.csv" in archive.namelist()

        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["partialSelection"] is True
        paths = [f["path"] for clip in manifest["clips"] for f in clip["files"]]
        assert paths == ["original/0001.mp4"]
        # The CSV agrees with it, so a spreadsheet and the folder match.
        csv_text = archive.read("manifest.csv").decode("utf-8-sig")
        assert csv_text.count("original/0001.mp4") == 1
        assert "0002.mp4" not in csv_text

    def test_no_internal_path_or_secret_leaks_into_the_bundle(self, auth_client, exported, settings):
        job_id, export_id = exported
        response = auth_client.get(
            bundle_url(job_id, export_id), params={"group": ["original:1-2"]}
        )
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        blob = archive.read("manifest.json").decode() + archive.read("manifest.csv").decode("utf-8-sig")
        assert str(settings.data_dir) not in blob
        assert "/data/" not in blob
        assert "AIza" not in blob

    @pytest.mark.parametrize(
        "groups",
        [["original:99"], ["max1080p:1"], ["original:1", "max1080p:1"]],
    )
    def test_a_selection_that_cannot_be_fulfilled_is_refused(
        self, auth_client, exported, groups
    ):
        job_id, export_id = exported
        response = auth_client.get(bundle_url(job_id, export_id), params={"group": groups})
        # Never a partial archive that silently drops what was asked for.
        assert response.status_code in (404, 409)
        assert response.headers["content-type"].startswith("application/json")

    def test_a_malformed_selection_is_a_422(self, auth_client, exported):
        job_id, export_id = exported
        response = auth_client.get(
            bundle_url(job_id, export_id), params={"group": ["original:abc"]}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_selection"

    def test_an_empty_selection_is_a_422(self, auth_client, exported):
        job_id, export_id = exported
        assert auth_client.get(bundle_url(job_id, export_id)).status_code == 422

    def test_a_missing_file_on_disk_is_reported_not_skipped(self, auth_client, exported, settings):
        job_id, export_id = exported
        with session_scope() as db:
            row = (
                db.query(ExportFile)
                .filter(ExportFile.export_id == export_id, ExportFile.resolution == "original")
                .order_by(ExportFile.serial)
                .first()
            )
            storage.resolve(row.relative_path, settings).unlink()

        response = auth_client.get(
            bundle_url(job_id, export_id), params={"group": ["original:1-2"]}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "selection_unavailable"

    def test_the_bundle_requires_authentication(self, auth_client, exported):
        job_id, export_id = exported
        # `client` and `auth_client` are one object, so the session is ended
        # rather than a second client being made.
        assert auth_client.post("/api/v1/auth/logout").status_code == 204
        response = auth_client.get(
            bundle_url(job_id, export_id), params={"group": ["original:1"]}
        )
        assert response.status_code == 401

    def test_an_export_id_from_another_job_is_not_reachable(self, auth_client, exported):
        _job_id, export_id = exported
        response = auth_client.get(
            bundle_url("01a00000-0000-7000-8000-000000000000", export_id),
            params={"group": ["original:1"]},
        )
        assert response.status_code == 404
