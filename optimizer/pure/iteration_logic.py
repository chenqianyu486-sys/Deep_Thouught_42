"""Iteration end logic pure functions.

Extracted from dcp_optimizer.py: _on_iteration_end (L2413-2450),
_infer_strategy_from_tools (L2460-2485), _append_iteration_narrative (L2487-2525).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import TaskCategory, OPTIMIZATION_PATTERNS


def update_iteration_counters(
    state: OptimizerState,
    wns_improved: bool,
    model_used: str,
    is_rollback: bool = False,
) -> None:
    """Update global_no_improvement, worker_consecutive_success/failures.

    Mutates state.iteration and state.model in-place.
    """
    task_type = state.model.current_task_type
    if not task_type:
        from .model_select import classify_task as _classify_task
        last_tool = state.iteration.tools_used[-1] if state.iteration.tools_used else ""
        task_type = _classify_task(last_tool)
    is_optimization = task_type == TaskCategory.OPTIMIZATION

    if wns_improved:
        state.model.worker_consecutive_failures = 0
        state.iteration.global_no_improvement = 0
        if model_used == state.model.worker_model and is_optimization:
            state.model.worker_consecutive_success += 1
    else:
        state.model.worker_consecutive_success = 0
        if model_used == state.model.worker_model and is_optimization:
            state.model.worker_consecutive_failures += 1
        # For high-frequency designs (period <= 2.0ns), small improvements
        # within 1% of clock period still count as progress
        # But if delta = 0 (no change at all), still increment
        if not is_rollback:
            if (state.timing.clock_period is not None
                    and state.timing.clock_period <= 2.0
                    and state.timing.latest_wns is not None
                    and state.timing.best_wns != float('-inf')):
                delta = state.timing.latest_wns - state.timing.best_wns
                threshold = state.timing.clock_period * 0.01  # 1% of period
                if delta > -threshold and delta < 0:  # Only skip if small negative delta
                    return  # don't increment no_improvement
        state.iteration.global_no_improvement += 1


def update_task_type_stats(
    state: OptimizerState,
    task_type: str,
    success: bool,
) -> None:
    """Update per-task model success stats used by model selection."""
    if not task_type or task_type == TaskCategory.UNKNOWN:
        return
    stats = state.model.task_type_stats.setdefault(task_type, {"total": 0, "success": 0})
    stats["total"] = int(stats.get("total", 0)) + 1
    if success:
        stats["success"] = int(stats.get("success", 0)) + 1


def infer_strategy_from_tools(tools: list[str]) -> str:
    """Deduce strategy label from tool sequence."""
    tool_str = " ".join(tools).lower()
    if any(kw in tool_str for kw in ["analyze_pblock", "pblock_strategy", "create_and_apply_pblock", "convert_fabric_region_to_pblock"]):
        return "PBLOCK"
    if any(kw in tool_str for kw in ["physopt_strategy", "phys_opt_design"]):
        return "PhysOpt"
    if any(kw in tool_str for kw in ["fanout_strategy", "optimize_fanout"]):
        return "Fanout"
    if any(kw in tool_str for kw in ["flatten_lut_cascade", "lut_cascade"]):
        return "LUTCascade"
    if any(kw in tool_str for kw in ["replicate_critical_cells", "cell_replication"]):
        return "CellReplication"
    if any(kw in tool_str for kw in ["optimize_pin_swapping", "pin_swap"]):
        return "PinSwap"
    if any(kw in tool_str for kw in ["register_retiming", "register_retime"]):
        return "RegisterRetiming"
    if any(kw in tool_str for kw in ["smart_retiming", "smart_retime"]):
        return "SmartRetiming"
    if any(kw in tool_str for kw in ["congestion_spread", "execute_congestion_spreading"]):
        return "CongestionSpreading"
    if any(kw in tool_str for kw in ["net_swapping", "execute_net_swapping"]):
        return "NetSwap"
    if any(kw in tool_str for kw in ["place_design", "route_design"]):
        return "PlaceRoute"
    if any(t in tool_str for t in ["report_", "get_", "extract_", "analyze_"]):
        return "Information"
    return "Unknown"


def build_iteration_narrative(
    iteration: int,
    model_used: str,
    current_task_type: str,
    wns_before: float | None,
    wns_after: float | None,
    tools_used: list[str],
    result_status: str | None,
    declared_strategy: str | None = None,
) -> dict:
    """Build structured narrative entry for iteration.

    Args:
        declared_strategy: Strategy name explicitly declared by LLM via
            report_step_state(strategy_name=...). Preferred over tool-name
            inference. If None, falls back to infer_strategy_from_tools().
    """
    if wns_before is not None and wns_after is not None:
        wns_delta = wns_after - wns_before
    else:
        wns_delta = None

    if wns_delta is None:
        outcome = "unknown"
    elif wns_delta > 0.001:
        outcome = "improved"
    elif wns_delta < -0.001:
        outcome = "regression"
    else:
        outcome = "unchanged"

    # Prefer LLM's declared strategy; fall back to tool-name inference
    strategy_label = declared_strategy or infer_strategy_from_tools(tools_used)

    return {
        "iteration": iteration,
        "model": model_used or "unknown",
        "task_type": current_task_type,
        "wns_before": wns_before,
        "wns_after": wns_after,
        "wns_delta": wns_delta,
        "tool_count": len(tools_used),
        "strategy_label": strategy_label,
        "outcome": outcome,
        "result_status": result_status,
    }
