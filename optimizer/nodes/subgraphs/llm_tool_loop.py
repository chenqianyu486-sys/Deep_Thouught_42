"""LLM tool loop node: 4-phase state machine dispatcher.

The monolithic while-True loop has been refactored into a 4-phase state machine:
  ANALYZE -> SELECT_STRATEGY -> EXECUTE -> EVALUATE -> (loop back or exit)

Each phase has targeted context and a focused tool subset.
Phase isolation with structured handoff keeps context clean per phase.
"""

from __future__ import annotations

import logging
import time

from optimizer.state import OptimizerState, record_flow_signal
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, MAX_STRATEGY_CYCLES
from optimizer.pure.phase_policy import build_phase_exit_contract
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
    strategy_cycle_count = 0  # tracks how many strategies tried this iteration

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
            logger.error(
                f"[llm_tool_loop] Phase {phase.value} failed: {e}",
                exc_info=True,
            )
            record_flow_signal(
                state, "PHASE_ERROR", f"{phase.value}:{type(e).__name__}",
                phase=phase.value,
            )
            # Recovery: attempt to continue via SWITCH_STRATEGY instead of
            # silently killing the iteration.  The LLM will see the failure
            # in the next ANALYZE round and can adapt.
            if phase == LoopPhase.EVALUATE:
                # EVALUATE failed — safe to restart from ANALYZE
                next_phase = LoopPhase.ANALYZE
            else:
                # ANALYZE / SELECT / EXECUTE failed — force strategy switch
                state.strategy.current_strategy = ""
                state.strategy.current_phase = ""
                state.control.done_reason = "phase_error"
                next_phase = LoopPhase.EVALUATE

        logger.info(
            f"[llm_tool_loop] Phase transition: {phase.value} -> {next_phase.value}"
        )

        # ── Validate phase results ────────────────────────────────
        validation_issue = _validate_phase_result(phase, state)
        if validation_issue:
            logger.warning(
                f"[llm_tool_loop] Phase {phase.value} validation: {validation_issue}"
            )
            record_flow_signal(
                state, "PHASE_VALIDATION", validation_issue,
                phase=phase.value,
            )

        # ── After EVALUATE: check if we should exit the iteration ──
        if phase == LoopPhase.EVALUATE:
            if state.control.is_done:
                logger.info(green(f"[llm_tool_loop] Done: {state.control.done_reason}"))
                return NodeName.ITERATION_END

            # Multi-strategy loop: allow trying another strategy within same iteration
            if state.control.done_reason in ("switch_strategy", "iteration_success"):
                if strategy_cycle_count < MAX_STRATEGY_CYCLES:
                    prev_reason = state.control.done_reason
                    strategy_cycle_count += 1
                    next_phase = LoopPhase.SELECT_STRATEGY
                    state.control.done_reason = ""  # clear to avoid re-trigger
                    logger.info(
                        f"[llm_tool_loop] Strategy cycle {strategy_cycle_count}/{MAX_STRATEGY_CYCLES} "
                        f"(prev={prev_reason}), looping to SELECT_STRATEGY"
                    )
                else:
                    logger.info(
                        f"[llm_tool_loop] Max strategy cycles reached ({strategy_cycle_count}), "
                        f"exiting iteration: {state.control.done_reason}"
                    )
                    return NodeName.ITERATION_END

            elif state.control.done_reason in ("flow_control_done_next_iteration", "rollback"):
                logger.info(f"[llm_tool_loop] Exiting iteration: {state.control.done_reason}")
                return NodeName.ITERATION_END

            else:
                # CONTINUE or unknown — loop back to ANALYZE for re-analysis
                if next_phase == LoopPhase.ANALYZE:
                    logger.info("[llm_tool_loop] Looping back to ANALYZE")
                elif next_phase == LoopPhase.SELECT_STRATEGY:
                    logger.info("[llm_tool_loop] Looping back to SELECT_STRATEGY")
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
    contract = build_phase_exit_contract(
        round_count=total_rounds,
        max_rounds=MAX_TOOL_ROUNDS,
        start_time=state.control.start_time,
        wall_clock_timeout=state.control.wall_clock_timeout,
        now=time.time(),
        user_exit_requested=state.control.user_exit_requested,
        total_cost=state.cost.total_cost,
        cost_hard_limit=state.cost.cost_hard_limit,
    )
    if not contract.should_exit:
        return False
    if contract.event == "max_rounds":
        logger.warning(yellow(f"[llm_tool_loop] Max rounds reached ({total_rounds} > {MAX_TOOL_ROUNDS})"))
    elif contract.event == "wall_clock_timeout":
        elapsed = time.time() - state.control.start_time if state.control.start_time else 0.0
        logger.warning(f"[llm_tool_loop] Wall-clock timeout: {elapsed:.0f}s")
    elif contract.event == "user_requested":
        logger.info("[llm_tool_loop] User exit requested")
    elif contract.event == "cost_limit":
        logger.warning("[llm_tool_loop] Cost limit reached")
    if contract.set_is_done:
        state.control.is_done = True
    if contract.done_reason:
        state.control.done_reason = contract.done_reason
    if contract.record_reason:
        record_flow_signal(state, "SYSTEM_EXIT", contract.record_reason, phase=state.strategy.current_phase)
    return True


def _check_wns_target_met(state: OptimizerState) -> bool:
    """Check if WNS target is met."""
    return (
        state.timing.latest_wns is not None
        and state.timing.latest_wns >= WNS_TARGET_THRESHOLD
        and is_valid_wns(state.timing.latest_wns, state.timing.clock_period, state.timing.best_wns)
    )


def _validate_phase_result(phase: LoopPhase, state: OptimizerState) -> str:
    """Validate that a phase produced meaningful results.

    Returns an issue description string, or empty string if OK.
    These are advisory warnings — they do NOT alter control flow.
    """
    if phase == LoopPhase.SELECT_STRATEGY:
        if not state.strategy.current_strategy:
            return "no_strategy_selected"
    elif phase == LoopPhase.EXECUTE:
        if not state.iteration.tools_used:
            return "no_tools_executed"
    elif phase == LoopPhase.ANALYZE:
        if not state.timing.field_freshness:
            return "no_dashboard_data_refreshed"
    return ""
