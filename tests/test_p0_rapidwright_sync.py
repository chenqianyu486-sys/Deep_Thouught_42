"""Tests for P0: RapidWright <-> Vivado design-state sync invariant.

Covers sync_rapidwright_baseline reload/skip behavior and the
RAPIDWRIGHT_MUTATE_TOOLS classification. The bug: RapidWright's in-memory
_current_design is mutated in place by netlist strategies and never resynced
to best_checkpoint, so later RW strategies run on a drifted netlist
(run-20260713_130643: after fanout, replicate/muxf ran on original+fanout).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from optimizer.state import OptimizerState
from optimizer.nodes.subgraphs.phase_execute import (
    RAPIDWRIGHT_MUTATE_TOOLS,
    sync_rapidwright_baseline,
)


def _make_state(best_path: Path) -> OptimizerState:
    state = OptimizerState()
    state.control.best_checkpoint_path = best_path
    state.timing.design_size_factor = 1.0
    return state


def _make_deps() -> MagicMock:
    deps = MagicMock()
    deps.rapidwright_session = MagicMock()
    deps.vivado_session = MagicMock()
    return deps


def _patch_call(monkeypatch, recorder: list):
    async def fake_call(tool_name, arguments, rw, viv, design_size_factor=1.0, **kw):
        recorder.append((tool_name, arguments))
        return ""

    monkeypatch.setattr(
        "optimizer.nodes.subgraphs.phase_execute.call_tool_fn", fake_call
    )


def test_state_has_rapidwright_sync_fields():
    state = OptimizerState()
    assert state.control.rapidwright_dcp_path is None
    assert state.control.rapidwright_design_dirty is False


def test_sync_reloads_when_dirty(monkeypatch, tmp_path):
    # After a netlist-mutating strategy, RW is dirty even though the path still
    # matches best_checkpoint - reload must happen (mirrors Vivado live_design_dirty).
    best = tmp_path / "best.dcp"
    best.write_bytes(b"x")
    state = _make_state(best)
    state.control.rapidwright_dcp_path = best
    state.control.rapidwright_design_dirty = True

    rec: list = []
    _patch_call(monkeypatch, rec)
    asyncio.run(sync_rapidwright_baseline(state, _make_deps(), best))

    assert rec == [("rapidwright_read_checkpoint", {"dcp_path": str(best.resolve())})]
    assert state.control.rapidwright_dcp_path == best.resolve()
    assert state.control.rapidwright_design_dirty is False


def test_sync_reloads_on_path_mismatch(monkeypatch, tmp_path):
    best = tmp_path / "best.dcp"
    best.write_bytes(b"x")
    state = _make_state(best)
    state.control.rapidwright_dcp_path = tmp_path / "stale.dcp"
    state.control.rapidwright_design_dirty = False

    rec: list = []
    _patch_call(monkeypatch, rec)
    asyncio.run(sync_rapidwright_baseline(state, _make_deps(), best))
    assert len(rec) == 1 and rec[0][0] == "rapidwright_read_checkpoint"


def test_sync_skips_when_already_synced(monkeypatch, tmp_path):
    best = tmp_path / "best.dcp"
    best.write_bytes(b"x")
    state = _make_state(best)
    state.control.rapidwright_dcp_path = best
    state.control.rapidwright_design_dirty = False

    async def boom(*a, **kw):
        raise AssertionError("should not reload when target already loaded and clean")

    monkeypatch.setattr("optimizer.nodes.subgraphs.phase_execute.call_tool_fn", boom)
    asyncio.run(sync_rapidwright_baseline(state, _make_deps(), best))


def test_sync_skips_when_target_missing(monkeypatch, tmp_path):
    missing = tmp_path / "nope.dcp"
    state = _make_state(missing)
    state.control.rapidwright_design_dirty = True

    async def boom(*a, **kw):
        raise AssertionError("should not reload a missing target")

    monkeypatch.setattr("optimizer.nodes.subgraphs.phase_execute.call_tool_fn", boom)
    asyncio.run(sync_rapidwright_baseline(state, _make_deps(), missing))


def test_sync_reloads_on_first_load(monkeypatch, tmp_path):
    best = tmp_path / "best.dcp"
    best.write_bytes(b"x")
    state = _make_state(best)
    assert state.control.rapidwright_dcp_path is None  # never loaded

    rec: list = []
    _patch_call(monkeypatch, rec)
    asyncio.run(sync_rapidwright_baseline(state, _make_deps(), best))
    assert len(rec) == 1


def test_mutate_tools_classification():
    # Netlist-mutating tools must be tracked so the next sync reloads.
    for t in [
        "rapidwright_execute_fanout_strategy",
        "rapidwright_replicate_critical_cells",
        "rapidwright_execute_muxf_tree_reorder_strategy",
        "rapidwright_flatten_lut_cascade",
        "rapidwright_execute_combinational_rebalancing_strategy",
        "rapidwright_execute_lut_muxf_repack_strategy",
        "rapidwright_smart_retiming",
        "rapidwright_execute_register_retiming",
        "rapidwright_optimize_pin_swapping",
        "rapidwright_execute_net_swapping",
        "rapidwright_execute_congestion_spreading",
    ]:
        assert t in RAPIDWRIGHT_MUTATE_TOOLS, t
    # Plan-only (Vivado executes) and read-only tools must NOT mark RW dirty.
    for t in [
        "rapidwright_execute_pblock_strategy",
        "rapidwright_execute_opt_design_strategy",
        "rapidwright_get_design_info",
        "rapidwright_analyze_pblock_region",
        "rapidwright_analyze_congestion",
        "rapidwright_read_checkpoint",
        "vivado_physopt_and_route",
        "vivado_place_design",
    ]:
        assert t not in RAPIDWRIGHT_MUTATE_TOOLS, t
