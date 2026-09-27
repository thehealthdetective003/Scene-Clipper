"""SQLAlchemy 2 ORM models (spec 7.4).

Conventions
-----------
* Every primary key is a UUIDv7 string.
* Media positions are integer microseconds (``BigInteger``), never floats.
* Timestamps are timezone-aware UTC via :class:`UtcDateTime`.
* Paths are stored relative to ``DATA_DIR`` and built only from server-side
  identifiers (spec 7.3, 10.2).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.util.ids import new_id


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class UtcDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on a backend that forgets tzinfo."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not accepted; use utcnow().")
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------
# Settings and sessions
# --------------------------------------------------------------------------


class Settings(Base, TimestampMixin):
    """Singleton deployment settings holding encrypted provider key material.

    Only the envelope components are persisted. The plaintext key is never
    stored, returned, or logged (spec 5.2).
    """

    __tablename__ = "settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    gemini_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gemini_request_cap: Mapped[int] = mapped_column(Integer, default=8, nullable=False)

    # Global defaults copied onto each new job.  Existing jobs never consult
    # this row again, so changing the look cannot mutate an earlier result.
    source_label_font_preset: Mapped[str] = mapped_column(
        String(32), default="bebas-neue", nullable=False
    )
    source_label_fill_color: Mapped[str] = mapped_column(
        String(7), default="#FFFFFF", nullable=False
    )
    source_label_outline_color: Mapped[str] = mapped_column(
        String(7), default="#000000", nullable=False
    )
    source_label_size_percent: Mapped[float] = mapped_column(
        Float, default=4.0, nullable=False
    )

    key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_tag: Mapped[bytes | None] = mapped_column(LargeBinary(16), nullable=True)
    key_updated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    @property
    def key_configured(self) -> bool:
        return all(
            component is not None
            for component in (self.key_version, self.key_nonce, self.key_ciphertext, self.key_tag)
        )


#: Upper bound on stored provider keys (failover pool).
MAX_GEMINI_KEYS = 5

#: Key health. `exhausted` and `invalid` are the states the UI shows in red.
KEY_ACTIVE = "active"
KEY_EXHAUSTED = "exhausted"     # quota or rate limit; recovers after a cooldown
KEY_INVALID = "invalid"         # rejected credential; needs the operator
KEY_UNAVAILABLE = "unavailable"  # transient provider fault; short cooldown
KEY_DISABLED = "disabled"       # switched off by the operator


class GeminiKey(Base, TimestampMixin):
    """One provider credential in the failover pool.

    Several keys can be stored so a job continues when one hits its quota.
    Only the AES-256-GCM envelope is persisted -- never the key itself, and
    never a masked form of it (spec 5.2). Health is tracked per key so the
    operator can see exactly which credential stopped working.
    """

    __tablename__ = "gemini_keys"
    __table_args__ = (UniqueConstraint("position", name="uq_gemini_keys_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Failover order; the lowest usable position is tried first.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    key_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_tag: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=KEY_ACTIVE, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: While set and in the future, the key is skipped during failover.
    cooldown_until: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requests_succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requests_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def is_available(self, now: dt.datetime | None = None) -> bool:
        """Usable right now: not disabled, not invalid, not cooling down."""
        if self.status in (KEY_DISABLED, KEY_INVALID):
            return False
        if self.cooldown_until is not None and (now or utcnow()) < self.cooldown_until:
            return False
        return True


class Session(Base):
    """Server-side session. The cookie carries a token; only its hash is stored."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # The CSRF token is returned by GET /session on every call and is only
    # usable together with the session cookie, so it is stored verbatim.
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    idle_expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    absolute_expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------


class Upload(Base, TimestampMixin):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Display-only; the on-disk path is built from the upload UUID (spec 5.3).
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    declared_mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    declared_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verified_offset_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    chunk_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="created", nullable=False, index=True)

    #: ``file`` for browser uploads, ``url`` for yt-dlp imports.  A remote URL
    #: is worker-only metadata: it is never returned by an API serializer or
    #: placed in a Redis queue payload.
    source_kind: Mapped[str] = mapped_column(String(16), default="file", nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Authoritative server-computed digest, set at completion.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Optional client-declared digest, used only for a mismatch check.
    client_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    storage_ext: Mapped[str] = mapped_column(String(16), default="bin", nullable=False)
    probe: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    #: Number of live jobs referencing this source file (spec 7.3).
    reference_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="upload")
    job_sources: Mapped[list["JobSource"]] = relationship(back_populates="upload")

    @property
    def relative_dir(self) -> str:
        return f"uploads/{self.id}"

    @property
    def relative_source_path(self) -> str:
        return f"{self.relative_dir}/source.{self.storage_ext}"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=False, index=True)

    # --- Snapshotted configuration (spec 5.4) -----------------------------
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    target_clip_count: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    content_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_prompt_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(48), nullable=True)
    source_label_style: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    use_gemini: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: When false, candidates are left unselected in source order for a fully
    #: manual review.  This is distinct from local-only ranking.
    ranking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    gemini_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gemini_request_cap: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    detector_config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Probed media -----------------------------------------------------
    video: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # --- Progress and results --------------------------------------------
    progress_phase: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=False)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    progress_message: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    #: Last committed pipeline checkpoint; a retry resumes from here (spec 5.5).
    checkpoint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eligible_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    partial_result_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cache_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Tombstone: set synchronously on DELETE, hides the job from every API.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True, index=True)
    cleanup_completed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    upload: Mapped[Upload] = relationship(back_populates="jobs")
    sources: Mapped[list["JobSource"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobSource.order_index",
    )
    usage: Mapped["AnalysisUsage"] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def relative_dir(self) -> str:
        return f"jobs/{self.id}"


class JobSource(Base, TimestampMixin):
    """One independently configured source video inside a job."""

    __tablename__ = "job_sources"
    __table_args__ = (
        UniqueConstraint("job_id", "order_index", name="uq_job_sources_order"),
        UniqueConstraint("job_id", "upload_id", name="uq_job_sources_upload"),
        Index("ix_job_sources_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id"), nullable=False, index=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(48), nullable=True)
    source_label_style: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    content_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_prompt_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    video: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checkpoint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    detected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eligible_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    job: Mapped[Job] = relationship(back_populates="sources")
    upload: Mapped[Upload] = relationship(back_populates="job_sources")


class JobEvent(Base):
    """Durable SSE backlog; retained until the job is deleted (spec 8.4)."""

    __tablename__ = "job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_job_events_job_sequence"),
        Index("ix_job_events_job_sequence", "job_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)


class DetectedShot(Base):
    """A time range between local transition boundaries (spec 3)."""

    __tablename__ = "detected_shots"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "shot_number", name="uq_detected_shots_source_number"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id"), nullable=False, index=True
    )
    shot_number: Mapped[int] = mapped_column(Integer, nullable=False)

    source_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    safe_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    safe_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usable_duration_us: Mapped[int] = mapped_column(BigInteger, nullable=False)

    incoming_boundary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    outgoing_boundary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ineligible_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CandidateShot(Base):
    """A detected shot whose safe interval is at least three seconds."""

    __tablename__ = "candidate_shots"
    __table_args__ = (
        UniqueConstraint("job_id", "detected_shot_id", name="uq_candidate_per_detected_shot"),
        Index("ix_candidate_shots_job_rank", "job_id", "rank"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("job_sources.id"), nullable=False, index=True
    )
    detected_shot_id: Mapped[str] = mapped_column(ForeignKey("detected_shots.id"), nullable=False)
    shot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(48), nullable=True)
    source_file_name: Mapped[str] = mapped_column(String(512), nullable=False)

    source_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    safe_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    safe_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usable_duration_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recommended_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recommended_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_long_shot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    incoming_boundary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    outgoing_boundary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # --- Stage E local measurements --------------------------------------
    features: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    local_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Ranking ----------------------------------------------------------
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.35, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gemini_relevance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gemini_interest: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gemini_clarity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    motion_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # --- Product-focus signals (only meaningful with a focus prompt) -------
    #: True when any part of a person is visible. ``None`` means "not assessed".
    human_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: How the human verdict was reached: ``model`` or ``local`` (best effort).
    human_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: True when the prompted product is clearly shown.
    product_visible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 0-100, how clearly and centrally the product is framed.
    product_prominence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Why automatic selection skipped this candidate, if it did.
    excluded_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    scoring_source: Mapped[str] = mapped_column(
        String(32), default="local-fallback", nullable=False
    )
    cache_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    prompt_relevance_evaluated: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    thumbnail_path: Mapped[str | None] = mapped_column(String(512), nullable=True)


class SelectedClip(Base):
    """A candidate included in the current user-reviewed export order."""

    __tablename__ = "selected_clips"
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id", name="uq_selected_clip_candidate"),
        UniqueConstraint("job_id", "order_index", name="uq_selected_clip_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate_shots.id"), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    review_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------
# Provider accounting
# --------------------------------------------------------------------------


class AnalysisUsage(Base):
    __tablename__ = "analysis_usage"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_cap: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requests_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    coarse_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fine_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    proxy_video_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    proxy_video_candidates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_file_operations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    proxy_video_bytes_sent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    proxy_video_seconds_sent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    local_fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    job: Mapped[Job] = relationship(back_populates="usage")


class AnalysisAttempt(Base):
    """Durable budget reservation ledger (spec 6.7).

    One row is committed *before* each outbound model request. A worker that
    dies after transmission leaves the row in ``reserved``; that unit stays
    consumed, which keeps the cap conservative across crashes.
    """

    __tablename__ = "analysis_attempts"
    __table_args__ = (Index("ix_analysis_attempts_job_stage", "job_id", "stage"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # coarse|fine|proxy
    status: Mapped[str] = mapped_column(String(16), default="reserved", nullable=False)
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reserved_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    settled_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AnalysisCache(Base):
    """Reusable ranking results keyed by content + configuration (spec 6.10)."""

    __tablename__ = "analysis_cache"

    cache_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    gemini_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detector_config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # coarse|fine
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reference_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)


class ProviderFile(Base):
    """Remote provider file lifecycle, persisted before upload (spec 7.3)."""

    __tablename__ = "provider_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), default="proxy-video", nullable=False)
    cleanup_status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )
    cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


class Export(Base, TimestampMixin):
    __tablename__ = "exports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), default="queued", nullable=False, index=True)
    review_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resolutions: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    include_audio: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    progress_message: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    zip_relative_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    zip_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    zip_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    files: Mapped[list["ExportFile"]] = relationship(
        back_populates="export", cascade="all, delete-orphan"
    )


class ExportFile(Base):
    """One validated media file inside an export (a clip x resolution pair)."""

    __tablename__ = "export_files"
    __table_args__ = (
        UniqueConstraint("export_id", "resolution", "serial", name="uq_export_file_slot"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    export_id: Mapped[str] = mapped_column(ForeignKey("exports.id"), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False)
    serial: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ZIP-relative path, e.g. ``1080p/0001.mp4``.
    archive_path: Mapped[str] = mapped_column(String(255), nullable=False)
    #: DATA_DIR-relative path of the published file.
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    export: Mapped[Export] = relationship(back_populates="files")


# --------------------------------------------------------------------------
# Coordination and audit
# --------------------------------------------------------------------------


class WorkerLease(Base):
    """Lease + heartbeat so an abandoned job can be safely reclaimed (spec 5.5)."""

    __tablename__ = "worker_leases"

    #: ``{kind}:{resource_id}``, e.g. ``analysis:0192...``.
    lease_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    heartbeat_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)


class AuditEvent(Base):
    """Security-relevant actions, never containing secrets or media (spec 10.1)."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class IdempotencyRecord(Base):
    """Replay protection for mutating routes (spec 8.1)."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "principal", "method", "route", "idempotency_key", name="uq_idempotency_scope"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    principal: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
