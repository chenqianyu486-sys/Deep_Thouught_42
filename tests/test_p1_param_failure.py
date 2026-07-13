"""P1 per-combo failure + cross-phase failure-memory tests.

Covers:
- ②A: compute_param_signature + combo_is_cooled + per-combo dedup + the
  SELECT_STRATEGY blocking narrowing (param_signature!="" combos are NOT
  strategy-blocked; only param_signature="" structural failures are).
- ②A: catalog three-way rendering ([RETRY] / [BLOCKED] / [COMBO COOLED]).
- ③C: PhaseHandoff.recent_failures survives phase transitions.

(①B num_paths chain param was deferred - low value, MCP-boundary cost.)
"""

from __future__ import annotations

import pytest

from optimizer.state import (
    OptimizerState,
    FailedStrategyRecord,
    RETRY_BUDGET,
    record_strategy_failure,
)
from optimizer.pure.execute_contracts import compute_param_signature, combo_is_cooled
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.pure.tool_filter import LoopPhase
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff
from strategy_library import get_strategy_catalog


# ── ②A: compute_param_signature ────────────────────────────────────────────


class TestParamSignature:
    def test_directive_distinguishes_combos(self):
        a = compute_param_signature("OptDesign", {"directive": "Explore"})
        b = compute_param_signature("OptDesign", {"directive": "AddRemap"})
        assert a == "directive=Explore"
        assert b == "directive=AddRemap"
        assert a != b

    def test_pblock_signature_includes_directives_and_multiplier(self):
        sig = compute_param_signature("PBLOCK", {
            "place_directive": "Explore", "route_directive": "AggressiveExplore",
            "resource_multiplier": 2.0,
        })
        assert "place_directive=Explore" in sig
        assert "route_directive=AggressiveExplore" in sig
        assert "resource_multiplier=2.0" in sig

    def test_data_only_strategy_returns_empty(self):
        # Fanout/NetSwap have no directive params -> strategy-level (blocks whole strategy)
        assert compute_param_signature("Fanout", {"nets": [{"net_name": "x", "fanout": 100}]}) == ""
        assert compute_param_signature("NetSwap", {"cells": ["a/b"]}) == ""

    def test_none_args(self):
        assert compute_param_signature("OptDesign", None) == ""

    def test_multiplier_float_quantized(self):
        # 2.0 and 2.01 should quantize to the same bucket (avoid float noise)
        assert compute_param_signature("PBLOCK", {"resource_multiplier": 2.0}) == \
               compute_param_signature("PBLOCK", {"resource_multiplier": 2.04})

    def test_omitted_directives_excluded(self):
        sig = compute_param_signature("OptDesign", {"directive": "Explore", "place_directive": None})
        assert sig == "directive=Explore"  # None place_directive excluded


# ── ②A: combo_is_cooled (EXECUTE guard logic, pure) ────────────────────────


class TestComboIsCooled:
    def _state_with_escalated_combo(self, strategy, sig, blocked_until):
        s = OptimizerState()
        s.iteration.current = 2
        s.context.failed_strategies = [
            FailedStrategyRecord(
                strategy=strategy, reason="strategy_ineffective",
                param_signature=sig, blocked_until_iter=blocked_until,
            )
        ]
        return s

    def test_cooled_combo_detected(self):
        s = self._state_with_escalated_combo("OptDesign", "directive=Explore", 3)
        cooled, remaining = combo_is_cooled(
            s.context.failed_strategies, "OptDesign", "directive=Explore", 2)
        assert cooled is True and remaining == 1

    def test_different_combo_not_cooled(self):
        s = self._state_with_escalated_combo("OptDesign", "directive=Explore", 3)
        cooled, _ = combo_is_cooled(
            s.context.failed_strategies, "OptDesign", "directive=AddRemap", 2)
        assert cooled is False  # the OTHER combo is free to run

    def test_expired_combo_not_cooled(self):
        s = self._state_with_escalated_combo("OptDesign", "directive=Explore", 3)
        cooled, _ = combo_is_cooled(
            s.context.failed_strategies, "OptDesign", "directive=Explore", 3)
        assert cooled is False  # TTL expired

    def test_empty_signature_never_cooled(self):
        # Strategy-level failures (param_signature="") are handled by SELECT_STRATEGY, not the guard
        s = self._state_with_escalated_combo("OptDesign", "", 3)
        cooled, _ = combo_is_cooled(s.context.failed_strategies, "OptDesign", "", 2)
        assert cooled is False


# ── ②A: per-combo dedup + SELECT narrowing ─────────────────────────────────


class TestPerComboDedupAndBlocking:
    def test_two_combos_are_distinct_records(self):
        s = OptimizerState(); s.iteration.current = 1
        record_strategy_failure(s, "OptDesign", "tool_error", param_signature="directive=Explore")
        record_strategy_failure(s, "OptDesign", "tool_error", param_signature="directive=AddRemap")
        assert len(s.context.failed_strategies) == 2

    def test_same_combo_dedups(self):
        s = OptimizerState(); s.iteration.current = 1
        record_strategy_failure(s, "OptDesign", "tool_error", param_signature="directive=Explore")
        record_strategy_failure(s, "OptDesign", "tool_error", param_signature="directive=Explore")
        assert len(s.context.failed_strategies) == 1

    def test_combo_escalation_does_not_block_strategy_at_select(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import _get_permanently_blocked_strategies
        s = OptimizerState(); s.iteration.current = 1
        # drive one combo to escalation
        for _ in range(RETRY_BUDGET + 1):
            record_strategy_failure(s, "OptDesign", "tool_error", param_signature="directive=Explore")
        # escalated to strategy_ineffective with non-empty param_signature
        e = s.context.failed_strategies[0]
        assert e.reason == "strategy_ineffective" and e.param_signature == "directive=Explore"
        # but NOT blocked at SELECT_STRATEGY (param_signature != "")
        assert "OptDesign" not in _get_permanently_blocked_strategies(s)

    def test_structural_failure_still_blocks_strategy(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import _get_permanently_blocked_strategies
        s = OptimizerState(); s.iteration.current = 1
        # structural strategy_ineffective (param_signature="")
        record_strategy_failure(s, "OptDesign", "strategy_ineffective", param_signature="")
        assert "OptDesign" in _get_permanently_blocked_strategies(s)

    def test_regression_still_blocks_strategy(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import _get_permanently_blocked_strategies
        s = OptimizerState(); s.iteration.current = 1
        record_strategy_failure(s, "OptDesign", "regression", param_signature="")
        assert "OptDesign" in _get_permanently_blocked_strategies(s)


# ── ②A: catalog three-way rendering ────────────────────────────────────────


class TestCatalogThreeWay:
    def test_combo_cooled_marker(self):
        cat = get_strategy_catalog(
            combo_cooled_strategies={"OptDesign": "directive=Explore - unblocks in 1 iter"})
        assert "COMBO COOLED: directive=Explore - unblocks in 1 iter" in cat

    def test_retry_marker_priority_below_combo_cooled(self):
        # When both apply, combo_cooled wins (stronger signal).
        cat = get_strategy_catalog(
            retryable_strategies={"OptDesign": "detail - 1 retry left"},
            combo_cooled_strategies={"OptDesign": "directive=Explore - unblocks in 1 iter"})
        assert "COMBO COOLED" in cat
        assert "RETRY" not in cat  # combo_cooled took priority on the same line

    def test_combo_cooled_shown_via_snapshot(self):
        # A combo-escalated strategy appears as [COMBO COOLED] in the SELECT catalog,
        # and the strategy stays in the available list (not [BLOCKED]).
        s = OptimizerState(); s.iteration.current = 1
        s.context.failed_strategies = [
            FailedStrategyRecord(
                strategy="OptDesign", reason="strategy_ineffective",
                param_signature="directive=Explore", blocked_until_iter=3,
                retry_count=RETRY_BUDGET,
            )
        ]
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "COMBO COOLED" in content
        # NOT shown as [BLOCKED] (that's for strategy-level failures)
        assert "unblocks in 2 iter" in content  # 3 - 1 = 2 remaining


# ── ③C: PhaseHandoff recent_failures ───────────────────────────────────────


class TestHandoffRecentFailures:
    def test_handoff_renders_recent_failures(self):
        h = build_phase_handoff(
            source_phase=LoopPhase.EXECUTE,
            llm_summary="x",
            recent_failures=["vivado_place_design: directive X not recognized"],
        )
        ctx = h.to_phase_context_string()
        assert "Recent Failures:" in ctx
        assert "vivado_place_design: directive X not recognized" in ctx

    def test_empty_failures_not_rendered(self):
        h = build_phase_handoff(source_phase=LoopPhase.EXECUTE, llm_summary="x")
        assert "Recent Failures" not in h.to_phase_context_string()
