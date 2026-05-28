"""Tests for optimizer/pure/state_space.py — StateSpace builder + LLM formatter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from optimizer.state import (
    CriticalPathEntry,
    OptimizerState,
    StateSpace,
)
from optimizer.pure.state_space import (
    build_state_space,
    format_state_space_for_llm,
)
from optimizer.pure.tool_filter import LoopPhase


# ── Helpers ─────────────────────────────────────────────────────────────

def make_state(**overrides) -> OptimizerState:
    """Create an OptimizerState with sensible defaults for testing."""
    return OptimizerState(**overrides)


# ── build_state_space: Module 1 — Global State ──────────────────────────

class TestBuildGlobalState:
    def test_default_state_returns_defaults(self):
        """Minimal state produces default/None StateSpace fields."""
        state = make_state()
        space = build_state_space(state)
        assert space.global_state.current_stage == "PLACEMENT"
        assert space.global_state.iteration_count == 0
        assert space.global_state.wns_setup is None
        assert space.global_state.lut_utilization is None

    def test_wns_and_tns_flow_through(self):
        state = make_state()
        state.timing.latest_wns = -0.523
        state.timing.latest_tns = -12.34
        space = build_state_space(state)
        assert space.global_state.wns_setup == -0.523
        assert space.global_state.tns_setup == -12.34

    def test_utilization_percentages(self):
        state = make_state()
        state.timing.resource_utilization = {"LUT": 24000, "FF": 15000, "BRAM": 120, "DSP": 36}
        state.timing.device_capacity = {"LUT": 48000, "FF": 30000, "BRAM": 240, "DSP": 72}
        space = build_state_space(state)
        assert space.global_state.lut_utilization == 0.5  # 24000/48000
        assert space.global_state.ff_utilization == 0.5   # 15000/30000
        assert space.global_state.bram_utilization == 0.5  # 120/240
        assert space.global_state.dsp_utilization == 0.5   # 36/72

    def test_utilization_capacity_zero_returns_none(self):
        """When capacity is 0, utilization should be None (avoid division by zero)."""
        state = make_state()
        state.timing.resource_utilization = {"LUT": 100}
        state.timing.device_capacity = {"LUT": 0}
        space = build_state_space(state)
        assert space.global_state.lut_utilization is None

    def test_utilization_no_capacity_returns_none(self):
        state = make_state()
        state.timing.resource_utilization = {"LUT": 100}
        state.timing.device_capacity = {}
        space = build_state_space(state)
        assert space.global_state.lut_utilization is None

    def test_target_frequency_from_clock_period(self):
        state = make_state()
        state.timing.clock_period = 3.333  # 300 MHz
        space = build_state_space(state)
        assert space.global_state.target_frequency == 300.0

    def test_target_frequency_zero_period(self):
        state = make_state()
        state.timing.clock_period = 0
        space = build_state_space(state)
        assert space.global_state.target_frequency == 0.0

    def test_iteration_count(self):
        state = make_state()
        state.iteration.current = 5
        space = build_state_space(state)
        assert space.global_state.iteration_count == 5


# ── build_state_space: Module 2 — Timing Clusters ───────────────────────

class TestBuildTimingClusters:
    def test_critical_paths_truncated_to_20(self):
        state = make_state()
        paths = [
            CriticalPathEntry(
                cells=[f"u_cell_{i}"],
                path_length=1, slack=-0.5,
                logic_delay=0.3, net_delay=0.2, levels=5,
            )
            for i in range(25)
        ]
        state.timing.critical_paths = paths
        space = build_state_space(state)
        assert len(space.timing_clusters.top_violating_paths) == 20

    def test_no_critical_paths_returns_empty(self):
        state = make_state()
        state.timing.critical_paths = []
        space = build_state_space(state)
        assert space.timing_clusters.top_violating_paths == []

    def test_path_fields_mapped(self):
        state = make_state()
        state.timing.critical_paths = [
            CriticalPathEntry(
                cells=["u_top/u_sub/reg_out"],
                path_length=1, slack=-0.123,
                logic_delay=0.4, net_delay=0.6, levels=8,
            )
        ]
        space = build_state_space(state)
        p = space.timing_clusters.top_violating_paths[0]
        assert p.endpoint_name == "u_top/u_sub/reg_out"
        assert p.slack == -0.123
        assert p.logic_levels == 8

    def test_delay_percentages(self):
        state = make_state()
        state.timing.critical_paths = [
            CriticalPathEntry(
                cells=["end"], path_length=1, slack=-0.1,
                logic_delay=0.3, net_delay=0.7, levels=3,
            )
        ]
        space = build_state_space(state)
        p = space.timing_clusters.top_violating_paths[0]
        assert p.logic_delay_pct == 0.3   # 0.3 / (0.3+0.7)
        assert p.route_delay_pct == 0.7    # 0.7 / (0.3+0.7)


# ── build_state_space: Module 3 — Physical Congestion ───────────────────

class TestBuildPhysicalCongestion:
    def test_congestion_hotspots(self):
        state = make_state()
        state.timing.congestion_data = {
            "global_score": 0.75,
            "hotspots": [
                {"x1": 10, "y1": 20, "x2": 30, "y2": 40,
                 "severity": 0.9, "dominant_module": "u_core"},
            ],
            "pblock_overflow_count": 2,
        }
        space = build_state_space(state)
        pc = space.physical_congestion
        assert pc.global_congestion_score == 0.75
        assert pc.pblock_overflow_count == 2
        assert len(pc.congestion_hotspots) == 1
        h = pc.congestion_hotspots[0]
        assert h.x1 == 10 and h.y1 == 20
        assert h.severity == 0.9
        assert h.dominant_module == "u_core"

    def test_no_congestion_data(self):
        state = make_state()
        space = build_state_space(state)
        pc = space.physical_congestion
        assert pc.global_congestion_score is None
        assert pc.congestion_hotspots == []


# ── build_state_space: Module 4 — Netlist Quality ───────────────────────

class TestBuildNetlistQuality:
    def test_high_fanout_nets_dict_format(self):
        state = make_state()
        state.timing.high_fanout_nets = [
            {"net_name": "net_a", "fanout": 120, "is_replicated": False},
            {"net_name": "net_b", "fanout": 200, "is_replicated": True},
        ]
        space = build_state_space(state)
        nets = space.netlist_quality.high_fanout_nets
        assert len(nets) == 2
        assert nets[0].net_name == "net_a"
        assert nets[0].fanout_count == 120
        assert nets[1].is_replicated is True

    def test_no_high_fanout_nets(self):
        state = make_state()
        space = build_state_space(state)
        assert space.netlist_quality.high_fanout_nets == []


# ── build_state_space: Module 5 — Constraints ──────────────────────────

class TestBuildConstraints:
    def test_clock_definitions(self):
        state = make_state()
        state.timing.clock_period = 5.0  # 200 MHz
        space = build_state_space(state)
        assert space.constraints_env.clock_definitions.get("clk_fpl26contest") == 200.0

    def test_no_clock_period(self):
        state = make_state()
        state.timing.clock_period = None
        space = build_state_space(state)
        assert space.constraints_env.clock_definitions == {}


# ── build_state_space: Module 6 — Dynamic Gradient ──────────────────────

class TestBuildDynamicGradient:
    def test_action_status_mapping_improved(self):
        state = make_state()
        state.strategy.evaluation_result = "IMPROVED"
        space = build_state_space(state)
        assert space.dynamic_gradient.action_status == "Success"

    def test_action_status_mapping_regression(self):
        state = make_state()
        state.strategy.evaluation_result = "REGRESSION"
        space = build_state_space(state)
        assert space.dynamic_gradient.action_status == "Failed"

    def test_action_status_mapping_unchanged(self):
        state = make_state()
        state.strategy.evaluation_result = "UNCHANGED"
        space = build_state_space(state)
        assert space.dynamic_gradient.action_status == "Success"

    def test_action_status_mapping_pending(self):
        state = make_state()
        state.strategy.evaluation_result = "PENDING"
        space = build_state_space(state)
        assert space.dynamic_gradient.action_status == ""

    def test_delta_tns_from_narratives(self):
        state = make_state()
        state.iteration.narratives = [
            {"tns": -50.0},  # iteration N-1
            {"tns": -30.0},  # iteration N
        ]
        space = build_state_space(state)
        assert space.dynamic_gradient.delta_tns == 20.0  # -30.0 - (-50.0)

    def test_delta_tns_single_narrative(self):
        state = make_state()
        state.iteration.narratives = [{"tns": -10.0}]
        space = build_state_space(state)
        assert space.dynamic_gradient.delta_tns is None

    def test_current_strategy_mapped(self):
        state = make_state()
        state.strategy.current_strategy = "PhysOpt"
        space = build_state_space(state)
        assert space.dynamic_gradient.last_action_taken == "PhysOpt"


# ── format_state_space_for_llm: Phase-aware filtering ──────────────────

class TestPhaseAwareFiltering:
    def test_analyze_phase_has_correct_modules(self, sample_space):
        text = format_state_space_for_llm(space=sample_space, phase=LoopPhase.ANALYZE)
        assert "Module 1: Global State" in text
        assert "Module 2: Timing Path Clusters" in text
        assert "Module 3: Physical & Congestion Metrics" in text
        assert "Module 4: Netlist Quality Profiler" in text
        assert "Module 6: Dynamic Gradient" in text
        assert "Module 5: Constraints Environment" not in text

    def test_select_strategy_has_all_six_modules(self, sample_space):
        text = format_state_space_for_llm(
            space=sample_space, phase=LoopPhase.SELECT_STRATEGY
        )
        for m in [1, 2, 3, 4, 5, 6]:
            assert f"Module {m}:" in text, f"Module {m} missing in SELECT_STRATEGY"

    def test_execute_only_global_and_delta(self, sample_space):
        text = format_state_space_for_llm(space=sample_space, phase=LoopPhase.EXECUTE)
        assert "Module 1: Global State" in text
        assert "Module 6: Dynamic Gradient" in text
        assert "Module 2:" not in text
        assert "Module 3:" not in text
        assert "Module 4:" not in text
        assert "Module 5:" not in text

    def test_evaluate_only_global_and_delta(self, sample_space):
        text = format_state_space_for_llm(space=sample_space, phase=LoopPhase.EVALUATE)
        assert "Module 1: Global State" in text
        assert "Module 6: Dynamic Gradient" in text
        assert "Module 2:" not in text

    def test_header_matches_phase_label(self):
        space = StateSpace()
        text = format_state_space_for_llm(space=space, phase=LoopPhase.ANALYZE)
        assert text.startswith("[ANALYZE — Context & Dashboard]")
        text_ss = format_state_space_for_llm(space=space, phase=LoopPhase.SELECT_STRATEGY)
        assert text_ss.startswith("[SELECT_STRATEGY — Context & Dashboard]")

    def test_ends_with_dashboard_marker(self, sample_space):
        text = format_state_space_for_llm(space=sample_space, phase=LoopPhase.ANALYZE)
        assert text.endswith("--- End Dashboard ---")

    def test_no_phase_shows_all_modules(self, sample_space):
        text = format_state_space_for_llm(space=sample_space)
        for m in [1, 2, 3, 4, 5, 6]:
            assert f"Module {m}:" in text


# ── format_state_space_for_llm: Handoff & Catalog ──────────────────────

class TestHandoffAndCatalog:
    def test_handoff_summary_included(self, sample_space):
        text = format_state_space_for_llm(
            space=sample_space,
            phase=LoopPhase.ANALYZE,
            handoff_summary="[previous phase context] strats considered: PBLOCK",
        )
        assert "strats considered: PBLOCK" in text

    def test_strategy_catalog_in_select_strategy(self, sample_space):
        text = format_state_space_for_llm(
            space=sample_space,
            phase=LoopPhase.SELECT_STRATEGY,
            show_strategy_catalog=True,
        )
        assert "strategy_catalog:" in text
        assert "PBLOCK" in text  # known strategy

    def test_strategy_catalog_not_in_analyze(self, sample_space):
        text = format_state_space_for_llm(
            space=sample_space,
            phase=LoopPhase.ANALYZE,
            show_strategy_catalog=False,
        )
        assert "strategy_catalog:" not in text


# ── format_state_space_for_llm: YAML Formatting ────────────────────────

class TestYamlFormatting:
    def test_wns_three_decimal_places(self):
        state = make_state()
        state.timing.latest_wns = -0.5
        space = build_state_space(state)
        text = format_state_space_for_llm(space=space)
        assert "wns_setup: -0.500" in text

    def test_delta_wns_four_decimal_places(self):
        state = make_state()
        state.strategy.evaluation_wns_delta = 0.077
        space = build_state_space(state)
        text = format_state_space_for_llm(space=space)
        # delta_wns 使用 +.4f 格式
        assert "delta_wns: +0.0770" in text

    def test_none_values_display_na(self):
        space = StateSpace()  # all defaults = None/0
        text = format_state_space_for_llm(space=space, phase=LoopPhase.ANALYZE)
        assert 'wns_setup: "N/A(not_analyzed)"' in text
        assert 'tns_setup: "N/A(not_analyzed)"' in text
        assert 'global_congestion_score: "N/A(congestion_analysis_not_supported)"' in text

    def test_percentage_format(self):
        state = make_state()
        state.timing.resource_utilization = {"LUT": 24000}
        state.timing.device_capacity = {"LUT": 48000}
        space = build_state_space(state)
        text = format_state_space_for_llm(space=space)
        assert "lut_utilization: 50.00%" in text

    def test_empty_list_brackets(self):
        space = StateSpace()
        text = format_state_space_for_llm(space=space, phase=LoopPhase.ANALYZE)
        assert "top_paths: []" in text or "top_paths:  # 0" in text


# ── format_state_space_for_llm: Strategy Lifecycle ──────────────────────

class TestStrategyLifecycle:
    def test_strategy_lifecycle_shown(self, sample_space):
        text = format_state_space_for_llm(
            space=sample_space,
            phase=LoopPhase.ANALYZE,
            current_strategy="PhysOpt",
            evaluation_result="IMPROVED",
        )
        assert "strategy_lifecycle:" in text
        assert "current_strategy: PhysOpt" in text
        assert "evaluation: IMPROVED" in text

    def test_no_strategy_lifecycle_when_empty(self, sample_space):
        text = format_state_space_for_llm(
            space=sample_space,
            phase=LoopPhase.ANALYZE,
            current_strategy="",
            evaluation_result="",
        )
        assert "strategy_lifecycle:" not in text


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def sample_space() -> StateSpace:
    """A realistic StateSpace for format tests."""
    state = make_state()
    state.timing.latest_wns = -0.523
    state.timing.latest_tns = -12.34
    state.timing.clock_period = 3.333
    state.timing.critical_paths = [
        CriticalPathEntry(
            cells=["u_core/u_alu/reg_0"],
            slack=-0.523, logic_delay=0.4, net_delay=0.6, levels=12,
        ),
    ]
    state.timing.congestion_data = {"global_score": 0.65}
    state.timing.resource_utilization = {"LUT": 20000, "FF": 10000}
    state.timing.device_capacity = {"LUT": 50000, "FF": 20000}
    state.iteration.current = 3
    state.strategy.evaluation_wns_delta = 0.077
    state.strategy.evaluation_result = "IMPROVED"
    state.strategy.current_strategy = "PhysOpt"
    return build_state_space(state)
