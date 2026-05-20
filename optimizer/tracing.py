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
