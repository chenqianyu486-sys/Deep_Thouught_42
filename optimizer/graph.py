"""State machine graph executor.

NodeGraph runs a directed graph of async nodes. Each node receives
(state, deps) and returns the name of the next node. Edges can be
deterministic (string) or conditional (callable).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Union

from .state import OptimizerState
from .deps import NodeDeps
from .tracing import StateTracer
from .color import cyan

logger = logging.getLogger(__name__)

# Node function: async (state, deps) -> next_node_name
NodeFn = Callable[[OptimizerState, NodeDeps], Awaitable[str]]
# Edge: either a fixed node name or a conditional function
EdgeTarget = Union[str, Callable[[OptimizerState], str]]


class NodeGraph:
    """State machine graph executor."""

    def __init__(self, tracer: StateTracer | None = None):
        self._nodes: dict[str, NodeFn] = {}
        self._edges: dict[str, EdgeTarget] = {}
        self._tracer = tracer or StateTracer()

    def add_node(self, name: str, fn: NodeFn) -> None:
        """Register a node function."""
        if name in self._nodes:
            raise ValueError(f"Node '{name}' already registered")
        self._nodes[name] = fn

    def add_edge(self, from_node: str, to: EdgeTarget) -> None:
        """Register a deterministic (str) or conditional (callable) edge."""
        self._edges[from_node] = to

    async def run(
        self,
        initial_state: OptimizerState,
        deps: NodeDeps,
        entry: str,
    ) -> OptimizerState:
        """Execute the graph from entry node until 'end' or no edge."""
        state = initial_state
        current = entry

        while current != "end":
            # Check user exit request before each node
            if state.control.user_exit_requested:
                logger.info(cyan("[GRAPH]") + " User exit requested, routing to save_output")
                state.control.is_done = True
                state.control.done_reason = "user_requested"
                state.control.user_exit_requested = False
                current = "save_output"
                continue

            if current not in self._nodes:
                raise ValueError(
                    f"Unknown node: '{current}'. "
                    f"Registered: {list(self._nodes.keys())}"
                )

            self._tracer.on_enter(current, state)
            try:
                next_node = await self._nodes[current](state, deps)
            except Exception as e:
                logger.error(
                    f"[GRAPH] Node '{current}' raised {type(e).__name__}: {e}",
                    exc_info=True,
                )
                # Recovery: route to save_output so partial progress is preserved
                state.control.is_done = True
                state.control.done_reason = f"node_error:{current}"
                current = "save_output"
                continue
            self._tracer.on_exit(current, state)

            # Resolve edge
            edge = self._edges.get(current)
            if edge is None:
                logger.info(f"No edge from '{current}', stopping graph")
                break
            if callable(edge):
                resolved = edge(state)
                self._tracer.on_edge(current, resolved, edge_type="conditional")
                current = resolved
            else:
                self._tracer.on_edge(current, edge, edge_type="static")
                current = edge

            # Validate the resolved next node exists (unless 'end')
            if current != "end" and current not in self._nodes:
                raise ValueError(
                    f"Edge resolved to unknown node '{current}' "
                    f"(node function returned '{next_node}')"
                )

        return state


# Per-node execution timeout (seconds) - prevents any single node
# from consuming the entire wall-clock budget
NODE_TIMEOUT_SECONDS = {
    "INIT_ANALYSIS": 600,      # 10 min for initial timing analysis
    "ITERATION_START": 30,     # 30 sec for iteration setup
    "SELECT_MODEL": 30,        # 30 sec for model selection
    "PREPARE_CONTEXT": 60,     # 60 sec for context assembly
    "LLM_TOOL_LOOP": 1800,     # 30 min for LLM + tool execution (per iteration)
    "ITERATION_END": 60,       # 60 sec for WNS update and narrative
    "CHECK_EXIT": 30,          # 30 sec for exit condition check
    "SAVE_OUTPUT": 300,        # 5 min for final save and hold check
    "default": 120,            # 2 min default
}


# Error fallback: if any node fails, route to a safe state
ERROR_FALLBACK_NODE = "SAVE_OUTPUT"  # Always save output on critical error
NODE_RETRY_COUNT = 1                 # Retry failed nodes once before fallback
NODE_RETRY_DELAY_SECONDS = 5         # Wait before retry

# Graph: max total node transitions before forced exit
MAX_TOTAL_TRANSITIONS = 100

def get_node_priority(node_name: str) -> int:
    """Get node execution priority (lower = more critical)."""
    PRIORITIES = {"SAVE_OUTPUT": 0, "CHECK_EXIT": 1, "INIT_ANALYSIS": 2, "default": 5}
    return PRIORITIES.get(node_name, PRIORITIES["default"])

def estimate_graph_execution_time(state) -> float:
    """Estimate remaining execution time in seconds."""
    remaining_iters = max(0, 8 - state.iteration.current)
    return remaining_iters * 600  # ~10 min per iteration

def compute_graph_depth(node_name: str) -> int:
    """How deep in the graph is this node? (0=INIT, higher=later)."""
    depths = {"INIT_ANALYSIS": 0, "ITERATION_START": 1, "SELECT_MODEL": 2, "PREPARE_CONTEXT": 3, "LLM_TOOL_LOOP": 4, "ITERATION_END": 5, "CHECK_EXIT": 6, "SAVE_OUTPUT": 7}
    return depths.get(node_name, -1)

def get_node_retry_policy(node: str) -> dict:
    """Retry policy for each node."""
    critical = ["SAVE_OUTPUT", "INIT_ANALYSIS"]
    important = ["CHECK_EXIT", "ITERATION_END"]
    if node in critical: return {"max_retries": 3, "delay": 10}
    if node in important: return {"max_retries": 2, "delay": 5}
    return {"max_retries": 1, "delay": 2}

def compute_graph_cycle_count(state) -> int:
    """How many times we have cycled through the graph."""
    return max(0, state.iteration.current - 1)

def compute_graph_path_length(start: str, end: str) -> int:
    """Number of transitions between two graph nodes."""
    order = ["INIT_ANALYSIS", "ITERATION_START", "SELECT_MODEL", "PREPARE_CONTEXT", "LLM_TOOL_LOOP", "ITERATION_END", "CHECK_EXIT", "SAVE_OUTPUT"]
    try:
        return abs(order.index(end) - order.index(start))
    except ValueError:
        return 99
