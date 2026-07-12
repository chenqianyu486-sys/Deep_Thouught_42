"""Unit tests for the high-priority context-engineering fixes derived from
run dcp_optimizer_run-20260712_013828 log analysis.

Covers three fixes:
  Fix #1 - VivadoMCP.extract_critical_path_cells now resolves truncated Vivado
           net labels in top_delay_nodes (e.g. "M1[76]" -> "M1w[76]") via
           get_property PARENT, so hotspot names match the netlist.
  Fix #2 - phase_analyze persists live-fetched high_fanout_nets into state and
           auto-refreshes them on ANALYZE entry when stale (rollback recovery).
           parse_high_fanout_nets is the parser the persist path depends on.
  Fix #3 - Fanout tooling rejects low-fanout nets (MIN_FANOUT_TO_SPLIT=50) so
           they are skipped instead of harmfully split, and the framework
           skips the Vivado P&R chain when no net was actually split
           (should_skip_chain_for_empty_result).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make the MCP package dirs importable without their servers running.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("VivadoMCP", "RapidWrightMCP"):
    _p = str(_REPO_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vivado_mcp_server  # noqa: E402
import rapidwright_tools  # noqa: E402
from optimizer.pure.tool_chain_policy import should_skip_chain_for_empty_result  # noqa: E402
from optimizer.pure.timing import parse_high_fanout_nets  # noqa: E402


# ── Fix #1: _resolve_hotspot_net_names ──────────────────────────────────

class TestResolveHotspotNetNames:
    """Vivado's report_timing drops the 'w' suffix on LUT/MUXF wire nets.
    Hotspot labels must be resolved to parent net names so the LLM cannot
    misuse them as net names (run-20260712_013828: -1.220ns regression)."""

    def test_resolves_net_nodes_to_parent_names(self, monkeypatch):
        # Mock get_property PARENT resolution: M1[76] -> layer1_reg/M1w[76].
        def fake_run_tcl(command, timeout=None):
            if "M1[76]" in command:
                return "layer1_reg/M1w[76]"
            if "M2[84]" in command:
                return "layer2_reg/M2w[84]"
            return ""
        monkeypatch.setattr(vivado_mcp_server, "run_tcl_command", fake_run_tcl)

        paths = [{
            "top_delay_nodes": [
                {"kind": "net", "name": "layer1_reg/M1[76]", "incr_delay": 1.134},
                {"kind": "net", "name": "layer2_reg/M2[84]", "incr_delay": 0.883},
                {"kind": "cell", "name": "layer0_inst/N25/data_out[76]_i_19", "incr_delay": 0.376},
            ],
        }]
        vivado_mcp_server._resolve_hotspot_net_names(paths)

        names = {n["name"] for n in paths[0]["top_delay_nodes"]}
        assert "layer1_reg/M1w[76]" in names
        assert "layer2_reg/M2w[84]" in names
        # Cell node is left untouched.
        assert "layer0_inst/N25/data_out[76]_i_19" in names

    def test_keeps_original_name_when_resolution_fails(self, monkeypatch):
        # Tcl returns empty / error -> original label kept.
        monkeypatch.setattr(
            vivado_mcp_server, "run_tcl_command",
            lambda command, timeout=None: "[ERROR] net not found",
        )
        paths = [{
            "top_delay_nodes": [
                {"kind": "net", "name": "layer1_reg/M1[76]", "incr_delay": 1.0},
            ],
        }]
        vivado_mcp_server._resolve_hotspot_net_names(paths)
        assert paths[0]["top_delay_nodes"][0]["name"] == "layer1_reg/M1[76]"

    def test_noop_when_no_net_nodes(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            vivado_mcp_server, "run_tcl_command",
            lambda command, timeout=None: called.__setitem__("n", called["n"] + 1) or "",
        )
        paths = [{
            "top_delay_nodes": [
                {"kind": "cell", "name": "some_cell", "incr_delay": 0.5},
            ],
        }]
        vivado_mcp_server._resolve_hotspot_net_names(paths)
        assert called["n"] == 0  # no Tcl calls when no net-kind hotspots

    def test_deduplicates_repeated_net_names(self, monkeypatch):
        calls = []
        def fake_run_tcl(command, timeout=None):
            calls.append(command)
            return "layer1_reg/M1w[76]" if "M1[76]" in command else ""
        monkeypatch.setattr(vivado_mcp_server, "run_tcl_command", fake_run_tcl)

        # Same net label appears on multiple paths' top_delay_nodes.
        paths = [
            {"top_delay_nodes": [{"kind": "net", "name": "layer1_reg/M1[76]", "incr_delay": 1.0}]},
            {"top_delay_nodes": [{"kind": "net", "name": "layer1_reg/M1[76]", "incr_delay": 0.9}]},
        ]
        vivado_mcp_server._resolve_hotspot_net_names(paths)
        # Resolved once per unique name, applied to all occurrences.
        assert len(calls) == 1
        assert paths[0]["top_delay_nodes"][0]["name"] == "layer1_reg/M1w[76]"
        assert paths[1]["top_delay_nodes"][0]["name"] == "layer1_reg/M1w[76]"


# ── Fix #2: parse_high_fanout_nets (parser the persist path depends on) ──

class TestParseHighFanoutNets:
    def test_parses_parent_net_name_section(self):
        report = (
            "=== High Fanout Nets in Critical Paths (Parent Net Names) ===\n"
            "Analyzed 50 worst timing paths\n"
            "Found 33 high fanout nets:\n\n"
            " Paths    Fanout  Parent Net Name\n"
            "------  --------  --------------------------------------------------\n"
            "     4       239  layer0_reg/M0w[48]\n"
            "     3       303  layer0_reg/M0w[18]\n"
            "=== End ===\n"
        )
        nets = parse_high_fanout_nets(report)
        assert nets == [
            ("layer0_reg/M0w[48]", 239, 4),
            ("layer0_reg/M0w[18]", 303, 3),
        ]

    def test_returns_empty_for_no_section(self):
        assert parse_high_fanout_nets("No high fanout nets found.") == []


# ── Fix #3: should_skip_chain_for_empty_result (fanout chain-skip) ──────

class TestFanoutChainSkip:
    """When every net is skipped (low fanout), successful_count == 0 and the
    framework must skip the Vivado P&R chain - no design change occurred."""

    def test_skips_chain_when_no_net_split(self):
        skill_data = {
            "status": "success",
            "successful_count": 0,
            "failed_count": 0,
            "results": [{"status": "skipped"}],
        }
        skip, reason = should_skip_chain_for_empty_result(
            "rapidwright_execute_fanout_strategy", skill_data,
        )
        assert skip is True
        assert reason == "no data produced"

    def test_runs_chain_when_at_least_one_split(self):
        skill_data = {
            "status": "success",
            "successful_count": 3,
            "failed_count": 2,
            "results": [{"status": "success"}, {"status": "skipped"}],
        }
        skip, _ = should_skip_chain_for_empty_result(
            "rapidwright_execute_fanout_strategy", skill_data,
        )
        assert skip is False  # successful_count > 0 -> design was modified


# ── Fix #3: optimize_fanout_batch MIN_FANOUT_TO_SPLIT guard ────────────

class TestFanoutMinFanoutSplit:
    """Nets with real fanout < MIN_FANOUT_TO_SPLIT are skipped (not split),
    so a fabricated net name whose real fanout is tiny cannot cause a
    harmful split (run-20260712_013828: fanout=2 net split into 2 parts)."""

    def test_low_fanout_net_is_skipped_not_split(self, monkeypatch):
        # Stub RapidWright globals so the function runs without Java/Vivado.
        monkeypatch.setattr(rapidwright_tools, "_initialized", True)
        mock_design = MagicMock()
        monkeypatch.setattr(rapidwright_tools, "_current_design", mock_design)
        monkeypatch.setattr(rapidwright_tools, "_ensure_design_loaded", lambda: None)

        low_fanout_net = MagicMock()
        low_fanout_net.getFanOut.return_value = 2  # below MIN_FANOUT_TO_SPLIT
        monkeypatch.setattr(
            rapidwright_tools, "_resolve_net_name",
            lambda design, name: (low_fanout_net, "layer1_reg/M1w[21]"),
        )

        # Stub the RapidWright Java package chain so the function's
        # `from com.xilinx.rapidwright.eco import FanOutOptimization` resolves
        # to a mock without needing JPype/Java. cutFanOutOfRoutedNet must never
        # be called for a low-fanout net (the skip happens before it).
        cut_called = []
        eco_mock = MagicMock()
        eco_mock.FanOutOptimization.cutFanOutOfRoutedNet.side_effect = (
            lambda *a, **k: cut_called.append(a)
        )
        for mod_name in (
            "com", "com.xilinx", "com.xilinx.rapidwright", "com.xilinx.rapidwright.eco",
        ):
            monkeypatch.setitem(sys.modules, mod_name, eco_mock if mod_name.endswith(".eco") else MagicMock())

        result = rapidwright_tools.optimize_fanout_batch(
            [{"net_name": "layer1_reg/M1[21]", "fanout": 60}],
        )

        assert result["successful_count"] == 0
        assert result["results"][0]["status"] == "skipped"
        assert result["results"][0]["original_fanout"] == 2
        assert cut_called == []  # no split performed

    def test_min_fanout_constant_is_50(self):
        assert rapidwright_tools.MIN_FANOUT_TO_SPLIT == 50
