"""Review controls and export packaging (spec 5.6, 5.7, 12.5, 13)."""

from __future__ import annotations

import csv
import io
import json
import subprocess
import zipfile

import pytest
from PIL import Image

from app.config import get_settings
from app.media.runner import ffmpeg_binary
from app.media.timebase import (
    MAX_CLIP_US,
    MIN_CLIP_US,
    frame_duration_us_ceil,
    parse_rational,
)
from app.services import storage
from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import JOBS, analyse, run_export
from tests.mock_provider import MockProvider

pytestmark = requires_media


@pytest.fixture
def analysed_job(auth_client, tmp_path):
    fixture = fixtures_media.hard_cuts(tmp_path / "media")
    job_id = analyse(auth_client, fixture.path, MockProvider())
    body = auth_client.get(f"{JOBS}/{job_id}/candidates").json()
    return job_id, body


def review_payload(body, *, revision=None, clips=None):
    return {
        "revision": revision if revision is not None else body["reviewRevision"],
        "clips": clips
        if clips is not None
        else [
            {
                "candidateId": clip["candidateId"],
                "order": clip["order"],
                "startUs": clip["startUs"],
                "endUs": clip["endUs"],
            }
            for clip in body["selectedClips"]
        ],
    }


class TestReviewSelection:
    def test_deselecting_a_clip_renumbers_the_rest(self, auth_client, analysed_job):
        job_id, body = analysed_job
        remaining = body["selectedClips"][1:]
        clips = [
            {
                "candidateId": clip["candidateId"],
                "order": index,
                "startUs": clip["startUs"],
                "endUs": clip["endUs"],
            }
            for index, clip in enumerate(remaining, start=1)
        ]
        response = auth_client.put(
            f"{JOBS}/{job_id}/review", json=review_payload(body, clips=clips)
        )
        assert response.status_code == 200
        assert len(response.json()["clips"]) == len(remaining)
        assert [c["order"] for c in response.json()["clips"]] == list(
            range(1, len(remaining) + 1)
        )

    def test_reordering_is_persisted(self, auth_client, analysed_job):
        job_id, body = analysed_job
        reversed_clips = list(reversed(body["selectedClips"]))
        clips = [
            {
                "candidateId": clip["candidateId"],
                "order": index,
                "startUs": clip["startUs"],
                "endUs": clip["endUs"],
            }
            for index, clip in enumerate(reversed_clips, start=1)
        ]
        response = auth_client.put(
            f"{JOBS}/{job_id}/review", json=review_payload(body, clips=clips)
        )
        assert response.status_code == 200
        returned = response.json()["clips"]
        assert returned[0]["candidateId"] == reversed_clips[0]["candidateId"]

    def test_revision_increments_on_each_replacement(self, auth_client, analysed_job):
        job_id, body = analysed_job
        first = auth_client.put(f"{JOBS}/{job_id}/review", json=review_payload(body))
        assert first.json()["reviewRevision"] == body["reviewRevision"] + 1

    def test_stale_revision_conflicts_and_mutates_nothing(self, auth_client, analysed_job):
        job_id, body = analysed_job
        auth_client.put(f"{JOBS}/{job_id}/review", json=review_payload(body))

        stale = auth_client.put(
            f"{JOBS}/{job_id}/review", json=review_payload(body, revision=body["reviewRevision"])
        )
        assert stale.status_code == 409
        error = stale.json()["error"]
        assert error["code"] == "stale_review_revision"
        assert error["details"]["currentRevision"] == body["reviewRevision"] + 1
        assert "selectedClips" in error["details"]

    def test_restore_recommended_trim(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        clips = [
            {
                "candidateId": candidate["id"],
                "order": 1,
                "startUs": candidate["recommendedStartUs"],
                "endUs": candidate["recommendedEndUs"],
            }
        ]
        response = auth_client.put(
            f"{JOBS}/{job_id}/review", json=review_payload(body, clips=clips)
        )
        assert response.status_code == 200
        assert response.json()["clips"][0]["startUs"] == candidate["recommendedStartUs"]


class TestTrimValidation:
    def _put(self, client, job_id, body, candidate, start, end):
        return client.put(
            f"{JOBS}/{job_id}/review",
            json=review_payload(
                body,
                clips=[
                    {"candidateId": candidate["id"], "order": 1, "startUs": start, "endUs": end}
                ],
            ),
        )

    def test_minimum_legal_trim_is_accepted(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        start = candidate["safeStartUs"]
        response = self._put(auth_client, job_id, body, candidate, start, start + MIN_CLIP_US)
        assert response.status_code == 200

    def test_maximum_legal_trim_is_accepted(self, auth_client, tmp_path):
        # The hard-cut fixture's shots are five seconds, so a six-second trim
        # needs the long-take fixture.
        fixture = fixtures_media.long_take(tmp_path / "media_long")
        job_id = analyse(auth_client, fixture.path, MockProvider())
        body = auth_client.get(f"{JOBS}/{job_id}/candidates").json()

        candidate = next(
            c for c in body["candidates"] if c["usableDurationUs"] >= MAX_CLIP_US
        )
        start = candidate["safeStartUs"]
        response = self._put(auth_client, job_id, body, candidate, start, start + MAX_CLIP_US)
        assert response.status_code == 200
        # Frame quantization may shorten the request by up to one source frame.
        tolerance = frame_duration_us_ceil(parse_rational("30000/1001"))
        assert MAX_CLIP_US - tolerance <= response.json()["clips"][0]["durationUs"] <= MAX_CLIP_US

    def test_trim_below_the_minimum_is_rejected(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        start = candidate["safeStartUs"]
        # Comfortably under the floor, whatever the floor currently is.
        response = self._put(
            auth_client, job_id, body, candidate, start, start + MIN_CLIP_US // 2
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "trim_too_short"

    def test_trim_above_the_maximum_is_rejected(self, auth_client, tmp_path):
        # Needs a safe interval long enough that an over-length trim is not
        # merely rejected for leaving the safe interval.
        fixture = fixtures_media.long_take(tmp_path / "media_over")
        job_id = analyse(auth_client, fixture.path, MockProvider())
        body = auth_client.get(f"{JOBS}/{job_id}/candidates").json()
        candidate = body["candidates"][0]
        start = candidate["safeStartUs"]
        response = self._put(
            auth_client, job_id, body, candidate, start, start + MAX_CLIP_US + 1_000_000
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "trim_too_long"

    def test_trim_before_the_safe_interval_is_rejected(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = next(c for c in body["candidates"] if c["safeStartUs"] > 1_000_000)
        start = max(0, candidate["safeStartUs"] - 500_000)
        response = self._put(auth_client, job_id, body, candidate, start, start + MIN_CLIP_US)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "trim_outside_safe_interval"

    def test_trim_past_the_safe_interval_is_rejected(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        end = candidate["safeEndUs"] + 500_000
        response = self._put(auth_client, job_id, body, candidate, end - MIN_CLIP_US, end)
        assert response.status_code == 422

    def test_inverted_interval_is_rejected(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        start = candidate["safeStartUs"]
        response = self._put(auth_client, job_id, body, candidate, start + MIN_CLIP_US, start)
        assert response.status_code == 422

    def test_duplicate_candidate_is_rejected(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        clip = {
            "candidateId": candidate["id"],
            "startUs": candidate["recommendedStartUs"],
            "endUs": candidate["recommendedEndUs"],
        }
        response = auth_client.put(
            f"{JOBS}/{job_id}/review",
            json=review_payload(
                body, clips=[{**clip, "order": 1}, {**clip, "order": 2}]
            ),
        )
        assert response.status_code == 422

    def test_non_gapless_order_is_rejected(self, auth_client, analysed_job):
        job_id, body = analysed_job
        clips = [
            {
                "candidateId": clip["candidateId"],
                "order": index * 2,  # 2, 4, 6... -- not gapless
                "startUs": clip["startUs"],
                "endUs": clip["endUs"],
            }
            for index, clip in enumerate(body["selectedClips"], start=1)
        ]
        response = auth_client.put(
            f"{JOBS}/{job_id}/review", json=review_payload(body, clips=clips)
        )
        assert response.status_code == 422

    def test_candidate_from_another_job_is_rejected(self, auth_client, analysed_job, tmp_path):
        job_id, body = analysed_job
        other_job = analyse(
            auth_client, fixtures_media.long_take(tmp_path / "media2").path, MockProvider()
        )
        other = auth_client.get(f"{JOBS}/{other_job}/candidates").json()["candidates"][0]

        response = auth_client.put(
            f"{JOBS}/{job_id}/review",
            json=review_payload(
                body,
                clips=[
                    {
                        "candidateId": other["id"],
                        "order": 1,
                        "startUs": other["recommendedStartUs"],
                        "endUs": other["recommendedEndUs"],
                    }
                ],
            ),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unknown_candidate"


class TestPreviewAccess:
    def test_preview_requires_authentication(self, client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        client.cookies.clear()
        response = client.get(f"{JOBS}/{job_id}/candidates/{candidate['id']}/preview")
        assert response.status_code == 401

    def test_preview_supports_range_requests(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        url = f"{JOBS}/{job_id}/candidates/{candidate['id']}/preview"

        full = auth_client.get(url)
        assert full.status_code == 200
        assert full.headers["Accept-Ranges"] == "bytes"

        partial = auth_client.get(url, headers={"Range": "bytes=0-1023"})
        assert partial.status_code == 206
        assert partial.headers["Content-Range"].startswith("bytes 0-1023/")
        assert len(partial.content) == 1024

    def test_unsatisfiable_range(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        response = auth_client.get(
            f"{JOBS}/{job_id}/candidates/{candidate['id']}/preview",
            headers={"Range": "bytes=99999999-"},
        )
        assert response.status_code == 416

    def test_candidate_from_another_job_is_not_found(self, auth_client, analysed_job, tmp_path):
        job_id, _ = analysed_job
        other_job = analyse(
            auth_client, fixtures_media.long_take(tmp_path / "media3").path, MockProvider()
        )
        other = auth_client.get(f"{JOBS}/{other_job}/candidates").json()["candidates"][0]
        response = auth_client.get(f"{JOBS}/{job_id}/candidates/{other['id']}/preview")
        assert response.status_code == 404

    def test_thumbnail_is_served(self, auth_client, analysed_job):
        job_id, body = analysed_job
        candidate = body["candidates"][0]
        response = auth_client.get(f"{JOBS}/{job_id}/candidates/{candidate['id']}/thumbnail")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/jpeg")


class TestExport:
    def test_source_label_is_visible_in_preview_and_export(
        self, auth_client, tmp_path, settings
    ):
        fixture = fixtures_media.hard_cuts(tmp_path / "labelled-media")
        job_id = analyse(
            auth_client, fixture.path, MockProvider(), source_name="Driver Sphere"
        )
        body = auth_client.get(f"{JOBS}/{job_id}/candidates").json()

        preview_response = auth_client.get(body["candidates"][0]["previewUrl"])
        assert preview_response.status_code == 200
        preview = tmp_path / "labelled-preview.mp4"
        preview.write_bytes(preview_response.content)
        assert _top_left_contains_white_text(preview, tmp_path / "preview-frame.png")

        export = run_export(auth_client, job_id, resolutions=["original"])
        archive = storage.resolve(
            f"exports/{job_id}/{export['id']}/scene-clips-{job_id}-{export['id']}.zip",
            settings,
        )
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["options"]["sourceLabel"]["text"] == "DRIVER SPHERE"
            rendered = tmp_path / "labelled-export.mp4"
            rendered.write_bytes(zf.read("original/0001.mp4"))
        assert _top_left_contains_white_text(rendered, tmp_path / "export-frame.png")

    def test_multi_resolution_export_produces_a_valid_archive(
        self, auth_client, analysed_job, settings
    ):
        job_id, _ = analysed_job
        export = run_export(
            auth_client, job_id, resolutions=["original", "max720p"], include_audio=True
        )
        assert export["state"] == "complete", export
        assert export["downloadAvailable"] is True

        archive = storage.resolve(
            f"exports/{job_id}/{export['id']}/scene-clips-{job_id}-{export['id']}.zip", settings
        )
        with zipfile.ZipFile(archive) as zf:
            assert zf.testzip() is None
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "manifest.csv" in names
            assert any(n.startswith("original/") for n in names)
            assert any(n.startswith("720p/") for n in names)
            # No traversal or absolute members.
            for name in names:
                assert not name.startswith("/")
                assert ".." not in name.split("/")

    def test_serial_numbers_are_gapless_per_folder(self, auth_client, analysed_job, settings):
        job_id, _ = analysed_job
        export = run_export(auth_client, job_id, resolutions=["original", "max720p"])
        archive = storage.resolve(
            f"exports/{job_id}/{export['id']}/scene-clips-{job_id}-{export['id']}.zip", settings
        )
        with zipfile.ZipFile(archive) as zf:
            for folder in ("original", "720p"):
                serials = sorted(
                    int(n.split("/")[1].split(".")[0])
                    for n in zf.namelist()
                    if n.startswith(f"{folder}/")
                )
                assert serials == list(range(1, len(serials) + 1))

    def test_manifests_match_the_generated_files(self, auth_client, analysed_job, settings):
        job_id, _ = analysed_job
        export = run_export(auth_client, job_id, resolutions=["original", "max720p"])
        archive = storage.resolve(
            f"exports/{job_id}/{export['id']}/scene-clips-{job_id}-{export['id']}.zip", settings
        )
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            rows = list(csv.DictReader(io.StringIO(zf.read("manifest.csv").decode("utf-8-sig"))))
            members = set(zf.namelist())

            listed = {
                f["path"] for clip in manifest["clips"] for f in clip["files"]
            }
            assert listed <= members

            for clip in manifest["clips"]:
                for entry in clip["files"]:
                    assert zf.getinfo(entry["path"]).file_size == entry["sizeBytes"]

            assert len(rows) == sum(len(c["files"]) for c in manifest["clips"])
            assert manifest["source"]["averageFrameRate"]
            assert manifest["schemaVersion"] == "1.2"
            assert "sourceLabel" in manifest["options"]

    def test_download_streams_the_archive(self, auth_client, analysed_job):
        job_id, _ = analysed_job
        export = run_export(auth_client, job_id, resolutions=["original"])
        response = auth_client.get(f"{JOBS}/{job_id}/exports/{export['id']}/download")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.content[:2] == b"PK"

    def test_muted_export_contains_no_audio_stream(self, auth_client, analysed_job, settings, tmp_path):
        from app.media.probe import probe_media

        job_id, _ = analysed_job
        export = run_export(
            auth_client, job_id, resolutions=["original"], include_audio=False
        )
        archive = storage.resolve(
            f"exports/{job_id}/{export['id']}/scene-clips-{job_id}-{export['id']}.zip", settings
        )
        with zipfile.ZipFile(archive) as zf:
            extracted = tmp_path / "muted.mp4"
            extracted.write_bytes(zf.read("original/0001.mp4"))
        assert probe_media(extracted).has_audio is False

    def test_audio_export_keeps_streams_synchronized(self, auth_client, analysed_job, settings, tmp_path):
        from app.media.probe import probe_media

        job_id, _ = analysed_job
        export = run_export(auth_client, job_id, resolutions=["original"], include_audio=True)
        archive = storage.resolve(
            f"exports/{job_id}/{export['id']}/scene-clips-{job_id}-{export['id']}.zip", settings
        )
        with zipfile.ZipFile(archive) as zf:
            extracted = tmp_path / "audio.mp4"
            extracted.write_bytes(zf.read("original/0001.mp4"))
        info = probe_media(extracted)
        assert info.has_audio is True
        # The renderer validates start/end synchronization before publishing;
        # reaching here means that validation passed.
        assert MIN_CLIP_US - 100_000 <= info.duration_us <= MAX_CLIP_US + 100_000

    def test_silent_source_exports_successfully_with_audio_requested(
        self, auth_client, tmp_path
    ):
        fixture = fixtures_media.silent_portrait(tmp_path / "media")
        job_id = analyse(auth_client, fixture.path, MockProvider())
        export = run_export(
            auth_client, job_id, resolutions=["original"], include_audio=True
        )
        assert export["state"] == "complete", export

    def test_export_requires_a_matching_review_revision(self, auth_client, analysed_job):
        job_id, _ = analysed_job
        response = auth_client.post(
            f"{JOBS}/{job_id}/exports",
            json={"reviewRevision": 999, "resolutions": ["original"], "includeAudio": True},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "stale_review_revision"

    def test_export_rejects_an_empty_resolution_set(self, auth_client, analysed_job):
        job_id, body = analysed_job
        response = auth_client.post(
            f"{JOBS}/{job_id}/exports",
            json={"reviewRevision": body["reviewRevision"], "resolutions": []},
        )
        assert response.status_code == 422

    def test_export_rejects_duplicate_resolutions(self, auth_client, analysed_job):
        job_id, body = analysed_job
        response = auth_client.post(
            f"{JOBS}/{job_id}/exports",
            json={
                "reviewRevision": body["reviewRevision"],
                "resolutions": ["original", "original"],
            },
        )
        assert response.status_code == 422

    def test_re_export_after_completion(self, auth_client, analysed_job):
        job_id, _ = analysed_job
        first = run_export(auth_client, job_id, resolutions=["original"])
        assert first["state"] == "complete"

        second = run_export(auth_client, job_id, resolutions=["max720p"], include_audio=False)
        assert second["state"] == "complete"
        assert second["id"] != first["id"]

        # The earlier export stays downloadable (spec 8.5).
        assert (
            auth_client.get(f"{JOBS}/{job_id}/exports/{first['id']}/download").status_code == 200
        )
        assert auth_client.get(f"{JOBS}/{job_id}").json()["state"] == "complete"

    def test_export_never_upscales(self, auth_client, analysed_job, settings, tmp_path):
        from app.media.probe import probe_media

        job_id, _ = analysed_job
        export = run_export(auth_client, job_id, resolutions=["max1080p"])
        archive = storage.resolve(
            f"exports/{job_id}/{export['id']}/scene-clips-{job_id}-{export['id']}.zip", settings
        )
        with zipfile.ZipFile(archive) as zf:
            extracted = tmp_path / "big.mp4"
            extracted.write_bytes(zf.read("1080p/0001.mp4"))
        info = probe_media(extracted)
        # The 320x180 source must not be blown up to 1080p.
        assert (info.display_width, info.display_height) == (320, 180)


class TestJobDeletion:
    def test_deletion_tombstones_and_cleans_up(self, auth_client, analysed_job, settings):
        from app.workers.tasks import cleanup_job

        job_id, _ = analysed_job
        run_export(auth_client, job_id, resolutions=["original"])

        assert auth_client.delete(f"{JOBS}/{job_id}").status_code == 204
        # Hidden from the API immediately.
        assert auth_client.get(f"{JOBS}/{job_id}").status_code == 404
        assert all(item["id"] != job_id for item in auth_client.get(JOBS).json()["items"])

        cleanup_job(job_id)
        assert not (settings.jobs_dir / job_id).exists()
        assert not (settings.exports_dir / job_id).exists()


def _top_left_contains_white_text(video, frame) -> bool:  # noqa: ANN001
    result = subprocess.run(
        [
            ffmpeg_binary(get_settings()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0.1",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(frame),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    with Image.open(frame).convert("RGB") as image:
        crop = image.crop((0, 0, min(220, image.width), min(55, image.height)))
        # At 320x180 the reference 4% label is only seven pixels high. H.264
        # chroma subsampling and anti-aliasing keep its brightest decoded pixel
        # just below pure white, so use a luminance floor rather than requiring
        # an exact near-white sample.
        return any(min(pixel) > 180 for pixel in crop.get_flattened_data())
