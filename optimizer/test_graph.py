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
from optimizer.nodes.check_exit import _competition_score_guard_reason
from optimizer.nodes.save_output import (
    _classify_design_state,
    _restore_best_checkpoint_for_delivery,
)
from optimizer.nodes.subgraphs.phase_execute import _execute_exit_reason_after_timing_update
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


class TestSaveOutputHelpers:
    def test_status_routed_takes_precedence(self):
        assert _classify_design_state("Routed") == "routed"

    def test_empty_status_uses_timing_summary_routed_state(self):
        timing_summary = """
        --------------------------------------------------------------------------------
        Design Timing Summary
        --------------------------------------------------------------------------------
        Design State      : Routed
        """
        assert _classify_design_state("", timing_summary) == "routed"

    def test_empty_status_uses_timing_summary_placed_state(self):
        timing_summary = "Design State      : Placed"
        assert _classify_design_state("", timing_summary) == "placed"

    def test_empty_status_uses_timing_summary_optimized_state(self):
        # "Optimized" means the design is not yet placed (post-synthesis state).
        # This must be recognized so save_output guard can detect unplaced designs.
        timing_summary = "Design State      : Optimized"
        assert _classify_design_state("", timing_summary) == "optimized"

    def test_unknown_when_no_state_signal_exists(self):
        assert _classify_design_state("", "Timing unavailable") == "unknown"

    def test_restore_best_checkpoint_before_delivery(self, tmp_path, monkeypatch):
        checkpoint = tmp_path / "best_checkpoint.dcp"
        checkpoint.touch()
        state = OptimizerState()
        state.control.best_checkpoint_path = checkpoint
        state.timing.best_wns = -0.452
        deps = NodeDeps(vivado_session=object())
        calls = []

        async def fake_call_tool(name, arguments, *args, **kwargs):
            calls.append((name, arguments))
            if name == "vivado_open_checkpoint":
                return "Opened checkpoint"
            if name == "vivado_report_timing_summary":
                return "WNS(ns) TNS(ns) Failing Endpoints\n-0.452 -397.976 1462"
            raise AssertionError(f"unexpected tool: {name}")

        monkeypatch.setattr(
            "optimizer.nodes.save_output.call_tool_fn", fake_call_tool
        )

        restored = run_async(_restore_best_checkpoint_for_delivery(state, deps))

        assert restored is True
        assert [name for name, _ in calls] == [
            "vivado_open_checkpoint",
            "vivado_report_timing_summary",
        ]
        assert calls[0][1]["dcp_path"] == str(checkpoint.resolve())
        assert state.control.current_dcp_path == checkpoint.resolve()
        assert state.timing.latest_wns == -0.452

    def test_wns_mismatch_preserves_incremental_output(self, tmp_path, monkeypatch):
        checkpoint = tmp_path / "best_checkpoint.dcp"
        checkpoint.touch()
        state = OptimizerState()
        state.control.best_checkpoint_path = checkpoint
        state.timing.best_wns = -0.452
        deps = NodeDeps(vivado_session=object())

        async def fake_call_tool(name, arguments, *args, **kwargs):
            if name == "vivado_open_checkpoint":
                return "Opened checkpoint"
            return "WNS(ns) TNS(ns) Failing Endpoints\n-0.698 -500.000 1500"

        monkeypatch.setattr(
            "optimizer.nodes.save_output.call_tool_fn", fake_call_tool
        )

        restored = run_async(_restore_best_checkpoint_for_delivery(state, deps))

        assert restored is False


class TestExecutePhaseHelpers:
    def test_target_met_exits_after_any_tool(self):
        assert (
            _execute_exit_reason_after_timing_update(
                "rapidwright_search_cells",
                None,
                target_met=True,
            )
            == "wns_target_met"
        )

    @pytest.mark.parametrize(
        ("verdict", "reason"),
        [
            ("IMPROVED", "post_eval_improved"),
            ("UNCHANGED", "post_eval_unchanged"),
            ("REGRESSED", "post_eval_regressed"),
        ],
    )
    def test_post_eval_verdict_exits_for_evaluated_execution_tools(self, verdict, reason):
        assert (
            _execute_exit_reason_after_timing_update(
                "vivado_route_design",
                verdict,
                target_met=False,
            )
            == reason
        )

    def test_unknown_verdict_does_not_exit(self):
        assert (
            _execute_exit_reason_after_timing_update(
                "vivado_route_design",
                None,
                target_met=False,
            )
            == ""
        )

    def test_non_evaluated_tool_does_not_exit_without_target(self):
        assert (
            _execute_exit_reason_after_timing_update(
                "rapidwright_search_cells",
                "IMPROVED",
                target_met=False,
            )
            == ""
        )


class TestCheckExitHelpers:
    def test_score_guard_banks_recent_late_gain(self):
        state = OptimizerState()
        state.control.wall_clock_timeout = 3600.0
        state.iteration.current = 3
        state.timing.initial_wns = -0.978
        state.timing.best_wns = -0.435
        state.timing.best_wns_iteration = 3

        reason = _competition_score_guard_reason(state, elapsed=3050.0)

        assert reason.startswith("score_guard_bank_best:")
        assert "gain=0.543ns" in reason

    def test_score_guard_does_not_stop_before_late_window(self):
        state = OptimizerState()
        state.control.wall_clock_timeout = 3600.0
        state.iteration.current = 3
        state.timing.initial_wns = -0.978
        state.timing.best_wns = -0.435
        state.timing.best_wns_iteration = 3

        assert _competition_score_guard_reason(state, elapsed=2400.0) == ""

    def test_score_guard_requires_recent_best_iteration(self):
        state = OptimizerState()
        state.control.wall_clock_timeout = 3600.0
        state.iteration.current = 4
        state.timing.initial_wns = -0.978
        state.timing.best_wns = -0.435
        state.timing.best_wns_iteration = 3

        assert _competition_score_guard_reason(state, elapsed=3200.0) == ""

    def test_score_guard_requires_meaningful_gain(self):
        state = OptimizerState()
        state.control.wall_clock_timeout = 3600.0
        state.iteration.current = 3
        state.timing.initial_wns = -0.978
        state.timing.best_wns = -0.970
        state.timing.best_wns_iteration = 3

        assert _competition_score_guard_reason(state, elapsed=3050.0) == ""


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

    def test_graph_all_nodes_have_edges(self):
        """Every registered node has an outgoing edge."""
        from optimizer import build_optimizer_graph
        graph = build_optimizer_graph()
        for node_name in graph._nodes:
            assert node_name in graph._edges, f"Node '{node_name}' has no edge registered"

    def test_graph_loop_execution_with_mocked_nodes(self):
        """Full graph traversal with mocked nodes: init -> 3 iterations -> save.

        Uses mocked node functions to avoid MCP dependencies
        while testing edge routing and graph loop logic.
        """
        from optimizer import build_optimizer_graph

        graph = build_optimizer_graph()

        async def mock_init(state, deps):
            state.control.start_time = 1000.0
            state.timing.initial_wns = -0.5
            state.timing.best_wns = -0.5
            state.timing.latest_wns = -0.5
            return NodeName.ITERATION_START

        async def mock_iter_start(state, deps):
            state.iteration.current += 1
            return NodeName.SELECT_MODEL

        async def mock_select_model(state, deps):
            state.model.current_model = "test-model"
            return NodeName.PREPARE_CONTEXT

        async def mock_prepare(state, deps):
            return NodeName.LLM_TOOL_LOOP

        async def mock_llm_loop(state, deps):
            state.model.llm_call_count += 1
            return NodeName.ITERATION_END

        async def mock_iter_end(state, deps):
            state.iteration.global_no_improvement += 1
            return NodeName.CHECK_EXIT

        async def mock_check_exit(state, deps):
            if state.iteration.global_no_improvement >= 3:
                state.control.is_done = True
                state.control.done_reason = "max_no_improvement"
            return NodeName.CHECK_EXIT

        async def mock_save(state, deps):
            state.control.end_time = 2000.0
            return NodeName.END

        graph._nodes = {
            NodeName.INIT_ANALYSIS: mock_init,
            NodeName.ITERATION_START: mock_iter_start,
            NodeName.SELECT_MODEL: mock_select_model,
            NodeName.PREPARE_CONTEXT: mock_prepare,
            NodeName.LLM_TOOL_LOOP: mock_llm_loop,
            NodeName.ITERATION_END: mock_iter_end,
            NodeName.CHECK_EXIT: mock_check_exit,
            NodeName.SAVE_OUTPUT: mock_save,
        }

        state = OptimizerState()
        deps = NodeDeps()
        final = run_async(graph.run(state, deps, entry=NodeName.INIT_ANALYSIS))

        assert final.control.start_time == 1000.0
        assert final.control.end_time == 2000.0
        assert final.control.done_reason == "max_no_improvement"
        assert final.iteration.current == 3
        assert final.iteration.global_no_improvement == 3
        assert final.model.llm_call_count == 3

    def test_user_exit_during_loop(self):
        """User exit request during loop should route to save_output."""
        from optimizer import build_optimizer_graph

        graph = build_optimizer_graph()

        async def mock_init(state, deps):
            state.control.start_time = 1000.0
            state.timing.initial_wns = -0.5
            state.timing.best_wns = -0.5
            state.timing.latest_wns = -0.5
            return NodeName.ITERATION_START

        async def mock_iter_start(state, deps):
            state.iteration.current += 1
            # Simulate user requesting exit mid-iteration
            state.control.user_exit_requested = True
            return NodeName.SELECT_MODEL

        async def passthrough(state, deps):
            return NodeName.END

        graph._nodes = {
            NodeName.INIT_ANALYSIS: mock_init,
            NodeName.ITERATION_START: mock_iter_start,
            NodeName.SELECT_MODEL: passthrough,
            NodeName.SAVE_OUTPUT: passthrough,
        }
        graph._edges[NodeName.SAVE_OUTPUT] = NodeName.END

        state = OptimizerState()
        deps = NodeDeps()
        final = run_async(graph.run(state, deps, entry=NodeName.INIT_ANALYSIS))

        assert final.control.is_done is True
        assert final.control.done_reason == "user_requested"
