"""Provider-agnostic ranking adapter contract (spec 6.6, 7.1).

The pipeline depends only on these types, so the Gemini SDK can be pinned,
swapped, or mocked without touching job or candidate schemas. Adapters are
backend-only: no adapter ever runs in, or exposes a key to, the browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

#: Allowed values for the coarse ranking reason code (spec 6.6).
REASON_CODES = frozenset(
    {
        "strong_visual",
        "prompt_match",
        "clear_composition",
        "weak_relevance",
        "low_clarity",
        # Product-focus outcomes (spec extension: product-only selection).
        "product_hero",
        "product_partial",
        "product_absent",
        "human_present",
    }
)

MAX_REASON_CHARS = 160


# --- Errors ----------------------------------------------------------------


class ProviderError(Exception):
    """Base class. ``retryable`` drives the single bounded retry (spec 6.7)."""

    retryable = False
    code = "provider_error"

    def __init__(self, message: str = "The ranking provider request failed.") -> None:
        super().__init__(message)
        self.message = message


class ProviderAuthError(ProviderError):
    """401/403: the key is invalid or lacks access. Never retried."""

    retryable = False
    code = "provider_auth_failed"


class ProviderQuotaError(ProviderError):
    """429: rate limited or out of quota. Retried at most once."""

    retryable = True
    code = "provider_quota_exceeded"


class ProviderTransientError(ProviderError):
    """Timeout or 5xx. Retried at most once with bounded backoff + jitter."""

    retryable = True
    code = "provider_unavailable"


class ProviderPermanentError(ProviderError):
    """Any other 4xx. Not retried."""

    retryable = False
    code = "provider_rejected_request"


class ProviderInvalidResponse(ProviderError):
    """The response failed schema or bounds validation (spec 10.3)."""

    retryable = True
    code = "provider_invalid_response"


# --- Result types ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CoarseResult:
    candidate_id: str
    relevance: int | None
    interest: int
    clarity: int
    confidence: float
    motion_ambiguous: bool
    reason_code: str
    reason: str
    #: True when any part of a person is visible -- face, body, or hands.
    #: ``None`` when no focus prompt was supplied and it was not assessed.
    human_present: bool | None = None
    #: True when the prompted product is clearly identifiable in frame.
    product_visible: bool | None = None
    #: 0-100: how clearly and centrally the product is framed.
    product_prominence: int | None = None


@dataclass(frozen=True, slots=True)
class FineChoice:
    candidate_id: str
    window_id: str
    confidence: float
    motion_ambiguous: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProxyChoice:
    candidate_id: str
    start_option_id: str
    confidence: float
    motion_ambiguous: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    coarse: list[CoarseResult] = field(default_factory=list)
    fine: list[FineChoice] = field(default_factory=list)
    proxy: list[ProxyChoice] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True, slots=True)
class KeyTestResult:
    valid: bool
    #: Sanitized category: ``ok``, ``invalid_key``, ``quota``, ``unavailable``,
    #: ``unsupported_model``. Never echoes the provider's raw message.
    result: str
    message: str


@dataclass(frozen=True, slots=True)
class ContactSheet:
    """One rendered JPEG sheet plus the candidate IDs it depicts."""

    path: Path
    candidate_ids: list[str]
    bytes_size: int


@dataclass(frozen=True, slots=True)
class WindowStoryboard:
    """A long-shot storyboard: server-issued window IDs the model must pick from."""

    path: Path
    candidate_id: str
    window_ids: list[str]
    bytes_size: int


@dataclass(frozen=True, slots=True)
class ProxyClip:
    """A short low-resolution excerpt and its server-issued start options."""

    path: Path
    candidate_id: str
    start_option_ids: list[str]
    bytes_size: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """A provider-hosted file that must be deleted after use (spec 7.3)."""

    name: str
    uri: str
    mime_type: str


class RankingProvider(Protocol):
    """What the analysis pipeline needs from a ranking provider."""

    def test_key(self, api_key: str, model: str) -> KeyTestResult:
        """Validate a key without persisting it."""

    def rank_contact_sheets(
        self,
        *,
        api_key: str,
        model: str,
        sheets: list[ContactSheet],
        focus_prompt: str | None,
    ) -> ProviderResponse:
        """Stage F: score every depicted candidate."""

    def choose_windows(
        self,
        *,
        api_key: str,
        model: str,
        storyboards: list[WindowStoryboard],
        focus_prompt: str | None,
    ) -> ProviderResponse:
        """Stage H: pick one supplied window ID per long shot."""

    def review_proxies(
        self,
        *,
        api_key: str,
        model: str,
        clips: list[ProxyClip],
        remote_files: dict[str, RemoteFile],
        focus_prompt: str | None,
    ) -> ProviderResponse:
        """Stage I: pick one supplied start-option ID per proxy candidate."""

    def upload_file(self, *, api_key: str, path: Path, mime_type: str) -> RemoteFile:
        """Upload a proxy clip; recorded before transmission (spec 7.3)."""

    def delete_file(self, *, api_key: str, name: str) -> None:
        """Delete a remote file; retried until confirmed or expired."""
