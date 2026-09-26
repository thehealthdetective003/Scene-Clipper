"""Shared helpers for tests that drive the whole stack."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from app.providers.gemini import set_provider

UPLOADS = "/api/v1/uploads"
JOBS = "/api/v1/jobs"

#: The sentinel planted by security tests; also used as the working test key.
SENTINEL_KEY = "AIzaSyFAKEKEYSENTINEL_do_not_log_0000000"


def checksum_header(payload: bytes) -> str:
    return "sha256 " + base64.b64encode(hashlib.sha256(payload).digest()).decode()


def upload_file(client, path: Path, *, chunk_size: int = 64 * 1024, interrupt_after=None) -> str:
    """Upload through the real chunk API, optionally simulating an interruption."""
    payload = path.read_bytes()
    created = client.post(
        UPLOADS,
        json={
            "fileName": path.name,
            "sizeBytes": len(payload),
            "mimeType": "video/mp4",
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]

    offset = 0
    index = 0
    while offset < len(payload):
        block = payload[offset : offset + chunk_size]
        if interrupt_after is not None and index == interrupt_after:
            head = client.head(f"{UPLOADS}/{upload_id}")
            offset = int(head.headers["Upload-Offset"])
            interrupt_after = None
            continue

        response = client.put(
            f"{UPLOADS}/{upload_id}/chunks",
            content=block,
            headers={
                "Upload-Offset": str(offset),
                "Content-Length": str(len(block)),
                "Upload-Checksum": checksum_header(block),
            },
        )
        assert response.status_code == 204, response.text
        offset = int(response.headers["Upload-Offset"])
        index += 1

    assert client.post(f"{UPLOADS}/{upload_id}/complete").status_code == 202

    from app.workers.tasks import verify_upload

    verify_upload(upload_id)
    state = client.get(f"{UPLOADS}/{upload_id}").json()
    assert state["state"] == "ready", state
    return upload_id


def configure_key(
    client, provider, *, request_cap: int = 8, key: str = SENTINEL_KEY, label: str | None = None
):
    set_provider(provider)
    body = {"apiKey": key, "model": "gemini-test-model", "requestCap": request_cap}
    if label:
        body["label"] = label
    response = client.put("/api/v1/settings/gemini", json=body)
    assert response.status_code == 200, response.text
    return response


def configure_keys(client, provider, labels, *, request_cap: int = 8):
    """Fill the failover pool, one distinct key per label, in order.

    Each key is a recognizable sentinel so a test can assert exactly which
    credential a call was made with.
    """
    set_provider(provider)
    for index, label in enumerate(labels):
        configure_key(
            client,
            provider,
            request_cap=request_cap,
            key=f"{SENTINEL_KEY[:-1]}{index}",
            label=label,
        )
    return client.get("/api/v1/settings/gemini").json()["keys"]


def key_state(client):
    """``{label: status}`` for the stored pool."""
    keys = client.get("/api/v1/settings/gemini").json()["keys"]
    return {key["label"]: key["status"] for key in keys}


def run_job(
    client,
    upload_id: str,
    *,
    target: int = 20,
    prompt=None,
    source_name: str | None = None,
    use_gemini: bool = True,
) -> str:
    from app.workers.tasks import run_analysis

    body = {"uploadId": upload_id, "targetClipCount": target, "useGemini": use_gemini}
    if prompt:
        body["contentPrompt"] = prompt
    if source_name:
        body["sourceName"] = source_name
    created = client.post(JOBS, json=body)
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    run_analysis(job_id)
    return job_id


def analyse(client, path: Path, provider, **kwargs) -> str:
    configure_key(client, provider, request_cap=kwargs.pop("request_cap", 8))
    return run_job(client, upload_file(client, path), **kwargs)


def run_export(client, job_id: str, *, resolutions, include_audio: bool = True) -> dict:
    from app.workers.tasks import run_export as execute

    job = client.get(f"{JOBS}/{job_id}").json()
    created = client.post(
        f"{JOBS}/{job_id}/exports",
        json={
            "reviewRevision": job["reviewRevision"],
            "resolutions": resolutions,
            "includeAudio": include_audio,
        },
    )
    assert created.status_code == 202, created.text
    export_id = created.json()["id"]
    execute(export_id)
    return client.get(f"{JOBS}/{job_id}/exports/{export_id}").json()
