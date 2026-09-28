"""Durable, ordered progress snapshots for concurrently analysed sources."""

from pathlib import Path

import pytest

from app.analysis.pipeline import _Context, _record_source_progress
from app.config import get_settings
from tests.helpers import JOBS


def _ready_upload(auth_client, name: str, digest: str) -> str:
    response = auth_client.post(
        "/api/v1/uploads",
        json={"fileName": name, "sizeBytes": 1, "sha256": digest},
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["id"]

    from app.db import session_scope
    from app.models import Upload

    with session_scope() as db:
        upload = db.get(Upload, upload_id)
        assert upload is not None
        upload.verified_offset_bytes = 1
        upload.state = "ready"
        upload.sha256 = digest
    return upload_id


@pytest.mark.parametrize(
    ("first_percent", "second_percent", "expected"),
    [(47.0, 71.0, 56.87), (100.0, 82.0, 86.63)],
)
def test_source_progress_is_independent_and_rolls_up_in_display_order(
    auth_client,
    first_percent,
    second_percent,
    expected,
):
    first_upload = _ready_upload(auth_client, "first.mp4", "a" * 64)
    second_upload = _ready_upload(auth_client, "second.mp4", "b" * 64)
    created = auth_client.post(
        JOBS,
        json={
            "sources": [
                {"uploadId": first_upload, "sourceName": "First"},
                {"uploadId": second_upload, "sourceName": "Second"},
            ],
            "rankingEnabled": False,
        },
    )
    assert created.status_code == 202, created.text
    body = created.json()
    settings = get_settings()
    contexts = [
        _Context(
            job_id=body["id"],
            source_id=source["id"],
            source_order=index,
            source_file_name=source["fileName"],
            source_label={"text": source["sourceName"]},
            settings=settings,
            source=Path("unused.mp4"),
        )
        for index, source in enumerate(body["sources"])
    ]

    first_phase = "ready" if first_percent == 100 else "detecting"
    _record_source_progress(contexts[0], first_phase, first_percent, "First task.")
    _record_source_progress(contexts[1], "measuring", second_percent, "Second task.")

    progress = auth_client.get(f"{JOBS}/{body['id']}").json()
    assert progress["progress"]["percent"] == expected
    assert [source["sourceName"] for source in progress["sources"]] == ["FIRST", "SECOND"]
    assert progress["sources"][0]["progress"] == {
        "phase": first_phase,
        "percent": first_percent,
        "message": "First task.",
    }
    assert progress["sources"][1]["progress"] == {
        "phase": "measuring",
        "percent": second_percent,
        "message": "Second task.",
    }
