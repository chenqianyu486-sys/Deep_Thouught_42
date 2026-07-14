"""Tests for P2 Issue 2A: no-op strategy transparency + missing-chain mutations.

Two bugs fixed:
1. Five DCP-writing strategies (replicate/pin_swap/net_swap/congestion_spreading/
   smart_retiming) had NO SKILL_CHAIN_ACTIONS entry, so their mutated DCP was
   never opened/placed/routed in Vivado (mutation silently lost).
2. should_skip_chain_for_empty_result only recognized successful_count (fanout),
   so replicate's real replications (replications_performed>0, no successful_count)
   would be wrongly skipped, and its no-op (skipped:True) wasn't surfaced clearly.

run-20260713_130643: replicate ran 0 replications (no-op) but the LLM thought
"replication ran but was ineffective" because the reason wasn't conveyed.
"""
from __future__ import annotations

from optimizer.pure.tool_chain_policy import (
    has_skill_chain,
    should_skip_chain_for_empty_result,
)


DCP_STRATEGIES = [
    "rapidwright_replicate_critical_cells",
    "rapidwright_optimize_pin_swapping",
    "rapidwright_execute_net_swapping",
    "rapidwright_execute_congestion_spreading",
    "rapidwright_smart_retiming",
]


def test_five_dcp_strategies_now_have_chains():
    # Previously missing -> mutations were never applied. All must have a chain
    # that opens the RW-written DCP, re-places, re-routes, and evaluates.
    for tool in DCP_STRATEGIES:
        assert has_skill_chain(tool), f"{tool} missing chain"


def test_replicate_noop_is_skipped():
    # 0 replications + skipped:True -> skip the chain (no DCP written).
    data = {
        "skipped": True,
        "replications_performed": 0,
        "message": "No cells with delay >= 0.25 ns or fanout >= 25 found on critical paths",
    }
    skip, reason = should_skip_chain_for_empty_result(
        "rapidwright_replicate_critical_cells", data)
    assert skip is True
    assert reason == "skipped"


def test_replicate_real_mutations_not_skipped():
    # Real replications wrote a DCP (checkpoint_path) -> must NOT skip, so the
    # chain opens+places+routes the mutated DCP (previously lost).
    data = {"replications_performed": 3, "checkpoint_path": "/tmp/cell_repl.dcp"}
    skip, _ = should_skip_chain_for_empty_result(
        "rapidwright_replicate_critical_cells", data)
    assert skip is False


def test_pin_swap_real_not_skipped_noop_skipped():
    # pin_swap success returns checkpoint_path (effect); no-op returns None.
    assert should_skip_chain_for_empty_result(
        "rapidwright_optimize_pin_swapping",
        {"swaps_performed": 5, "checkpoint_path": "/tmp/swap.dcp"}) == (False, None)
    assert should_skip_chain_for_empty_result(
        "rapidwright_optimize_pin_swapping",
        {"swaps_performed": 0, "checkpoint_path": None})[0] is True


def test_smart_retiming_uses_final_checkpoint_path_signal():
    # smart_retiming reports via final_checkpoint_path.
    assert should_skip_chain_for_empty_result(
        "rapidwright_smart_retiming",
        {"final_checkpoint_path": "/tmp/retime.dcp"}) == (False, None)
    assert should_skip_chain_for_empty_result(
        "rapidwright_smart_retiming",
        {"final_checkpoint_path": None, "skipped": True})[0] is True


def test_fanout_behavior_unchanged():
    # Regression guard: fanout (already chained) must behave as before.
    assert should_skip_chain_for_empty_result(
        "rapidwright_execute_fanout_strategy",
        {"successful_count": 16, "checkpoint_path": "/tmp/f.dcp"}) == (False, None)
    assert should_skip_chain_for_empty_result(
        "rapidwright_execute_fanout_strategy",
        {"successful_count": 0})[0] is True


def test_plan_strategies_still_run_on_ready_plan():
    # Regression guard: muxf/opt_design plan-style (status=ready + steps) run.
    for tool in ("rapidwright_execute_muxf_tree_reorder_strategy",
                 "rapidwright_execute_opt_design_strategy"):
        assert should_skip_chain_for_empty_result(
            tool, {"status": "ready", "steps": [{"tool": "vivado_phys_opt_design"}]}) == (False, None)
