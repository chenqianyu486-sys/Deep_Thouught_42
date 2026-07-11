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
        # NOTE: Performance_NetDelay_high is a valid *route* directive only;
        # Vivado 2025.1 rejects it for place_design (Constraints 18-641), so it
        # was removed from PLACE_SAFE_DIRECTIVES (kept in ROUTE_SAFE_DIRECTIVES).
        for d in (
            "Default", "Explore", "Performance_Explore",
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


# ── P0-1 (run-20260711_193102): place/route_design descriptions must not
# advertise directives removed from the whitelist. The LLM follows the
# description text (not the enum) and burned 5 place_design calls on
# Performance_NetDelay_high / _ExtraTimingOpt / _RefinePlacement. ─────────


class TestP0DescriptionSync:
    REMOVED_PLACE_DIRECTIVES = (
        "Performance_ExtraTimingOpt",
        "Performance_NetDelay_high",
        "Performance_RefinePlacement",
    )

    def _tool_descriptions(self):
        import asyncio
        tools = asyncio.run(vms.list_tools())
        return {t.name: t.description for t in tools}

    def test_place_design_description_drops_full_whitelist(self):
        desc = self._tool_descriptions()["place_design"]
        assert "Full whitelist:" not in desc  # hardcoded list removed
        for d in self.REMOVED_PLACE_DIRECTIVES:
            assert d not in desc, f"place_design description still advertises {d}"

    def test_route_design_description_drops_full_whitelist(self):
        desc = self._tool_descriptions()["route_design"]
        assert "Full whitelist:" not in desc

    def test_descriptions_reference_enum_as_authoritative(self):
        descs = self._tool_descriptions()
        assert "enum" in descs["place_design"].lower()
        assert "enum" in descs["route_design"].lower()


# ── P0-3 (run-20260711_193102): Fanout's EXECUTE whitelist must expose a
# high-fanout fetch tool (the LLM idled 4 rounds with no way to obtain the
# required `nets` arg). ────────────────────────────────────────────────────


class TestP0FanoutDataAccess:
    def test_fanout_dependency_includes_fetch_tool(self):
        from optimizer.pure.tool_filter import STRATEGY_DEPENDENCY_TOOLS
        assert "Fanout" in STRATEGY_DEPENDENCY_TOOLS
        assert "vivado_get_cached_high_fanout_nets" in STRATEGY_DEPENDENCY_TOOLS["Fanout"]

    def test_fanout_execute_whitelist_keeps_fetch_tool(self):
        from optimizer.pure.tool_filter import filter_tools_for_phase, LoopPhase

        def mk(name):
            return {"function": {"name": name, "parameters": {"properties": {}}}}

        all_tools = [
            mk("rapidwright_execute_fanout_strategy"),
            mk("vivado_get_cached_high_fanout_nets"),
            mk("vivado_route_design"),  # unrelated -> must be filtered out
        ]
        filtered = filter_tools_for_phase(all_tools, LoopPhase.EXECUTE, "Fanout")
        names = {t["function"]["name"] for t in filtered}
        assert "vivado_get_cached_high_fanout_nets" in names  # P0-3 B
        assert "rapidwright_execute_fanout_strategy" in names
        assert "vivado_route_design" not in names  # strategy narrowing holds


# ── P0-2 (run-20260711_193102): NetSwap analysis must iterate placed cells
# (not device.getAllSites()) and early-exit once enough candidates are found. ─


class TestP0NetSwapScan:
    def test_analyze_uses_placed_cells_not_all_sites(self, monkeypatch):
        from unittest.mock import MagicMock
        import skills.net_swapping_strategy as nss

        site = MagicMock()
        site.getName.return_value = "SLICE_X0Y0"
        site.getInstanceX.return_value = 0
        site.getInstanceY.return_value = 0
        site.getSiteTypeEnum.return_value = "SLICE"
        cell = MagicMock()
        cell.getSite.return_value = site
        cell.getSiteInst.return_value = MagicMock()

        design = MagicMock()
        design.getCells.return_value = [cell]
        device = MagicMock()
        device.getAllSites.side_effect = AssertionError(
            "P0-2: analyze_net_swapping must not call device.getAllSites()"
        )
        design.getDevice.return_value = device

        monkeypatch.setattr(nss, "_find_lut_cells_in_site", lambda si: [])
        res = nss.analyze_net_swapping(design, max_candidates=5)
        assert "error" not in res
        assert res["summary"]["sites_scanned"] == 1

    def test_analyze_early_exit_and_truncation(self, monkeypatch):
        from unittest.mock import MagicMock
        import skills.net_swapping_strategy as nss

        cells = []
        for i in range(30):
            site = MagicMock()
            site.getName.return_value = f"SLICE_X{i}Y0"
            site.getInstanceX.return_value = i
            site.getInstanceY.return_value = 0
            site.getSiteTypeEnum.return_value = "SLICE"
            c = MagicMock()
            c.getSite.return_value = site
            c.getSiteInst.return_value = MagicMock()
            cells.append(c)

        design = MagicMock()
        design.getCells.return_value = cells
        design.getDevice.return_value = MagicMock()

        info_i = {"cell": MagicMock(), "cell_name": "ci", "bel_name": "bi",
                  "init": "0x1", "pin_net_map": {"A": "netA"}}
        info_j = {"cell": MagicMock(), "cell_name": "cj", "bel_name": "bj",
                  "init": "0x1", "pin_net_map": {"B": "netB"}}
        monkeypatch.setattr(nss, "_find_lut_cells_in_site", lambda si: [info_i, info_j])
        monkeypatch.setattr(nss, "_estimate_wirelength_reduction",
                            lambda *a, **k: 100.0)
        # max_candidates=5 -> collect_cap=max(10,20)=20 -> stop after 20 sites.
        res = nss.analyze_net_swapping(design, max_candidates=5, wirelength_threshold=50.0)
        assert len(res["candidates"]) == 5
        assert res["summary"]["sites_scanned"] == 20  # early-exited, not all 30

    def test_analyze_timeout_raised_to_120s(self):
        from skills import SkillRegistry
        skill = SkillRegistry.get("analyze_net_swapping")
        assert skill.get_metadata().timeout_ms == 120000
