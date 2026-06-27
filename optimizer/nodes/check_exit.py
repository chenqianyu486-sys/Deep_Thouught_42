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

# Contest score is dominated by Fmax improvement, but cost and wall-clock
# penalties scale with that improvement. Late in the run, bank a verified gain
# instead of spending the final budget on low-probability exploration.
SCORE_GUARD_ELAPSED_FRACTION = 0.70
SCORE_GUARD_MIN_ITERATION = 2
SCORE_GUARD_MIN_WNS_GAIN_NS = 0.003
SCORE_GUARD_STALL_LIMIT = 1


def _competition_score_guard_reason(state: OptimizerState, elapsed: float) -> str:
    """Return early-stop reason when it is better to bank the current best."""
    timeout = state.control.wall_clock_timeout
    if timeout <= 0 or elapsed < timeout * SCORE_GUARD_ELAPSED_FRACTION:
        return ""
    if state.iteration.current < SCORE_GUARD_MIN_ITERATION:
        return ""
    if state.timing.initial_wns is None or state.timing.best_wns == float('-inf'):
        return ""
    best_iteration = state.timing.best_wns_iteration
    if best_iteration is None:
        return ""

    improved_this_iteration = best_iteration == state.iteration.current
    stalled_after_best = (
        best_iteration < state.iteration.current
        and state.iteration.global_no_improvement >= SCORE_GUARD_STALL_LIMIT
    )
    if not improved_this_iteration and not stalled_after_best:
        return ""

    wns_gain = state.timing.best_wns - state.timing.initial_wns
    if wns_gain < SCORE_GUARD_MIN_WNS_GAIN_NS:
        return ""

    remaining = max(timeout - elapsed, 0.0)
    return (
        "score_guard_bank_best:"
        f"gain={wns_gain:.3f}ns,"
        f"stalls={state.iteration.global_no_improvement},"
        f"elapsed={elapsed / 60:.1f}min,"
        f"remaining={remaining / 60:.1f}min"
    )



    # Check if initial timing already meets constraints - skip all optimization
    if (state.timing.initial_wns is not None 
            and state.timing.initial_wns >= 0.0
            and state.control.done_reason != "timing_already_met"):
        logger.info(
            "[check_exit] Setup timing already met (WNS=%.3fns). "
            "Exiting early to save time and cost.",
            state.timing.initial_wns,
        )
        state.control.done_reason = "timing_already_met"
        record_flow_signal(state, "SYSTEM_EXIT", "timing_already_met", phase="CHECK_EXIT")
        return NodeName.SAVE_OUTPUT
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

    # Cost limit check: exit if approaching budget limit with unclear improvement
    COST_LIMIT_WARN_FRACTION = 0.90  # Warn at 90% of budget
    cost_used = getattr(getattr(state, "cost", None), "total_cost", 0.0)
    cost_limit = getattr(getattr(state, "cost", None), "cost_hard_limit", 10.0)
    if cost_limit > 0 and cost_used > cost_limit * COST_LIMIT_WARN_FRACTION:
        logger.warning(
            "[check_exit] Cost limit approaching: $%.4f / $%.2f (%.0f%%)",
            cost_used, cost_limit, 100 * cost_used / cost_limit,
        )
        # If no improvement in last 2 iterations and cost is high, exit
        if (state.iteration.global_no_improvement >= 2 
                and state.timing.best_wns is not None
                and state.timing.best_wns > float("-inf")):
            logger.info("[check_exit] Exiting due to cost limit + no improvement")
            state.control.done_reason = "cost_limit_no_improvement"
            record_flow_signal(state, "SYSTEM_EXIT", "cost_limit_no_improvement", phase="CHECK_EXIT")
            return NodeName.SAVE_OUTPUT

    # Per-iteration cost guard: if single iteration cost exceeds threshold, warn
    ITER_COST_WARN_THRESHOLD = 0.50  # dollars
    if (hasattr(state, "cost") and hasattr(state.cost, "iteration_cost")
            and state.cost.iteration_cost > ITER_COST_WARN_THRESHOLD):
        logger.warning(
            "[check_exit] High iteration cost: $%.4f (threshold $%.2f). "
            "Consider using cheaper models.",
            state.cost.iteration_cost, ITER_COST_WARN_THRESHOLD,
        )
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

    # Contest scoring guard: once a strong late-run gain is banked, stop before
    # the remaining wall-clock/cost penalties erode the score.
    elapsed = time.time() - state.control.start_time if state.control.start_time is not None else 0.0
    score_guard_reason = _competition_score_guard_reason(state, elapsed)
    if score_guard_reason:
        state.control.is_done = True
        state.control.done_reason = "score_guard_bank_best"
        logger.info(
            green(f"[check_exit] Banking best result for contest score: {score_guard_reason}")
        )
        record_flow_signal(state, "SYSTEM_EXIT", score_guard_reason, phase="CHECK_EXIT")
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

    # REMOVED: wns_stagnated check was too aggressive (exited after 2 iterations
    # with no best improvement). With GLOBAL_NO_IMPROVEMENT_LIMIT=5, the
    # no-improvement limit provides sufficient exit condition.

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

# Check exit: absolute maximum wall clock ratio
ABSOLUTE_MAX_WALL_CLOCK_RATIO = 1.05  # Allow 5% overrun for cleanup

def should_exit_immediately(state) -> bool:
    """Emergency exit check - true if process must stop NOW."""
    if state.control.done_reason: return True
    if getattr(state.control, "emergency_exit", False): return True
    return False

def compute_exit_urgency(state) -> str:
    """How urgent is it to exit? none, low, medium, high, critical."""
    if state.iteration.global_no_improvement >= 4: return "critical"
    if state.iteration.global_no_improvement >= 3: return "high"
    if state.iteration.global_no_improvement >= 2: return "medium"
    if state.iteration.global_no_improvement >= 1: return "low"
    return "none"

def compute_remaining_budget_ratio(state) -> float:
    """How much of wall clock + cost budget remains."""
    t_elapsed = getattr(getattr(state, "control", None), "elapsed_seconds", 0)
    t_total = max(state.control.wall_clock_timeout, 1)
    c_spent = getattr(getattr(state, "cost", None), "total_cost", 0)
    c_total = max(getattr(getattr(state, "cost", None), "cost_hard_limit", 10), 0.01)
    t_ratio = 1 - min(t_elapsed / t_total, 1)
    c_ratio = 1 - min(c_spent / c_total, 1)
    return min(t_ratio, c_ratio)

def compute_exit_score_threshold(initial_wns: float, cost_spent: float, elapsed_h: float) -> float:
    """Minimum score needed to justify continuing."""
    return elapsed_h * 5.0 + cost_spent * 10.0

def _compute_urgency_score(state) -> float:
    """0=no urgency, 1=must exit now."""
    score = 0.0
    if state.iteration.global_no_improvement >= 4: score += 0.4
    if state.iteration.global_no_improvement >= 3: score += 0.3
    if state.iteration.global_no_improvement >= 2: score += 0.2
    if state.iteration.current >= 8: score += 0.3
    return min(score, 1.0)

def _compute_optimal_stopping_point(wns_history: list) -> int:
    """Optimal iteration to stop based on diminishing returns."""
    if len(wns_history) < 3: return -1
    improvements = [wns_history[i+1] - wns_history[i] for i in range(len(wns_history)-1)]
    for i in range(1, len(improvements)):
        if improvements[i] < improvements[i-1] * 0.3:
            return i + 1  # Diminishing returns detected
    return -1

def _compute_time_value(remaining_seconds: float, expected_gain_mhz: float) -> float:
    """Value of remaining time: expected score gain per second."""
    if remaining_seconds <= 0: return 0.0
    return expected_gain_mhz / remaining_seconds
