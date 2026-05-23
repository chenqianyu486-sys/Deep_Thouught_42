"""Unit tests for the optimizer state machine framework.

Tests cover:
    - NodeGraph: node registration, edge routing, conditional edges, run loop
    - State: dataclass creation, default values
    - Edges: conditional edge functions
    - Tracing: state transition logging

Run: python3 -m pytest optimizer/test_graph.py -v
"""

from __future__ import annotations

import asyncio
import pytest

from optimizer.state import OptimizerState, TimingState, IterationState, ControlState
from optimizer.deps import NodeDeps
from optimizer.graph import NodeGraph
from optimizer.edges import NodeName, after_init, after_check_exit
from optimizer.tracing import StateTracer


# ── Helpers ─────────────────────────────────────────────────────

def run_async(coro):
    """Run an async function in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── State tests ─────────────────────────────────────────────────

class TestOptimizerState:
    def test_default_state(self):
        state = OptimizerState()
        assert state.timing.best_wns == float('-inf')
        assert state.timing.latest_wns is None
        assert state.iteration.current == 0
        assert state.iteration.max_iterations == 50
        assert state.iteration.max_tool_rounds == 80
        assert state.cost.total_cost == 0.0
        assert state.control.is_done is False
        assert state.control.done_reason is None

    def test_mutable_state(self):
        """State is mutable — nodes can modify in-place."""
        state = OptimizerState()
        state.timing.best_wns = -0.5
        state.iteration.current = 3
        assert state.timing.best_wns == -0.5
        assert state.iteration.current == 3

    def test_sub_slices_independent(self):
        """Modifying one slice doesn't affect others."""
        state = OptimizerState()
        state.timing.best_wns = -1.0
        state.cost.total_cost = 0.5
        assert state.timing.best_wns == -1.0
        assert state.cost.total_cost == 0.5
        assert state.iteration.current == 0


# ── Edge tests ──────────────────────────────────────────────────

class TestEdges:
    def test_after_init_timing_met(self):
        """If initial WNS >= 0, skip to save_output."""
        state = OptimizerState()
        state.timing.initial_wns = 0.1
        assert after_init(state) == NodeName.SAVE_OUTPUT

    def test_after_init_timing_exactly_zero(self):
        """Boundary: WNS == 0.0 should route to save_output."""
        state = OptimizerState()
        state.timing.initial_wns = 0.0
        assert after_init(state) == NodeName.SAVE_OUTPUT

    def test_after_init_timing_not_met(self):
        """If initial WNS < 0, proceed to iteration_start."""
        state = OptimizerState()
        state.timing.initial_wns = -0.5
        assert after_init(state) == NodeName.ITERATION_START

    def test_after_init_no_wns(self):
        """If initial WNS is None, proceed to iteration_start."""
        state = OptimizerState()
        assert after_init(state) == NodeName.ITERATION_START

    def test_after_check_exit_done(self):
        """If is_done, go to save_output."""
        state = OptimizerState()
        state.control.is_done = True
        assert after_check_exit(state) == NodeName.SAVE_OUTPUT

    def test_after_check_exit_user_exit(self):
        """If user exit requested, go to save_output."""
        state = OptimizerState()
        state.control.user_exit_requested = True
        assert after_check_exit(state) == NodeName.SAVE_OUTPUT

    def test_after_check_exit_max_iterations(self):
        """If max iterations reached, go to save_output."""
        state = OptimizerState()
        state.iteration.current = 50
        assert after_check_exit(state) == NodeName.SAVE_OUTPUT

    def test_after_check_exit_continue(self):
        """Otherwise, loop back to iteration_start."""
        state = OptimizerState()
        state.iteration.current = 5
        assert after_check_exit(state) == NodeName.ITERATION_START


# ── Graph tests ─────────────────────────────────────────────────

class TestNodeGraph:
    def test_add_node(self):
        graph = NodeGraph()
        async def dummy(state, deps):
            return "end"
        graph.add_node("test", dummy)
        assert "test" in graph._nodes

    def test_add_duplicate_node_raises(self):
        graph = NodeGraph()
        async def dummy(state, deps):
            return "end"
        graph.add_node("test", dummy)
        with pytest.raises(ValueError, match="already registered"):
            graph.add_node("test", dummy)

    def test_simple_linear_graph(self):
        """Three nodes in sequence: a -> b -> end."""
        graph = NodeGraph()
        trace = []

        async def node_a(state, deps):
            trace.append("a")
            return "b"

        async def node_b(state, deps):
            trace.append("b")
            return "end"

        graph.add_node("a", node_a)
        graph.add_node("b", node_b)
        graph.add_edge("a", "b")
        graph.add_edge("b", "end")

        state = OptimizerState()
        deps = NodeDeps()
        final = run_async(graph.run(state, deps, entry="a"))

        assert trace == ["a", "b"]
        assert final is state  # same object (mutable)

    def test_conditional_edge(self):
        """Conditional edge routes based on state."""
        graph = NodeGraph()

        async def check(state, deps):
            return "next"  # ignored; edge function decides

        async def path_yes(state, deps):
            state.control.done_reason = "yes_path"
            return "end"

        async def path_no(state, deps):
            state.control.done_reason = "no_path"
            return "end"

        def route(state):
            if state.timing.best_wns >= 0:
                return "path_yes"
            return "path_no"

        graph.add_node("check", check)
        graph.add_node("path_yes", path_yes)
        graph.add_node("path_no", path_no)
        graph.add_edge("check", route)
        graph.add_edge("path_yes", "end")
        graph.add_edge("path_no", "end")

        # Test path_yes
        state = OptimizerState()
        state.timing.best_wns = 0.1
        run_async(graph.run(state, NodeDeps(), entry="check"))
        assert state.control.done_reason == "yes_path"

        # Test path_no
        state = OptimizerState()
        state.timing.best_wns = -0.5
        run_async(graph.run(state, NodeDeps(), entry="check"))
        assert state.control.done_reason == "no_path"

    def test_loop_with_counter(self):
        """Graph with a loop that exits after N iterations."""
        graph = NodeGraph()
        iterations = []

        async def loop_node(state, deps):
            state.iteration.current += 1
            iterations.append(state.iteration.current)
            return "check"

        async def check_node(state, deps):
            return "decide"

        def decide(state):
            if state.iteration.current >= 3:
                return "end"
            return "loop_node"

        graph.add_node("loop_node", loop_node)
        graph.add_node("check_node", check_node)
        graph.add_edge("loop_node", "check_node")
        graph.add_edge("check_node", decide)

        state = OptimizerState()
        run_async(graph.run(state, NodeDeps(), entry="loop_node"))

        assert iterations == [1, 2, 3]
        assert state.iteration.current == 3

    def test_unknown_node_raises(self):
        """Referencing an unregistered node raises ValueError."""
        graph = NodeGraph()

        async def bad(state, deps):
            return "nonexistent"

        graph.add_node("bad", bad)
        graph.add_edge("bad", "nonexistent")

        with pytest.raises(ValueError, match="nonexistent"):
            run_async(graph.run(OptimizerState(), NodeDeps(), entry="bad"))

    def test_no_edge_stops(self):
        """If a node has no edge, the graph stops."""
        graph = NodeGraph()

        async def terminal(state, deps):
            state.control.is_done = True
            return "end"

        graph.add_node("terminal", terminal)
        # No edge registered

        state = OptimizerState()
        run_async(graph.run(state, NodeDeps(), entry="terminal"))
        assert state.control.is_done is True


# ── Tracing tests ───────────────────────────────────────────────

class TestStateTracer:
    def test_records_transitions(self):
        tracer = StateTracer()

        state = OptimizerState()
        state.iteration.current = 1
        state.timing.latest_wns = -0.5

        tracer.on_enter("test_node", state)
        tracer.on_exit("test_node", state)

        assert len(tracer.transitions) == 1
        entry = tracer.transitions[0]
        assert entry["node"] == "test_node"
        assert entry["iteration"] == 1
        assert entry["latest_wns"] == -0.5

    def test_export_json(self, tmp_path):
        tracer = StateTracer()
        state = OptimizerState()
        tracer.on_exit("test", state)

        path = str(tmp_path / "transitions.json")
        tracer.export(path)

        import json
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["node"] == "test"


# ── Integration: build_optimizer_graph ──────────────────────────

class TestBuildGraph:
    def test_build_returns_graph(self):
        from optimizer import build_optimizer_graph
        graph = build_optimizer_graph()
        assert isinstance(graph, NodeGraph)
        assert NodeName.INIT_ANALYSIS in graph._nodes
        assert NodeName.SAVE_OUTPUT in graph._nodes

    def test_graph_runs_with_init_to_save(self):
        """If initial_wns >= 0, graph goes init -> save -> end."""
        from pathlib import Path
        from optimizer import build_optimizer_graph

        graph = build_optimizer_graph()
        state = OptimizerState()
        state.timing.initial_wns = 0.1  # timing met
        state.control.input_dcp = Path("/tmp/test.dcp")  # avoid early exit
        deps = NodeDeps()

        final = run_async(graph.run(state, deps, entry="init_analysis"))
        assert final.control.start_time is not None
        assert final.control.end_time is not None
