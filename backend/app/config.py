"""Deployment configuration.

The application MUST refuse startup if the encryption key, administrator
username, or administrator password hash is missing or malformed (spec 7.5).
Validation therefore happens eagerly at import of :func:`get_settings`.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when the deployment is not safely configured."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Identity and secrets ------------------------------------------------
    app_base_url: str = "http://localhost:5173"
    app_encryption_key: str = ""
    admin_username: str = ""
    admin_password_hash: str = ""

    # --- Storage -------------------------------------------------------------
    data_dir: Path = Path("/data")
    max_upload_bytes: int = 21_474_836_480
    upload_chunk_bytes: int = 16_777_216

    # --- Gemini --------------------------------------------------------------
    gemini_model: str = "gemini-3.8-flash"
    gemini_request_cap: int = Field(default=8, ge=0, le=50)
    gemini_inline_payload_limit_bytes: int = 18_874_368
    gemini_timeout_seconds: float = 120.0

    # --- Sessions ------------------------------------------------------------
    session_idle_minutes: int = Field(default=60, ge=1)
    session_absolute_hours: int = Field(default=12, ge=1)

    # --- Operations ----------------------------------------------------------
    retention_enabled: bool = False
    log_level: str = "INFO"
    redis_url: str = "redis://redis:6379/0"
    trust_proxy_headers: bool = True
    https_enabled: bool = True

    # --- Media tooling -------------------------------------------------------
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    media_timeout_seconds: int = 1800
    media_memory_bytes: int = 3_221_225_472

    # --- Detector defaults (spec 6.2) ---------------------------------------
    detector_config_version: str = "1"
    detect_adaptive_threshold: float = 3.0
    detect_min_content_val: float = 15.0
    detect_window_width: int = 2
    detect_min_scene_len_frames: int = 2
    detect_threshold_value: float = 12.0
    detect_fade_bias: float = 0.0
    detect_analysis_width: int = 480

    # --- Rate limits ---------------------------------------------------------
    login_rate_limit_per_minute: int = 5
    key_test_rate_limit_per_minute: int = 10

    @field_validator("app_encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "APP_ENCRYPTION_KEY is required. Generate one with "
                "'python -m app.cli generate-key' or 'openssl rand -base64 32'."
            )
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:  # pragma: no cover - message path
            raise ValueError("APP_ENCRYPTION_KEY must be valid base64.") from exc
        if len(raw) != 32:
            raise ValueError(
                f"APP_ENCRYPTION_KEY must decode to exactly 32 bytes, got {len(raw)}."
            )
        return value

    @field_validator("admin_username")
    @classmethod
    def _validate_admin_username(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ADMIN_USERNAME is required.")
        return value.strip()

    @field_validator("admin_password_hash")
    @classmethod
    def _validate_admin_password_hash(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "ADMIN_PASSWORD_HASH is required. Generate one with: "
                "python -m app.cli hash-password"
            )
        if not value.startswith("$argon2id$"):
            raise ValueError(
                "ADMIN_PASSWORD_HASH must be an Argon2id PHC string "
                "(it starts with '$argon2id$'). Plaintext passwords are refused."
            )
        return value

    @field_validator("upload_chunk_bytes")
    @classmethod
    def _validate_chunk_bytes(cls, value: int, info: ValidationInfo) -> int:
        if value < 64 * 1024:
            raise ValueError("UPLOAD_CHUNK_BYTES must be at least 64 KiB.")
        max_bytes = info.data.get("max_upload_bytes")
        if max_bytes and value > max_bytes:
            raise ValueError("UPLOAD_CHUNK_BYTES cannot exceed MAX_UPLOAD_BYTES.")
        return value

    # --- Derived -------------------------------------------------------------
    @property
    def encryption_key_bytes(self) -> bytes:
        return base64.b64decode(self.app_encryption_key, validate=True)

    @property
    def allowed_origin(self) -> str:
        """Exact origin permitted for state-changing requests (spec 5.1)."""
        parts = urlsplit(self.app_base_url)
        if not parts.scheme or not parts.netloc:
            raise ConfigurationError("APP_BASE_URL must be an absolute URL.")
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def is_loopback(self) -> bool:
        host = urlsplit(self.app_base_url).hostname or ""
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

    @property
    def cookie_secure(self) -> bool:
        return self.https_enabled and not self.is_loopback

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{(self.data_dir / 'app.sqlite3').as_posix()}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.uploads_dir,
            self.jobs_dir,
            self.exports_dir,
            self.cache_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process."""
    try:
        return Settings()
    except Exception as exc:  # noqa: BLE001 - surfaced as a startup refusal
        raise ConfigurationError(f"Invalid deployment configuration: {exc}") from exc


def reset_settings_cache() -> None:
    """Test hook: drop the memoized settings instance."""
    get_settings.cache_clear()
