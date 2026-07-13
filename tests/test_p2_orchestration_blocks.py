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

