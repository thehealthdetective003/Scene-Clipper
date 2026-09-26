"""Versioned algorithm identifiers.

Every value here participates in the analysis cache key (spec 6.10). Bump the
relevant constant whenever a change would produce different candidates, safe
intervals, local scores, or recommended intervals for the same input, so that
previously cached results are invalidated instead of silently reused.
"""

from __future__ import annotations

# Overall analysis pipeline: stage ordering, eligibility gate, ranking assembly.
ANALYSIS_PIPELINE_VERSION = "1"

# Stage E deterministic local measurements and localScore weighting.
FEATURE_ALGORITHM_VERSION = "1"

# Envelope version for AES-256-GCM encrypted secrets at rest (spec 5.2).
ENCRYPTION_FORMAT_VERSION = 1

# Export manifest schema version (spec 9).
MANIFEST_SCHEMA_VERSION = "1.1"
