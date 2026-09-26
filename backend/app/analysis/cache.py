"""Stage J: analysis cache (spec 6.10).

The cache key is derived from the source digest, the normalized focus prompt,
the exact model identifier, the detector configuration version, the pipeline
and feature versions, and every ranking setting that alters results. No raw
secret is ever part of a key or a record.

A complete cache hit performs zero provider requests. Cache status and scoring
source stay orthogonal: a cached entry remembers whether it originally came
from a contact sheet, a proxy video, or local fallback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models import AnalysisCache, Job

STAGE_COARSE = "coarse"
STAGE_FINE = "fine"

CACHE_NONE = "none"
CACHE_PARTIAL = "partial"
CACHE_COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class CacheIdentity:
    source_sha256: str
    normalized_prompt: str | None
    model: str | None
    detector_config_version: str
    pipeline_version: str
    feature_version: str
    ranking_settings: dict[str, Any]

    def key_for(self, stage: str) -> str:
        material = {
            "stage": stage,
            "source": self.source_sha256,
            "prompt": self.normalized_prompt or "",
            "model": self.model or "local-only",
            "detector": self.detector_config_version,
            "pipeline": self.pipeline_version,
            "features": self.feature_version,
            "ranking": self.ranking_settings,
        }
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:48]


def identity_for_job(job: Job, ranking_settings: dict[str, Any] | None = None) -> CacheIdentity:
    return CacheIdentity(
        source_sha256=job.source_sha256,
        normalized_prompt=job.content_prompt_normalized,
        model=job.gemini_model if job.use_gemini else None,
        detector_config_version=job.detector_config_version,
        pipeline_version=job.pipeline_version,
        feature_version=job.feature_version,
        ranking_settings=ranking_settings
        or {
            # Settings that change which candidates are produced or how they
            # are ordered. targetClipCount is deliberately excluded: it changes
            # only how many ranked candidates get auto-selected.
            "useGemini": job.use_gemini,
            "requestCap": job.gemini_request_cap,
        },
    )


def load(db: DbSession, identity: CacheIdentity, stage: str) -> dict[str, Any] | None:
    entry = db.get(AnalysisCache, identity.key_for(stage))
    if entry is None:
        return None
    # Defensive: a key collision across versions must never be reused.
    if (
        entry.pipeline_version != identity.pipeline_version
        or entry.detector_config_version != identity.detector_config_version
        or entry.feature_version != identity.feature_version
    ):
        return None
    return dict(entry.payload or {})


def store(
    db: DbSession, identity: CacheIdentity, stage: str, payload: dict[str, Any]
) -> AnalysisCache:
    """Commit an entry only after schema and boundary validation (spec 6.10)."""
    key = identity.key_for(stage)
    entry = db.get(AnalysisCache, key)
    if entry is None:
        entry = AnalysisCache(
            cache_key=key,
            source_sha256=identity.source_sha256,
            gemini_model=identity.model,
            detector_config_version=identity.detector_config_version,
            pipeline_version=identity.pipeline_version,
            feature_version=identity.feature_version,
            stage=stage,
            payload=payload,
            reference_count=1,
        )
        db.add(entry)
    else:
        entry.payload = payload
        entry.reference_count += 1
    db.flush()
    return entry


def release(db: DbSession, cache_key: str) -> None:
    """Drop one reference; the entry is removed when the last one goes."""
    entry = db.get(AnalysisCache, cache_key)
    if entry is None:
        return
    entry.reference_count -= 1
    if entry.reference_count <= 0:
        db.delete(entry)
    db.flush()


def invalidate_for_source(db: DbSession, source_sha256: str) -> int:
    entries = list(
        db.execute(
            select(AnalysisCache).where(AnalysisCache.source_sha256 == source_sha256)
        ).scalars()
    )
    for entry in entries:
        db.delete(entry)
    db.flush()
    return len(entries)


def combined_status(coarse_hit: bool, fine_hit: bool, fine_required: bool) -> str:
    """Overall cache status for the job (spec 6.10)."""
    if not coarse_hit and not fine_hit:
        return CACHE_NONE
    if coarse_hit and (fine_hit or not fine_required):
        return CACHE_COMPLETE
    return CACHE_PARTIAL
