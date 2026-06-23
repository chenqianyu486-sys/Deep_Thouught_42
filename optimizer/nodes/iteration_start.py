"""Iteration start node: begin a new optimization iteration.

Increments iteration counter, checks exit conditions (user quit,
wall-clock timeout, max iterations), snapshots WNS for rollback.

Reference: dcp_optimizer.py optimize() loop start (~line 5195)
"""

from __future__ import annotations

import logging
import time

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..color import green

logger = logging.getLogger(__name__)


async def iteration_start_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Begin a new optimization iteration.

    Actions:
        1. Increment iteration counter
        2. Reset per-iteration tool errors
        3. Check exit conditions (user quit, wall-clock, max iterations)
        4. Snapshot WNS state for rollback

    Note: Node return values are not used for routing — graph edges decide.
    The static edge from iteration_start always routes to select_model.
    Early-exit flags (is_done) are picked up by check_exit on the next pass.

    Returns:
        Next node name (deterministic: select_model).
    """
    # Check max iterations BEFORE incrementing (fix C-1: off-by-one)
    if state.iteration.current >= state.iteration.max_iterations:
        logger.info(
            f"[iteration_start] Max iterations reached: "
            f"{state.iteration.current} >= {state.iteration.max_iterations}"
        )
        state.control.is_done = True
        state.control.done_reason = "max_iterations_reached"
        return NodeName.SAVE_OUTPUT

    # Check wall-clock timeout BEFORE incrementing
    if state.control.start_time is not None:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            logger.warning(
                f"[iteration_start] Wall-clock timeout: "
                f"{elapsed:.0f}s > {state.control.wall_clock_timeout:.0f}s"
            )
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            return NodeName.SAVE_OUTPUT

    # Increment iteration
    state.iteration.current += 1
    state.iteration.tool_errors.clear()
    state.iteration.tools_used.clear()
    state.iteration.blocked_strategies.clear()
    state.iteration.tool_round = 0
    state.model.current_task_type = ""

    iter_num = state.iteration.current
    logger.info(green(
        f"[iteration_start] === Iteration {iter_num} === "
        f"(best_wns={state.timing.best_wns:.3f}ns, "
        f"cost=${state.cost.total_cost:.4f})"
    ))

    # Snapshot WNS/TNS for rollback (store prev_best_*)
    state.timing.prev_best_wns = state.timing.best_wns
    state.timing.prev_best_tns = state.timing.best_wns_tns

    return NodeName.SELECT_MODEL
