"""LLM tool loop node: 4-phase state machine dispatcher.

The monolithic while-True loop has been refactored into a 4-phase state machine:
  ANALYZE -> SELECT_STRATEGY -> EXECUTE -> EVALUATE -> (loop back or exit)

Each phase has targeted context and a focused tool subset.
Phase isolation with structured handoff keeps context clean per phase.
"""

from __future__ import annotations

import logging
import time

from optimizer.state import OptimizerState
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase
from optimizer.pure.constants import WNS_TARGET_THRESHOLD
from optimizer.pure.timing import is_valid_wns
from optimizer.pure.compress import compress_context
from optimizer.color import green, yellow
from optimizer.nodes.subgraphs.phase_analyze import run_analyze_phase
from optimizer.nodes.subgraphs.phase_select_strategy import run_select_strategy_phase
from optimizer.nodes.subgraphs.phase_execute import run_execute_phase
from optimizer.nodes.subgraphs.phase_evaluate import run_evaluate_phase

logger = logging.getLogger(__name__)

# Max tool rounds per iteration (safety limit across all phases)
MAX_TOOL_ROUNDS = 80

# Compression check interval
COMPRESS_CHECK_INTERVAL = 5

# Phase runner dispatch table
PHASE_RUNNERS = {
    LoopPhase.ANALYZE: run_analyze_phase,
    LoopPhase.SELECT_STRATEGY: run_select_strategy_phase,
    LoopPhase.EXECUTE: run_execute_phase,
    LoopPhase.EVALUATE: run_evaluate_phase,
}


async def llm_tool_loop_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Execute the 4-phase optimization loop.

    The loop cycles through ANALYZE -> SELECT_STRATEGY -> EXECUTE -> EVALUATE.
    EVALUATE decides whether to loop back to ANALYZE or exit the iteration.

    Returns:
        Next node name (deterministic: ITERATION_END).
    """
    phase = LoopPhase.ANALYZE
    total_rounds = 0

    while True:
        total_rounds += 1

        # ── Check exit conditions ─────────────────────────────────
        if _check_exit_conditions(state, total_rounds):
            return NodeName.ITERATION_END

        # ── Compress context (throttled) ───────────────────────────
        if deps.memory_manager is not None and total_rounds % COMPRESS_CHECK_INTERVAL == 0:
            try:
                compress_context(state, deps)
            except Exception as e:
                logger.warning(f"[llm_tool_loop] Compression failed: {e}")

        # ── Run current phase ─────────────────────────────────────
        runner = PHASE_RUNNERS.get(phase)
        if runner is None:
            logger.error(f"[llm_tool_loop] Unknown phase: {phase}")
            return NodeName.ITERATION_END

        try:
            next_phase = await runner(state, deps)
        except Exception as e:
            logger.error(f"[llm_tool_loop] Phase {phase.value} failed: {e}")
            return NodeName.ITERATION_END

        logger.info(
            f"[llm_tool_loop] Phase transition: {phase.value} -> {next_phase.value}"
        )

        # ── After EVALUATE: check if we should exit the iteration ──
        if phase == LoopPhase.EVALUATE:
            if state.control.is_done:
                logger.info(green(f"[llm_tool_loop] Done: {state.control.done_reason}"))
                return NodeName.ITERATION_END

            if state.control.done_reason in ("switch_strategy", "iteration_success",
                                               "flow_control_done_next_iteration", "rollback"):
                logger.info(f"[llm_tool_loop] Exiting iteration: {state.control.done_reason}")
                return NodeName.ITERATION_END

            # If CONTINUE or unknown, loop back to ANALYZE for re-analysis
            if next_phase == LoopPhase.ANALYZE:
                logger.info("[llm_tool_loop] Looping back to ANALYZE")
            else:
                logger.info(f"[llm_tool_loop] Unexpected post-eval phase: {next_phase}, defaulting to ANALYZE")
                next_phase = LoopPhase.ANALYZE

        # ── Check WNS target ──────────────────────────────────────
        if _check_wns_target_met(state):
            state.control.is_done = True
            state.control.done_reason = "wns_target_met"
            return NodeName.ITERATION_END

        phase = next_phase


def _check_exit_conditions(state: OptimizerState, total_rounds: int) -> bool:
    """Check if the outer loop should exit."""
    if total_rounds > MAX_TOOL_ROUNDS:
        logger.warning(yellow(f"[llm_tool_loop] Max rounds reached ({total_rounds} > {MAX_TOOL_ROUNDS})"))
        return True

    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            logger.warning(f"[llm_tool_loop] Wall-clock timeout: {elapsed:.0f}s")
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            return True

    if state.control.user_exit_requested:
        logger.info("[llm_tool_loop] User exit requested")
        return True

    if state.cost.total_cost >= state.cost.cost_hard_limit:
        logger.warning(f"[llm_tool_loop] Cost limit reached")
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        return True

    return False


def _check_wns_target_met(state: OptimizerState) -> bool:
    """Check if WNS target is met."""
    return (
        state.timing.latest_wns is not None
        and state.timing.latest_wns >= WNS_TARGET_THRESHOLD
        and is_valid_wns(state.timing.latest_wns, state.timing.clock_period, state.timing.best_wns)
    )
