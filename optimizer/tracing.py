"""State transition tracer for observability.

Logs every node entry/exit with key state metrics.
Exports transition history as JSON.
"""

from __future__ import annotations

import json
import logging
import time

from .state import OptimizerState

logger = logging.getLogger(__name__)


class StateTracer:
    """Logs every state transition for observability."""

    def __init__(self):
        self.transitions: list[dict] = []

    def on_enter(self, node_name: str, state: OptimizerState) -> None:
        logger.info(
            f"[GRAPH] Entering: {node_name} "
            f"(iter={state.iteration.current}, "
            f"wns={state.timing.latest_wns}, "
            f"cost=${state.cost.total_cost:.4f})"
        )

    def on_exit(self, node_name: str, state: OptimizerState) -> None:
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
        }
        self.transitions.append(entry)
        logger.info(
            f"[GRAPH] Exiting: {node_name} "
            f"(wns={state.timing.latest_wns}, "
            f"cost=${state.cost.total_cost:.4f})"
        )

    def export(self, path: str) -> None:
        """Export transition history to JSON file."""
        with open(path, 'w') as f:
            json.dump(self.transitions, f, indent=2)
        logger.info(f"[GRAPH] Exported {len(self.transitions)} transitions to {path}")
