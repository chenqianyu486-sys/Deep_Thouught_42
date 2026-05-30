"""Check exit node: evaluate termination conditions.

Checks WNS target, no-improvement limit, cost limit, and wall-clock timeout.

Reference: dcp_optimizer.py get_completion() (L4926-4948),
optimize() (L5203-5216).
"""

from __future__ import annotations

import logging
import time

from ..state import OptimizerState, record_flow_signal
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.timing import is_valid_wns
from ..pure.constants import WNS_TARGET_THRESHOLD, GLOBAL_NO_IMPROVEMENT_LIMIT
from ..color import green, yellow

logger = logging.getLogger(__name__)


async def check_exit_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Evaluate termination conditions.

    Checks:
        1. WNS target met (latest_wns >= 0)
        2. Global no-improvement limit reached
        3. Cost hard limit reached
        4. Wall-clock timeout (redundant safety net with iteration_start)

    Note: This node sets state.control.is_done; the after_check_exit edge
    function reads it to route to SAVE_OUTPUT or ITERATION_START.
    Node return values are not used for routing (graph edges decide).

    Returns:
        Next node name (edge after_check_exit resolves final destination).
    """
    # Wall-clock timeout (redundant with iteration_start)
    if state.control.start_time is not None:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            logger.warning(
                yellow(f"[check_exit] Wall-clock timeout: "
                       f"{elapsed:.0f}s > {state.control.wall_clock_timeout:.0f}s")
            )
            record_flow_signal(state, "SYSTEM_EXIT", "wall_clock_timeout", phase="CHECK_EXIT")
            return NodeName.CHECK_EXIT

    # WNS target check
    current_valid = (
        state.timing.latest_wns is not None
        and state.timing.latest_wns >= WNS_TARGET_THRESHOLD
        and is_valid_wns(
            state.timing.latest_wns,
            state.timing.clock_period,
            state.timing.best_wns,
        )
    )

    if current_valid:
        state.control.is_done = True
        state.control.done_reason = "wns_target_met"
        logger.info(
            green(f"[check_exit] WNS target met: "
                  f"{state.timing.latest_wns:.3f} ns >= {WNS_TARGET_THRESHOLD:.1f} ns")
        )
        record_flow_signal(state, "DONE", "wns_target_met", phase="CHECK_EXIT")
        return NodeName.CHECK_EXIT

    # No-improvement limit
    if state.iteration.global_no_improvement >= GLOBAL_NO_IMPROVEMENT_LIMIT:
        state.control.is_done = True
        state.control.done_reason = "max_no_improvement"
        logger.info(
            f"[check_exit] No-improvement limit reached: "
            f"{state.iteration.global_no_improvement} >= {GLOBAL_NO_IMPROVEMENT_LIMIT}"
        )
        record_flow_signal(state, "SYSTEM_EXIT", "max_no_improvement", phase="CHECK_EXIT")
        return NodeName.CHECK_EXIT

    # WNS stagnation: best WNS unchanged for 2+ iterations AND no strategy
    # switch in the most recent iteration — design is likely at its limit.
    if (state.iteration.global_no_improvement >= 2
            and state.timing.best_wns_iteration is not None
            and state.timing.best_wns_iteration < state.iteration.current - 1):
        state.control.is_done = True
        state.control.done_reason = "wns_stagnated"
        logger.info(
            f"[check_exit] WNS stagnated: best_wns={state.timing.best_wns:.3f}ns "
            f"reached at iter {state.timing.best_wns_iteration}, "
            f"now at iter {state.iteration.current} with {state.iteration.global_no_improvement} no-improvement iterations"
        )
        record_flow_signal(state, "SYSTEM_EXIT", "wns_stagnated", phase="CHECK_EXIT")
        return NodeName.CHECK_EXIT

    # Cost limit
    if state.cost.total_cost >= state.cost.cost_hard_limit:
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        logger.warning(
            yellow(f"[check_exit] Cost limit reached: "
                   f"${state.cost.total_cost:.4f} >= ${state.cost.cost_hard_limit:.2f}")
        )
        record_flow_signal(state, "SYSTEM_EXIT", "cost_limit", phase="CHECK_EXIT")
        return NodeName.CHECK_EXIT

    logger.debug(
        f"[check_exit] Continuing: WNS={state.timing.latest_wns}, "
        f"no_improve={state.iteration.global_no_improvement}, "
        f"cost=${state.cost.total_cost:.4f}"
    )
    return NodeName.CHECK_EXIT
