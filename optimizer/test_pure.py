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
)
from optimizer.pure.model_select import (
    classify_task,
    compute_model_scores,
    select_model,
    estimate_context_complexity,
    get_task_capability_score,
)
from optimizer.pure.iteration_logic import (
    update_iteration_counters,
    infer_strategy_from_tools,
    build_iteration_narrative,
)


# ── Timing tests ─────────────────────────────────────────────────

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
        report = """
    Setup path
    WNS(ns)    TNS(ns)    Failing Endpoints
    -1.000    -5.000    10

    Hold path WNS(ns)    THS(ns)    Failing Endpoints
     0.050      0.000      0
    """
        result = parse_hold_timing(report)
        assert result["hold_wns"] == 0.05
        assert result["hold_tns"] == 0.0

    def test_hold_violated(self):
        report = """
    Hold path WNS(ns)    THS(ns)    Failing Endpoints
    -0.123     -5.678    12
    """
        result = parse_hold_timing(report)
        assert result["hold_wns"] == -0.123
        assert result["hold_tns"] == -5.678

    def test_hold_negative_value_parsed(self):
        """Negative hold WNS should be parsed correctly (not skipped by separator check)."""
        report = """
    --- Hold path ---
    Hold path WNS(ns)    THS(ns)    Failing Endpoints
    -0.999     -10.000    5
    """
        result = parse_hold_timing(report)
        assert result["hold_wns"] == -0.999
        assert result["hold_tns"] == -10.0

    def test_no_hold_section(self):
        result = parse_hold_timing("Setup paths all met")
        assert result["hold_wns"] is None
        assert result["hold_tns"] is None


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
        """Tool name 'report' matches, but vivado_run_tcl doesn't contain 'report'.
        INFO patterns are only checked against tool_name, not TCL args.
        """
        result = classify_task("vivado_run_tcl", {"command": "report_timing"})
        assert result == "unknown"

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
        result = select_model(0, 10, state, 160_000)  # >= WORKER_CONTEXT_FORCE_TOKENS
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