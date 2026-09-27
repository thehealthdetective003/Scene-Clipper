"""Multi-source analysis, manual review, and export integration."""

from __future__ import annotations

import io
import json
import zipfile

from app.workers.tasks import run_analysis
from tests import fixtures_media
from tests.conftest import requires_media
from tests.helpers import JOBS, run_export, upload_file

pytestmark = requires_media


def test_manual_multi_source_job_can_select_and_export_across_sources(auth_client, tmp_path):
    first_media = fixtures_media.hard_cuts(tmp_path / "first")
    second_media = fixtures_media.continuous_pan(tmp_path / "second")
    first_upload = upload_file(auth_client, first_media.path)
    second_upload = upload_file(auth_client, second_media.path)

    created = auth_client.post(
        JOBS,
        json={
            "sources": [
                {
                    "uploadId": first_upload,
                    "sourceName": "First source",
                    "contentPrompt": "red scenes",
                },
                {
                    "uploadId": second_upload,
                    "sourceName": "Second source",
                    "contentPrompt": "moving scenes",
                },
            ],
            "rankingEnabled": False,
            "useGemini": True,
        },
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]

    run_analysis(job_id)

    job = auth_client.get(f"{JOBS}/{job_id}").json()
    assert job["state"] == "review-ready", job
    assert job["rankingEnabled"] is False
    assert job["useGemini"] is False
    assert job["selectedCount"] == 0
    assert [source["sourceName"] for source in job["sources"]] == [
        "FIRST SOURCE",
        "SECOND SOURCE",
    ]
    assert all(source["video"] is not None for source in job["sources"])

    review = auth_client.get(f"{JOBS}/{job_id}/candidates").json()
    assert review["selectedClips"] == []
    candidates = review["candidates"]
    by_source = {}
    for candidate in candidates:
        by_source.setdefault(candidate["sourceId"], []).append(candidate)
        assert candidate["score"] == 0
        assert candidate["confidence"] == 0
    assert len(by_source) == 2

    chosen = [source_candidates[0] for source_candidates in by_source.values()]
    replaced = auth_client.put(
        f"{JOBS}/{job_id}/review",
        json={
            "revision": review["reviewRevision"],
            "clips": [
                {
                    "candidateId": candidate["id"],
                    "order": index,
                    "startUs": candidate["recommendedStartUs"],
                    "endUs": candidate["recommendedEndUs"],
                }
                for index, candidate in enumerate(chosen, start=1)
            ],
        },
    )
    assert replaced.status_code == 200, replaced.text

    exported = run_export(auth_client, job_id, resolutions=["original"])
    assert exported["state"] == "complete", exported
    archive_response = auth_client.get(
        f"{JOBS}/{job_id}/exports/{exported['id']}/download"
    )
    assert archive_response.status_code == 200
    archive_path = tmp_path / "multi-source.zip"
    archive_path.write_bytes(archive_response.content)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest["source"]["sources"]) == 2
    assert {clip["sourceName"] for clip in manifest["clips"]} == {
        "FIRST SOURCE",
        "SECOND SOURCE",
    }

    files_response = auth_client.get(
        f"{JOBS}/{job_id}/exports/{exported['id']}/files"
    )
    assert files_response.status_code == 200
    files_body = files_response.json()
    assert {file["sourceId"] for file in files_body["files"]} == set(by_source)
    assert [bundle["fileName"] for bundle in files_body["sourceBundles"]] == [
        "FIRST-SOURCE.zip",
        "SECOND-SOURCE.zip",
    ]

    for source_bundle in files_body["sourceBundles"]:
        response = auth_client.get(source_bundle["downloadUrl"])
        assert response.status_code == 200
        assert source_bundle["fileName"] in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            source_manifest = json.loads(archive.read("manifest.json"))
            assert {clip["sourceId"] for clip in source_manifest["clips"]} == {
                source_bundle["sourceId"]
            }
            mp4s = [name for name in archive.namelist() if name.endswith(".mp4")]
            assert len(mp4s) == source_bundle["fileCount"]
