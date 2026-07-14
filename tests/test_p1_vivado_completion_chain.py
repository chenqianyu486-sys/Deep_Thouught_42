"""Tests for P1 Issue 1: Vivado-only place strategy EXEC_DONE completion chain.

PlaceRouteDirectiveExplore's execute_tool is vivado_place_design (a single
primitive with no SKILL_CHAIN_ACTIONS entry, not in POST_EVAL_TOOLS). Without
the completion chain, EXEC_DONE exits without routing/evaluating - the placed
design's WNS stays stale and the strategy is wrongly marked UNCHANGED
(run-20260713_130643: 12-min place_design never routed).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from optimizer.state import OptimizerState, DesignState
from optimizer.nodes.subgraphs.phase_execute import (
    _ensure_vivado_strategy_completion,
    _VIVADO_PLACE_COMPLETION_CHAIN,
)


def _make_state(strategy: str, design_state: DesignState = DesignState.PLACED) -> OptimizerState:
    state = OptimizerState()
    state.strategy.current_strategy = strategy
    state.timing.design_state = design_state
    state.timing.design_size_factor = 1.0
    return state


def _make_deps() -> MagicMock:
    deps = MagicMock()
    deps.rapidwright_session = MagicMock()
    deps.vivado_session = MagicMock()
    deps.compat = None
    return deps


def _patch_chain(monkeypatch, recorder: list):
    async def fake_chain(state, deps, tool_name, skill_data, tools_called,
                         chain_override=None):
        recorder.append({
            "tool_name": tool_name,
            "chain_override": chain_override,
        })

    monkeypatch.setattr(
        "optimizer.nodes.subgraphs.phase_execute._execute_single_chain_actions",
        fake_chain,
    )


def test_completion_runs_for_place_strategy(monkeypatch):
    # PlaceRouteDirectiveExplore -> vivado_place_design, design placed but not routed.
    state = _make_state("PlaceRouteDirectiveExplore", DesignState.PLACED)
    rec: list = []
    _patch_chain(monkeypatch, rec)
    asyncio.run(_ensure_vivado_strategy_completion(state, _make_deps(), []))

    assert len(rec) == 1
    assert rec[0]["tool_name"] == "vivado_place_design"
    assert rec[0]["chain_override"] is _VIVADO_PLACE_COMPLETION_CHAIN
    # Completion chain must route then evaluate (no open_checkpoint: design in memory).
    tools = [step["tool"] for step in _VIVADO_PLACE_COMPLETION_CHAIN]
    assert tools == ["vivado_route_design", "vivado_report_timing_summary",
                     "vivado_extract_critical_path_cells"]


def test_completion_runs_regardless_of_design_state(monkeypatch):
    # A re-place invalidates prior routing, so the completion chain runs even if
    # design_state claims ROUTED (it is stale after a place). It must not be gated
    # on design_state (only route tools set ROUTED, so post-place it is unreliable).
    for ds in (DesignState.PLACED, DesignState.ROUTED, DesignState.UNPLACED):
        state = _make_state("PlaceRouteDirectiveExplore", ds)
        rec: list = []
        _patch_chain(monkeypatch, rec)
        asyncio.run(_ensure_vivado_strategy_completion(state, _make_deps(), []))
        assert len(rec) == 1, f"completion should run for design_state={ds}"


def test_completion_skipped_for_non_place_strategy(monkeypatch):
    # PhysOpt -> vivado_physopt_and_route (composite, routes internally); no completion.
    # CongestionRouteExplore -> vivado_route_design (route-only, in POST_EVAL_TOOLS).
    for strategy in ("PhysOpt", "CongestionRouteExplore", "PBLOCK"):
        state = _make_state(strategy, DesignState.PLACED)

        async def boom(*a, **kw):
            raise AssertionError(f"should not run completion for {strategy}")

        monkeypatch.setattr(
            "optimizer.nodes.subgraphs.phase_execute._execute_single_chain_actions",
            boom)
        asyncio.run(_ensure_vivado_strategy_completion(state, _make_deps(), []))
