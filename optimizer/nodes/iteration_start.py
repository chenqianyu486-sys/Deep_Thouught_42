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
from optimizer.pure.tool_router import call_tool as call_tool_fn
from .subgraphs.phase_handoff import reset_design_fingerprint

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

    # Clear cross-iteration caches: previous iteration's tool results and
    # raw outputs are invalid for the new iteration's different design state.
    state.context.tool_cache.clear()
    state.context.raw_tool_outputs.clear()
    # Reset phase-handoff design fingerprint so the next phase transition
    # clears any remaining cached tool results from the prior iteration.
    reset_design_fingerprint()
    logger.info(f"[iteration_start] Tool cache and raw outputs cleared for iteration {state.iteration.current}")

    iter_num = state.iteration.current
    logger.info(green(
        f"[iteration_start] === Iteration {iter_num} === "
        f"(best_wns={state.timing.best_wns:.3f}ns, "
        f"cost=${state.cost.total_cost:.4f})"
    ))

    # Snapshot WNS/TNS for rollback (store prev_best_*)
    state.timing.prev_best_wns = state.timing.best_wns
    state.timing.prev_best_tns = state.timing.best_wns_tns

    # Save iteration start checkpoint for rollback baseline.
    # _ensure_iteration_start_checkpoint (in EXECUTE) reuses this.
    try:
        iter_ckpt = state.control.run_dir / f"iteration_{state.iteration.current}_start.dcp"
        if not iter_ckpt.exists():
            await call_tool_fn(
                "vivado_write_checkpoint",
                {"dcp_path": str(iter_ckpt.resolve()), "force": True},
                deps.rapidwright_session,
                deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            state.control.iteration_checkpoints.append((state.iteration.current, iter_ckpt))
            logger.info(f"[iteration_start] Saved iteration {state.iteration.current} start DCP")
    except Exception as e:
        logger.warning(f"[iteration_start] Failed to save iteration checkpoint: {e}")

    return NodeName.SELECT_MODEL
