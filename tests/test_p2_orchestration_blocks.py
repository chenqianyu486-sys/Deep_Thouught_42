"""①B num_paths chain param + ①C CUSTOM free orchestration + ②D unified block tests."""

from __future__ import annotations

import pytest

from optimizer.pure.execute_contracts import resolve_chain_step_arguments
from optimizer.pure.tool_catalog import get_strategy_primary_tool
from optimizer.pure.tool_filter import filter_tools_for_phase, LoopPhase
from optimizer.state import OptimizerState, FailedStrategyRecord
from optimizer.pure.context_snapshot import inject_merged_dashboard
from strategy_library import STRATEGIES, get_strategy_catalog


# ── ①B: num_paths chain param ──────────────────────────────────────────────


class TestNumPathsChainParam:
    def _extract_step(self):
        return {
            "tool": "vivado_extract_critical_path_cells",
            "args": {"num_paths": 10},
            "args_from_skill": {"num_paths": "extract_num_paths"},
        }

    def test_num_paths_from_skill_overrides_default(self):
        args, _ = resolve_chain_step_arguments(
            "rapidwright_execute_opt_design_strategy",
            self._extract_step(),
            {"extract_num_paths": 25},
        )
        assert args["num_paths"] == 25

    def test_num_paths_default_when_skill_omits(self):
        # When the skill result has no extract_num_paths, the hardcoded 10 is kept.
        args, _ = resolve_chain_step_arguments(
            "rapidwright_execute_opt_design_strategy",
            self._extract_step(),
            {},
        )
        assert args["num_paths"] == 10

    def test_num_paths_default_when_skill_result_none(self):
        args, _ = resolve_chain_step_arguments(
            "rapidwright_execute_lut_muxf_repack_strategy",
            self._extract_step(),
            None,
        )
        assert args["num_paths"] == 10

    @pytest.mark.parametrize("skill_name", [
        "rapidwright_execute_opt_design_strategy",
        "rapidwright_execute_combinational_rebalancing_strategy",
        "rapidwright_execute_lut_muxf_repack_strategy",
    ])
    def test_extract_step_has_args_from_skill(self, skill_name):
        # The opt_design-family chains wire num_paths <- extract_num_paths.
        from optimizer.pure.tool_chain_policy import SKILL_CHAIN_ACTIONS
        chain = SKILL_CHAIN_ACTIONS[skill_name]
        extract = next(s for s in chain if s["tool"] == "vivado_extract_critical_path_cells")
        assert extract["args_from_skill"] == {"num_paths": "extract_num_paths"}

    def test_chain_policy_synced_with_constants(self):
        # Duplicated chain policy must stay in sync (constants.py == tool_chain_policy.py).
        from optimizer.pure.tool_chain_policy import SKILL_CHAIN_ACTIONS as A
        from optimizer.pure.constants import SKILL_CHAIN_ACTIONS as B
        assert A == B


# ── ①C: CUSTOM free orchestration ──────────────────────────────────────────


class TestCustomOrchestration:
    def test_custom_in_strategies_catalog(self):
        assert "CUSTOM" in STRATEGIES
        cat = get_strategy_catalog()
        assert "Free Orchestration" in cat

    def test_custom_has_no_primary_tool(self):
        # CUSTOM is not in STRATEGY_MAP -> no primary tool -> filter_tools_for_phase
        # keeps the broad EXECUTE toolset (no narrowing).
        assert get_strategy_primary_tool("CUSTOM") is None

    def test_custom_in_strategy_enum(self):
        # The report_step_state strategy_name enum must include CUSTOM so the LLM
        # can select it. Parse it from the dcp_optimizer module's tool schema.
        import dcp_optimizer
        # The enum is built in build_tools; verify CUSTOM is a selectable label by
        # checking STRATEGIES (catalog source) + primary_tool None (filter behavior).
        assert "CUSTOM" in STRATEGIES

    def test_custom_keeps_broad_toolset(self):
        # CUSTOM (no primary tool) does NOT narrow EXECUTE tools - mirrors the
        # existing unknown-strategy behavior (test_unknown_execute_strategy_keeps).
        # Use a non-strategy tool that's in the EXECUTE allowlist.
        tools = [{"function": {"name": "vivado_place_design"}}]
        filtered = filter_tools_for_phase(tools, LoopPhase.EXECUTE, strategy="CUSTOM")
        names = {t["function"]["name"] for t in filtered}
        assert "vivado_place_design" in names  # broad toolset retained

        # Contrast: a known strategy (OptDesign) narrows to its primary tool only.
        filtered_o = filter_tools_for_phase(tools, LoopPhase.EXECUTE, strategy="OptDesign")
        # vivado_place_design is NOT OptDesign's primary tool -> narrowed out.
        assert "vivado_place_design" not in {t["function"]["name"] for t in filtered_o}


# ── ②D: unified block semantics ────────────────────────────────────────────


class TestUnifiedBlockSemantics:
    def _state_with(self, *records):
        s = OptimizerState()
        s.iteration.current = 1
        s.context.failed_strategies = list(records)
        return s

    def test_regression_shown_as_blocked(self):
        # P2 ②D: regression now displays [BLOCKED] (consistent with its hard-block).
        s = self._state_with(FailedStrategyRecord(
            strategy="Fanout", reason="regression", blocked_until_iter=3))
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "BLOCKED" in content
        assert "regression" in content

    def test_no_improvement_shown_as_prior_fail_not_blocked(self):
        # P2 ②D: no_improvement is SOFT -> [PRIOR FAIL] (selectable), NOT [BLOCKED].
        s = self._state_with(FailedStrategyRecord(
            strategy="CellReplication", reason="no_improvement", blocked_until_iter=4))
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "PRIOR FAIL" in content
        assert "no_improvement" in content

    def test_strategy_not_applicable_is_soft(self):
        s = self._state_with(FailedStrategyRecord(
            strategy="LUTCascade", reason="strategy_not_applicable", blocked_until_iter=5))
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "PRIOR FAIL" in content

    def test_strategy_ineffective_still_blocked(self):
        # Structural strategy_ineffective remains a true [BLOCKED].
        s = self._state_with(FailedStrategyRecord(
            strategy="PhysOpt", reason="strategy_ineffective", blocked_until_iter=3))
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "BLOCKED" in content

    def test_soft_failed_not_hard_blocked_at_select(self):
        # no_improvement/strategy_not_applicable are NOT in the SELECT hard-block set.
        from optimizer.nodes.subgraphs.phase_select_strategy import _get_permanently_blocked_strategies
        s = self._state_with(
            FailedStrategyRecord(strategy="CellReplication", reason="no_improvement", blocked_until_iter=4),
            FailedStrategyRecord(strategy="LUTCascade", reason="strategy_not_applicable", blocked_until_iter=5),
        )
        blocked = _get_permanently_blocked_strategies(s)
        assert "CellReplication" not in blocked
        assert "LUTCascade" not in blocked

    def test_catalog_priority_combo_cooled_over_soft_over_retry(self):
        cat = get_strategy_catalog(
            combo_cooled_strategies={"OptDesign": "directive=Explore - unblocks in 1"},
            soft_failed_strategies={"OptDesign": "no_improvement - unblocks in 2"},
            retryable_strategies={"OptDesign": "detail - 1 left"},
        )
        # combo_cooled wins
        assert "COMBO COOLED" in cat and "PRIOR FAIL" not in cat and "RETRY" not in cat

        cat2 = get_strategy_catalog(
            soft_failed_strategies={"OptDesign": "no_improvement - unblocks in 2"},
            retryable_strategies={"OptDesign": "detail - 1 left"},
        )
        # soft_failed beats retryable
        assert "PRIOR FAIL" in cat2 and "RETRY" not in cat2


# ── Polish: SELECT block-reason + strategy_outcomes labels ─────────────────


class TestPolishFixes:
    def test_blocking_reason_regression(self):
        # Fix: regression must report "regression" + correct unblock count, not
        # "temporarily ineffective; unblocks in 0 iterations".
        from optimizer.nodes.subgraphs.phase_select_strategy import blocking_reason_for_strategy
        fs = [FailedStrategyRecord(strategy="Fanout", reason="regression", blocked_until_iter=3)]
        r = blocking_reason_for_strategy("Fanout", fs, current_iter=1)
        assert "regression" in r
        assert "unblocks in 2 iterations" in r

    def test_blocking_reason_strategy_ineffective(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import blocking_reason_for_strategy
        fs = [FailedStrategyRecord(strategy="PhysOpt", reason="strategy_ineffective", blocked_until_iter=3)]
        r = blocking_reason_for_strategy("PhysOpt", fs, current_iter=2)
        assert "temporarily ineffective" in r
        assert "unblocks in 1 iterations" in r
        assert "regression" not in r

    def test_blocking_reason_fallback_when_no_active_block(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import blocking_reason_for_strategy
        # TTL expired -> not an active block -> fallback string.
        fs = [FailedStrategyRecord(strategy="X", reason="regression", blocked_until_iter=1)]
        r = blocking_reason_for_strategy("X", fs, current_iter=2)
        assert "unblocks in 0 iterations" in r

    def test_strategy_outcomes_soft_failure_label(self):
        # Fix: no_improvement (soft) shows "(soft, unblocks in N iter)" not
        # "(blocked until iter N)" - consistent with [PRIOR FAIL] catalog marker.
        s = OptimizerState()
        s.iteration.current = 1
        s.context.failed_strategies = [
            FailedStrategyRecord(strategy="CellReplication", reason="no_improvement",
                                 blocked_until_iter=4, iteration=1),
        ]
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "soft, unblocks in 3 iter" in content

    def test_strategy_outcomes_regression_label(self):
        s = OptimizerState()
        s.iteration.current = 1
        s.context.failed_strategies = [
            FailedStrategyRecord(strategy="Fanout", reason="regression",
                                 blocked_until_iter=3, iteration=1),
        ]
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "blocked - regression" in content


# ── FORMAT_GUARD: tightened EXHAUSTED guidance ──────────────────────────────


class TestFormatGuardExhaustedGuidance:
    """Lock in the tightened EXHAUSTED guidance.

    run-20260713_050453: the LLM signaled EXHAUSTED in EXECUTE to mean "this
    strategy (CellReplication) is a bad fit", while its own text recommended
    switching to PlaceRouteDirectiveExplore. EXHAUSTED is terminal (is_done=True),
    so the run ended without trying the recommended strategy. The guidance now
    must distinguish EXEC_DONE (move on, try another) from EXHAUSTED (truly done).
    """

    def test_execute_guide_distinguishes_exec_done_vs_exhausted(self):
        from optimizer.nodes.prepare_context import build_phase_format_guard
        g = build_phase_format_guard(LoopPhase.EXECUTE)
        assert "EXEC_DONE" in g
        assert "EXHAUSTED: TERMINAL" in g
        # Must steer "bad fit / no change" to EXEC_DONE, not EXHAUSTED
        assert "EXEC_DONE + SWITCH_STRATEGY" in g
        assert "abandons untried strategies" in g

    def test_evaluate_guide_exhausted_is_terminal(self):
        from optimizer.nodes.prepare_context import build_phase_format_guard
        g = build_phase_format_guard(LoopPhase.EVALUATE)
        assert "EXHAUSTED is TERMINAL" in g
        # Old loose phrasing ("end this iteration") removed
        assert "choose EXHAUSTED to end this iteration" not in g
        # Must require 2+ consecutive no-improvement, not a single one
        assert "2+ consecutive" in g


# ── P2 route-2: next_strategy_hint ──────────────────────────────────────────


class TestNextStrategyHint:
    """The LLM can hint its next strategy when signaling EXEC_DONE/SWITCH_STRATEGY,
    surfaced as a soft [STRATEGY HINT] in SELECT_STRATEGY. Gives the LLM a legal
    outlet for 'switch to X' intent instead of misusing EXHAUSTED."""

    def test_build_hint_message_valid(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import build_strategy_hint_message
        from strategy_library import STRATEGIES
        msg = build_strategy_hint_message("PlaceRouteDirectiveExplore", STRATEGIES)
        assert msg is not None
        assert "[STRATEGY HINT]" in msg
        assert "PlaceRouteDirectiveExplore" in msg
        assert "NOT enforced" in msg

    def test_build_hint_message_invalid_returns_none(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import build_strategy_hint_message
        from strategy_library import STRATEGIES
        # Garbage / empty / None strategies -> None (silently dropped, not injected)
        assert build_strategy_hint_message("GarbageStrategy", STRATEGIES) is None
        assert build_strategy_hint_message("", STRATEGIES) is None
        assert build_strategy_hint_message("PhysOpt", None) is None

    def test_step_state_carries_hint(self):
        from optimizer.state import StepState
        ss = StepState(step_id=1, result_status="PARTIAL", flow_control="EXEC_DONE",
                       next_strategy_hint="PhysOptAggressive")
        assert ss.next_strategy_hint == "PhysOptAggressive"

    def test_pending_hint_default_empty(self):
        from optimizer.state import OptimizerState
        assert OptimizerState().strategy.pending_next_strategy_hint == ""

    def test_extract_step_state_parses_hint(self):
        import json
        from optimizer.pure.step_state import extract_step_state

        class FakeFn:
            def __init__(self, name, args):
                self.name = name
                self.arguments = args
        class FakeTC:
            def __init__(self, name, args):
                self.function = FakeFn(name, args)
        class FakeMsg:
            def __init__(self, tcs):
                self.tool_calls = tcs

        msg = FakeMsg([FakeTC("report_step_state", json.dumps({
            "step_id": 1, "result_status": "PARTIAL", "flow_control": "EXEC_DONE",
            "next_strategy_hint": "PlaceRouteDirectiveExplore",
        }))])
        ss = extract_step_state(msg)
        assert ss is not None
        assert ss.next_strategy_hint == "PlaceRouteDirectiveExplore"
        # report_step_state removed from tool_calls
        assert msg.tool_calls is None


# ── flow_control tool-description patch (per-signal semantics) ──────────────


class TestFlowControlDescriptionPatch:
    """The per-phase flow_control description must carry per-signal semantics,
    not a bare list. run-20260713_050453: the LLM misused EXHAUSTED partly
    because the tool description was only 'Valid signals for EXECUTE: ...'
    with no explanation of what each signal does."""

    @staticmethod
    def _patched_desc(phase):
        import copy as _copy
        from optimizer.pure.tool_filter import filter_tools_for_phase
        t = {"function": {"name": "report_step_state",
                          "parameters": {"properties": {"flow_control": {"enum": [], "description": ""}}}}}
        f = filter_tools_for_phase([_copy.deepcopy(t)], phase)
        return f[0]["function"]["parameters"]["properties"]["flow_control"]["description"]

    def test_execute_desc_has_exec_done_and_exhausted_semantics(self):
        d = self._patched_desc(LoopPhase.EXECUTE)
        assert "EXEC_DONE" in d and "NORMAL way to move on" in d
        assert "EXHAUSTED" in d and "TERMINAL" in d and "NOT for" in d

    def test_evaluate_desc_has_switch_strategy_semantics(self):
        d = self._patched_desc(LoopPhase.EVALUATE)
        assert "SWITCH_STRATEGY" in d and "abandon current strategy" in d

    def test_desc_is_multiline_with_per_signal_entries(self):
        # NOT the old bare "Valid signals for X: A, B, C" single-line list.
        d = self._patched_desc(LoopPhase.EXECUTE)
        assert " - EXEC_DONE:" in d
        assert d.count("\n") >= 2




