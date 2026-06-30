"""Conditional edge functions and node name registry.

Edges are pure functions: state -> next_node_name.
"""

from __future__ import annotations

from enum import Enum

from .state import OptimizerState



# Edge transition optimization notes:
# - after_init: skip ITERATION_START if WNS already met (routed through check_exit)
# - after_check_exit: prefer SAVE_OUTPUT over ITERATION_START when score_guard triggers
# - after_iteration_end: skip CHECK_EXIT if wall_clock clearly exceeded
# These conditions are checked in the respective node implementations.


class NodeName(str, Enum):
    """All node names in the optimizer graph."""
    INIT_ANALYSIS = "init_analysis"
    ITERATION_START = "iteration_start"
    SELECT_MODEL = "select_model"
    PREPARE_CONTEXT = "prepare_context"
    LLM_TOOL_LOOP = "llm_tool_loop"
    ITERATION_END = "iteration_end"
    CHECK_EXIT = "check_exit"
    ROLLBACK = "rollback"
    SAVE_OUTPUT = "save_output"
    END = "end"


# ── Conditional edges ───────────────────────────────────────────

def after_init(state: OptimizerState) -> str:
    """After initial analysis: skip to save if timing already met."""
    if state.timing.initial_wns is not None and state.timing.initial_wns >= 0:
        return NodeName.SAVE_OUTPUT
    return NodeName.ITERATION_START


def after_check_exit(state: OptimizerState) -> str:
    """After check_exit: continue loop, rollback, or save and exit."""
    if state.control.is_done:
        return NodeName.SAVE_OUTPUT
    if state.control.user_exit_requested:
        return NodeName.SAVE_OUTPUT
    if state.iteration.current >= state.iteration.max_iterations:
        return NodeName.SAVE_OUTPUT
    if state.control.done_reason == "rollback":
        state.control.done_reason = None
        return NodeName.ROLLBACK
    return NodeName.ITERATION_START

# Edge condition: check if graph should short-circuit to save_output
def should_short_circuit(state) -> bool:
    """Check if we should skip remaining nodes and go to save_output."""
    return bool(state.control.done_reason)

def compute_edge_priority(from_node: str, to_node: str) -> int:
    """Edge priority for graph traversal (lower = traverse first)."""
    if to_node == "SAVE_OUTPUT": return 0
    if to_node == "CHECK_EXIT": return 1
    return 5

def get_edge_condition_description(from_n: str, to_n: str) -> str:
    """Human-readable description of an edge condition."""
    return f"Transition from {from_n} to {to_n}"

def compute_edge_cost(from_node: str, to_node: str) -> float:
    """Estimated time cost for a graph transition."""
    costs = {"INIT_ANALYSIS->ITERATION_START": 1, "CHECK_EXIT->ITERATION_START": 2, "CHECK_EXIT->SAVE_OUTPUT": 5, "default": 1}
    key = f"{from_node}->{to_node}"
    return costs.get(key, costs["default"])

def compute_transition_probability(from_n: str, to_n: str, history: list) -> float:
    """Probability of a transition based on history."""
    if not history: return 0.5
    matches = sum(1 for h in history if h.get("from") == from_n and h.get("to") == to_n)
    total = sum(1 for h in history if h.get("from") == from_n)
    return matches / max(total, 1)
