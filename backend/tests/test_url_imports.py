"""URL video imports share the upload verification and job pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import session_scope
from app.downloading import (
    UrlDownloadError,
    _assert_public_origin,
    _suggested_source_name,
    run_url_download,
)
from app.models import Upload
from app.workers import recovery

IMPORT = "/api/v1/uploads/from-url"


class TestUrlImportApi:
    def test_queues_a_public_single_video_url(self, auth_client, no_queue):
        response = auth_client.post(
            IMPORT,
            json={"url": "https://www.youtube.com/watch?v=abc123#chapter"},
            headers={"Idempotency-Key": "url-import-1"},
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["state"] == "downloading"
        assert body["sourceKind"] == "url"
        assert body["declaredSizeBytes"] == 0
        assert body["suggestedSourceName"] is None
        assert body["id"] in no_queue["download"]
        assert "url" not in {key.lower() for key in body}

        with session_scope() as db:
            stored = db.get(Upload, body["id"])
            assert stored is not None
            assert stored.source_url == "https://www.youtube.com/watch?v=abc123"

    def test_idempotent_retry_reuses_the_download(self, auth_client):
        headers = {"Idempotency-Key": "url-import-replay"}
        payload = {"url": "https://youtu.be/abc123"}
        first = auth_client.post(IMPORT, json=payload, headers=headers)
        replay = auth_client.post(IMPORT, json=payload, headers=headers)
        assert first.status_code == replay.status_code == 202
        assert first.json()["id"] == replay.json()["id"]

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://localhost/video.mp4",
            "http://127.0.0.1/video.mp4",
            "http://10.0.0.5/video.mp4",
            "http://metadata/video",
            "https://user:password@example.com/video",
        ],
    )
    def test_refuses_non_web_private_and_credentialed_urls(self, auth_client, url):
        response = auth_client.post(IMPORT, json={"url": url})
        assert response.status_code == 422
        assert response.json()["error"]["code"] in {
            "invalid_video_url",
            "private_video_url",
        }


def test_downloader_publishes_into_the_normal_verification_lifecycle(
    auth_client, monkeypatch, settings
):
    upload_id = auth_client.post(
        IMPORT, json={"url": "https://video.example/watch/one"}
    ).json()["id"]

    monkeypatch.setattr("app.downloading._assert_public_origin", lambda _url: None)

    def fake_download(_url, workspace: Path, **_kwargs):
        result = workspace / "download.webm"
        result.write_bytes(b"downloaded-media")
        return "A useful title", "Global Times", result

    monkeypatch.setattr("app.downloading._download", fake_download)
    queued: list[str] = []
    monkeypatch.setattr(
        "app.workers.queue.enqueue_upload_verification",
        lambda value: queued.append(value) or True,
    )

    run_url_download(upload_id)

    with session_scope() as db:
        upload = db.get(Upload, upload_id)
        assert upload is not None
        assert upload.state == "verifying"
        assert upload.file_name == "A useful title.webm"
        assert upload.suggested_source_name == "Global Times"
        assert upload.storage_ext == "webm"
        assert upload.declared_size_bytes == len(b"downloaded-media")
        assert upload.source_url == "https://video.example/watch/one"
        assert upload.relative_source_path == f"uploads/{upload_id}/source.webm"
    assert (settings.uploads_dir / upload_id / "source.webm").read_bytes() == b"downloaded-media"
    assert queued == [upload_id]

    body = auth_client.get(f"/api/v1/uploads/{upload_id}").json()
    assert body["suggestedSourceName"] == "Global Times"


def test_channel_metadata_is_preferred_for_the_source_name():
    assert (
        _suggested_source_name(
            {
                "channel": " Global   Times ",
                "uploader": "Uploader fallback",
                "creator": "Creator fallback",
            }
        )
        == "Global Times"
    )


def test_invalid_channel_metadata_falls_back_to_a_renderable_uploader():
    assert (
        _suggested_source_name({"channel": "🚀", "uploader": "Usable uploader"})
        == "Usable uploader"
    )


def test_unsupported_channel_decoration_is_removed_without_losing_the_name():
    assert _suggested_source_name({"channel": "Channel 🚀"}) == "Channel"


def test_downloader_failure_is_sanitized_for_the_client(auth_client, monkeypatch):
    upload_id = auth_client.post(
        IMPORT, json={"url": "https://video.example/private?token=secret"}
    ).json()["id"]
    monkeypatch.setattr("app.downloading._assert_public_origin", lambda _url: None)

    def unavailable(*_args, **_kwargs):
        raise UrlDownloadError(
            "video_download_failed",
            "The video could not be downloaded. It may be private, unavailable, or unsupported.",
            retryable=True,
        )

    monkeypatch.setattr("app.downloading._download", unavailable)
    run_url_download(upload_id)

    body = auth_client.get(f"/api/v1/uploads/{upload_id}").json()
    assert body["state"] == "failed"
    assert body["error"]["phase"] == "downloading"
    assert body["error"]["retryable"] is True
    assert "secret" not in str(body)


def test_dns_rebinding_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("192.168.1.20", 443))],
    )
    with pytest.raises(UrlDownloadError) as caught:
        _assert_public_origin("https://public-looking.example/video")
    assert caught.value.code == "private_video_url"


def test_recovery_requeues_an_interrupted_download(auth_client, monkeypatch):
    upload_id = auth_client.post(
        IMPORT, json={"url": "https://video.example/watch/recover-me"}
    ).json()["id"]
    requeued: list[str] = []
    monkeypatch.setattr(
        recovery,
        "enqueue_url_download",
        lambda value: requeued.append(value) or True,
    )
    monkeypatch.setattr(recovery, "enqueue_export", lambda _value: True)
    monkeypatch.setattr(recovery, "enqueue_analysis", lambda _value: True)
    monkeypatch.setattr(recovery, "enqueue_job_cleanup", lambda _value: True)
    monkeypatch.setattr(recovery, "enqueue_upload_verification", lambda _value: True)
    monkeypatch.setattr(recovery, "enqueue_provider_file_cleanup", lambda: True)

    counts = recovery.requeue_abandoned_work()

    assert requeued == [upload_id]
    assert counts["downloads"] == 1
