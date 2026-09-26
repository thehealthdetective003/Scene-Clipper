"""Ranking formulas, budget allocation, and cache keys (spec 6.6-6.10, 12.1/12.4)."""

from __future__ import annotations

import pytest

from app.analysis import budget, cache, scoring
from app.analysis.budget import Allocation


class TestGeminiScore:
    def test_with_focus_prompt_weights(self):
        # 0.50 x 80 + 0.30 x 60 + 0.20 x 40 = 66
        assert scoring.gemini_score(
            relevance=80, interest=60, clarity=40, has_focus_prompt=True
        ) == pytest.approx(66.0)

    def test_without_focus_prompt_weights(self):
        # 0.60 x 60 + 0.40 x 40 = 52
        assert scoring.gemini_score(
            relevance=None, interest=60, clarity=40, has_focus_prompt=False
        ) == pytest.approx(52.0)

    def test_missing_relevance_under_a_prompt_falls_back_to_no_prompt_weights(self):
        assert scoring.gemini_score(
            relevance=None, interest=60, clarity=40, has_focus_prompt=True
        ) == pytest.approx(52.0)


class TestFinalScore:
    def test_api_candidate_blends_gemini_and_local(self):
        # 0.85 x 66 + 0.15 x 40 = 62.1
        assert scoring.final_score(gemini=66.0, local=40.0) == pytest.approx(62.1)

    def test_local_only_candidate_uses_local_score_alone(self):
        assert scoring.final_score(gemini=None, local=73.5) == pytest.approx(73.5)

    def test_local_candidate_confidence_and_prompt_flag(self):
        item = scoring.score_local_candidate(
            candidate_id="a", source_start_us=0, local=50.0
        )
        assert item.confidence == pytest.approx(scoring.LOCAL_FALLBACK_CONFIDENCE)
        assert item.confidence == pytest.approx(0.35)
        assert item.prompt_relevance_evaluated is False
        assert item.scoring_source == scoring.SCORING_LOCAL_FALLBACK


class TestRanking:
    def test_sorts_by_score_then_confidence_then_source_time(self):
        items = [
            scoring.ScoredCandidate("c", 3_000_000, 0, 50.0, 0.9, "", None, "local-fallback", False),
            scoring.ScoredCandidate("a", 1_000_000, 0, 90.0, 0.5, "", None, "contact-sheet", True),
            scoring.ScoredCandidate("b", 2_000_000, 0, 50.0, 0.9, "", None, "contact-sheet", True),
        ]
        assert [item.candidate_id for item in scoring.rank(items)] == ["a", "b", "c"]

    def test_a_strong_local_candidate_can_outrank_an_api_candidate(self):
        # Spec 6.7: do not automatically place API results ahead of stronger
        # local candidates.
        local = scoring.score_local_candidate(
            candidate_id="local", source_start_us=5_000_000, local=95.0
        )
        api = scoring.score_api_candidate(
            candidate_id="api",
            source_start_us=0,
            local=10.0,
            relevance=None,
            interest=20,
            clarity=20,
            confidence=0.9,
            reason="",
            reason_code="low_clarity",
            has_focus_prompt=False,
        )
        assert [item.candidate_id for item in scoring.rank([api, local])] == ["local", "api"]


class TestCoarseSelection:
    def test_all_candidates_sent_when_capacity_allows(self):
        candidates = [(f"c{i}", float(i), i * 1_000_000) for i in range(10)]
        selected, deferred = scoring.select_for_coarse(candidates, 50)
        assert len(selected) == 10
        assert deferred == []

    def test_zero_capacity_defers_everything(self):
        candidates = [(f"c{i}", float(i), i * 1_000_000) for i in range(10)]
        selected, deferred = scoring.select_for_coarse(candidates, 0)
        assert selected == []
        assert len(deferred) == 10

    def test_splits_seventy_five_percent_by_score_and_the_rest_by_coverage(self):
        # 100 candidates; the best scores cluster at the start of the timeline.
        candidates = [
            (f"c{i}", 100.0 - i, i * 1_000_000) for i in range(100)
        ]
        selected, deferred = scoring.select_for_coarse(candidates, 20)
        assert len(selected) == 20
        assert len(deferred) == 80
        assert len(set(selected)) == 20

        # 15 slots (75%) go to the top local scores.
        top_fifteen = {f"c{i}" for i in range(15)}
        assert top_fifteen <= set(selected)
        # The remaining slots reach later in the timeline, not just the head.
        late = [cid for cid in selected if int(cid[1:]) >= 30]
        assert late, "stratified coverage must reach beyond the highest scores"

    def test_selection_is_deterministic(self):
        candidates = [(f"c{i}", 100.0 - i, i * 1_000_000) for i in range(60)]
        first, _ = scoring.select_for_coarse(candidates, 17)
        second, _ = scoring.select_for_coarse(candidates, 17)
        assert first == second


class TestShortlistSize:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [(1, 40), (20, 40), (25, 50), (100, 200), (150, 200)],
    )
    def test_min_max_clamping(self, target, expected):
        assert scoring.fine_shortlist_size(target) == expected


class TestBudgetAllocation:
    def test_cap_zero_is_local_only(self):
        allocation = budget.allocate(0, has_long_shots=True)
        assert allocation.local_only
        assert allocation.total == 0

    def test_cap_below_three_reserves_no_proxy_unit(self):
        allocation = budget.allocate(2, has_long_shots=False)
        assert allocation.proxy == 0
        assert allocation.coarse == 2

    def test_cap_three_reserves_one_proxy_unit(self):
        allocation = budget.allocate(3, has_long_shots=False)
        assert allocation.proxy == 1
        assert allocation.coarse == 2

    def test_long_shots_reserve_a_fine_unit_when_two_non_proxy_units_remain(self):
        allocation = budget.allocate(8, has_long_shots=True)
        assert allocation.proxy == 1
        assert allocation.fine == 1
        assert allocation.coarse == 6
        assert allocation.total == 8

    def test_no_fine_unit_without_long_shots(self):
        allocation = budget.allocate(8, has_long_shots=False)
        assert allocation.fine == 0
        assert allocation.coarse == 7

    def test_a_positive_cap_always_assigns_at_least_one_coarse_unit(self):
        for cap in range(1, 51):
            allocation = budget.allocate(cap, has_long_shots=True)
            assert allocation.coarse >= 1
            assert allocation.total <= cap

    def test_max_cap_allocation(self):
        allocation = budget.allocate(50, has_long_shots=True)
        assert (allocation.coarse, allocation.fine, allocation.proxy) == (48, 1, 1)

    def test_unused_coarse_rolls_into_fine_then_proxy(self):
        allocation = Allocation(cap=10, coarse=8, fine=1, proxy=1)
        rolled = budget.roll_forward(allocation, coarse_used=3, fine_used=0)
        assert rolled.fine == 1 + 5
        rolled_again = budget.roll_forward(allocation, coarse_used=3, fine_used=6)
        assert rolled_again.proxy == 1


class TestCacheKeys:
    def _identity(self, **overrides):
        base = {
            "source_sha256": "a" * 64,
            "normalized_prompt": "sunset over water",
            "model": "gemini-test-model",
            "detector_config_version": "1",
            "pipeline_version": "1",
            "feature_version": "1",
            "ranking_settings": {"useGemini": True, "requestCap": 8},
        }
        base.update(overrides)
        return cache.CacheIdentity(**base)

    def test_same_inputs_give_the_same_key(self):
        assert self._identity().key_for("coarse") == self._identity().key_for("coarse")

    def test_stage_changes_the_key(self):
        identity = self._identity()
        assert identity.key_for("coarse") != identity.key_for("fine")

    @pytest.mark.parametrize(
        "override",
        [
            {"normalized_prompt": "different prompt"},
            {"model": "other-model"},
            {"detector_config_version": "2"},
            {"pipeline_version": "2"},
            {"feature_version": "2"},
            {"source_sha256": "b" * 64},
            {"ranking_settings": {"useGemini": True, "requestCap": 9}},
        ],
    )
    def test_every_component_invalidates_the_key(self, override):
        assert self._identity().key_for("coarse") != self._identity(**override).key_for("coarse")

    def test_no_secret_material_reaches_the_key(self):
        identity = self._identity()
        # The key is a digest, so assert the derivation inputs instead: the
        # identity holds no key field at all.
        assert not hasattr(identity, "api_key")
        assert "apiKey" not in identity.ranking_settings

    def test_combined_status(self):
        assert cache.combined_status(False, False, True) == cache.CACHE_NONE
        assert cache.combined_status(True, True, True) == cache.CACHE_COMPLETE
        assert cache.combined_status(True, False, False) == cache.CACHE_COMPLETE
        assert cache.combined_status(True, False, True) == cache.CACHE_PARTIAL
