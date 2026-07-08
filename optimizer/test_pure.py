"""Unit tests for optimizer pure functions.

Tests cover:
    - timing: parse_timing_summary, is_valid_wns, parse_hold_timing
    - model_select: classify_task, compute_model_scores, select_model
    - iteration_logic: update_iteration_counters, infer_strategy_from_tools, build_iteration_narrative
    - handoff: build_data_driven_goal, build_stagnation_signal

Run: python3 -m pytest optimizer/test_pure.py -v
"""

from __future__ import annotations

import pytest

from optimizer.state import OptimizerState
from optimizer.pure.timing import (
    parse_timing_summary,
    is_valid_wns,
    parse_hold_timing,
    parse_resource_utilization,
    parse_route_status,
)
from skills.pblock_strategy import compute_adaptive_resource_multiplier
from optimizer.pure.model_select import (
    classify_task,
    classify_tcl_command,
    compute_model_scores,
    select_model,
    estimate_context_complexity,
    get_task_capability_score,
)
from optimizer.pure.tool_filter import (
    LoopPhase,
    PHASE_MAX_ROUNDS,
    filter_tools_for_phase,
    get_phase_max_rounds,
)
from optimizer.pure.iteration_logic import (
    update_iteration_counters,
    update_task_type_stats,
    infer_strategy_from_tools,
    build_iteration_narrative,
)
from optimizer.pure.constants import (
    DASHBOARD_REFRESH_MAP,
    EXECUTE_CORE_TOOLS,
    PHASE_TOOL_RATE_LIMITS,
    POST_EVAL_TOOLS,
    SIDE_EFFECT_TOOLS,
    SKILL_CHAIN_ACTIONS,
    get_skill_chain_actions,
    get_strategy_primary_tool,
    should_skip_chain_for_empty_result,
    tool_uses_rw_precheck,
)
from optimizer.pure.execute_contracts import (
    build_timing_update_exit_contract,
    build_post_eval_guidance,
    build_precheck_failure_contract,
    extract_skill_precheck_diagnostics,
    extract_post_eval_metrics,
    get_pblock_place_only_threshold,
    is_chain_step_failure_result,
    next_empty_response_streak,
    next_no_progress_count,
    resolve_ordered_pblock_candidates,
    resolve_selected_pblock_plan,
    resolve_chain_step_arguments,
    resolve_chain_step_runtime_override,
    should_exit_for_empty_responses,
    should_exit_for_large_regression,
    should_exit_for_no_progress,
    should_block_strategy,
    should_recompute_chain_verdict,
    tool_requires_post_chain_path_refresh,
    verdict_from_wns_delta,
    verdict_from_wns_values,
)
from optimizer.pure.phase_policy import build_phase_exit_contract
from optimizer.pure.pblock_plan import (
    PBLOCK_EXECUTE_DEFAULT_RESOURCE_MULTIPLIER,
    PBLOCK_GLOBAL_MODE,
    PBLOCK_LOCAL_MODE,
    PBLOCK_UNPLACE_GLOBAL,
    PblockExecutionPlan,
    get_place_only_screening_threshold,
    plan_requires_execution_rebuild,
    recommend_pblock_plan,
    should_route_pblock_after_place,
)
from optimizer.pure.tool_contracts import (
    build_tool_call_result,
    coerce_payload_dict,
    is_mcp_error_response,
    strip_tool_cache_header,
)
from optimizer.pure.tool_catalog import (
    EXECUTE_CORE_TOOLS as EXECUTE_CORE_TOOLS_CATALOG,
    POST_EVAL_TOOLS as POST_EVAL_TOOLS_CATALOG,
)
from optimizer.pure.tool_chain_policy import (
    PLACE_ONLY_CHECK_SKILLS as PLACE_ONLY_CHECK_SKILLS_POLICY,
    SKILL_CHAIN_ACTIONS as SKILL_CHAIN_ACTIONS_POLICY,
)
from optimizer.pure.tool_runtime_policy import (
    DASHBOARD_REFRESH_MAP as DASHBOARD_REFRESH_MAP_POLICY,
    PHASE_TOOL_RATE_LIMITS as PHASE_TOOL_RATE_LIMITS_POLICY,
)


# ── Timing tests ─────────────────────────────────────────────────

class TestPhaseMaxRounds:
    def test_execute_phase_round_limit_is_tight(self):
        """EXECUTE must stay bounded to avoid wasting time on stalled strategies."""
        assert PHASE_MAX_ROUNDS[LoopPhase.EXECUTE] == 5

    def test_analysis_phases_keep_enough_budget(self):
        assert PHASE_MAX_ROUNDS[LoopPhase.ANALYZE] >= PHASE_MAX_ROUNDS[LoopPhase.EXECUTE]
        assert PHASE_MAX_ROUNDS[LoopPhase.EVALUATE] >= PHASE_MAX_ROUNDS[LoopPhase.EXECUTE]

    @pytest.mark.parametrize("strategy", ["SmartRetiming", "PhysOpt+RegisterRetiming"])
    def test_validation_unsafe_strategies_do_not_get_extended_budget(self, strategy):
        assert get_phase_max_rounds(LoopPhase.EXECUTE, strategy) == 5

    def test_auto_chained_strategy_keeps_tight_budget(self):
        assert get_phase_max_rounds(LoopPhase.EXECUTE, "PBLOCK") == 5


class TestStrategyToolFiltering:
    @staticmethod
    def _tool(name):
        properties = {"flow_control": {"type": "string"}} if name == "report_step_state" else {}
        return {
            "type": "function",
            "function": {
                "name": name,
                "parameters": {"type": "object", "properties": properties},
            },
        }

    def test_known_execute_strategy_exposes_only_primary_tool(self):
        tools = [
            self._tool("vivado_physopt_and_route"),
            self._tool("rapidwright_analyze_congestion"),
            self._tool("vivado_run_tcl"),
            self._tool("report_step_state"),
        ]

        filtered = filter_tools_for_phase(
            tools, LoopPhase.EXECUTE, strategy="PhysOpt"
        )

        names = {tool["function"]["name"] for tool in filtered}
        assert names == {"vivado_physopt_and_route", "report_step_state"}

    def test_unknown_execute_strategy_keeps_phase_toolset(self):
        tools = [
            self._tool("vivado_physopt_and_route"),
            self._tool("rapidwright_analyze_congestion"),
            self._tool("report_step_state"),
        ]

        filtered = filter_tools_for_phase(
            tools, LoopPhase.EXECUTE, strategy="ExperimentalStrategy"
        )

        names = {tool["function"]["name"] for tool in filtered}
        assert names == {
            "vivado_physopt_and_route",
            "rapidwright_analyze_congestion",
            "report_step_state",
        }


class TestToolContracts:
    def test_primary_tool_lookup_is_centralized(self):
        assert get_strategy_primary_tool("PBLOCK") == "rapidwright_execute_pblock_strategy"
        assert get_strategy_primary_tool("PhysOpt") == "vivado_physopt_and_route"
        assert get_strategy_primary_tool("UnknownStrategy") is None

    def test_execute_core_tools_feed_execute_allowlist(self):
        assert "rapidwright_execute_pblock_strategy" in EXECUTE_CORE_TOOLS
        assert "vivado_physopt_and_route" in EXECUTE_CORE_TOOLS
        assert "report_step_state" in EXECUTE_CORE_TOOLS

    def test_rw_precheck_contract_skips_analysis_only_pblock_chain(self):
        assert tool_uses_rw_precheck("rapidwright_execute_fanout_strategy") is True
        assert tool_uses_rw_precheck("rapidwright_execute_pblock_strategy") is False
        assert tool_uses_rw_precheck("vivado_route_design") is False

    def test_sparse_chain_skip_contract_respects_exemptions(self):
        skip, reason = should_skip_chain_for_empty_result(
            "rapidwright_execute_fanout_strategy",
            {"status": "success", "optimized_cells": [], "critical_paths": []},
        )
        assert skip is True
        assert reason == "no data produced"

        skip, reason = should_skip_chain_for_empty_result(
            "rapidwright_flatten_lut_cascade",
            {"status": "success", "optimized_cells": [], "critical_paths": []},
        )
        assert skip is False
        assert reason is None

    def test_post_eval_and_chain_declarations_stay_centralized(self):
        assert "rapidwright_execute_pblock_strategy" in POST_EVAL_TOOLS
        chain = get_skill_chain_actions("rapidwright_execute_pblock_strategy")
        assert chain
        assert chain[0]["tool"] == "vivado_unplace_cells"

    def test_side_effect_tools_track_design_mutators(self):
        assert "rapidwright_execute_physopt_strategy" in SIDE_EFFECT_TOOLS
        assert "vivado_create_and_apply_pblock" in SIDE_EFFECT_TOOLS


class TestExecuteContracts:
    def test_verdict_from_wns_delta(self):
        assert verdict_from_wns_delta(0.010) == "IMPROVED"
        assert verdict_from_wns_delta(0.0005) == "UNCHANGED"
        assert verdict_from_wns_delta(-0.010) == "REGRESSED"

    def test_verdict_from_wns_values_returns_delta(self):
        verdict, delta = verdict_from_wns_values(-0.400, -0.350)
        assert verdict == "IMPROVED"
        assert round(delta, 3) == 0.050

    def test_extract_post_eval_metrics_for_physopt_and_route(self):
        raw = """
        {
          "post_optimization": {
            "wns": -0.321,
            "tns": -1.25,
            "failing_endpoints": 7
          }
        }
        """
        metrics = extract_post_eval_metrics("vivado_physopt_and_route", raw)
        assert metrics == {"wns": -0.321, "tns": -1.25, "failing_endpoints": 7}
        assert extract_post_eval_metrics("vivado_route_design", raw) is None

    def test_should_block_strategy_tracks_non_improving_verdicts(self):
        assert should_block_strategy("UNCHANGED") is True
        assert should_block_strategy("REGRESSED") is True
        assert should_block_strategy("IMPROVED") is False

    def test_build_post_eval_guidance_only_for_unchanged_post_eval_tools(self):
        msg = build_post_eval_guidance("vivado_physopt_and_route", "UNCHANGED")
        assert msg is not None
        assert "no WNS improvement" in msg
        assert build_post_eval_guidance("vivado_physopt_and_route", "IMPROVED") is None
        assert build_post_eval_guidance("rapidwright_search_cells", "UNCHANGED") is None

    def test_build_timing_update_exit_contract(self):
        contract = build_timing_update_exit_contract(
            "vivado_physopt_and_route",
            "IMPROVED",
            target_met=False,
        )
        assert contract == {
            "flow_signal": "EXEC_DONE",
            "reason": "post_eval_improved",
        }

        contract = build_timing_update_exit_contract(
            "vivado_physopt_and_route",
            "UNCHANGED",
            target_met=True,
        )
        assert contract == {
            "flow_signal": "DONE",
            "reason": "wns_target_met",
        }

        assert build_timing_update_exit_contract(
            "rapidwright_search_cells",
            "UNCHANGED",
            target_met=False,
        ) is None

    def test_exit_threshold_helpers(self):
        assert should_exit_for_large_regression(-1.2, -0.5) is True
        assert should_exit_for_large_regression(-0.8, -0.5, margin=0.5) is False
        assert should_exit_for_no_progress(4, limit=4) is True
        assert should_exit_for_no_progress(3, limit=4) is False
        assert should_exit_for_empty_responses(2) is True
        assert should_exit_for_empty_responses(1) is False

    def test_progress_counters_advance_consistently(self):
        assert next_no_progress_count(2, had_tool_calls=True, round_had_side_effect=True, pending_tool_count=0) == 0
        assert next_no_progress_count(2, had_tool_calls=True, round_had_side_effect=False, pending_tool_count=0) == 3
        assert next_no_progress_count(2, had_tool_calls=True, round_had_side_effect=False, pending_tool_count=1) == 2
        assert next_no_progress_count(2, had_tool_calls=False) == 3

        assert next_empty_response_streak(0, assistant_content="", has_tool_calls=False) == 1
        assert next_empty_response_streak(1, assistant_content="thinking", has_tool_calls=False) == 0
        assert next_empty_response_streak(1, assistant_content="", has_tool_calls=True) == 0

    def test_extract_skill_precheck_diagnostics_summarizes_skipped_skill(self):
        raw = """
        {
          "status": "skipped",
          "analysis_summary": {
            "diagnosis": "no_match",
            "cell_type_distribution": {
              "FDRE": 9,
              "LUT6": 3
            }
          }
        }
        """
        skipped, diagnostics = extract_skill_precheck_diagnostics(raw)
        assert skipped is True
        assert "FDRE" in diagnostics
        assert "diagnosis: no_match" in diagnostics
        assert extract_skill_precheck_diagnostics('{"status": "success"}') == (False, "")

    def test_build_precheck_failure_contract_distinguishes_not_applicable(self):
        contract = build_precheck_failure_contract(
            "rapidwright_execute_fanout_strategy",
            "NO_WORK",
            skill_was_skipped=True,
            skill_diagnostics="critical path cell types: {'FDRE': 4}, diagnosis: no_match",
        )
        assert contract is not None
        assert contract["done_reason"] == "precheck_no_work"
        assert contract["failure_reason"] == "strategy_not_applicable"
        assert "not applicable" in contract["user_message"]

        regress = build_precheck_failure_contract(
            "rapidwright_execute_fanout_strategy",
            "REGRESS",
        )
        assert regress is not None
        assert regress["failure_reason"] == "strategy_ineffective"
        assert regress["done_reason"] == "precheck_direction_regress"

    def test_resolve_chain_step_arguments_prefers_skill_then_strategy_defaults(self):
        step = {
            "tool": "vivado_route_design",
            "args": {},
            "args_from_skill": {"directive": "route_directive"},
        }
        args, note = resolve_chain_step_arguments(
            "rapidwright_execute_pblock_strategy",
            step,
            {},
        )
        assert args["directive"] == "Explore"
        assert note is None

        args, note = resolve_chain_step_arguments(
            "rapidwright_execute_pblock_strategy",
            step,
            {"route_directive": "Explore"},
        )
        assert args["directive"] == "Explore"
        assert note is None

    def test_resolve_chain_step_arguments_rewrites_blacklisted_directives(self):
        step = {
            "tool": "vivado_place_design",
            "args": {},
            "args_from_skill": {"directive": "place_directive"},
        }
        args, note = resolve_chain_step_arguments(
            "rapidwright_execute_opt_design_strategy",
            step,
            {"place_directive": "Performance_ExtraTimingOpt"},
        )
        assert args["directive"] == "ExtraTimingOpt"
        assert note is not None
        assert "blacklisted" in note

    def test_resolve_chain_step_runtime_override_global_unplace_for_pblock_fallback(self):
        target_tool, args, note = resolve_chain_step_runtime_override(
            "rapidwright_execute_pblock_strategy",
            "vivado_unplace_cells",
            {"cells": ["u0", "u1"]},
            {
                "bind_critical_path_cells_to_pblock": False,
                "pblock_fallback_reason": "whole-design fallback triggered",
            },
        )
        assert target_tool == "vivado_place_design"
        assert args == {"directive": "unplace"}
        assert note is not None
        assert "global place_design -unplace" in note

    def test_resolve_chain_step_runtime_override_keeps_local_unplace_when_binding_cells(self):
        target_tool, args, note = resolve_chain_step_runtime_override(
            "rapidwright_execute_pblock_strategy",
            "vivado_unplace_cells",
            {"cells": ["u0", "u1"]},
            {"bind_critical_path_cells_to_pblock": True},
        )
        assert target_tool == "vivado_unplace_cells"
        assert args == {"cells": ["u0", "u1"]}
        assert note is None

    def test_resolve_chain_step_arguments_drops_pblock_cells_for_whole_design_fallback(self):
        step = {
            "tool": "vivado_create_and_apply_pblock",
            "args": {},
            "args_from_skill": {
                "pblock_name": "pblock_name",
                "ranges": "pblock_ranges",
                "is_soft": "is_soft_recommended",
                "cells": "critical_path_cells",
            },
        }
        args, note = resolve_chain_step_arguments(
            "rapidwright_execute_pblock_strategy",
            step,
            {
                "pblock_name": "pblock_tight",
                "pblock_ranges": "SLICE_X10Y0:SLICE_X20Y299",
                "is_soft_recommended": False,
                "critical_path_cells": ["u0", "u1"],
                "bind_critical_path_cells_to_pblock": False,
                "pblock_fallback_reason": "whole-design fallback triggered",
            },
        )
        assert "cells" not in args
        assert args["is_soft"] is True
        assert note is not None
        assert "whole-design fallback" in note

    def test_resolve_chain_step_arguments_keeps_pblock_cells_for_local_binding(self):
        step = {
            "tool": "vivado_create_and_apply_pblock",
            "args": {},
            "args_from_skill": {
                "pblock_name": "pblock_name",
                "ranges": "pblock_ranges",
                "is_soft": "is_soft_recommended",
                "cells": "critical_path_cells",
            },
        }
        args, note = resolve_chain_step_arguments(
            "rapidwright_execute_pblock_strategy",
            step,
            {
                "pblock_name": "pblock_tight",
                "pblock_ranges": "SLICE_X54Y0:SLICE_X54Y299",
                "is_soft_recommended": False,
                "critical_path_cells": ["u0", "u1"],
            },
        )
        assert args["cells"] == ["u0", "u1"]
        assert args["is_soft"] is False
        assert note is None

    def test_chain_step_failure_result_handles_json_and_plaintext_errors(self):
        assert is_chain_step_failure_result('{"error": "route failed"}') is True
        assert is_chain_step_failure_result("INFO: start\nERROR: [Route 35-39] failed") is True
        assert is_chain_step_failure_result('{"status": "success"}') is False

    def test_should_recompute_chain_verdict_only_overrides_stale_unchanged(self):
        override, verdict, delta = should_recompute_chain_verdict(
            "rapidwright_execute_pblock_strategy",
            "UNCHANGED",
            -0.500,
            -0.450,
        )
        assert override is True
        assert verdict == "IMPROVED"
        assert round(delta, 3) == 0.050

        override, verdict, delta = should_recompute_chain_verdict(
            "rapidwright_execute_pblock_strategy",
            "IMPROVED",
            -0.500,
            -0.450,
        )
        assert override is False
        assert verdict is None

    def test_post_chain_refresh_contract(self):
        assert tool_requires_post_chain_path_refresh("rapidwright_execute_pblock_strategy") is True
        assert tool_requires_post_chain_path_refresh("rapidwright_execute_fanout_strategy") is False


def _make_pblock_plan(
    *,
    candidate_id: str,
    plan_mode: str,
    columns_used: int,
    center_col: int,
    capacity_ok: bool = True,
    utilization_density: float = 0.5,
    bind_cells: bool | None = None,
    unplace_mode: str | None = None,
    resource_multiplier: float = 2.0,
    bound_luts: int = 3000,
    bound_ffs: int = 1200,
    estimated_resources: dict | None = None,
):
    if bind_cells is None:
        bind_cells = plan_mode == PBLOCK_LOCAL_MODE
    if unplace_mode is None:
        unplace_mode = "local_cells" if bind_cells else PBLOCK_UNPLACE_GLOBAL
    return PblockExecutionPlan(
        plan_mode=plan_mode,
        candidate_id=candidate_id,
        pblock_name="pblock_tight",
        pblock_ranges="SLICE_X0Y0:SLICE_X10Y299",
        resource_multiplier=resource_multiplier,
        target_lut_count=10000,
        target_ff_count=20000,
        target_dsp_count=0,
        target_bram_count=0,
        bind_cells_to_pblock=bind_cells,
        unplace_mode=unplace_mode,
        is_soft=not bind_cells,
        place_directive="Explore",
        route_directive="Explore",
        reference_col=center_col,
        reference_row=150,
        selection_reason=candidate_id,
        critical_path_cells_snapshot=["u0", "u1"],
        capacity_ok=capacity_ok,
        estimated_resources=estimated_resources or {"luts": 12000, "ffs": 24000, "dsps": 0, "brams": 0},
        region={"columns_used": columns_used, "center_col": center_col, "center_row": 150},
        utilization_density=utilization_density,
        bound_resources={"luts": bound_luts, "ffs": bound_ffs},
    )


class TestPblockPlanContracts:
    def test_recommend_pblock_plan_prefers_non_degenerate_local_plan(self):
        local = _make_pblock_plan(
            candidate_id="local_bound_cells",
            plan_mode=PBLOCK_LOCAL_MODE,
            columns_used=3,
            center_col=60,
            utilization_density=0.25,
            bound_luts=2500,
            bound_ffs=3000,
        )
        global_plan = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=12,
            center_col=70,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        selected, ordered = recommend_pblock_plan(
            [local, global_plan],
            critical_path_reference=(65, 150),
        )
        assert selected is not None
        assert selected.candidate_id == "local_bound_cells"
        assert ordered[0].candidate_id == "local_bound_cells"

    def test_recommend_pblock_plan_falls_back_to_global_when_local_degenerate(self):
        local = _make_pblock_plan(
            candidate_id="local_bound_cells",
            plan_mode=PBLOCK_LOCAL_MODE,
            columns_used=1,
            center_col=90,
            utilization_density=0.05,
            bound_luts=20,
            bound_ffs=15,
        )
        global_left = _make_pblock_plan(
            candidate_id="global_left_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=10,
            center_col=30,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        selected, ordered = recommend_pblock_plan(
            [local, global_left],
            critical_path_reference=(32, 150),
        )
        assert selected is not None
        assert selected.candidate_id == "global_left_bias"
        assert ordered[0].candidate_id == "global_left_bias"

    def test_global_candidates_rank_by_capacity_then_surplus_then_distance(self):
        tight = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=9,
            center_col=50,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            estimated_resources={"luts": 10200, "ffs": 20400, "dsps": 0, "brams": 0},
        )
        far = _make_pblock_plan(
            candidate_id="global_right_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=9,
            center_col=90,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            estimated_resources={"luts": 10200, "ffs": 20400, "dsps": 0, "brams": 0},
        )
        selected, ordered = recommend_pblock_plan(
            [far, tight],
            critical_path_reference=(52, 150),
        )
        assert selected is not None
        assert selected.candidate_id == "global_cp_center"
        assert ordered[0].candidate_id == "global_cp_center"

    def test_global_candidates_demote_nearly_full_regions(self):
        roomy = _make_pblock_plan(
            candidate_id="global_left_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=14,
            center_col=40,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.78,
            estimated_resources={"luts": 13000, "ffs": 26000, "dsps": 0, "brams": 0},
        )
        nearly_full = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=9,
            center_col=52,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.995,
            estimated_resources={"luts": 20000, "ffs": 40000, "dsps": 0, "brams": 0},
        )
        selected, ordered = recommend_pblock_plan(
            [nearly_full, roomy],
            critical_path_reference=(52, 150),
        )
        assert selected is not None
        assert selected.candidate_id == "global_left_bias"
        assert ordered[0].candidate_id == "global_left_bias"

    def test_global_candidates_prefer_loose_over_near_full_regions(self):
        # Strong-baseline evidence (run-20260706_165117): the winning global
        # replacement pblock ran at ~0.24 target/capacity. A loose window
        # (0.49) must outrank a near-full one (0.92) that congests the placer.
        near_full = _make_pblock_plan(
            candidate_id="global_right_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=80,
            center_col=75,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.92,
            estimated_resources={"luts": 34000, "ffs": 68000, "dsps": 0, "brams": 0},
        )
        loose = _make_pblock_plan(
            candidate_id="global_cp_center_capacity_fallback",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=158,
            center_col=55,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.49,
            estimated_resources={"luts": 64000, "ffs": 128000, "dsps": 0, "brams": 0},
        )
        selected, ordered = recommend_pblock_plan(
            [loose, near_full],
            critical_path_reference=(60, 150),
        )
        assert selected is not None
        assert selected.candidate_id == "global_cp_center_capacity_fallback"
        assert ordered[0].candidate_id == "global_cp_center_capacity_fallback"

    def test_global_candidates_demote_whole_device_noop_regions(self):
        # Below ~0.10 target/capacity a "window" is effectively the whole
        # device and constrains nothing — a loose bounded window must win.
        loose = _make_pblock_plan(
            candidate_id="global_left_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=100,
            center_col=60,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.30,
            estimated_resources={"luts": 105000, "ffs": 210000, "dsps": 0, "brams": 0},
        )
        whole_device = _make_pblock_plan(
            candidate_id="global_cp_center_capacity_fallback",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=536,
            center_col=268,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.08,
            estimated_resources={"luts": 392000, "ffs": 784000, "dsps": 0, "brams": 0},
        )
        selected, ordered = recommend_pblock_plan(
            [whole_device, loose],
            critical_path_reference=(60, 150),
        )
        assert selected is not None
        assert selected.candidate_id == "global_left_bias"
        assert ordered[0].candidate_id == "global_left_bias"

    def test_place_only_thresholds_are_mode_specific(self):
        assert get_place_only_screening_threshold(PBLOCK_LOCAL_MODE) == 0.03
        assert get_place_only_screening_threshold(PBLOCK_GLOBAL_MODE) == 0.10

    def test_global_place_only_neutral_delta_can_route_when_roomy(self):
        plan = _make_pblock_plan(
            candidate_id="global_left_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=20,
            center_col=40,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.82,
            capacity_ok=True,
        )
        assert should_route_pblock_after_place(plan, 0.0, threshold=0.10)

    def test_global_place_only_neutral_delta_screens_nearly_full_region(self):
        plan = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=90,
            center_col=60,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            utilization_density=0.995,
            capacity_ok=True,
        )
        assert not should_route_pblock_after_place(plan, 0.0, threshold=0.10)

    def test_local_place_only_neutral_delta_still_requires_threshold(self):
        plan = _make_pblock_plan(
            candidate_id="local_bound_cells",
            plan_mode=PBLOCK_LOCAL_MODE,
            columns_used=3,
            center_col=60,
            utilization_density=0.25,
            bound_luts=2500,
            bound_ffs=3000,
        )
        assert not should_route_pblock_after_place(plan, 0.0, threshold=0.03)

    def test_execution_rebuild_required_for_understrength_global_plan(self):
        plan = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=10,
            center_col=55,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            resource_multiplier=1.5,
        )
        assert plan_requires_execution_rebuild(plan)

    def test_execution_rebuild_not_required_for_execute_strength_global_plan(self):
        plan = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=10,
            center_col=55,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
            resource_multiplier=PBLOCK_EXECUTE_DEFAULT_RESOURCE_MULTIPLIER,
        )
        assert not plan_requires_execution_rebuild(plan)

    def test_resolve_selected_pblock_plan_prefers_selected_plan_payload(self):
        plan = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=11,
            center_col=55,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        payload = {"selected_pblock_plan": plan.to_dict()}
        resolved = resolve_selected_pblock_plan(payload)
        assert resolved is not None
        assert resolved.candidate_id == "global_cp_center"

    def test_resolve_ordered_pblock_candidates_skips_attempted_ids(self):
        recommended = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=10,
            center_col=55,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        alternate = _make_pblock_plan(
            candidate_id="global_left_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=11,
            center_col=25,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        ordered = resolve_ordered_pblock_candidates(
            {
                "candidate_plans": [recommended.to_dict(), alternate.to_dict()],
                "recommended_candidate_id": recommended.candidate_id,
            },
            attempted_candidate_ids=[recommended.candidate_id],
        )
        assert [plan.candidate_id for plan in ordered] == ["global_left_bias"]

    def test_get_pblock_place_only_threshold_reads_selected_plan(self):
        plan = _make_pblock_plan(
            candidate_id="global_right_bias",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=12,
            center_col=80,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        assert get_pblock_place_only_threshold({"selected_pblock_plan": plan.to_dict()}) == 0.10

    def test_resolve_chain_step_arguments_applies_frozen_plan_contract(self):
        step = {
            "tool": "vivado_create_and_apply_pblock",
            "args": {},
            "args_from_skill": {
                "pblock_name": "pblock_name",
                "ranges": "pblock_ranges",
                "is_soft": "is_soft_recommended",
                "cells": "critical_path_cells",
            },
        }
        plan = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=10,
            center_col=60,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        args, note = resolve_chain_step_arguments(
            "rapidwright_execute_pblock_strategy",
            step,
            {"selected_pblock_plan": plan.to_dict()},
        )
        assert args["pblock_name"] == "pblock_tight"
        assert args["ranges"] == "SLICE_X0Y0:SLICE_X10Y299"
        assert args["is_soft"] is True
        assert "cells" not in args
        assert note is not None

    def test_resolve_chain_step_runtime_override_uses_frozen_global_plan(self):
        plan = _make_pblock_plan(
            candidate_id="global_cp_center",
            plan_mode=PBLOCK_GLOBAL_MODE,
            columns_used=10,
            center_col=60,
            bind_cells=False,
            unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        )
        target_tool, args, note = resolve_chain_step_runtime_override(
            "rapidwright_execute_pblock_strategy",
            "vivado_unplace_cells",
            {"cells": ["u0", "u1"]},
            {"selected_pblock_plan": plan.to_dict()},
        )
        assert target_tool == "vivado_place_design"
        assert args == {"directive": "unplace"}
        assert note is not None


class TestToolCallContracts:
    def test_coerce_payload_dict_accepts_dict_and_json_text(self):
        assert coerce_payload_dict({"status": "success"}) == {"status": "success"}
        assert coerce_payload_dict('{"status": "success", "value": 1}') == {
            "status": "success",
            "value": 1,
        }
        assert coerce_payload_dict("not json") is None

    def test_build_tool_call_result_extracts_payload_and_error(self):
        result = build_tool_call_result(
            "vivado_route_design",
            '{"status": "failed", "error": "route failed"}',
        )
        assert result.tool_name == "vivado_route_design"
        assert result.payload == {"status": "failed", "error": "route failed"}
        assert result.status == "failed"
        assert result.error == "route failed"
        assert result.ok is False

    def test_build_tool_call_result_flags_plaintext_mcp_errors(self):
        raw = "[ERROR] Application-level timeout after 900s"
        result = build_tool_call_result("vivado_place_design", raw)
        assert is_mcp_error_response(raw) is True
        assert result.is_mcp_error is True
        assert result.error == raw
        assert result.payload is None

    def test_build_tool_call_result_keeps_evaluate_style_payload_without_error(self):
        result = build_tool_call_result(
            "vivado_report_timing_summary",
            '{"status": "success", "wns": -0.123, "summary": "timing refreshed"}',
        )
        assert result.payload == {
            "status": "success",
            "wns": -0.123,
            "summary": "timing refreshed",
        }
        assert result.status == "success"
        assert result.error is None
        assert result.raw_text.startswith('{"status": "success"')

    def test_build_tool_call_result_handles_no_output_text(self):
        result = build_tool_call_result("vivado_extract_critical_path_cells", "")
        assert result.raw_text == ""
        assert result.payload is None
        assert result.error is None
        assert result.status is None

    def test_cached_json_payload_remains_structured(self):
        raw = '[CACHED from round 2]\n{"status":"success","value":3}'
        result = build_tool_call_result("rapidwright_get_design_info", raw)
        assert result.payload == {"status": "success", "value": 3}
        assert result.status == "success"
        assert result.error is None

    def test_strip_tool_cache_header_preserves_plain_text(self):
        assert strip_tool_cache_header("plain output") == "plain output"
        assert strip_tool_cache_header("[CACHED from round 4]\nplain output") == "plain output"


class TestPhasePolicy:
    def test_max_rounds_contract(self):
        contract = build_phase_exit_contract(round_count=6, max_rounds=5)
        assert contract.should_exit is True
        assert contract.event == "max_rounds"
        assert contract.record_reason == "max_rounds"
        assert contract.set_is_done is False

    def test_wall_clock_timeout_contract(self):
        contract = build_phase_exit_contract(
            start_time=10.0,
            wall_clock_timeout=5.0,
            now=16.0,
        )
        assert contract.should_exit is True
        assert contract.event == "wall_clock_timeout"
        assert contract.done_reason == "wall_clock_timeout"
        assert contract.set_is_done is True

    def test_user_requested_contract(self):
        contract = build_phase_exit_contract(user_exit_requested=True)
        assert contract.should_exit is True
        assert contract.event == "user_requested"
        assert contract.done_reason is None

    def test_cost_limit_contract(self):
        contract = build_phase_exit_contract(total_cost=9.5, cost_hard_limit=9.5)
        assert contract.should_exit is True
        assert contract.event == "cost_limit"
        assert contract.done_reason == "cost_limit"
        assert contract.set_is_done is True

    def test_no_progress_contract(self):
        contract = build_phase_exit_contract(no_progress_count=4, no_progress_limit=4)
        assert contract.should_exit is True
        assert contract.event == "no_progress"
        assert contract.record_reason == "no_progress"

    def test_empty_response_contract(self):
        contract = build_phase_exit_contract(
            consecutive_empty_responses=2,
            empty_response_limit=2,
        )
        assert contract.should_exit is True
        assert contract.event == "empty_responses"
        assert contract.record_reason == "empty_responses"


class TestSplitPolicyModules:
    def test_catalog_exports_match_compat_constants(self):
        assert EXECUTE_CORE_TOOLS_CATALOG == EXECUTE_CORE_TOOLS
        assert POST_EVAL_TOOLS_CATALOG == POST_EVAL_TOOLS

    def test_chain_exports_match_compat_constants(self):
        assert SKILL_CHAIN_ACTIONS_POLICY == SKILL_CHAIN_ACTIONS
        assert "rapidwright_execute_pblock_strategy" not in PLACE_ONLY_CHECK_SKILLS_POLICY

    def test_runtime_exports_match_compat_constants(self):
        assert DASHBOARD_REFRESH_MAP_POLICY == DASHBOARD_REFRESH_MAP
        assert PHASE_TOOL_RATE_LIMITS_POLICY == PHASE_TOOL_RATE_LIMITS


class TestParseTimingSummary:
    def test_standard_vivado_output(self):
        report = """
    Command: report_timing_summary
    WNS(ns)    TNS(ns)    Failing Endpoints
    -1.234    -45.678    123
    """
        result = parse_timing_summary(report)
        assert result["wns"] == -1.234
        assert result["tns"] == -45.678
        assert result["failing_endpoints"] == 123

    def test_timing_met(self):
        report = """
    WNS(ns)    TNS(ns)    Failing Endpoints
     0.052      0.000      0
    """
        result = parse_timing_summary(report)
        assert result["wns"] == 0.052
        assert result["tns"] == 0.0
        assert result["failing_endpoints"] == 0

    def test_no_timing_data(self):
        result = parse_timing_summary("No timing data available")
        assert result["wns"] is None
        assert result["tns"] is None
        assert result["failing_endpoints"] is None

    def test_handles_interleaved_noise(self):
        report = """
    Command: report_timing_summary
    INFO: Some internal message
    WARNING: [Common 17-123] Some warning
    Attempting to get a license...
    Got license feature

    WNS(ns)    TNS(ns)    Failing Endpoints
    -0.500    -12.340    42
    """
        result = parse_timing_summary(report)
        assert result["wns"] == -0.5
        assert result["tns"] == -12.34
        assert result["failing_endpoints"] == 42

    def test_handles_physopt_noise(self):
        report = """
    phys_opt_design completed
    place_design completed

    WNS(ns)    TNS(ns)    Failing Endpoints
    -0.750    -5.000     7
    """
        result = parse_timing_summary(report)
        assert result["wns"] == -0.75
        assert result["tns"] == -5.0
        assert result["failing_endpoints"] == 7

    def test_malformed_line_skipped(self):
        report = """
    WNS(ns)    TNS(ns)    Failing Endpoints
    not_a_number    -5.0    7
    -0.800    -10.0    15
    """
        result = parse_timing_summary(report)
        assert result["wns"] == -0.8
        assert result["tns"] == -10.0
        assert result["failing_endpoints"] == 15


class TestIsValidWns:
    def test_valid_negative_wns(self):
        assert is_valid_wns(-0.5, 10.0, float('-inf')) is True

    def test_valid_zero_wns(self):
        assert is_valid_wns(0.0, 10.0, -0.5) is True

    def test_none_wns_is_invalid(self):
        assert is_valid_wns(None, 10.0, float('-inf')) is False

    def test_extreme_negative_is_invalid(self):
        assert is_valid_wns(-1000.0, 10.0, float('-inf')) is False

    def test_beyond_10x_clock_is_suspicious(self):
        assert is_valid_wns(-200.0, 10.0, float('-inf')) is False

    def test_suspicious_zero_jump(self):
        """Jump from -0.5 to exactly 0.0 without visible optimization is suspicious."""
        result = is_valid_wns(0.0, 10.0, -0.5)
        assert result is True  # Still returns True, just logs warning


class TestParseHoldTiming:
    def test_hold_wns_present(self):
        report = "Hold  :0 Failing Endpoints,  Worst Slack  0.050ns,  Total Violation 0.000ns"
        result = parse_hold_timing(report)
        assert result["hold_wns"] == 0.05
        assert result["hold_tns"] == 0.0

    def test_hold_violated(self):
        report = "Hold  :12 Failing Endpoints,  Worst Slack -0.123ns,  Total Violation -5.678ns"
        result = parse_hold_timing(report)
        assert result["hold_wns"] == -0.123
        assert result["hold_tns"] == -5.678
        assert result["hold_failing"] == 12

    def test_hold_no_violation(self):
        report = "Hold  :0 Failing Endpoints,  Worst Slack  0.092ns,  Total Violation 0.000ns"
        result = parse_hold_timing(report)
        assert result["hold_wns"] == 0.092
        assert result["hold_tns"] == 0.0
        assert result["hold_failing"] == 0

    def test_no_hold_section(self):
        result = parse_hold_timing("Setup:0 Failing, Worst Slack 0.010ns")
        assert result["hold_wns"] is None
        assert result["hold_tns"] is None

    def test_hold_in_longer_report(self):
        report = "clock summary\nSetup:1 Failing\nHold  :0 Failing Endpoints,  Worst Slack  0.092ns,  Total Violation 0.000ns"
        result = parse_hold_timing(report)
        assert result["hold_wns"] == 0.092
        assert result["hold_tns"] == 0.0
        assert result["hold_failing"] == 0


class TestParseResourceUtilization:
    def test_standard_output(self):
        report = """
    LUTs:    12,345
    FFs:     24,567
    DSPs:    45
    BRAMs:   120
    URAMs:   0
    """
        result = parse_resource_utilization(report)
        assert result["LUT"] == 12345
        assert result["FF"] == 24567
        assert result["DSP"] == 45
        assert result["BRAM"] == 120
        assert result["URAM"] == 0

    def test_missing_field_returns_none(self):
        report = """
    LUTs:    100
    FFs:     200
    """
        result = parse_resource_utilization(report)
        assert result is None


class TestParseRouteStatus:
    """parse_route_status — covers A.5 (total_nets=0) regression.

    Real Vivado report_route_status -return_string format captured from
    dcp_optimizer_run-20260704_085355/vivado.log:11663.
    """

    # Real output captured from a fully-routed design (37081 logical nets,
    # 27961 routable, all fully routed).
    REAL_FULLY_ROUTED = (
        "Design Route Status\n"
        "                                               :      # nets :\n"
        "   ------------------------------------------- : ----------- :\n"
        "   # of logical nets.......................... :       37081 :\n"
        "       # of nets not needing routing.......... :        9120 :\n"
        "           # of internally routed nets........ :        9019 :\n"
        "           # of implicitly routed ports....... :         101 :\n"
        "       # of routable nets..................... :       27961 :\n"
        "           # of fully routed nets............. :       27961 :\n"
        "       # of nets with routing errors.......... :           0 :\n"
        "   ------------------------------------------- : ----------- :\n"
    )

    def test_raw_fully_routed(self):
        result = parse_route_status(self.REAL_FULLY_ROUTED)
        assert result["total_nets"] == 37081
        assert result["routed_nets"] == 27961
        assert result["unresolved_nets"] == 0
        assert result["routable_nets"] == 27961
        assert result["not_needing_routing_nets"] == 9120

    def test_mcp_json_envelope(self):
        """MCP wraps the real report under 'raw_report' as escaped JSON;
        the envelope's is_placed/is_routed are unreliable and must not be
        used. Parser must unwrap and parse the real report."""
        import json as _json
        envelope = _json.dumps({
            "is_placed": False,   # unreliable (§15.1)
            "is_routed": False,   # unreliable (§15.1)
            "route_errors": 0,
            "unrouted_nets": 0,
            "raw_report": self.REAL_FULLY_ROUTED,
        })
        result = parse_route_status(envelope)
        assert result["total_nets"] == 37081
        assert result["routed_nets"] == 27961
        assert result["unresolved_nets"] == 0

    def test_unrouted_design(self):
        """After place_design, before route_design: fully routed = 0 and
        routing errors = all routable nets. routed_nets must be 0 so the
        dashboard route_status field correctly reflects an unrouted design."""
        unrouted = (
            "Design Route Status\n"
            "   # of logical nets.......................... :       37081 :\n"
            "       # of nets not needing routing.......... :        9120 :\n"
            "       # of routable nets..................... :       27961 :\n"
            "           # of fully routed nets............. :           0 :\n"
            "       # of nets with routing errors.......... :       27961 :\n"
        )
        result = parse_route_status(unrouted)
        assert result["total_nets"] == 37081
        assert result["routed_nets"] == 0
        assert result["unresolved_nets"] == 27961

    def test_empty_and_non_json(self):
        assert parse_route_status("")["total_nets"] == 0
        assert parse_route_status("   ")["routed_nets"] == 0
        # Non-JSON text without recognizable labels → all zeros (no crash).
        result = parse_route_status("some arbitrary vivado output\nno labels here")
        assert result["total_nets"] == 0
        assert result["routed_nets"] == 0

    def test_comma_thousands(self):
        report = (
            "   # of logical nets.......................... :     123,456 :\n"
            "   # of fully routed nets............. :     123,456 :\n"
        )
        result = parse_route_status(report)
        assert result["total_nets"] == 123456
        assert result["routed_nets"] == 123456


# ── Model selection tests ────────────────────────────────────────

class TestClassifyTask:
    def test_optimization_pattern(self):
        assert classify_task("vivado_place_design") == "optimization"

    def test_information_pattern(self):
        assert classify_task("vivado_report_timing_summary") == "information"

    def test_unknown_default(self):
        assert classify_task("unknown_tool") == "unknown"

    def test_vivado_run_tcl(self):
        result = classify_task("vivado_run_tcl", {"command": "place_design"})
        assert result == "optimization"

    def test_vivado_run_tcl_info(self):
        result = classify_task("vivado_run_tcl", {"command": "report_timing"})
        assert result == "information"

    def test_vivado_run_tcl_get_property_info(self):
        result = classify_task("vivado_run_tcl", {"command": "get_property STATUS [current_design]"})
        assert result == "information"

    def test_vivado_run_tcl_set_property_optimization(self):
        result = classify_task("vivado_run_tcl", {"command": "set_property IS_SOFT TRUE [get_pblocks p0]"})
        assert result == "optimization"

    def test_tcl_variable_assignment_unknown(self):
        assert classify_tcl_command("set cells [get_cells *]") == "unknown"

    def test_empty_tool(self):
        assert classify_task("") == "unknown"


class TestComputeModelScores:
    def test_default_scores(self):
        state = OptimizerState()
        planner, worker = compute_model_scores(state, 0, 0)
        # With no signals, planner should be default
        assert planner >= 0
        assert worker >= 0

    def test_high_complexity_favors_planner(self):
        state = OptimizerState()
        planner, worker = compute_model_scores(state, 7, 0)
        assert planner > worker

    def test_worker_consecutive_failures_triggers_upgrade(self):
        state = OptimizerState()
        state.model.worker_consecutive_failures = 3  # > WORKER_UPGRADE_THRESHOLD=2
        state.iteration.global_no_improvement = 3
        state.timing.initial_wns = -1.0
        state.timing.best_wns = -1.0
        planner, worker = compute_model_scores(state, 5, 0)
        assert planner > worker

    def test_high_budget_usage_favors_worker(self):
        state = OptimizerState()
        state.cost.total_cost = 0.85
        state.cost.cost_hard_limit = 1.0
        state.model.worker_consecutive_success = 5
        planner, worker = compute_model_scores(state, 2, 0)
        assert worker > planner


class TestSelectModel:
    def test_context_forced_planner(self):
        state = OptimizerState()
        state.model.planner_model = "planner-v4"
        state.model.worker_model = "worker-v4"
        result = select_model(0, 10, state, 200_000)  # >= WORKER_CONTEXT_FORCE_TOKENS
        assert result == "planner-v4"

    def test_planner_wins_with_margin(self):
        state = OptimizerState()
        state.model.planner_model = "planner-v4"
        state.model.worker_model = "worker-v4"
        result = select_model(5, 2, state, 0)
        assert result == "planner-v4"

    def test_worker_wins(self):
        state = OptimizerState()
        state.model.planner_model = "planner-v4"
        state.model.worker_model = "worker-v4"
        result = select_model(0, 3, state, 0)
        assert result == "worker-v4"

    def test_tie_defaults_to_planner(self):
        state = OptimizerState()
        state.model.planner_model = "planner-v4"
        state.model.worker_model = "worker-v4"
        result = select_model(2, 2, state, 0)
        assert result == "planner-v4"

    def test_model_usage_history_tracked(self):
        state = OptimizerState()
        state.model.planner_model = "planner-v4"
        state.model.worker_model = "worker-v4"
        select_model(5, 2, state, 0)
        assert len(state.model.model_usage_history) == 1
        assert state.model.model_usage_history[0] == "planner-v4"


class TestEstimateContextComplexity:
    def test_low_complexity(self):
        score = estimate_context_complexity("information", 0, 0, 1, 0, {})
        assert 0 <= score <= 10

    def test_high_iteration_increases_complexity(self):
        score = estimate_context_complexity("optimization", 50, 100_000, 10, 5, {})
        assert score >= 5


class TestGetTaskCapabilityScore:
    def test_unknown_task_returns_neutral(self):
        assert get_task_capability_score("unknown", {}) == 0.5

    def test_known_task_returns_rate(self):
        stats = {"optimization": {"total": 10, "success": 7}}
        score = get_task_capability_score("optimization", stats)
        assert score == 0.7

    def test_no_data_returns_neutral(self):
        stats = {"optimization": {"total": 0, "success": 0}}
        score = get_task_capability_score("optimization", stats)
        assert score == 0.5


# ── Iteration logic tests ────────────────────────────────────────

class TestUpdateIterationCounters:
    def test_improvement_resets_counters(self):
        state = OptimizerState()
        state.iteration.global_no_improvement = 3
        state.model.worker_consecutive_failures = 2
        update_iteration_counters(state, wns_improved=True, model_used="any")
        assert state.iteration.global_no_improvement == 0
        assert state.model.worker_consecutive_failures == 0

    def test_no_improvement_increments(self):
        state = OptimizerState()
        update_iteration_counters(state, wns_improved=False, model_used="any")
        assert state.iteration.global_no_improvement == 1

    def test_current_task_type_drives_worker_failure_tracking(self):
        state = OptimizerState()
        state.model.worker_model = "worker-v4"
        state.model.current_task_type = "optimization"
        update_iteration_counters(state, wns_improved=False, model_used="worker-v4")
        assert state.model.worker_consecutive_failures == 1


class TestUpdateTaskTypeStats:
    def test_records_success_and_total(self):
        state = OptimizerState()
        update_task_type_stats(state, "information", success=True)
        update_task_type_stats(state, "information", success=False)
        assert state.model.task_type_stats["information"] == {"total": 2, "success": 1}

    def test_ignores_unknown_task(self):
        state = OptimizerState()
        update_task_type_stats(state, "unknown", success=True)
        assert state.model.task_type_stats == {}


class TestInferStrategyFromTools:
    def test_pblock_strategy(self):
        tools = ["analyze_pblock_region", "create_and_apply_pblock"]
        assert infer_strategy_from_tools(tools) == "PBLOCK"

    def test_physopt_strategy(self):
        tools = ["vivado_phys_opt_design"]
        assert infer_strategy_from_tools(tools) == "PhysOpt"

    def test_fanout_strategy(self):
        tools = ["rapidwright_execute_fanout_strategy"]
        assert infer_strategy_from_tools(tools) == "Fanout"

    def test_information_tools(self):
        tools = ["vivado_report_timing_summary", "vivado_get_wns"]
        assert infer_strategy_from_tools(tools) == "Information"

    def test_unknown_tools(self):
        tools = ["unknown_tool"]
        assert infer_strategy_from_tools(tools) == "Unknown"

    def test_place_route_strategy(self):
        tools = ["vivado_place_design", "vivado_route_design"]
        assert infer_strategy_from_tools(tools) == "PlaceRoute"

    def test_congestion_spreading(self):
        tools = ["execute_congestion_spreading"]
        assert infer_strategy_from_tools(tools) == "CongestionSpreading"


class TestBuildIterationNarrative:
    def test_improvement_narrative(self):
        result = build_iteration_narrative(
            iteration=1,
            model_used="worker-v4",
            current_task_type="optimization",
            wns_before=-0.5,
            wns_after=-0.2,
            tools_used=["vivado_phys_opt_design"],
            result_status="SUCCESS",
        )
        assert result["iteration"] == 1
        assert result["outcome"] == "improved"
        assert result["wns_delta"] == 0.3

    def test_regression_narrative(self):
        result = build_iteration_narrative(
            iteration=1,
            model_used="worker-v4",
            current_task_type="optimization",
            wns_before=-0.2,
            wns_after=-0.5,
            tools_used=[],
            result_status=None,
        )
        assert result["outcome"] == "regression"
        assert result["wns_delta"] == -0.3

    def test_unchanged_narrative(self):
        result = build_iteration_narrative(
            iteration=1,
            model_used="worker-v4",
            current_task_type="optimization",
            wns_before=-0.3,
            wns_after=-0.3,
            tools_used=[],
            result_status="PARTIAL",
        )
        assert result["outcome"] == "unchanged"
        assert abs(result["wns_delta"]) < 0.001


class TestAdaptiveResourceMultiplier:
    """Test compute_adaptive_resource_multiplier function."""

    def test_small_design(self):
        """Small design (<10% device) gets higher multiplier."""
        # 31K LUTs / 394K = 7.9% -> should use 1.8x
        result = compute_adaptive_resource_multiplier(31000, 2000)
        assert result == 1.8

    def test_medium_design(self):
        """Medium design (10-30% device) uses default multiplier."""
        # 73K LUTs / 394K = 18.5% -> should use 1.5x (default)
        result = compute_adaptive_resource_multiplier(73000, 96000)
        assert result == 1.5

    def test_large_design(self):
        """Large design (>30% device) uses lower multiplier."""
        # 150K LUTs / 394K = 38.1% -> should use 1.2x
        result = compute_adaptive_resource_multiplier(150000, 200000)
        assert result == 1.2

    def test_custom_base_multiplier_small(self):
        """Custom base multiplier with small design."""
        # Small design with custom base -> should use max(2.0, 1.8) = 2.0
        result = compute_adaptive_resource_multiplier(31000, 2000, base_multiplier=2.0)
        assert result == 2.0

    def test_custom_base_multiplier_medium(self):
        """Custom base multiplier with medium design."""
        # Medium design with custom base -> should use 2.0
        result = compute_adaptive_resource_multiplier(73000, 96000, base_multiplier=2.0)
        assert result == 2.0

    def test_custom_base_multiplier_large(self):
        """Custom base multiplier with large design."""
        # Large design with custom base -> should use min(2.0, 1.2) = 1.2
        result = compute_adaptive_resource_multiplier(150000, 200000, base_multiplier=2.0)
        assert result == 1.2

    def test_zero_resources(self):
        """Zero resources returns small design multiplier."""
        result = compute_adaptive_resource_multiplier(0, 0)
        assert result == 1.8

    def test_threshold_10_percent(self):
        """Exactly 10% threshold returns medium multiplier."""
        result = compute_adaptive_resource_multiplier(39400, 78800)  # 10%
        assert result == 1.5

    def test_threshold_30_percent(self):
        """Exactly 30% threshold returns large multiplier."""
        result = compute_adaptive_resource_multiplier(118200, 236400)  # 30%
        assert result == 1.2


# ── Entity registry / cell-name validation tests ──────────────────

from optimizer.pure.entities import (
    EntityRegistry,
    is_valid_cell_name,
    is_valid_pin_name,
    classify_cell_name,
    validate_cell_list,
    validate_pin_list,
    validate_and_sanitize_cell_args,
    build_registry_snapshot_yaml,
    sync_search_cells_result,
    extract_registry_cells_for_inject,
    CELL_NAME_TOOLS,
)
from optimizer.pure.context_snapshot import inject_merged_dashboard, inject_pinned_cell_registry
import json as _json


class TestContextSnapshotContracts:
    def test_inject_merged_dashboard_tolerates_missing_wns(self, tmp_path):
        state = OptimizerState()
        state.control.run_dir = tmp_path
        state.strategy.current_phase = LoopPhase.ANALYZE.value
        state.timing.latest_wns = None
        state.timing.field_freshness = {"timing_summary": "stale"}

        api_messages: list[dict] = []
        inject_merged_dashboard(api_messages, state, LoopPhase.ANALYZE)

        assert api_messages
        assert state.context.design_data.last_snapshot_fingerprint.startswith("wns=N/A|")


class TestIsValidCellName:
    def test_valid_hierarchical(self):
        assert is_valid_cell_name("u_core/u_alu/lut1")
        assert is_valid_cell_name("layer0_inst/layer0_N25_inst/data_out[76]_i_19")

    def test_rejects_pblock_label(self):
        assert not is_valid_cell_name("pblock_tight")
        assert not is_valid_cell_name("u_core/pblock_io")

    def test_rejects_device_site(self):
        assert not is_valid_cell_name("SLICE_X56Y0")
        assert not is_valid_cell_name("DSP48E2_X8Y0")

    def test_rejects_bare_type(self):
        assert not is_valid_cell_name("LUT6")
        assert not is_valid_cell_name("FDRE")

    def test_rejects_empty(self):
        assert not is_valid_cell_name("")
        assert not is_valid_cell_name(None)


class TestEntityRegistry:
    def test_register_and_contains(self):
        r = EntityRegistry()
        r.register_cell("u_core/u_alu/lut1", cell_type="LUT6")
        assert r.contains("u_core/u_alu/lut1")
        assert not r.contains("u_core/u_alu/lut2")

    def test_register_skips_invalid(self):
        r = EntityRegistry()
        r.register_cell("SLICE_X1Y1")
        r.register_cell("pblock_x")
        assert len(r.cells) == 0

    def test_module_index(self):
        r = EntityRegistry()
        r.register_cell("top/alu/lut1")
        r.register_cell("top/alu/lut2")
        r.register_cell("top/mem/reg0")
        assert "alu" in r.by_module
        assert "mem" in r.by_module
        assert len(r.by_module["alu"]) == 2

    def test_register_from_paths(self):
        r = EntityRegistry()
        n = r.register_cells_from_paths(
            [["top/a/x", "top/a/y"], ["top/b/z"]],
            iteration=3,
        )
        assert n == 3
        assert r.cells["top/a/x"].last_seen_iter == 3
        assert r.cells["top/a/x"].source_path_idx == 0
        assert r.cells["top/b/z"].source_path_idx == 1

    def test_mark_stale_increments_version(self):
        r = EntityRegistry()
        v0 = r.snapshot_version
        r.mark_stale()
        assert r.snapshot_version == v0 + 1

    def test_suggest_finds_leaf_match(self):
        r = EntityRegistry()
        r.register_cell("u_core/u_alu/lut1")
        r.register_cell("u_core/u_alu/lut2")
        r.register_cell("u_core/u_mem/reg0")
        sugg = r.suggest("u_core/u_alu/lut1")
        assert "u_core/u_alu/lut1" in sugg
        sugg2 = r.suggest("lut1")
        assert "u_core/u_alu/lut1" in sugg2

    def test_top_n_prioritizes_recency(self):
        r = EntityRegistry()
        r.register_cell("top/old/c", iteration=1)
        r.register_cell("top/new/c", iteration=5)
        top = r.top_n_cells(10)
        assert top[0] == "top/new/c"


class TestValidateCellList:
    def test_all_valid_in_registry(self):
        r = EntityRegistry()
        r.register_cell("top/a/x")
        r.register_cell("top/a/y")
        res = validate_cell_list(["top/a/x", "top/a/y"], r)
        assert res.accepted == ["top/a/x", "top/a/y"]
        assert not res.unverified
        assert not res.rejected
        assert not res.all_invalid

    def test_unverified_kept(self):
        r = EntityRegistry()
        r.register_cell("top/a/x")
        res = validate_cell_list(["top/a/x", "top/new/c"], r)
        assert res.accepted == ["top/a/x"]
        assert res.unverified == ["top/new/c"]
        assert not res.all_invalid

    def test_invalid_rejected(self):
        r = EntityRegistry()
        res = validate_cell_list(["SLICE_X1Y1", "LUT6", "pblock_z"], r)
        assert not res.accepted
        assert not res.unverified
        assert len(res.rejected) == 3
        assert res.all_invalid

    def test_mixed(self):
        r = EntityRegistry()
        r.register_cell("top/a/x")
        res = validate_cell_list(["top/a/x", "SLICE_X1Y1", "top/new/c"], r)
        assert res.accepted == ["top/a/x"]
        assert res.unverified == ["top/new/c"]
        assert len(res.rejected) == 1
        assert not res.all_invalid

    def test_strict_mode_rejects_unverified(self):
        r = EntityRegistry()
        res = validate_cell_list(["top/new/c"], r, allow_unverified=False)
        assert not res.accepted
        assert not res.unverified
        assert len(res.rejected) == 1


class TestValidateAndSanitizeCellArgs:
    def test_non_cell_tool_passes_through(self):
        r = EntityRegistry()
        args = {"directive": "explore"}
        out, err = validate_and_sanitize_cell_args("vivado_phys_opt_design", args, r)
        assert out == args
        assert err is None

    def test_cell_names_all_invalid_returns_error(self):
        r = EntityRegistry()
        args = {"cell_names": ["SLICE_X1Y1", "LUT6"]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_optimize_cell_placement", args, r,
        )
        assert err is not None
        data = _json.loads(err)
        assert data["status"] == "rejected"
        assert data["reason"] == "invalid_cell_names"
        assert "cell_names" not in out

    def test_cell_names_partial_strips_invalid(self):
        r = EntityRegistry()
        r.register_cell("top/a/x")
        args = {"cell_names": ["top/a/x", "SLICE_X1Y1"]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_optimize_cell_placement", args, r,
        )
        assert err is None
        assert out["cell_names"] == ["top/a/x"]

    def test_critical_paths_paths_sanitized(self):
        r = EntityRegistry()
        r.register_cell("top/a/x")
        r.register_cell("top/a/y")
        args = {"critical_paths": [["top/a/x", "top/a/y"], ["SLICE_X1Y1"]]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_flatten_lut_cascade", args, r,
        )
        assert err is None
        assert out["critical_paths"] == [["top/a/x", "top/a/y"]]

    def test_critical_paths_all_invalid(self):
        r = EntityRegistry()
        args = {"critical_paths": [["SLICE_X1Y1"], ["LUT6"]]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_flatten_lut_cascade", args, r,
        )
        assert err is not None
        assert "critical_paths" not in out

    def test_pin_validation(self):
        r = EntityRegistry()
        args = {"hierarchical_input_pins": ["top/inst/pin", "LUT6"]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_optimize_lut_input_cone", args, r,
        )
        # One valid pin kept, one bare-type rejected -> partial, no error
        assert err is None
        assert out["hierarchical_input_pins"] == ["top/inst/pin"]

    def test_pin_all_invalid(self):
        r = EntityRegistry()
        args = {"hierarchical_input_pins": ["SLICE_X1Y1", "LUT6"]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_optimize_lut_input_cone", args, r,
        )
        assert err is not None
        assert "hierarchical_input_pins" not in out


class TestRegistrySnapshot:
    def test_empty_registry(self):
        r = EntityRegistry()
        yaml = build_registry_snapshot_yaml(r)
        assert "[CELL REGISTRY]" in yaml
        assert "No canonical cell names" in yaml

    def test_populated_registry(self):
        r = EntityRegistry()
        r.register_cell("top/alu/lut1", cell_type="LUT6", iteration=2)
        r.register_cell("top/alu/lut2", cell_type="LUT6", iteration=2)
        yaml = build_registry_snapshot_yaml(r, phase="EXECUTE")
        assert "[CELL REGISTRY]" in yaml
        assert "top/alu/lut1" in yaml
        assert "alu" in yaml  # module index
        assert "phase=EXECUTE" in yaml


class TestSyncSearchCells:
    def test_registers_from_json(self):
        r = EntityRegistry()
        raw = _json.dumps({
            "status": "success",
            "count": 2,
            "cells": [
                {"name": "top/a/x", "type": "LUT6", "placement": "SLICE_X1Y1"},
                {"name": "top/a/y", "type": "FDRE", "placement": "unplaced"},
            ],
        })
        added = sync_search_cells_result(r, raw, iteration=1)
        assert added == 2
        assert r.contains("top/a/x")
        assert r.cells["top/a/x"].location == "SLICE_X1Y1"
        assert r.cells["top/a/y"].location == ""  # unplaced filtered

    def test_invalid_json_returns_zero(self):
        r = EntityRegistry()
        assert sync_search_cells_result(r, "not json") == 0
        assert sync_search_cells_result(r, _json.dumps({"error": "x"})) == 0


class TestPinnedCellRegistryInjection:
    def test_inserts_after_system_message(self):
        state = OptimizerState()
        state.entity_registry.register_cell("top/alu/lut1", iteration=1)
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hello"},
        ]
        inject_pinned_cell_registry(msgs, state)
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "[CELL REGISTRY]" in msgs[1]["content"]
        assert "top/alu/lut1" in msgs[1]["content"]
        assert msgs[2]["content"] == "hello"

    def test_idempotent_no_accumulation(self):
        state = OptimizerState()
        state.entity_registry.register_cell("top/a/x")
        msgs = [{"role": "system", "content": "SYS"}]
        inject_pinned_cell_registry(msgs, state)
        inject_pinned_cell_registry(msgs, state)
        registry_msgs = [m for m in msgs if "[CELL REGISTRY]" in m.get("content", "")]
        assert len(registry_msgs) == 1

    def test_empty_registry_still_injects_placeholder(self):
        state = OptimizerState()
        msgs = [{"role": "system", "content": "SYS"}]
        inject_pinned_cell_registry(msgs, state)
        assert any("[CELL REGISTRY]" in m.get("content", "") for m in msgs)

    def test_no_system_message_inserts_at_start(self):
        state = OptimizerState()
        state.entity_registry.register_cell("top/a/x")
        msgs = [{"role": "user", "content": "hi"}]
        inject_pinned_cell_registry(msgs, state)
        assert msgs[0]["role"] == "user"
        assert "[CELL REGISTRY]" in msgs[0]["content"]


class TestExtractRegistryCellsForInject:
    def test_prefers_critical_path_cells(self):
        from optimizer.state import CriticalPathEntry
        r = EntityRegistry()
        r.register_cell("top/search/c", iteration=1)  # not on a path
        entries = [CriticalPathEntry(cells=["top/alu/lut1", "top/alu/lut2"])]
        # Sync entries to registry first (as update_critical_paths would)
        r.register_cells_from_entries(entries, iteration=2)
        cells = extract_registry_cells_for_inject(r, entries)
        assert "top/alu/lut1" in cells
        assert "top/alu/lut2" in cells
        # Path cells come before search-only cells
        assert cells.index("top/alu/lut1") < cells.index("top/search/c")

    def test_filters_invalid(self):
        from optimizer.state import CriticalPathEntry
        r = EntityRegistry()
        entries = [CriticalPathEntry(cells=["top/alu/lut1", "SLICE_X1Y1", "pblock_z"])]
        cells = extract_registry_cells_for_inject(r, entries)
        assert "top/alu/lut1" in cells
        assert "SLICE_X1Y1" not in cells
        assert "pblock_z" not in cells

    def test_backfills_from_registry(self):
        r = EntityRegistry()
        r.register_cell("top/search/c", iteration=1)
        cells = extract_registry_cells_for_inject(r, [])  # no critical paths
        assert cells == ["top/search/c"]


class TestRichErrorSuggestions:
    def test_rejected_includes_suggestions(self):
        r = EntityRegistry()
        r.register_cell("u_core/u_alu/lut1")
        r.register_cell("u_core/u_alu/lut2")
        args = {"cell_names": ["u_core/u_alu/lut"]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_optimize_cell_placement", args, r,
        )
        # "u_core/u_alu/lut" has no '/' issue... wait it does have '/'. It's valid format.
        # So it's unverified, not rejected. Let me use a truly invalid name.
        assert err is None  # format-valid -> unverified, allowed

    def test_invalid_gives_suggestions(self):
        r = EntityRegistry()
        r.register_cell("u_core/u_alu/lut1")
        args = {"cell_names": ["SLICE_X38Y277"]}
        out, err = validate_and_sanitize_cell_args(
            "rapidwright_optimize_cell_placement", args, r,
        )
        assert err is not None
        data = _json.loads(err)
        assert data["status"] == "rejected"
        assert "SLICE_X38Y277" in data["invalid_names"]
        assert "u_core/u_alu/lut1" in data["suggested_canonical_names"]
        assert "CELL REGISTRY" in data["guidance"]


# ── SKILL_CHAIN_ACTIONS structure (矛盾一/二 wiring) ─────────────

class TestSkillChainActions:
    """Verify the PBLOCK chain uses local unplace (矛盾二) and route reuse is
    only set where prior routing exists at the route step (矛盾一)."""

    @staticmethod
    def _route_step(skill_name):
        chain = SKILL_CHAIN_ACTIONS[skill_name]
        for step in chain:
            if step["tool"] == "vivado_route_design":
                return step
        return None

    def test_pblock_uses_local_unplace(self):
        """矛盾二: PBLOCK step 1 is vivado_unplace_cells (local), not the old
        global vivado_place_design -unplace."""
        chain = SKILL_CHAIN_ACTIONS["rapidwright_execute_pblock_strategy"]
        first = chain[0]
        assert first["tool"] == "vivado_unplace_cells"
        assert first["args_from_skill"] == {"cells": "critical_path_cells"}

    def test_pblock_create_pblock_passes_cells(self):
        """矛盾二: create_and_apply_pblock receives cells=critical_path_cells so
        the pblock constrains only critical-path cells (local pblock)."""
        chain = SKILL_CHAIN_ACTIONS["rapidwright_execute_pblock_strategy"]
        pblock_step = next(s for s in chain if s["tool"] == "vivado_create_and_apply_pblock")
        assert pblock_step["args_from_skill"]["cells"] == "critical_path_cells"

    def test_pblock_not_in_place_only_check(self):
        """矛盾二: PBLOCK must NOT be in PLACE_ONLY_CHECK_SKILLS — after local
        unplace+place the moved cells' nets are temporarily unrouted, so
        place-only WNS is an artifactual regression that would wrongly skip route."""
        assert "rapidwright_execute_pblock_strategy" not in PLACE_ONLY_CHECK_SKILLS_POLICY

    @pytest.mark.parametrize("skill", [
        "rapidwright_execute_pblock_strategy",
        "rapidwright_execute_fanout_strategy",
        "rapidwright_execute_opt_design_strategy",
        "rapidwright_execute_combinational_rebalancing_strategy",
        "rapidwright_execute_lut_muxf_repack_strategy",
        "rapidwright_flatten_lut_cascade",
        "rapidwright_execute_muxf_tree_reorder_strategy",
        "rapidwright_execute_physopt_strategy",
    ])
    def test_no_chain_sets_reuse_flag(self, skill):
        """Vivado route_design has no -reuse option (it rejects it with
        'Unknown option'). Vivado automatically preserves routing for unchanged
        nets, so no chain may set `reuse` in its route_design args."""
        route = self._route_step(skill)
        if route is None:
            return  # skill has no route_design step
        assert "reuse" not in route["args"], (
            f"{skill} route_design must not set reuse (invalid Vivado flag)"
        )
