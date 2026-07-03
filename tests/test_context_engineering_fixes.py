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
