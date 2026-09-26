"""A controllable stand-in for the Gemini adapter (spec 12: mock by default).

Failure modes mirror the ones the spec requires tests for: auth rejection,
quota, transient errors, malformed output, missing/duplicate/unknown candidate
ids, and out-of-range values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.providers.base import (
    CoarseResult,
    FineChoice,
    KeyTestResult,
    ProviderAuthError,
    ProviderInvalidResponse,
    ProviderQuotaError,
    ProviderResponse,
    ProviderTransientError,
    ProxyChoice,
    RemoteFile,
    TokenUsage,
)


@dataclass
class MockProvider:
    """Records every call and returns schema-valid responses by default."""

    # --- failure switches -------------------------------------------------
    fail_coarse_with: Exception | None = None
    fail_fine_with: Exception | None = None
    fail_proxy_with: Exception | None = None
    #: Fail only the first N coarse calls, then succeed (retry behaviour).
    transient_coarse_failures: int = 0
    key_valid: bool = True

    # --- per-key failure switches (failover) ------------------------------
    #: Keys that answer 429, and keys that answer 401/403. Any key not listed
    #: works, so a test can retire exactly one credential and watch the pool
    #: move to the next.
    quota_keys: set[str] = field(default_factory=set)
    auth_keys: set[str] = field(default_factory=set)

    # --- response shaping -------------------------------------------------
    omit_candidates: set[str] = field(default_factory=set)
    duplicate_candidate: str | None = None
    inject_unknown_id: str | None = None
    out_of_range: bool = False
    low_confidence: bool = False
    motion_ambiguous: bool = False
    relevance_override: int | None = None
    invent_window_id: bool = False

    # --- product focus ----------------------------------------------------
    #: Candidate ids the model reports a person in, and ids it reports the
    #: product missing from. Everything else comes back as a clean product
    #: shot, so a test states only the exceptions it cares about.
    humans_in: set[str] = field(default_factory=set)
    product_missing_in: set[str] = field(default_factory=set)
    #: Answer the product questions with null, as a model would when no focus
    #: prompt was supplied -- or when it declines to judge.
    omit_product_signals: bool = False

    # --- call log ---------------------------------------------------------
    coarse_calls: list[list[str]] = field(default_factory=list)
    fine_calls: list[list[str]] = field(default_factory=list)
    proxy_calls: list[list[str]] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    keys_seen: list[str] = field(default_factory=list)

    @property
    def total_inference_calls(self) -> int:
        return len(self.coarse_calls) + len(self.fine_calls) + len(self.proxy_calls)

    # --- adapter surface --------------------------------------------------

    def _reject_key(self, api_key: str) -> None:
        """Raise if this particular credential is configured to fail."""
        if api_key in self.auth_keys:
            raise ProviderAuthError("The key was rejected.")
        if api_key in self.quota_keys:
            raise ProviderQuotaError("Quota exhausted for this key.")

    def test_key(self, api_key: str, model: str) -> KeyTestResult:
        self.keys_seen.append(api_key)
        if api_key in self.auth_keys:
            return KeyTestResult(False, "invalid_key", "The key was rejected.")
        if api_key in self.quota_keys:
            return KeyTestResult(False, "quota", "This key is out of quota.")
        if self.key_valid:
            return KeyTestResult(True, "ok", "The key is valid for this model.")
        return KeyTestResult(False, "invalid_key", "The key was rejected.")

    def rank_contact_sheets(self, *, api_key, model, sheets, focus_prompt) -> ProviderResponse:  # noqa: ANN001
        self.keys_seen.append(api_key)
        candidate_ids = [cid for sheet in sheets for cid in sheet.candidate_ids]
        self.coarse_calls.append(candidate_ids)

        self._reject_key(api_key)
        if self.transient_coarse_failures > 0:
            self.transient_coarse_failures -= 1
            raise ProviderTransientError("Simulated transient failure.")
        if self.fail_coarse_with is not None:
            raise self.fail_coarse_with

        results: list[CoarseResult] = []
        for index, candidate_id in enumerate(candidate_ids):
            if candidate_id in self.omit_candidates:
                continue
            results.append(self._coarse_result(candidate_id, index, bool(focus_prompt)))

        if self.duplicate_candidate and results:
            results.append(self._coarse_result(self.duplicate_candidate, 0, bool(focus_prompt)))
        if self.inject_unknown_id:
            results.append(self._coarse_result(self.inject_unknown_id, 0, bool(focus_prompt)))

        return ProviderResponse(
            coarse=results, usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        )

    def _coarse_result(self, candidate_id: str, index: int, has_prompt: bool) -> CoarseResult:
        if self.out_of_range:
            # Values the adapter's own parser would reject; constructed directly
            # here so pipeline-level validation is what gets exercised.
            return CoarseResult(candidate_id, 500, 500, 500, 5.0, False, "strong_visual", "x")
        # Deterministic descending interest so ranking order is predictable.
        interest = max(5, 95 - index * 7)

        assess = has_prompt and not self.omit_product_signals
        human = candidate_id in self.humans_in if assess else None
        visible = candidate_id not in self.product_missing_in if assess else None
        prominence = None
        if assess:
            prominence = 0 if not visible else max(10, 90 - index * 4)

        if not assess:
            reason_code = "prompt_match" if has_prompt else "strong_visual"
        elif human:
            reason_code = "human_present"
        elif not visible:
            reason_code = "product_absent"
        else:
            reason_code = "product_hero" if prominence >= 70 else "product_partial"

        return CoarseResult(
            candidate_id=candidate_id,
            relevance=(self.relevance_override if self.relevance_override is not None
                       else (max(5, 90 - index * 5) if has_prompt else None)),
            interest=interest,
            clarity=max(5, 85 - index * 3),
            confidence=0.30 if self.low_confidence else 0.88,
            motion_ambiguous=self.motion_ambiguous,
            reason_code=reason_code,
            reason=f"Mock ranking for candidate {index}.",
            human_present=human,
            product_visible=visible,
            product_prominence=prominence,
        )

    def choose_windows(self, *, api_key, model, storyboards, focus_prompt) -> ProviderResponse:  # noqa: ANN001
        self.keys_seen.append(api_key)
        self.fine_calls.append([board.candidate_id for board in storyboards])
        self._reject_key(api_key)
        if self.fail_fine_with is not None:
            raise self.fail_fine_with

        choices = []
        for board in storyboards:
            window_id = "not-a-real-window" if self.invent_window_id else board.window_ids[0]
            choices.append(
                FineChoice(
                    candidate_id=board.candidate_id,
                    window_id=window_id,
                    confidence=0.9,
                    motion_ambiguous=False,
                    reason="Mock window choice.",
                )
            )
        return ProviderResponse(fine=choices, usage=TokenUsage(10, 5, 15))

    def review_proxies(self, *, api_key, model, clips, remote_files, focus_prompt) -> ProviderResponse:  # noqa: ANN001
        self.keys_seen.append(api_key)
        self.proxy_calls.append([clip.candidate_id for clip in clips])
        self._reject_key(api_key)
        if self.fail_proxy_with is not None:
            raise self.fail_proxy_with

        return ProviderResponse(
            proxy=[
                ProxyChoice(
                    candidate_id=clip.candidate_id,
                    start_option_id=clip.start_option_ids[0],
                    confidence=0.93,
                    motion_ambiguous=False,
                    reason="Mock proxy review.",
                )
                for clip in clips
            ],
            usage=TokenUsage(20, 10, 30),
        )

    def upload_file(self, *, api_key: str, path: Path, mime_type: str = "video/mp4") -> RemoteFile:
        self.keys_seen.append(api_key)
        self._reject_key(api_key)
        name = f"files/mock-{len(self.uploaded)}"
        self.uploaded.append(name)
        return RemoteFile(name=name, uri=f"https://mock.invalid/{name}", mime_type=mime_type)

    def delete_file(self, *, api_key: str, name: str) -> None:
        self.deleted.append(name)


def auth_failure() -> Exception:
    return ProviderAuthError("The Gemini key was rejected.")


def quota_failure() -> Exception:
    return ProviderQuotaError("Rate limited.")


def transient_failure() -> Exception:
    return ProviderTransientError("Service unavailable.")


def invalid_response() -> Exception:
    return ProviderInvalidResponse("Malformed output.")
