"""Conditional edge functions and node name registry.

Edges are pure functions: state -> next_node_name.
"""

from __future__ import annotations

from enum import Enum

from .state import OptimizerState


class NodeName(str, Enum):
    """All node names in the optimizer graph."""
    INIT_ANALYSIS = "init_analysis"
    ITERATION_START = "iteration_start"
    SELECT_MODEL = "select_model"
    PREPARE_CONTEXT = "prepare_context"
    LLM_TOOL_LOOP = "llm_tool_loop"
    ITERATION_END = "iteration_end"
    CHECK_EXIT = "check_exit"
    SAVE_OUTPUT = "save_output"
    END = "end"


# ── Conditional edges ───────────────────────────────────────────

def after_init(state: OptimizerState) -> str:
    """After initial analysis: skip to save if timing already met."""
    if state.timing.initial_wns is not None and state.timing.initial_wns >= 0:
        return NodeName.SAVE_OUTPUT
    return NodeName.ITERATION_START


def after_check_exit(state: OptimizerState) -> str:
    """After check_exit: continue loop or save and exit."""
    if state.control.is_done:
        return NodeName.SAVE_OUTPUT
    if state.control.user_exit_requested:
        return NodeName.SAVE_OUTPUT
    if state.iteration.current >= state.iteration.max_iterations:
        return NodeName.SAVE_OUTPUT
    return NodeName.ITERATION_START
