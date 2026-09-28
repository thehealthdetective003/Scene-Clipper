"""Configurable source-label settings and immutable job snapshots."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from PIL import ImageFont

from alembic import command
from app.config import get_settings, reset_settings_cache
from app.db import session_scope
from app.media.clips import _font_size_that_fits, _source_label_filter
from app.models import Upload
from app.source_labels import FONT_PRESETS, font_path, font_path_for_text

SETTINGS = "/api/v1/settings/source-label"
JOBS = "/api/v1/jobs"


def ready_upload() -> str:
    with session_scope() as db:
        upload = Upload(
            file_name="source.mp4",
            declared_mime_type="video/mp4",
            declared_size_bytes=1024,
            verified_offset_bytes=1024,
            chunk_size_bytes=1024,
            state="ready",
            sha256="a" * 64,
            storage_ext="mp4",
        )
        db.add(upload)
        db.flush()
        return upload.id


def style(**overrides):
    return {
        "fontPreset": "bebas-neue",
        "fillColor": "#FFFFFF",
        "outlineColor": "#000000",
        "sizePercent": 4.0,
        **overrides,
    }


class TestSourceLabelSettings:
    def test_defaults_are_reference_style(self, auth_client):
        response = auth_client.get(SETTINGS)
        assert response.status_code == 200
        assert response.json() | {"updatedAt": None} == style() | {"updatedAt": None}

    def test_updates_and_canonicalizes_colors(self, auth_client):
        response = auth_client.put(
            SETTINGS,
            json=style(
                fontPreset="anton",
                fillColor="#12abef",
                outlineColor="#fedcba",
                sizePercent=5.25,
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["fillColor"] == "#12ABEF"
        assert response.json()["outlineColor"] == "#FEDCBA"
        assert response.json()["fontPreset"] == "anton"
        assert response.json()["sizePercent"] == 5.25

    @pytest.mark.parametrize(
        "override",
        [
            {"fontPreset": "missing-font"},
            {"fillColor": "white"},
            {"outlineColor": "#00000000"},
            {"sizePercent": 2.25},
            {"sizePercent": 8.25},
            {"sizePercent": 4.1},
        ],
    )
    def test_rejects_invalid_style(self, auth_client, override):
        response = auth_client.put(SETTINGS, json=style(**override))
        assert response.status_code == 422

    def test_all_packaged_fonts_are_readable(self):
        for preset in FONT_PRESETS:
            path = font_path(preset)
            assert path.is_file()
            ImageFont.truetype(str(path), size=24)


class TestSourceLabelJobs:
    def test_name_and_style_are_snapshotted(self, auth_client):
        original = style(
            fontPreset="oswald-semibold",
            fillColor="#F0E000",
            outlineColor="#102030",
            sizePercent=5.5,
        )
        assert auth_client.put(SETTINGS, json=original).status_code == 200

        created = auth_client.post(
            JOBS,
            json={
                "uploadId": ready_upload(),
                "targetClipCount": 1,
                "sourceName": "  Driver Sph\u00e8re  ",
                "useGemini": False,
            },
        )
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["sourceLabel"] == {"text": "DRIVER SPH\u00c8RE", "style": original}

        assert auth_client.put(SETTINGS, json=style(fontPreset="anton")).status_code == 200
        persisted = auth_client.get(f"{JOBS}/{job['id']}").json()
        assert persisted["sourceLabel"] == job["sourceLabel"]

    def test_blank_name_disables_the_label(self, auth_client):
        response = auth_client.post(
            JOBS,
            json={
                "uploadId": ready_upload(),
                "targetClipCount": 1,
                "sourceName": "   ",
                "useGemini": False,
            },
        )
        assert response.status_code == 202
        assert response.json()["sourceLabel"] is None

    def test_cjk_name_is_accepted_and_snapshotted(self, auth_client):
        response = auth_client.post(
            JOBS,
            json={
                "uploadId": ready_upload(),
                "targetClipCount": 1,
                "sourceName": "  环球时报 Global Times  ",
                "useGemini": False,
            },
        )
        assert response.status_code == 202, response.text
        assert response.json()["sourceLabel"]["text"] == "环球时报 GLOBAL TIMES"

    def test_idempotent_creation_replays_the_same_labelled_job(self, auth_client):
        payload = {
            "uploadId": ready_upload(),
            "targetClipCount": 1,
            "sourceName": "Torque You",
            "useGemini": False,
        }
        headers = {"Idempotency-Key": "source-label-job-1"}
        first = auth_client.post(JOBS, json=payload, headers=headers)
        replay = auth_client.post(JOBS, json=payload, headers=headers)

        assert first.status_code == replay.status_code == 202
        assert replay.json()["id"] == first.json()["id"]
        assert replay.json()["sourceLabel"] == first.json()["sourceLabel"]

    @pytest.mark.parametrize(
        "name",
        [
            "TORQUE\nYOU",
            "\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a",
            "\u064e",
            "CAR \U0001f697",
            "A" * 49,
        ],
    )
    def test_rejects_unsupported_names(self, auth_client, name):
        response = auth_client.post(
            JOBS,
            json={
                "uploadId": ready_upload(),
                "targetClipCount": 1,
                "sourceName": name,
                "useGemini": False,
            },
        )
        assert response.status_code == 422


def test_drawtext_uses_a_text_file_and_never_interpolates_the_name(tmp_path):
    label = {
        "text": "EV.COM: 100% 'SAFE'",
        "style": style(),
    }
    with _source_label_filter(label, width=1920, height=1080, workspace=tmp_path) as chain:
        assert chain is not None
        assert "textfile=" in chain
        assert "expansion=none" in chain
        assert label["text"] not in chain
        match = re.search(r"textfile='([^']+)'", chain)
        assert match is not None
        assert (
            list(tmp_path.glob("source-label-*.txt"))[0].read_text(encoding="utf-8")
            == label["text"]
        )
    assert not list(tmp_path.glob("source-label-*.txt"))


def test_cjk_labels_choose_the_multilingual_fallback(monkeypatch):
    fallback = font_path("roboto-condensed-bold")
    monkeypatch.setattr("app.source_labels.CJK_FONT_CANDIDATES", (fallback,))
    assert font_path_for_text("bebas-neue", "环球时报") == fallback


@pytest.mark.parametrize(
    ("width", "height"),
    [(1280, 720), (720, 1280), (1920, 1080), (1080, 1920)],
)
def test_drawtext_geometry_is_proportional_at_common_orientations(tmp_path, width, height):
    with _source_label_filter(
        {"text": "EV.COM", "style": style()},
        width=width,
        height=height,
        workspace=tmp_path,
    ) as chain:
        assert chain is not None
        assert f"x={max(1, round(width * 0.0075))}" in chain
        assert f"y={max(1, round(height * 0.01))}" in chain
        assert f"fontsize={round(height * 0.04)}" in chain


def test_long_name_font_size_shrinks_until_it_fits_a_portrait_frame():
    text = "W" * 48
    width, height = 360, 640
    margin = max(1, round(width * 0.0075))
    desired = round(height * 0.08)

    selected, outline = _font_size_that_fits(
        text,
        font_path("roboto-condensed-bold"),
        desired,
        width - (2 * margin),
    )
    font = ImageFont.truetype(str(font_path("roboto-condensed-bold")), size=selected)
    left, _top, right, _bottom = font.getbbox(text, stroke_width=outline)

    assert selected < desired
    assert right - left <= width - (2 * margin)


def test_migration_backfills_defaults_and_keeps_job_columns_nullable(tmp_path, monkeypatch):
    migration_data = tmp_path / "migration-data"
    monkeypatch.setenv("DATA_DIR", str(migration_data))
    reset_settings_cache()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    try:
        command.upgrade(config, "7c1e3a9d42b8")
        database = get_settings().data_dir / "app.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                INSERT INTO settings (
                    id, gemini_request_cap, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                ("singleton", 8, "2026-09-25 00:00:00", "2026-09-25 00:00:00"),
            )

        command.upgrade(config, "head")

        with sqlite3.connect(database) as connection:
            row = connection.execute(
                """
                SELECT source_label_font_preset, source_label_fill_color,
                       source_label_outline_color, source_label_size_percent
                FROM settings WHERE id = 'singleton'
                """
            ).fetchone()
            job_columns = {
                column[1]: column for column in connection.execute("PRAGMA table_info(jobs)")
            }

        assert row == ("bebas-neue", "#FFFFFF", "#000000", 4.0)
        assert job_columns["source_name"][3] == 0
        assert job_columns["source_label_style"][3] == 0
    finally:
        reset_settings_cache()

