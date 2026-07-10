"""Regression tests for P2 fixes from run-20260710_190708 cross-analysis.

Covers:
    P2.7  - phys_opt-class strategies blocked when WNS < -0.5ns
            (strategy_library constants + catalog display + select-time guard)
    P2.8  - place_design skipped when design already fully placed
            (tool_router._session_is_fully_placed parsing + conservative failure)
    P2.10 - frozen-plan fallback_reason not duplicated in chain warning

P2.6 (EVALUATE guidance text) and P2.9 (analyze_critical_path_spread envelope
unwrap, needs a live RapidWright session) are not unit-tested here.
"""
from __future__ import annotations

import asyncio

from strategy_library import (
    PHYSOPT_CLASS_STRATEGIES,
    PHYSOPT_INEFFECTIVE_WNS_THRESHOLD,
    STRATEGIES,
    get_strategy_catalog,
)
from optimizer.state import OptimizerState
from optimizer.pure.execute_contracts import resolve_chain_step_arguments
from optimizer.pure.pblock_plan import PBLOCK_GLOBAL_MODE, PBLOCK_UNPLACE_GLOBAL


# ── P2.7: phys_opt WNS guard ───────────────────────────────────────────────


def test_p27_physopt_class_constants():
    assert PHYSOPT_INEFFECTIVE_WNS_THRESHOLD == -0.5
    # PhysOpt / PhysOptAggressive chain vivado_phys_opt_design / physopt_and_route;
    # MUXFTreeReorder chains vivado_phys_opt_design. All are ineffective when WNS
    # is deeply negative (Vivado Physopt 32-745).
    assert PHYSOPT_CLASS_STRATEGIES == frozenset({
        "PhysOpt", "PhysOptAggressive", "MUXFTreeReorder",
    })
    for s in PHYSOPT_CLASS_STRATEGIES:
        assert s in STRATEGIES


def test_p27_catalog_marks_physopt_blocked():
    blocked = {
        s: f"ineffective when WNS<{PHYSOPT_INEFFECTIVE_WNS_THRESHOLD}ns"
        for s in PHYSOPT_CLASS_STRATEGIES
    }
    cat = get_strategy_catalog(blocked_strategies=blocked)
    # All three must appear as [BLOCKED] placeholders (not in Available).
    for s in PHYSOPT_CLASS_STRATEGIES:
        assert STRATEGIES[s]["name"] in cat
    assert cat.count("[BLOCKED") >= 3


def test_p27_wns_ineffective_guard_blocks_only_when_deep_negative():
    from optimizer.nodes.subgraphs.phase_select_strategy import (
        _get_wns_ineffective_strategies,
    )
    s = OptimizerState()
    s.timing.latest_wns = -0.6
    assert _get_wns_ineffective_strategies(s) == set(PHYSOPT_CLASS_STRATEGIES)
    s.timing.latest_wns = -0.4
    assert _get_wns_ineffective_strategies(s) == set()
    s.timing.latest_wns = None
    assert _get_wns_ineffective_strategies(s) == set()


# ── P2.8: place_design no-op skip ──────────────────────────────────────────


class _Block:
    def __init__(self, text: str):
        self.text = text


class _Result:
    def __init__(self, text: str):
        self.content = [_Block(text)]


class _MockSession:
    def __init__(self, result):
        self._result = result

    async def call_tool(self, name, args):
        return self._result


class _FailSession:
    async def call_tool(self, name, args):
        raise RuntimeError("boom")


def test_p28_session_is_fully_placed_parses_is_placed():
    from optimizer.pure.tool_router import _session_is_fully_placed

    placed = _MockSession(_Result('{"is_placed": true, "is_routed": true}'))
    assert asyncio.run(_session_is_fully_placed(placed)) is True

    unplaced = _MockSession(_Result('{"is_placed": false, "is_routed": false}'))
    assert asyncio.run(_session_is_fully_placed(unplaced)) is False


def test_p28_session_is_fully_placed_conservative_on_failure():
    from optimizer.pure.tool_router import _session_is_fully_placed

    # On any failure, return False so the caller runs place_design (don't skip).
    assert asyncio.run(_session_is_fully_placed(_FailSession())) is False


# ── P2.10: fallback_reason dedup ───────────────────────────────────────────


def _plan_dict_with_fallback(reason: str) -> dict:
    """Minimal PblockExecutionPlan dict carrying a fallback_reason."""
    return {
        "plan_mode": PBLOCK_GLOBAL_MODE,
        "candidate_id": "global_cp_center",
        "pblock_name": "pblock_tight",
        "pblock_ranges": "SLICE_X0Y0:SLICE_X10Y299",
        "resource_multiplier": 2.0,
        "target_lut_count": 10000,
        "target_ff_count": 20000,
        "target_dsp_count": 0,
        "target_bram_count": 0,
        "bind_cells_to_pblock": False,
        "unplace_mode": PBLOCK_UNPLACE_GLOBAL,
        "is_soft": True,
        "place_directive": "Explore",
        "route_directive": "Explore",
        "fallback_reason": reason,
    }


def _route_step() -> dict:
    return {
        "tool": "vivado_route_design",
        "args": {},
        "args_from_skill": {"directive": "route_directive"},
    }


def test_p210_fallback_reason_not_duplicated_in_chain_note():
    """run-20260710_190708: frozen-plan fallback_reason appeared 2-3x in one
    warning line ('reason | reason'). The dedupe must keep it to one."""
    reason = "local bound-cell sizing collapsed to a single-column region"
    skill_data = {
        "route_directive": "Explore",
        "selected_pblock_plan": _plan_dict_with_fallback(reason),
        "pblock_fallback_reason": reason,  # SAME text as the plan's fallback_reason
    }
    _args, note = resolve_chain_step_arguments(
        "rapidwright_execute_pblock_strategy", _route_step(), skill_data
    )
    assert note is not None
    assert note.count(reason) == 1  # not duplicated


def test_p210_distinct_fallback_reasons_both_kept():
    """When selected_plan.fallback_reason and pblock_fallback_reason differ,
    both are kept (only exact duplicates are deduped)."""
    plan_reason = "plan-level fallback reason"
    top_reason = "top-level fallback reason"
    skill_data = {
        "route_directive": "Explore",
        "selected_pblock_plan": _plan_dict_with_fallback(plan_reason),
        "pblock_fallback_reason": top_reason,
    }
    _args, note = resolve_chain_step_arguments(
        "rapidwright_execute_pblock_strategy", _route_step(), skill_data
    )
    assert note is not None
    assert plan_reason in note
    assert top_reason in note
