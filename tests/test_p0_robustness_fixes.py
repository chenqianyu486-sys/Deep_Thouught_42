"""Regression tests for P0 robustness fixes from run-20260711_015650.

P0-1: skip-reopen baseline pollution - a dirty in-memory design must force a
      checkpoint reopen even when current_dcp_path already matches the target,
      otherwise a failed/non-improving strategy's stale WNS pollutes every
      later strategy's baseline (reported -0.602 instead of real best -0.542).
P0-2: illegal Vivado 2025.1 directive names - the MCP whitelist is tightened
      to drop directives Vivado rejects (Constraints 18-641), and place/route
      handlers now auto-fall back to the default directive when a whitelisted
      directive is rejected, so version drift no longer fails a whole strategy.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from optimizer.state import OptimizerState
from optimizer.pure.execute_contracts import should_skip_reopen
from strategy_library import STRATEGIES

# VivadoMCP lives outside the optimizer package; put it on sys.path so the
# directive whitelist / fallback helper can be imported for unit testing.
_VIVADO_MCP_DIR = Path(__file__).resolve().parent.parent / "VivadoMCP"
if str(_VIVADO_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_VIVADO_MCP_DIR))
import vivado_mcp_server as vms  # noqa: E402


# ── P0-1: skip-reopen dirty-flag invariant ───────────────────────────────


class TestP0SkipReopenDirtyFlag:
    def test_live_design_dirty_defaults_false(self):
        """ControlState exposes live_design_dirty, defaulting to clean."""
        assert OptimizerState().control.live_design_dirty is False

    def test_skip_when_clean_and_path_matches(self):
        """Clean memory + matching path -> may skip reopen (saves ~27s)."""
        assert should_skip_reopen(Path("/run/best.dcp"), "/run/best.dcp", False)

    def test_dirty_design_forces_reopen_even_when_path_matches(self):
        """P0-1 core invariant: a dirty design must force reopen.

        A failed/non-improving strategy modifies Vivado memory (place/route/opt)
        without updating current_dcp_path, so the path still matches the target
        while memory is stale. Skip-reopen must return False so the baseline
        checkpoint is reloaded and the next strategy sees the real best WNS.
        """
        assert not should_skip_reopen(
            Path("/run/best.dcp"), "/run/best.dcp", live_design_dirty=True
        )

    def test_mismatched_path_forces_reopen(self):
        assert not should_skip_reopen(
            Path("/run/best.dcp"), "/run/iter_start.dcp", False
        )

    def test_no_current_path_forces_reopen(self):
        assert not should_skip_reopen(None, "/run/best.dcp", False)

    def test_dirty_overrides_everything(self):
        """Dirty must force reopen regardless of path state."""
        assert not should_skip_reopen(None, "/run/best.dcp", True)
        assert not should_skip_reopen(
            Path("/run/other.dcp"), "/run/best.dcp", True
        )


# ── P0-2: directive whitelist tightening ─────────────────────────────────


class TestP0DirectiveWhitelist:
    def test_place_whitelist_excludes_rejected_netdelay(self):
        """NetDelay_high/medium/low are rejected by Vivado 2025.1 (18-641)."""
        for d in ("NetDelay_high", "NetDelay_medium", "NetDelay_low"):
            assert d not in vms.PLACE_SAFE_DIRECTIVES, f"{d} should be removed"

    def test_route_whitelist_excludes_strategy_preset_names(self):
        """Congestion_Explore / Congestion_NetDelay_* are Vivado strategy preset
        names, not route_design -directive values; rejected by 2025.1."""
        for d in (
            "Congestion_Explore",
            "Congestion_NetDelay_high",
            "Congestion_NetDelay_medium",
            "Congestion_NetDelay_low",
        ):
            assert d not in vms.ROUTE_SAFE_DIRECTIVES, f"{d} should be removed"

    def test_route_whitelist_keeps_valid_directives(self):
        for d in (
            "Default", "Explore", "AlternateRoutability", "Performance_Explore",
            "HigherDelayCost", "NoTimingRelaxation", "SSI_Explore",
        ):
            assert d in vms.ROUTE_SAFE_DIRECTIVES, f"{d} should remain"

    def test_place_whitelist_keeps_valid_directives(self):
        for d in (
            "Default", "Explore", "Performance_NetDelay_high",
            "Congestion_SpreadLogic_high", "Area_Explore",
        ):
            assert d in vms.PLACE_SAFE_DIRECTIVES, f"{d} should remain"


# ── P0-2: dynamic directive fallback detection ───────────────────────────


class TestP0DirectiveFallback:
    def test_detects_18_641_error(self):
        out = ("ERROR: [Constraints 18-641] Directive 'Congestion_Explore' "
               "is not a recognized directive.")
        assert vms._is_unrecognized_directive_error(out)

    def test_detects_not_a_recognized_directive_phrase(self):
        out = "prefix Directive 'X' is not a recognized directive. trailing"
        assert vms._is_unrecognized_directive_error(out)

    def test_false_for_normal_output(self):
        assert not vms._is_unrecognized_directive_error("Placement complete.")

    def test_false_for_other_vivado_error(self):
        """A non-directive Vivado error must NOT trigger the directive fallback
        (retrying with default placement/routing would waste a full P&R run)."""
        out = "ERROR: [Place 30-99] Placing failed due to legalization."
        assert not vms._is_unrecognized_directive_error(out)

    def test_false_for_empty_or_none(self):
        assert not vms._is_unrecognized_directive_error("")
        assert not vms._is_unrecognized_directive_error(None)


# ── P0-2: strategy library directive fix ─────────────────────────────────


class TestP0StrategyLibraryDirective:
    def test_congestion_route_explore_uses_alternate_routability(self):
        seq = STRATEGIES["CongestionRouteExplore"]["sequence"]
        route_step = next(s for s in seq if s["step"] == "route_design")
        assert route_step["params"]["directive"] == "AlternateRoutability"
        assert route_step["params"]["directive"] != "Congestion_Explore"
