"""Individual MP4 downloads alongside the ZIP.

Each validated clip is published next to the archive so it can be fetched
directly, without unpacking anything.
"""

from __future__ import annotations

import zipfile

import pytest

from app.db import session_scope
from app.media.timebase import MAX_CLIP_US, MIN_CLIP_US
from app.models import ExportFile
from app.services import storage
from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import JOBS, analyse, run_export
from tests.mock_provider import MockProvider

pytestmark = requires_media


@pytest.fixture
def exported(auth_client, tmp_path):
    fixture = fixtures_media.hard_cuts(tmp_path / "media")
    job_id = analyse(auth_client, fixture.path, MockProvider())
    record = run_export(
        auth_client, job_id, resolutions=["original", "max720p"], include_audio=True
    )
    assert record["state"] == "complete", record
    return job_id, record["id"]


class TestListing:
    def test_lists_one_file_per_clip_and_resolution(self, auth_client, exported):
        job_id, export_id = exported
        body = auth_client.get(f"{JOBS}/{job_id}/exports/{export_id}/files").json()

        # Four clips x two resolutions.
        assert len(body["files"]) == 8
        assert {f["resolution"] for f in body["files"]} == {"original", "max720p"}
        assert all(f["available"] for f in body["files"])

    def test_each_file_carries_usable_metadata(self, auth_client, exported):
        job_id, export_id = exported
        body = auth_client.get(f"{JOBS}/{job_id}/exports/{export_id}/files").json()

        for file in body["files"]:
            assert file["fileName"].endswith(".mp4")
            assert file["sizeBytes"] > 0
            assert file["width"] > 0 and file["height"] > 0
            assert MIN_CLIP_US - 100_000 <= file["durationUs"] <= MAX_CLIP_US + 100_000
            assert len(file["sha256"]) == 64
            assert file["downloadUrl"].startswith(f"/api/v1/jobs/{job_id}/exports/{export_id}")

    def test_serials_are_gapless_within_each_resolution(self, auth_client, exported):
        job_id, export_id = exported
        body = auth_client.get(f"{JOBS}/{job_id}/exports/{export_id}/files").json()

        for resolution in ("original", "max720p"):
            serials = sorted(f["serial"] for f in body["files"] if f["resolution"] == resolution)
            assert serials == list(range(1, len(serials) + 1))

    def test_zip_remains_offered(self, auth_client, exported):
        job_id, export_id = exported
        body = auth_client.get(f"{JOBS}/{job_id}/exports/{export_id}/files").json()
        assert body["zipDownloadUrl"] == f"/api/v1/jobs/{job_id}/exports/{export_id}/download"

    def test_requires_authentication(self, client, exported):
        job_id, export_id = exported
        client.cookies.clear()
        assert client.get(f"{JOBS}/{job_id}/exports/{export_id}/files").status_code == 401


class TestDownload:
    def _files(self, client, job_id, export_id):
        return client.get(f"{JOBS}/{job_id}/exports/{export_id}/files").json()["files"]

    def test_downloads_a_playable_mp4(self, auth_client, exported, tmp_path):
        from app.media.probe import probe_media

        job_id, export_id = exported
        file = self._files(auth_client, job_id, export_id)[0]

        response = auth_client.get(file["downloadUrl"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert file["fileName"] in response.headers["content-disposition"]
        assert len(response.content) == file["sizeBytes"]

        # It is a real, probeable clip -- not the archive under another name.
        out = tmp_path / "one.mp4"
        out.write_bytes(response.content)
        info = probe_media(out)
        assert info.video_codec == "h264"
        assert (info.display_width, info.display_height) == (file["width"], file["height"])

    def test_content_matches_the_recorded_digest(self, auth_client, exported):
        import hashlib

        job_id, export_id = exported
        file = self._files(auth_client, job_id, export_id)[0]
        response = auth_client.get(file["downloadUrl"])
        assert hashlib.sha256(response.content).hexdigest() == file["sha256"]

    def test_matches_the_copy_inside_the_zip(self, auth_client, exported, settings):
        job_id, export_id = exported
        file = next(
            f for f in self._files(auth_client, job_id, export_id) if f["resolution"] == "original"
        )
        direct = auth_client.get(file["downloadUrl"]).content

        archive = storage.resolve(
            f"exports/{job_id}/{export_id}/scene-clips-{job_id}-{export_id}.zip", settings
        )
        with zipfile.ZipFile(archive) as zf:
            inside = zf.read(f"original/{file['serial']:04d}.mp4")
        assert direct == inside

    def test_supports_range_requests(self, auth_client, exported):
        job_id, export_id = exported
        file = self._files(auth_client, job_id, export_id)[0]

        response = auth_client.get(file["downloadUrl"], headers={"Range": "bytes=0-1023"})
        assert response.status_code == 206
        assert len(response.content) == 1024

    def test_requires_authentication(self, client, auth_client, exported):
        job_id, export_id = exported
        file = self._files(auth_client, job_id, export_id)[0]
        client.cookies.clear()
        assert client.get(file["downloadUrl"]).status_code == 401

    def test_unknown_file_is_not_found(self, auth_client, exported):
        from app.util.ids import new_id

        job_id, export_id = exported
        response = auth_client.get(
            f"{JOBS}/{job_id}/exports/{export_id}/files/{new_id()}/download"
        )
        assert response.status_code == 404

    def test_a_file_from_another_export_is_not_found(self, auth_client, exported, tmp_path):
        """File ids must not be probeable across exports."""
        job_id, export_id = exported
        other = run_export(auth_client, job_id, resolutions=["original"])
        other_file = self._files(auth_client, job_id, other["id"])[0]

        # Ask the first export for the second export's file id.
        response = auth_client.get(
            f"{JOBS}/{job_id}/exports/{export_id}/files/{other_file['id']}/download"
        )
        assert response.status_code == 404

    def test_missing_file_on_disk_reports_not_found(self, auth_client, exported, settings):
        """An export predating individual publication degrades gracefully."""
        job_id, export_id = exported
        files = self._files(auth_client, job_id, export_id)
        target = files[0]

        with session_scope() as db:
            row = db.get(ExportFile, target["id"])
            storage.remove_file(row.relative_path, settings)

        listed = next(
            f
            for f in self._files(auth_client, job_id, export_id)
            if f["id"] == target["id"]
        )
        assert listed["available"] is False
        assert auth_client.get(target["downloadUrl"]).status_code == 404


class TestPublication:
    def test_each_row_points_at_its_own_file_not_the_archive(self, auth_client, exported):
        job_id, export_id = exported
        with session_scope() as db:
            rows = db.query(ExportFile).filter(ExportFile.export_id == export_id).all()
            paths = [row.relative_path for row in rows]

        assert len(set(paths)) == len(paths), "each file needs a distinct path"
        assert all(path.endswith(".mp4") for path in paths)
        assert not any(path.endswith(".zip") for path in paths)

    def test_published_clips_sit_beside_the_archive(self, auth_client, exported, settings):
        job_id, export_id = exported
        base = settings.exports_dir / job_id / export_id

        assert (base / f"scene-clips-{job_id}-{export_id}.zip").is_file()
        assert sorted(p.name for p in (base / "original").glob("*.mp4")) == [
            "0001.mp4",
            "0002.mp4",
            "0003.mp4",
            "0004.mp4",
        ]
        assert (base / "720p").is_dir()

    def test_deleting_the_job_removes_the_published_clips(self, auth_client, exported, settings):
        from app.workers.tasks import cleanup_job

        job_id, export_id = exported
        assert auth_client.delete(f"{JOBS}/{job_id}").status_code == 204
        cleanup_job(job_id)

        assert not (settings.exports_dir / job_id).exists()
