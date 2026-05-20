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
            next_node = await self._nodes[current](state, deps)
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
                    f"Edge from '{current}' resolved to unknown node '{next_node}'"
                )

        return state
