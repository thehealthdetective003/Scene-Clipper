"""Shared pytest fixtures.

Environment variables are set at import time, before any application module is
loaded, because configuration is validated eagerly and memoized (spec 7.5).
Gemini is mocked for every routine test (spec 12); the credentialed smoke test
is opt-in and lives in ``test_gemini_smoke.py``.
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# --- Environment (must precede any `app.*` import) -------------------------

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="clipper-tests-"))

#: A recognizable sentinel. Security tests assert it never escapes (spec 10.4).
FAKE_GEMINI_KEY = "AIzaSyFAKEKEYSENTINEL_do_not_log_0000000"
TEST_PASSWORD = "correct-horse-battery-staple"

os.environ.setdefault("APP_BASE_URL", "https://clips.test.internal")
os.environ["APP_ENCRYPTION_KEY"] = base64.b64encode(b"0" * 32).decode("ascii")
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["HTTPS_ENABLED"] = "true"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["GEMINI_MODEL"] = "gemini-test-model"
os.environ["GEMINI_REQUEST_CAP"] = "8"

from argon2 import PasswordHasher  # noqa: E402

os.environ["ADMIN_PASSWORD_HASH"] = PasswordHasher(
    time_cost=1, memory_cost=8192, parallelism=1
).hash(TEST_PASSWORD)

# Locate FFmpeg for media tests; absent tools simply skip those tests.
_WINGET_FFMPEG = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
)
if "FFMPEG_PATH" not in os.environ:
    found = shutil.which("ffmpeg")
    if found:
        os.environ["FFMPEG_PATH"] = found
        os.environ["FFPROBE_PATH"] = shutil.which("ffprobe") or ""
    elif _WINGET_FFMPEG.exists():
        for candidate in _WINGET_FFMPEG.rglob("ffmpeg.exe"):
            os.environ["FFMPEG_PATH"] = str(candidate)
            os.environ["FFPROBE_PATH"] = str(candidate.with_name("ffprobe.exe"))
            break

import fakeredis  # noqa: E402

from app import db as db_module  # noqa: E402
from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.models import Base  # noqa: E402
from app.util import redis_client  # noqa: E402


def media_tools_available() -> bool:
    from app.media.runner import MediaToolError, ffmpeg_binary, ffprobe_binary

    try:
        ffmpeg_binary(get_settings())
        ffprobe_binary(get_settings())
    except MediaToolError:
        return False
    return True


requires_media = pytest.mark.skipif(
    not media_tools_available(), reason="ffmpeg/ffprobe are not available"
)


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Give every test its own DATA_DIR, database, and Redis."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()

    settings = get_settings()
    settings.ensure_directories()

    engine = db_module.build_engine(settings)
    Base.metadata.create_all(engine)
    db_module.reset_engine(engine)

    server = fakeredis.FakeServer()
    fake = fakeredis.FakeStrictRedis(server=server)
    redis_client.reset_redis_cache()
    # Every module that imported the name directly needs its own binding
    # replaced, or it will still reach for a real Redis.
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr("app.security.ratelimit.get_redis", lambda: fake)
    monkeypatch.setattr("app.workers.queue.get_redis", lambda: fake)

    yield

    db_module.reset_engine(None)
    reset_settings_cache()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def db_session():
    with db_module.session_scope() as session:
        yield session


@pytest.fixture
def no_queue(monkeypatch):
    """Neutralize enqueueing so tests drive workers explicitly."""
    calls: dict[str, list] = {"analysis": [], "export": [], "upload": [], "cleanup": []}

    monkeypatch.setattr(
        "app.api.routes_jobs.enqueue_analysis",
        lambda job_id: calls["analysis"].append(job_id) or True,
    )
    monkeypatch.setattr(
        "app.api.routes_jobs.enqueue_job_cleanup",
        lambda job_id: calls["cleanup"].append(job_id) or True,
    )
    monkeypatch.setattr(
        "app.api.routes_exports.enqueue_export",
        lambda export_id: calls["export"].append(export_id) or True,
    )
    monkeypatch.setattr(
        "app.api.routes_uploads.enqueue_upload_verification",
        lambda upload_id: calls["upload"].append(upload_id) or True,
    )
    return calls


@pytest.fixture
def client(no_queue):
    """An authenticated-capable TestClient with same-origin headers."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app, base_url="https://clips.test.internal") as test_client:
        test_client.headers.update(
            {
                "Origin": "https://clips.test.internal",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
            }
        )
        yield test_client


@pytest.fixture
def auth_client(client):
    """Signed in, with the CSRF token attached to every request."""
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": TEST_PASSWORD}
    )
    assert response.status_code == 204, response.text
    session = client.get("/api/v1/session").json()
    assert session["authenticated"] is True
    client.headers["X-CSRF-Token"] = session["csrfToken"]
    return client
