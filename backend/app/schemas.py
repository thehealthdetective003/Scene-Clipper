"""API request/response contracts (spec 9).

JSON fields are ``camelCase``; the Python attributes stay ``snake_case`` and are
aliased. Every constraint that the normative TypeScript contract states is
enforced here so the API and the workers validate identically.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.source_labels import (
    FONT_PRESETS,
    SOURCE_LABEL_SIZE_MAX,
    SOURCE_LABEL_SIZE_MIN,
    SOURCE_NAME_MAX_CHARS,
    normalize_color,
    normalize_size,
    normalize_source_name,
)


def to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", from_attributes=True
    )


Resolution = Literal["original", "max1080p", "max720p"]
JobState = Literal[
    "uploaded",
    "probing",
    "detecting",
    "ranking",
    "review-ready",
    "exporting",
    "complete",
    "failed",
    "cancelled",
]
ExportState = Literal["queued", "exporting", "complete", "failed", "cancelled"]
UploadState = Literal["created", "uploading", "verifying", "ready", "failed"]
ScoringSource = Literal["contact-sheet", "proxy-video", "local-fallback"]
CacheStatus = Literal["none", "partial", "complete"]

MAX_PROMPT_CHARS = 2000


# --- Auth and settings -----------------------------------------------------


class LoginRequest(ApiModel):
    username: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class SessionResponse(ApiModel):
    authenticated: bool
    csrf_token: str | None = None


KeyStatus = Literal["active", "exhausted", "invalid", "unavailable", "disabled"]
SourceLabelFontPreset = Literal[
    "bebas-neue", "anton", "oswald-semibold", "roboto-condensed-bold"
]


class SourceLabelStyleModel(ApiModel):
    font_preset: SourceLabelFontPreset
    fill_color: str
    outline_color: str
    size_percent: float


class SourceLabelSettingsResponse(SourceLabelStyleModel):
    updated_at: str | None


class SourceLabelSettingsUpdate(SourceLabelStyleModel):
    @field_validator("font_preset")
    @classmethod
    def _known_font(cls, value: str) -> str:
        if value not in FONT_PRESETS:
            raise ValueError("Choose one of the supported source-label fonts.")
        return value

    @field_validator("fill_color", "outline_color")
    @classmethod
    def _color(cls, value: str) -> str:
        return normalize_color(value)

    @field_validator("size_percent")
    @classmethod
    def _size(cls, value: float) -> float:
        if not SOURCE_LABEL_SIZE_MIN <= value <= SOURCE_LABEL_SIZE_MAX:
            raise ValueError(
                f"Size must be between {SOURCE_LABEL_SIZE_MIN:g}% and "
                f"{SOURCE_LABEL_SIZE_MAX:g}%."
            )
        return normalize_size(value)


class SourceLabelModel(ApiModel):
    text: str
    style: SourceLabelStyleModel


class GeminiKeyModel(ApiModel):
    """One credential in the failover pool.

    Carries health only -- never the key, and never a masked form of it.
    """

    id: str
    label: str
    position: int
    status: KeyStatus
    #: True while this key would be used for the next outbound call.
    available: bool
    last_error_code: str | None = None
    last_error_at: str | None = None
    last_success_at: str | None = None
    cooldown_until: str | None = None
    requests_succeeded: int = 0
    requests_failed: int = 0
    created_at: str | None = None


class GeminiSettingsResponse(ApiModel):
    configured: bool
    model: str | None
    request_cap: int
    updated_at: str | None
    keys: list[GeminiKeyModel] = Field(default_factory=list)
    max_keys: int = 5
    #: True when at least one key is usable right now.
    any_available: bool = False


class GeminiKeyTestRequest(ApiModel):
    api_key: Annotated[str, StringConstraints(min_length=8, max_length=512)]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None


class GeminiKeyTestResponse(ApiModel):
    valid: bool
    #: A sanitized category such as ``ok``, ``invalid_key``, ``quota``.
    result: str
    message: str


class _RequestCapMixin(ApiModel):
    """Shared ``requestCap`` validation. The field itself is declared by each
    subclass, because adding a key defaults it while editing preferences leaves
    it unchanged when omitted."""

    @field_validator("request_cap", mode="before", check_fields=False)
    @classmethod
    def _reject_fractional(cls, value: Any) -> Any:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("requestCap must be a whole number.")
        if isinstance(value, bool):
            raise ValueError("requestCap must be an integer.")
        return value


class GeminiSettingsUpdate(_RequestCapMixin):
    """Add a key to the pool, optionally updating model and cap alongside it."""

    api_key: Annotated[str, StringConstraints(min_length=8, max_length=512)]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    label: Annotated[str, StringConstraints(max_length=64)] | None = None
    request_cap: Annotated[int, Field(ge=0, le=50)] = 8


class GeminiPreferencesUpdate(_RequestCapMixin):
    """Change model or cap without touching the stored keys."""

    model: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    request_cap: Annotated[int, Field(ge=0, le=50)] | None = None


class GeminiKeyPatch(ApiModel):
    """Rename a key, or take it in or out of the failover rotation."""

    label: Annotated[str, StringConstraints(max_length=64)] | None = None
    enabled: bool | None = None


# --- Uploads ---------------------------------------------------------------


class JobErrorModel(ApiModel):
    phase: str
    code: str
    message: str
    retryable: bool
    occurred_at: str


class CreateUploadRequest(ApiModel):
    file_name: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    size_bytes: Annotated[int, Field(gt=0)]
    mime_type: Annotated[str, StringConstraints(max_length=255)] | None = None
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")] | None = None


class UploadResponse(ApiModel):
    id: str
    file_name: str
    declared_size_bytes: int
    verified_offset_bytes: int
    chunk_size_bytes: int
    state: UploadState
    sha256: str | None
    progress_percent: float
    error: JobErrorModel | None
    created_at: str
    updated_at: str


# --- Jobs ------------------------------------------------------------------


class CreateJobRequest(ApiModel):
    upload_id: str
    target_clip_count: Annotated[int, Field(ge=1, le=100)] = 20
    content_prompt: Annotated[str, StringConstraints(max_length=MAX_PROMPT_CHARS)] | None = None
    source_name: Annotated[
        str, StringConstraints(max_length=SOURCE_NAME_MAX_CHARS)
    ] | None = None
    use_gemini: bool | None = None

    @field_validator("content_prompt")
    @classmethod
    def _normalize_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("source_name")
    @classmethod
    def _normalize_source_name(cls, value: str | None) -> str | None:
        return normalize_source_name(value)


class ProgressModel(ApiModel):
    phase: JobState
    percent: float
    message: str


class VideoModel(ApiModel):
    sha256: str
    duration_us: int
    width: int
    height: int
    average_frame_rate: str
    has_audio: bool


class AnalysisUsageModel(ApiModel):
    model: str | None
    request_cap: int
    requests_used: int
    coarse_requests: int
    fine_requests: int
    proxy_video_requests: int
    proxy_video_candidates: int
    provider_file_operations: int
    image_bytes_sent: int
    proxy_video_bytes_sent: int
    proxy_video_seconds_sent: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_status: CacheStatus
    local_fallback_used: bool
    fallback_reason: str | None


class ExportSummaryModel(ApiModel):
    id: str
    state: ExportState
    created_at: str
    completed_at: str | None


class JobSummaryModel(ApiModel):
    id: str
    source_file_name: str
    state: JobState
    progress_percent: float
    target_clip_count: int
    selected_count: int
    latest_export: ExportSummaryModel | None
    created_at: str
    updated_at: str


class JobListResponse(ApiModel):
    items: list[JobSummaryModel]
    next_cursor: str | None


class JobResponse(ApiModel):
    id: str
    upload_id: str
    state: JobState
    target_clip_count: int
    content_prompt: str | None
    source_label: SourceLabelModel | None
    use_gemini: bool
    video: VideoModel | None
    progress: ProgressModel
    eligible_count: int | None
    selected_count: int
    review_revision: int
    latest_export: ExportSummaryModel | None
    partial_result_reason: str | None
    warnings: list[str]
    usage: AnalysisUsageModel
    error: JobErrorModel | None
    created_at: str
    updated_at: str


# --- Candidates and review -------------------------------------------------


class TransitionBoundaryModel(ApiModel):
    start_us: int
    end_us: int
    kinds: list[Literal["hard-cut", "fade", "dissolve"]]


class CandidateShotModel(ApiModel):
    id: str
    job_id: str
    shot_number: int
    source_start_us: int
    source_end_us: int
    safe_start_us: int
    safe_end_us: int
    usable_duration_us: int
    recommended_start_us: int
    recommended_end_us: int
    incoming_boundary: TransitionBoundaryModel | None
    outgoing_boundary: TransitionBoundaryModel | None
    rank: int
    score: float
    confidence: float
    reason: str
    scoring_source: ScoringSource
    cache_status: CacheStatus
    prompt_relevance_evaluated: bool
    thumbnail_url: str
    preview_url: str
    # --- product focus ---------------------------------------------------
    #: ``null`` means nobody assessed it, which is not the same as "no person".
    human_present: bool | None = None
    #: ``model`` (Gemini judged the frames) or ``local`` (OpenCV screen).
    human_source: Literal["model", "local"] | None = None
    product_visible: bool | None = None
    product_prominence: int | None = None
    #: Set when the candidate was passed over for automatic selection.
    excluded_reason: Literal["human_present", "product_absent"] | None = None


class SelectedClipModel(ApiModel):
    id: str
    candidate_id: str
    order: int
    start_us: int
    end_us: int
    duration_us: int


class CandidatesResponse(ApiModel):
    candidates: list[CandidateShotModel]
    selected_clips: list[SelectedClipModel]
    review_revision: int


class ReviewClipInput(ApiModel):
    candidate_id: str
    order: Annotated[int, Field(ge=1)]
    start_us: Annotated[int, Field(ge=0)]
    end_us: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def _check_interval(self) -> "ReviewClipInput":
        # Structural only. The duration bounds are deliberately *not* re-checked
        # here: this validator runs first and would collapse every out-of-range
        # trim into a generic `invalid_request`, hiding the specific
        # `trim_too_short` / `trim_too_long` codes that
        # `services.review.validate_trim` returns with the offending candidate
        # id and duration. That check is also frame-tolerant, which this one
        # cannot be -- it has no access to the source frame rate.
        if self.end_us <= self.start_us:
            raise ValueError("endUs must be greater than startUs.")
        return self


class ReviewRequest(ApiModel):
    revision: Annotated[int, Field(ge=0)]
    clips: list[ReviewClipInput]

    @field_validator("clips")
    @classmethod
    def _check_clips(cls, clips: list[ReviewClipInput]) -> list[ReviewClipInput]:
        if len(clips) > 100:
            raise ValueError("A review may contain at most 100 clips.")
        seen_candidates = {clip.candidate_id for clip in clips}
        if len(seen_candidates) != len(clips):
            raise ValueError("A candidate may appear at most once.")
        orders = sorted(clip.order for clip in clips)
        if orders != list(range(1, len(clips) + 1)):
            raise ValueError("Clip order values must be gapless and start at 1.")
        return clips


class ReviewResponse(ApiModel):
    review_revision: int
    clips: list[SelectedClipModel]


# --- Exports ---------------------------------------------------------------


class CreateExportRequest(ApiModel):
    review_revision: Annotated[int, Field(ge=0)]
    resolutions: list[Resolution]
    include_audio: bool = True

    @field_validator("resolutions")
    @classmethod
    def _check_resolutions(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("At least one resolution is required.")
        if len(set(values)) != len(values):
            raise ValueError("Resolutions must be unique.")
        return values


class ExportFileModel(ApiModel):
    """One downloadable clip inside an export."""

    id: str
    serial: int
    resolution: Resolution
    candidate_id: str
    file_name: str
    width: int
    height: int
    size_bytes: int
    duration_us: int
    sha256: str
    download_url: str
    #: False for exports produced before individual files were published.
    available: bool


class ExportFilesResponse(ApiModel):
    files: list[ExportFileModel]
    zip_download_url: str | None


class ExportProgressModel(ApiModel):
    percent: float
    message: str


class ExportResponse(ApiModel):
    id: str
    job_id: str
    state: ExportState
    review_revision: int
    resolutions: list[Resolution]
    include_audio: bool
    progress: ExportProgressModel
    error: JobErrorModel | None
    manifest_available: bool
    download_available: bool
    created_at: str
    updated_at: str
    completed_at: str | None
