"""Unit tests for the context-engineering fixes derived from run
dcp_optimizer_run-20260703_142810 log analysis.

Covers:
  P1 — parse_design_state no longer silently defaults to UNPLACED; design_state
       is preserved on parse failure and correctly set ROUTED after route/physopt
       tools; the dashboard "wireload estimate" warning is guarded.
  P2/P8/P3 — llm_call_logger header shows WNS freshness + baseline, uses the
       correct token key (total_tokens), and falls back to the last strategy.
  P5 — cooldown applies for measured no-improvement even when a strategy tool
       summary contained "error"; only genuine unmeasured crashes skip cooldown.
"""
from __future__ import annotations

import json

from optimizer.state import (
    CriticalPathEntry,
    DesignState,
    OptimizerState,
    PhaseEntry,
    parse_design_state,
)
from optimizer.llm_call_logger import _extract_snapshot, _format_readable
from optimizer.pure.state_space import (
    build_state_space,
    format_state_space_for_llm,
)
from optimizer.pure.tool_filter import LoopPhase


# ── P1: parse_design_state ──────────────────────────────────────────────

class TestParseDesignState:
    def test_routed(self):
        assert parse_design_state("Design State: routed") == DesignState.ROUTED

    def test_placed_only(self):
        assert parse_design_state("Design State | placed") == DesignState.PLACED

    def test_optimized_maps_to_unplaced(self):
        assert parse_design_state("Design State: optimized") == DesignState.UNPLACED

    def test_unparseable_returns_none_not_unplaced(self):
        """The catastrophic bug: a report without a 'Design State' header used
        to default to UNPLACED, falsely marking real post-route WNS as wireload
        estimates. It must now return None so callers can preserve prior state."""
        assert parse_design_state("WNS(ns) TNS(ns) Failing Endpoints\n-0.921 -831.3 1529") is None
        assert parse_design_state("") is None
        assert parse_design_state(None) is None


# ── P1: _track_wns_from_result design_state handling ────────────────────

def _make_state_routed() -> OptimizerState:
    s = OptimizerState()
    s.timing.design_state = DesignState.ROUTED
    s.timing.latest_wns = -0.920
    s.timing.best_wns = -0.920
    return s


class TestTrackWnsDesignState:
    def test_physopt_and_route_keeps_routed(self):
        from optimizer.nodes.subgraphs.phase_execute import _track_wns_from_result
        s = _make_state_routed()
        raw = json.dumps({"post_optimization": {"wns": -0.921, "tns": -831.3, "failing_endpoints": 1529}})
        _track_wns_from_result(s, "vivado_physopt_and_route", raw)
        # Previously this clobbered design_state to UNPLACED despite a real WNS.
        assert s.timing.design_state == DesignState.ROUTED
        assert s.timing.latest_wns == -0.921

    def test_route_design_sets_routed(self):
        from optimizer.nodes.subgraphs.phase_execute import _track_wns_from_result
        s = _make_state_routed()
        # route_design has no JSON wns path; pass a timing-summary-shaped payload
        raw = json.dumps({"wns": -0.920, "tns": -831.2, "failing_endpoints": 1529})
        _track_wns_from_result(s, "vivado_route_design", raw)
        assert s.timing.design_state == DesignState.ROUTED

    def test_report_timing_summary_without_design_state_header_preserves(self):
        """A timing report whose raw_report lacks a 'Design State' line must
        NOT flip a known-ROUTED design to UNPLACED."""
        from optimizer.nodes.subgraphs.phase_execute import _track_wns_from_result
        s = _make_state_routed()
        raw = json.dumps({
            "wns": -0.921, "tns": -831.3, "failing_endpoints": 1529,
            "raw_report": "Some Vivado report text with no Design State field",
        })
        _track_wns_from_result(s, "vivado_report_timing_summary", raw)
        assert s.timing.design_state == DesignState.ROUTED
        assert s.timing.latest_wns == -0.921

    def test_report_timing_summary_with_design_state_updates(self):
        from optimizer.nodes.subgraphs.phase_execute import _track_wns_from_result
        s = _make_state_routed()
        s.timing.design_state = DesignState.UNPLACED
        raw = json.dumps({
            "wns": -0.5, "tns": -10.0, "failing_endpoints": 5,
            "raw_report": "Design State: routed",
        })
        _track_wns_from_result(s, "vivado_report_timing_summary", raw)
        assert s.timing.design_state == DesignState.ROUTED


# ── P1: dashboard wireload-warning guard ────────────────────────────────

class TestDashboardWarningGuard:
    def test_unplaced_without_wns_shows_wireload_warning(self):
        s = OptimizerState()
        s.timing.design_state = DesignState.UNPLACED
        space = build_state_space(s)
        out = format_state_space_for_llm(space=space, phase=LoopPhase.ANALYZE, state=s)
        assert "WNS based on wireload estimates" in out

    def test_unplaced_with_real_wns_shows_softer_note(self):
        """The false-positive case: design_state=UNPLACED but a real WNS exists
        (stale/parse-failed state). Must NOT scream 'wireload estimate'."""
        s = OptimizerState()
        s.timing.design_state = DesignState.UNPLACED
        s.timing.latest_wns = -0.921
        space = build_state_space(s)
        out = format_state_space_for_llm(space=space, phase=LoopPhase.ANALYZE, state=s)
        assert "WNS based on wireload estimates" not in out
        assert "treat WNS as approximate" in out


# ── P2/P8/P3: llm_call_logger header ────────────────────────────────────

class TestLLMCallLoggerHeader:
    def test_snapshot_includes_baseline_and_freshness(self):
        s = OptimizerState()
        s.timing.latest_wns = -0.920
        s.timing.baseline_wns = -0.978
        s.timing.field_freshness["timing_summary"] = "stale"
        snap = _extract_snapshot(s)
        assert snap["baseline_wns"] == -0.978
        assert snap["wns_freshness"] == "stale"

    def test_display_strategy_falls_back_to_phase_history(self):
        """During ANALYZE after a CONTINUE/ROLLBACK clear, current_strategy is
        empty; the header should still show the last active strategy."""
        s = OptimizerState()
        s.strategy.current_strategy = ""
        s.strategy.phase_history.append(
            PhaseEntry(phase="EXECUTE_STRATEGY", strategy="PBLOCK", iteration=1, wns_at_entry=-0.978)
        )
        snap = _extract_snapshot(s)
        assert snap["display_strategy"] == "PBLOCK"

    def test_header_shows_freshness_tag_and_baseline(self):
        entry = {
            "call_id": 1, "phase": "EXECUTE", "iteration": 1, "model": "m",
            "latest_wns": -0.920, "baseline_wns": -0.978, "wns_freshness": "stale",
            "display_strategy": "PBLOCK", "current_strategy": "PBLOCK",
            "response_content": "x", "response_tool_calls": [], "usage": {},
        }
        line = _format_readable(entry).split("\n")[2]
        assert "WNS: -0.920 [stale]" in line
        assert "baseline=-0.978" in line
        assert "Strategy: PBLOCK" in line

    def test_header_uses_total_tokens_key(self):
        """P8: the readable log used usage.get('total') (always 0); must use
        'total_tokens' which is the key actually written to the entry."""
        entry = {
            "call_id": 1, "phase": "ANALYZE", "iteration": 1, "model": "m",
            "latest_wns": -0.9, "baseline_wns": None, "wns_freshness": "",
            "display_strategy": "", "current_strategy": "",
            "response_content": "x", "response_tool_calls": [],
            "usage": {"total_tokens": 14486, "cost": 0.013},
        }
        out = _format_readable(entry)
        assert "Tokens: 14486" in out
        assert "Tokens: 0" not in out


# ── P5: cooldown logic ──────────────────────────────────────────────────

class TestCooldownLogic:
    def _state_with_measured_no_improvement(self, strategy_tool_error: bool):
        """delta=0.0 (measured): best_wns == wns_at_entry."""
        from optimizer.pure.constants import STRATEGY_TOOL_NAMES
        s = OptimizerState()
        s.strategy.current_strategy = "PlaceRouteDirectiveExplore"
        s.iteration.current = 2
        s.timing.best_wns = -0.920
        s.strategy.phase_history.append(PhaseEntry(
            phase="EXECUTE_STRATEGY", strategy="PlaceRouteDirectiveExplore",
            iteration=2, wns_at_entry=-0.920, best_wns_at_entry=-0.920,
        ))
        if strategy_tool_error:
            # vivado_place_design is in STRATEGY_TOOL_NAMES and its summary
            # contained "error" (e.g. "already placed") — the loop bug case.
            assert "vivado_place_design" in STRATEGY_TOOL_NAMES
            s.iteration.tool_errors.append({
                "tool": "vivado_place_design",
                "result": "ERROR: design already placed, no action",
            })
        return s

    def test_measured_no_improvement_with_soft_error_applies_cooldown(self):
        """The observed loop: PlaceRouteDirectiveExplore re-selected because a
        soft 'already placed' error skipped cooldown. Now cooldown must apply."""
        from optimizer.nodes.subgraphs.phase_evaluate import _cool_down_current_strategy_if_stalled
        s = self._state_with_measured_no_improvement(strategy_tool_error=True)
        result = _cool_down_current_strategy_if_stalled(s, "EVALUATE switched away")
        assert result is True
        assert "PlaceRouteDirectiveExplore" in s.iteration.blocked_strategies

    def test_measured_no_improvement_clean_applies_cooldown(self):
        from optimizer.nodes.subgraphs.phase_evaluate import _cool_down_current_strategy_if_stalled
        s = self._state_with_measured_no_improvement(strategy_tool_error=False)
        assert _cool_down_current_strategy_if_stalled(s, "switched") is True
        assert "PlaceRouteDirectiveExplore" in s.iteration.blocked_strategies

    def test_unmeasured_crash_of_strategy_tool_skips_cooldown(self):
        """delta=None (genuine crash, no measurable result) for a strategy tool
        still gets a fair retry chance — cooldown skipped."""
        from optimizer.nodes.subgraphs.phase_evaluate import _cool_down_current_strategy_if_stalled
        s = OptimizerState()
        s.strategy.current_strategy = "PBLOCK"
        s.iteration.current = 1
        s.timing.best_wns = float('-inf')  # delta None
        s.iteration.tool_errors.append({
            "tool": "rapidwright_execute_pblock_strategy",
            "result": "ERROR: exception during execution",
        })
        result = _cool_down_current_strategy_if_stalled(s, "crash")
        assert result is False
        assert "PBLOCK" not in s.iteration.blocked_strategies

    def test_improved_strategy_no_cooldown(self):
        from optimizer.nodes.subgraphs.phase_evaluate import _cool_down_current_strategy_if_stalled
        s = OptimizerState()
        s.strategy.current_strategy = "PBLOCK"
        s.iteration.current = 1
        s.timing.best_wns = -0.400
        s.strategy.phase_history.append(PhaseEntry(
            phase="EXECUTE_STRATEGY", strategy="PBLOCK",
            iteration=1, wns_at_entry=-0.978, best_wns_at_entry=-0.978,
        ))
        assert _cool_down_current_strategy_if_stalled(s, "improved") is False


# ── Batch A: P0 system-message retention + shared extract fn ────────────

class TestExtractSystemMessage:
    """Shared extract_system_message preserves prompt-caching semantics."""

    def test_first_system_to_system_text_rest_to_api_clean(self):
        from optimizer.pure.context_snapshot import extract_system_message
        msgs = [
            {"role": "system", "content": "STATIC_PROMPT"},
            {"role": "system", "content": "FORMAT_GUARD"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        sys_text, clean = extract_system_message(msgs)
        assert sys_text == "STATIC_PROMPT"
        assert clean == [
            {"role": "system", "content": "FORMAT_GUARD"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

    def test_no_system_message(self):
        from optimizer.pure.context_snapshot import extract_system_message
        msgs = [{"role": "user", "content": "hi"}]
        sys_text, clean = extract_system_message(msgs)
        assert sys_text == ""
        assert clean == msgs

    def test_empty_list(self):
        from optimizer.pure.context_snapshot import extract_system_message
        sys_text, clean = extract_system_message([])
        assert sys_text == ""
        assert clean == []


class TestSystemMessageRetentionAcrossPhaseTransition:
    """P0: transition_phase must restore ALL system messages, not just the first."""

    def test_all_system_messages_restored(self):
        """Simulate a phase transition and verify that FORMAT_GUARD and budget
        system messages survive the clear+restore, not just SYSTEM_PROMPT.TXT."""
        import asyncio
        from context_manager.manager import MemoryManager
        from context_manager.compat import DCPOptimizerCompat
        from context_manager.events import EventBus
        from optimizer.pure.tool_filter import LoopPhase
        from optimizer.nodes.subgraphs.phase_handoff import (
            PhaseHandoff, transition_phase,
        )

        mm = MemoryManager(event_bus=EventBus())
        compat = DCPOptimizerCompat(mm)

        # Simulate initial injection: SYSTEM_PROMPT + FORMAT_GUARD + budget
        compat.add_message("system", "SYSTEM_PROMPT_TXT")
        compat.add_message("system", "FORMAT_GUARD_CONTENT")
        compat.add_message("system", "[BUDGET] ...")

        # Add some conversation
        compat.add_message("user", "analyze this")
        compat.add_message("assistant", "ok")

        # Build a minimal deps-like object
        class _Deps:
            pass
        deps = _Deps()
        deps.compat = compat
        deps.memory_manager = mm

        handoff = PhaseHandoff(source_phase="ANALYZE", llm_summary="done")
        asyncio.run(transition_phase(
            deps, LoopPhase.ANALYZE, LoopPhase.SELECT_STRATEGY, handoff,
        ))

        # After transition, all 3 system messages must be in working memory
        remaining = mm.get_context()
        system_contents = [m.content for m in remaining if m.role.value == "system"]
        assert "SYSTEM_PROMPT_TXT" in system_contents
        assert "FORMAT_GUARD_CONTENT" in system_contents
        assert "[BUDGET] ..." in system_contents


# ── Batch B: Dashboard data accuracy (P3, P5, P6) ──────────────────────

class TestClockNameNotHardcoded:
    """P5: Module 5 clock name must come from critical path data, not hardcoded."""

    def test_clock_name_from_critical_path(self):
        from optimizer.state import CriticalPathEntry, ClockDomainInfo
        s = OptimizerState()
        s.timing.clock_period = 5.0
        s.timing.critical_paths = [
            CriticalPathEntry(clock=ClockDomainInfo(source_clock="clk_custom")),
        ]
        from optimizer.pure.state_space import _build_constraints_env
        ce = _build_constraints_env(s)
        assert "clk_custom" in ce.clock_definitions
        assert "clk_fpl26contest" not in ce.clock_definitions
        assert ce.clock_definitions["clk_custom"] == 200.0

    def test_fallback_when_no_critical_paths(self):
        s = OptimizerState()
        s.timing.clock_period = 5.0
        s.timing.critical_paths = []
        from optimizer.pure.state_space import _build_constraints_env
        ce = _build_constraints_env(s)
        assert "clk_fpl26contest" in ce.clock_definitions

    def test_dest_clock_used_when_no_source(self):
        from optimizer.state import CriticalPathEntry, ClockDomainInfo
        s = OptimizerState()
        s.timing.clock_period = 4.0
        s.timing.critical_paths = [
            CriticalPathEntry(clock=ClockDomainInfo(source_clock="", dest_clock="clk_capture")),
        ]
        from optimizer.pure.state_space import _build_constraints_env
        ce = _build_constraints_env(s)
        assert "clk_capture" in ce.clock_definitions


class TestStrategyLifecycleClean:
    """P3: current_strategy must not leak into ANALYZE/SELECT_STRATEGY dashboard."""

    def test_analyze_phase_suppresses_current_strategy(self):
        s = OptimizerState()
        s.strategy.current_strategy = "PBLOCK"
        s.strategy.evaluation_result = "IMPROVED"
        s.strategy.current_phase = "ANALYZE"
        from optimizer.pure.state_space import build_state_space, format_state_space_for_llm
        space = build_state_space(s)
        yaml = format_state_space_for_llm(
            space=space, phase=LoopPhase.ANALYZE,
            current_strategy=s.strategy.current_strategy,
            evaluation_result=s.strategy.evaluation_result,
            state=s,
        )
        assert "current_strategy: PBLOCK" not in yaml

    def test_execute_phase_shows_current_strategy(self):
        s = OptimizerState()
        s.strategy.current_strategy = "PBLOCK"
        s.strategy.evaluation_result = "IMPROVED"
        s.strategy.current_phase = "EXECUTE_STRATEGY"
        from optimizer.pure.state_space import build_state_space, format_state_space_for_llm
        space = build_state_space(s)
        yaml = format_state_space_for_llm(
            space=space, phase=LoopPhase.EXECUTE,
            current_strategy=s.strategy.current_strategy,
            evaluation_result=s.strategy.evaluation_result,
            state=s,
        )
        assert "current_strategy: PBLOCK" in yaml

    def test_inject_merged_dashboard_analyze_suppresses_strategy(self):
        """End-to-end: inject_merged_dashboard reads state and suppresses stale
        current_strategy during ANALYZE phase."""
        from optimizer.pure.context_snapshot import inject_merged_dashboard
        s = OptimizerState()
        s.strategy.current_strategy = "PBLOCK"
        s.strategy.evaluation_result = "IMPROVED"
        s.strategy.current_phase = "ANALYZE"
        s.strategy.last_handoff_text = ""
        api_messages: list[dict] = []
        inject_merged_dashboard(api_messages, s, LoopPhase.ANALYZE)
        dashboard = api_messages[-1]["content"]
        assert "current_strategy: PBLOCK" not in dashboard


class TestCellRegistryFreshness:
    """P6: Cell Registry snapshot must show stale/fresh status and iteration."""

    def test_stale_marker_shown(self):
        from optimizer.pure.entities import EntityRegistry, CellRef
        reg = EntityRegistry()
        reg.cells["u_core/lut1"] = CellRef(canonical_name="u_core/lut1", cell_type="LUT6", last_seen_iter=1)
        from optimizer.pure.entities import build_registry_snapshot_yaml
        yaml = build_registry_snapshot_yaml(reg, stale=True, iteration=2)
        assert "STALE" in yaml
        assert "iter=2" in yaml

    def test_fresh_marker_shown(self):
        from optimizer.pure.entities import EntityRegistry, CellRef
        reg = EntityRegistry()
        reg.cells["u_core/lut1"] = CellRef(canonical_name="u_core/lut1", cell_type="LUT6", last_seen_iter=1)
        from optimizer.pure.entities import build_registry_snapshot_yaml
        yaml = build_registry_snapshot_yaml(reg, stale=False, iteration=1)
        assert "fresh" in yaml
        assert "STALE" not in yaml

    def test_inject_passes_stale_from_state(self):
        """inject_pinned_cell_registry propagates critical_paths_stale."""
        from optimizer.pure.context_snapshot import inject_pinned_cell_registry
        from optimizer.pure.entities import EntityRegistry, CellRef
        s = OptimizerState()
        s.timing.critical_paths_stale = True
        s.iteration.current = 3
        s.entity_registry = EntityRegistry()
        s.entity_registry.cells["u_core/lut1"] = CellRef(canonical_name="u_core/lut1", cell_type="LUT6")
        api: list[dict] = []
        inject_pinned_cell_registry(api, s)
        content = api[0]["content"]
        assert "STALE" in content
        assert "iter=3" in content


# ── Batch C: FORMAT_GUARD phase-specific + SYSTEM_PROMPT cleanup ───────

class TestFormatGuardPhaseSpecific:
    """P1/P7: FORMAT_GUARD must be phase-specific and injected per-phase."""

    def test_analyze_guard_has_analyze_addendum(self):
        from optimizer.nodes.prepare_context import build_phase_format_guard
        guard = build_phase_format_guard(LoopPhase.ANALYZE)
        assert "[FORMAT_GUARD:analyze]" in guard
        assert "ANALYZE: only diagnostic" in guard
        # EXECUTE-only content should NOT appear in ANALYZE guard
        assert "PBLOCK AUTO-CHAIN" not in guard

    def test_execute_guard_has_execute_addendum(self):
        from optimizer.nodes.prepare_context import build_phase_format_guard
        guard = build_phase_format_guard(LoopPhase.EXECUTE)
        assert "[FORMAT_GUARD:execute]" in guard
        assert "PBLOCK AUTO-CHAIN" in guard
        assert "tool filtering" in guard

    def test_select_guard_has_no_execute_content(self):
        from optimizer.nodes.prepare_context import build_phase_format_guard
        guard = build_phase_format_guard(LoopPhase.SELECT_STRATEGY)
        assert "[FORMAT_GUARD:select_strategy]" in guard
        assert "pick exactly one strategy" in guard
        assert "PBLOCK AUTO-CHAIN" not in guard

    def test_all_phases_have_base_content(self):
        from optimizer.nodes.prepare_context import build_phase_format_guard
        for phase in LoopPhase:
            guard = build_phase_format_guard(phase)
            # BASE_FORMAT_GUARD content present in all phases
            assert "report_step_state" in guard
            assert "CELL NAME CONTRACT" in guard
            assert "DESIGN CONSISTENCY" in guard
            assert "STRICTLY FORBIDDEN" in guard

    def test_inject_merged_dashboard_injects_guard(self):
        """inject_merged_dashboard should inject a FORMAT_GUARD system message."""
        from optimizer.pure.context_snapshot import inject_merged_dashboard
        s = OptimizerState()
        api: list[dict] = [{"role": "system", "content": "STATIC"}]
        inject_merged_dashboard(api, s, LoopPhase.ANALYZE)
        guard_msgs = [m for m in api if m.get("role") == "system"
                      and "[FORMAT_GUARD:" in m.get("content", "")]
        assert len(guard_msgs) == 1
        assert "[FORMAT_GUARD:analyze]" in guard_msgs[0]["content"]

    def test_guard_idempotent(self):
        """Calling inject_merged_dashboard twice should not accumulate guards."""
        from optimizer.pure.context_snapshot import inject_merged_dashboard
        s = OptimizerState()
        api: list[dict] = [{"role": "system", "content": "STATIC"}]
        inject_merged_dashboard(api, s, LoopPhase.ANALYZE)
        inject_merged_dashboard(api, s, LoopPhase.ANALYZE)
        guard_msgs = [m for m in api if m.get("role") == "system"
                      and "[FORMAT_GUARD:" in m.get("content", "")]
        assert len(guard_msgs) == 1

    def test_guard_updates_when_phase_changes(self):
        """Guard content should reflect the latest phase, not accumulate."""
        from optimizer.pure.context_snapshot import inject_merged_dashboard
        s = OptimizerState()
        api: list[dict] = [{"role": "system", "content": "STATIC"}]
        inject_merged_dashboard(api, s, LoopPhase.ANALYZE)
        inject_merged_dashboard(api, s, LoopPhase.EXECUTE)
        guard_msgs = [m for m in api if m.get("role") == "system"
                      and "[FORMAT_GUARD:" in m.get("content", "")]
        assert len(guard_msgs) == 1
        assert "[FORMAT_GUARD:execute]" in guard_msgs[0]["content"]
        assert "[FORMAT_GUARD:analyze]" not in guard_msgs[0]["content"]


class TestSystemPromptCleanup:
    """P1: SYSTEM_PROMPT.TXT should not contain PBLOCK sequence or hardcode multiplier."""

    def test_no_pblock_execution_sequence(self):
        from pathlib import Path
        prompt = Path("SYSTEM_PROMPT.TXT").read_text()
        assert "PBLOCK EXECUTION SEQUENCE" not in prompt
        assert "MANDATORY VIVADO FLOW" not in prompt

    def test_no_hardcoded_multiplier(self):
        from pathlib import Path
        prompt = Path("SYSTEM_PROMPT.TXT").read_text()
        assert "resource_multiplier=2.0" not in prompt
        assert "MULTIPLIER: Always use" not in prompt

    def test_role_and_rules_preserved(self):
        from pathlib import Path
        prompt = Path("SYSTEM_PROMPT.TXT").read_text()
        assert "FPGA_Timing_Optimization_Expert" in prompt
        assert "report_step_state" in prompt
        assert "DESIGN CONSISTENCY" not in prompt  # moved to FORMAT_GUARD
        # Strategy list still present (descriptions, not sequences)
        assert "PBLOCK" in prompt


# ── Batch D: Shared tool summary (P4) ──────────────────────────────────

class TestSharedToolSummary:
    """P4: compact_tool_summary is the single source for dashboard/handoff summaries."""

    def test_timing_summary_json(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        raw = json.dumps({"wns": -0.5, "tns": -3.2, "failing_endpoints": 10})
        s = compact_tool_summary("vivado_report_timing_summary", raw)
        assert "WNS=-0.5" in s
        assert "TNS=-3.2" in s
        assert "FE=10" in s

    def test_timing_summary_text_format(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        raw = "WNS: -0.500\nTNS: -3.200\nFailing Endpoints: 10"
        s = compact_tool_summary("vivado_report_timing_summary", raw)
        assert "WNS=" in s

    def test_critical_path_spread_json(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        raw = json.dumps({"avg_distance": 12.5, "max_distance": 30.0, "paths_analyzed": 5})
        s = compact_tool_summary("rapidwright_analyze_critical_path_spread", raw)
        assert "avg=12.5" in s
        assert "max=30.0" in s
        assert "paths=5" in s

    def test_congestion_json(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        raw = json.dumps({"global_score": 0.15, "severity": "HIGH"})
        s = compact_tool_summary("rapidwright_analyze_congestion", raw)
        assert "global_score=0.15" in s
        assert "severity=HIGH" in s

    def test_high_fanout_nets(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        raw = "net_a fanout=500\nnet_b fanout=200\nnet_c fanout=1000"
        s = compact_tool_summary("vivado_get_cached_high_fanout_nets", raw)
        assert "3 nets" in s
        assert "max_fanout=1000" in s

    def test_search_cells_json(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        raw = json.dumps({"cell_count": 42})
        s = compact_tool_summary("rapidwright_search_cells", raw)
        assert "42 cells" in s

    def test_empty_raw(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        assert compact_tool_summary("vivado_report_timing_summary", "") == ""
        assert compact_tool_summary("vivado_report_timing_summary", "   ") == ""

    def test_unknown_tool_fallback(self):
        from optimizer.pure.tool_summary import compact_tool_summary
        raw = "some output\nsecond line"
        s = compact_tool_summary("unknown_tool", raw)
        assert "some output" in s

    def test_extract_recent_tool_results_uses_shared_fn(self):
        """phase_analyze._extract_recent_tool_results delegates to compact_tool_summary."""
        from optimizer.nodes.subgraphs.phase_analyze import _extract_recent_tool_results
        s = OptimizerState()
        s.iteration.current = 1
        s.context.raw_tool_outputs[(1, "ANALYZE", 1, "vivado_report_timing_summary")] = \
            json.dumps({"wns": -0.5, "tns": -3.2, "failing_endpoints": 10})
        results = _extract_recent_tool_results(s)
        assert len(results) == 1
        assert "vivado_report_timing_summary" in results[0]
        assert "WNS=-0.5" in results[0]


# ── Context-engineering contradiction fixes (run-20260709_123409 analysis) ──

class TestContradictionFixesP0:
    """P0 data-honesty fixes: per-strategy delta, error status, failure attribution."""

    def test_delta_uses_strategy_entry_baseline_not_iteration_start(self):
        """P0-2C: PhysOpt's wns_before must be its own entry baseline (-0.655),
        not the iteration-start prev_best_wns (-0.978). Previously every strategy
        in an iteration was credited against the iteration's starting WNS,
        inflating later strategies' apparent delta (PhysOpt +0.335 vs real +0.012)."""
        from optimizer.nodes.subgraphs.phase_execute import _current_strategy_baseline_wns
        s = OptimizerState()
        s.iteration.current = 1
        s.timing.prev_best_wns = -0.978          # frozen at iteration start
        s.timing.best_wns = -0.643               # after PhysOpt
        s.strategy.phase_history.append(PhaseEntry(
            phase="EXECUTE_STRATEGY", strategy="PBLOCK", iteration=1,
            wns_at_entry=-0.978, best_wns_at_entry=-0.978,
        ))
        s.strategy.phase_history.append(PhaseEntry(
            phase="EXECUTE_STRATEGY", strategy="PhysOpt", iteration=1,
            wns_at_entry=-0.655, best_wns_at_entry=-0.655,
        ))
        s.strategy.current_strategy = "PhysOpt"
        baseline = _current_strategy_baseline_wns(s)
        assert baseline == -0.655            # PhysOpt's own entry baseline
        assert baseline != s.timing.prev_best_wns  # NOT the iteration-start value
        # The recorded delta would be -0.643 - (-0.655) = +0.012, not +0.335.
        assert round(s.timing.best_wns - baseline, 3) == 0.012

    def test_delta_baseline_falls_back_to_prev_best_without_entry(self):
        from optimizer.nodes.subgraphs.phase_execute import _current_strategy_baseline_wns
        s = OptimizerState()
        s.iteration.current = 1
        s.timing.prev_best_wns = -0.978
        s.strategy.current_strategy = "PhysOpt"  # no matching phase_history entry
        assert _current_strategy_baseline_wns(s) == -0.978

    def test_small_error_response_reports_error_status(self):
        """P0-3A: a compact JSON error response (e.g. directive not recognized)
        previously hit the small-output bypass and was labeled status: completed,
        hiding the failure. It must now report status: error."""
        from optimizer.pure.tool_summary import summarize_tool_result
        raw = json.dumps({"error": "Directive 'Performance_NetDelay_high' is not a recognized directive"})
        result = summarize_tool_result("vivado_place_design", raw)
        assert "status: error" in result
        assert "status: completed" not in result
        # The error text is preserved in the raw_output block.
        assert "not a recognized directive" in result

    def test_failure_attribution_uses_strategy_primary_tool(self):
        """P0-3D: Fanout's failure record must reference Fanout's own tool, not
        the cross-strategy accumulated tools_used[:3] (which contained
        CellReplication's vivado_physopt_and_route)."""
        from optimizer.pure.tool_catalog import get_strategy_primary_tool
        assert get_strategy_primary_tool("Fanout") == "rapidwright_execute_fanout_strategy"
        assert get_strategy_primary_tool("CellReplication") == "rapidwright_replicate_critical_cells"
        # Fanout's primary tool must NOT be CellReplication's tool.
        assert get_strategy_primary_tool("Fanout") != get_strategy_primary_tool("CellReplication")
        assert get_strategy_primary_tool("UnknownStrategy") is None


class TestContradictionFixesP1:
    """P1 strategy-history completeness: non-overwrite of stricter failure reasons."""

    def test_stricter_failure_reason_not_downgraded_to_tool_error(self):
        """P1-2D/3C: EXECUTE records strategy_not_applicable (TTL=5, see
        STRATEGY_NOT_APPLICABLE_TTL in state.py). iteration_end's independent
        empty-result re-scan must not overwrite it with tool_error (TTL=0,
        instantly retriable) - that would contradict the execution-time verdict."""
        from optimizer.state import record_strategy_failure
        s = OptimizerState()
        s.iteration.current = 2
        # EXECUTE-time record: strategy_not_applicable -> blocked_until_iter = 7 (2 + 5)
        record_strategy_failure(s, strategy="LUTCascade", reason="strategy_not_applicable",
                                tool="rapidwright_flatten_lut_cascade", detail="chain_skipped")
        entry = s.context.failed_strategies[0]
        assert entry.reason == "strategy_not_applicable"
        assert entry.blocked_until_iter == 7
        # iteration_end re-scan tries to downgrade to tool_error (TTL=0) -> must be preserved.
        record_strategy_failure(s, strategy="LUTCascade", reason="tool_error",
                                tool="rapidwright_flatten_lut_cascade", detail="empty result")
        assert len(s.context.failed_strategies) == 1     # no duplicate
        assert entry.reason == "strategy_not_applicable"  # not downgraded
        assert entry.blocked_until_iter == 7              # cooldown preserved

    def test_equally_strict_reason_refreshes_ttl(self):
        """A re-failure with an equally-or-more restrictive reason must refresh the TTL."""
        from optimizer.state import record_strategy_failure
        s = OptimizerState()
        s.iteration.current = 2
        record_strategy_failure(s, strategy="PhysOpt", reason="strategy_not_applicable",
                                tool="vivado_physopt_and_route", detail="first")
        assert s.context.failed_strategies[0].blocked_until_iter == 7
        # Re-fail at iter 3 with strategy_not_applicable (TTL=5 -> blocked until 8 >= 7) -> refresh.
        s.iteration.current = 3
        record_strategy_failure(s, strategy="PhysOpt", reason="strategy_not_applicable",
                                tool="vivado_physopt_and_route", detail="second")
        assert s.context.failed_strategies[0].blocked_until_iter == 8


# ── R1: Freshness write-path unification ──────────────────────────────────

class TestFreshnessUnification:
    """R1: mark_all_fields_stale / mark_critical_paths_stale /
    mark_critical_paths_fresh must keep critical_paths_stale (bool) and
    field_freshness["critical_path_cells"] (string) in sync — the two
    representations of the same fact must never drift (F4/F5 bug class).
    """

    def _state_with_ff(self) -> OptimizerState:
        """State with field_freshness pre-populated (as after init_analysis)."""
        s = OptimizerState()
        s.timing.field_freshness = {
            "timing_summary": "fresh",
            "cdc_paths": "fresh",
            "resource_utilization": "fresh",
            "high_fanout_nets": "fresh",
            "route_status": "fresh",
            "design_info": "fresh",
            "congestion_data": "fresh",
            "critical_path_cells": "fresh",
        }
        return s

    def test_mark_all_fields_stale_syncs_both(self):
        """mark_all_fields_stale must set bool=True AND field=stale together."""
        from optimizer.pure.freshness import mark_all_fields_stale
        s = self._state_with_ff()
        mark_all_fields_stale(s.timing, reason="strategy switch")
        assert s.timing.critical_paths_stale is True
        assert s.timing.critical_paths_stale_reason == "strategy switch"
        assert s.timing.field_freshness["critical_path_cells"] == "stale"
        # All other fields also stale
        assert all(v == "stale" for v in s.timing.field_freshness.values())

    def test_mark_all_fields_stale_creates_key_if_absent(self):
        """mark_all_fields_stale must create critical_path_cells key even
        when field_freshness is empty (pre-init_analysis guard)."""
        from optimizer.pure.freshness import mark_all_fields_stale
        s = OptimizerState()  # empty field_freshness
        mark_all_fields_stale(s.timing, reason="rollback")
        assert "critical_path_cells" in s.timing.field_freshness
        assert s.timing.field_freshness["critical_path_cells"] == "stale"
        assert s.timing.critical_paths_stale is True

    def test_mark_critical_paths_stale_syncs_both(self):
        """mark_critical_paths_stale must set bool=True AND field=stale.
        This fixes the latent drift at chain-step sites that set the bool
        but not the field (stale=true [fresh] contradiction)."""
        from optimizer.pure.freshness import mark_critical_paths_stale
        s = self._state_with_ff()
        mark_critical_paths_stale(s.timing, reason="place/route changed")
        assert s.timing.critical_paths_stale is True
        assert s.timing.critical_paths_stale_reason == "place/route changed"
        assert s.timing.field_freshness["critical_path_cells"] == "stale"
        # Other fields NOT touched
        assert s.timing.field_freshness["timing_summary"] == "fresh"

    def test_mark_critical_paths_fresh_syncs_both(self):
        """mark_critical_paths_fresh must set bool=False AND field=fresh
        together, and clear the reason."""
        from optimizer.pure.freshness import mark_critical_paths_fresh
        s = self._state_with_ff()
        s.timing.critical_paths_stale = True
        s.timing.critical_paths_stale_reason = "place/route changed"
        s.timing.field_freshness["critical_path_cells"] = "stale"
        mark_critical_paths_fresh(s.timing)
        assert s.timing.critical_paths_stale is False
        assert s.timing.critical_paths_stale_reason == ""
        assert s.timing.field_freshness["critical_path_cells"] == "fresh"

    def test_mark_critical_paths_fresh_no_key_no_crash(self):
        """mark_critical_paths_fresh must not crash when the
        critical_path_cells key is absent (pre-init_analysis). The bool
        is still cleared; the field is not created."""
        from optimizer.pure.freshness import mark_critical_paths_fresh
        s = OptimizerState()  # empty field_freshness
        s.timing.critical_paths_stale = True
        mark_critical_paths_fresh(s.timing)
        assert s.timing.critical_paths_stale is False
        assert "critical_path_cells" not in s.timing.field_freshness

    def test_no_drift_after_roundtrip(self):
        """Full stale→fresh roundtrip leaves bool and field in agreement."""
        from optimizer.pure.freshness import (
            mark_all_fields_stale, mark_critical_paths_fresh,
        )
        s = self._state_with_ff()
        mark_all_fields_stale(s.timing, reason="rollback")
        assert s.timing.critical_paths_stale is True
        assert s.timing.field_freshness["critical_path_cells"] == "stale"
        mark_critical_paths_fresh(s.timing)
        assert s.timing.critical_paths_stale is False
        assert s.timing.field_freshness["critical_path_cells"] == "fresh"


# ── R2: Data-driven phase-entry refresh spec table ───────────────────────

class TestRefreshSpecTable:
    """R2: the declarative RefreshSpec table covers the fields that were
    previously hardcoded, plus the newly-added route_status and
    congestion_data. critical_path_cells is deliberately excluded."""

    def test_analyze_specs_cover_expected_fields(self):
        from optimizer.pure.freshness import ANALYZE_REFRESH_SPECS
        fields = {s.field_name for s in ANALYZE_REFRESH_SPECS}
        # Original 4 fields
        assert "timing_summary" in fields
        assert "design_info" in fields
        assert "resource_utilization" in fields
        assert "high_fanout_nets" in fields
        # Newly added (R2 gap coverage)
        assert "route_status" in fields
        assert "congestion_data" in fields
        # Deliberately excluded
        assert "critical_path_cells" not in fields

    def test_select_specs_cover_only_timing(self):
        from optimizer.pure.freshness import SELECT_REFRESH_SPECS
        fields = {s.field_name for s in SELECT_REFRESH_SPECS}
        assert fields == {"timing_summary"}

    def test_design_info_has_post_rollback_condition(self):
        """design_info spec must have a condition gate (post-rollback only)."""
        from optimizer.pure.freshness import ANALYZE_REFRESH_SPECS
        di_spec = next(s for s in ANALYZE_REFRESH_SPECS if s.field_name == "design_info")
        assert di_spec.condition is not None
        # Condition is True when critical_paths_stale and no critical_paths
        s = OptimizerState()
        s.timing.critical_paths_stale = True
        s.timing.critical_paths = []
        assert di_spec.condition(s) is True
        # False when critical_paths exist (not post-rollback)
        s.timing.critical_paths = [CriticalPathEntry()]  # non-empty
        assert di_spec.condition(s) is False

    def test_high_fanout_nets_spec_has_args(self):
        from optimizer.pure.freshness import ANALYZE_REFRESH_SPECS
        hf_spec = next(s for s in ANALYZE_REFRESH_SPECS if s.field_name == "high_fanout_nets")
        assert hf_spec.args == {"num_paths": 50, "min_fanout": 50}

    def test_all_specs_have_post_process(self):
        """Every spec must have a post_process callback (or None for
        no-mutation fields). The runner marks fresh based on its return."""
        from optimizer.pure.freshness import ANALYZE_REFRESH_SPECS, SELECT_REFRESH_SPECS
        for spec in ANALYZE_REFRESH_SPECS + SELECT_REFRESH_SPECS:
            assert spec.post_process is not None, f"{spec.field_name} lacks post_process"

