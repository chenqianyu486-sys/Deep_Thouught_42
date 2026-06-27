"""State transition tracer for observability.

Logs every node entry/exit with key state metrics.
Exports transition history as JSON.
"""

from __future__ import annotations

import json
import logging
import time

from .state import OptimizerState
from .color import cyan

logger = logging.getLogger(__name__)


class StateTracer:
    """Logs every state transition for observability."""

    def __init__(self):
        self.transitions: list[dict] = []
        self._entry_times: dict[str, float] = {}

    def on_enter(self, node_name: str, state: OptimizerState) -> None:
        self._entry_times[node_name] = time.time()
        logger.debug(
            cyan("[GRAPH]") + f" Entering: {node_name} "
            f"(iter={state.iteration.current}, "
            f"wns={state.timing.latest_wns}, "
            f"cost=${state.cost.total_cost:.4f})"
        )

    def on_exit(self, node_name: str, state: OptimizerState) -> None:
        entry_time = self._entry_times.pop(node_name, time.time())
        duration = time.time() - entry_time

        # Extract flow_control signal from step_state if available
        flow_signal = None
        result_status = None
        if state.control.step_state:
            flow_signal = state.control.step_state.flow_control
            result_status = state.control.step_state.result_status

        entry = {
            "node": node_name,
            "timestamp": time.time(),
            "iteration": state.iteration.current,
            "best_wns": state.timing.best_wns,
            "latest_wns": state.timing.latest_wns,
            "model": state.model.current_model,
            "total_cost": state.cost.total_cost,
            "tool_round": state.iteration.tool_round,
            "is_done": state.control.is_done,
            "done_reason": state.control.done_reason,
            "flow_control_signal": flow_signal,
            "result_status": result_status,
            "current_phase": state.strategy.current_phase,
            "current_strategy": state.strategy.current_strategy,
            "duration": duration,
        }
        self.transitions.append(entry)
        logger.debug(
            cyan("[GRAPH]") + f" Exiting: {node_name} "
            f"(wns={state.timing.latest_wns}, "
            f"cost=${state.cost.total_cost:.4f}, "
            f"duration={duration:.1f}s)"
        )

    def on_edge(self, from_node: str, to_node: str, edge_type: str = "static") -> None:
        """Log edge resolution."""
        logger.info(
            cyan("[GRAPH]") + f" Edge: {from_node} -> {to_node} ({edge_type})"
        )

    def export(self, path: str) -> None:
        """Export transition history to JSON file."""
        with open(path, 'w') as f:
            json.dump(self.transitions, f, indent=2)
        logger.info(cyan("[GRAPH]") + f" Exported {len(self.transitions)} transitions to {path}")


# Score history for tracking contest formula over time
SCORE_HISTORY: list[dict] = []

def record_score_snapshot(state, label: str = ""):
    """Record a score snapshot for later analysis."""
    from .state import OptimizerState
    if state.timing.initial_wns is None or state.timing.best_wns == float("-inf"):
        return
    
    clock_ns = state.timing.clock_period or 1.5
    init_fmax = 1000.0 / (clock_ns - state.timing.initial_wns)
    final_fmax = 1000.0 / (clock_ns - state.timing.best_wns) if state.timing.best_wns < clock_ns else 9999
    alpha = final_fmax - init_fmax
    
    cost = getattr(getattr(state, "cost", None), "total_cost", 0.0)
    elapsed = getattr(getattr(state, "control", None), "elapsed_seconds", 0.0)
    
    score = alpha - 0.1 * alpha * cost - 0.1 * alpha * (elapsed / 3600.0)
    
    SCORE_HISTORY.append({
        "label": label,
        "iteration": state.iteration.current,
        "wns": state.timing.best_wns,
        "alpha": alpha,
        "cost": cost,
        "elapsed_h": elapsed / 3600.0,
        "score": score,
    })

# Tracing: log every phase transition for debugging
TRACE_PHASE_TRANSITIONS = True
TRACE_TOOL_CALLS = True

def trace_iteration_summary(state) -> str:
    """Generate a one-line summary of an iteration."""
    i = state.iteration.current
    w = state.timing.best_wns
    c = getattr(getattr(state, "cost", None), "total_cost", 0)
    return f"Iter{i}: WNS={w:.3f}ns Cost=${c:.4f}"

def format_trace_entry(node: str, event: str, data: dict) -> str:
    """Format a trace entry for logging."""
    import json
    return f"[TRACE] {node}:{event} {json.dumps(data)}"

def compute_trace_batch_size(trace_count: int) -> int:
    """Optimal batch size for trace flushing."""
    return min(100, max(10, trace_count // 10))

def compute_trace_retention_period(trace_count: int) -> int:
    """How many seconds to retain trace entries."""
    return min(3600, max(300, trace_count * 10))
