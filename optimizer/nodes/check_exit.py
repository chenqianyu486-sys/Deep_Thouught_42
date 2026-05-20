"""Check exit node: evaluate termination conditions.

Checks WNS target, no-improvement limit, and cost limit.

Reference: dcp_optimizer.py get_completion() (L4926-4948),
optimize() (L5203-5216).
"""

from __future__ import annotations

import logging

from ..state import OptimizerState
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

    Returns:
        Next node name (edge after_check_exit resolves final destination).
    """
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
        return NodeName.CHECK_EXIT

    # No-improvement limit
    if state.iteration.global_no_improvement >= GLOBAL_NO_IMPROVEMENT_LIMIT:
        state.control.is_done = True
        state.control.done_reason = "max_no_improvement"
        logger.info(
            f"[check_exit] No-improvement limit reached: "
            f"{state.iteration.global_no_improvement} >= {GLOBAL_NO_IMPROVEMENT_LIMIT}"
        )
        return NodeName.CHECK_EXIT

    # Cost limit
    if state.cost.total_cost >= state.cost.cost_hard_limit:
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        logger.warning(
            yellow(f"[check_exit] Cost limit reached: "
                   f"${state.cost.total_cost:.4f} >= ${state.cost.cost_hard_limit:.2f}")
        )
        return NodeName.CHECK_EXIT

    logger.debug(
        f"[check_exit] Continuing: WNS={state.timing.latest_wns}, "
        f"no_improve={state.iteration.global_no_improvement}, "
        f"cost=${state.cost.total_cost:.4f}"
    )
    return NodeName.CHECK_EXIT
