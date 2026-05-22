"""State-machine-driven optimizer framework.

Provides an explicit, traceable alternative to the message-conversation-driven
agent in dcp_optimizer.py. The graph executes typed nodes with conditional
edges, giving deterministic control over optimization flow.

Usage:
    from optimizer import build_optimizer_graph, OptimizerState, NodeDeps

    state = OptimizerState()
    state.control.input_dcp = Path("input.dcp")
    deps = NodeDeps(openai_client=..., memory_manager=...)
    graph = build_optimizer_graph()
    final_state = await graph.run(state, deps, entry="init_analysis")
"""

from .state import (
    OptimizerState,
    TimingState,
    IterationState,
    ModelState,
    CostState,
    ContextState,
    ControlState,
    StrategyState,
    StepState,
    WnsMilestone,
    PhaseEntry,
    FlowControlRecord,
)
from .deps import NodeDeps
from .graph import NodeGraph
from .edges import NodeName, after_init, after_check_exit
from .tracing import StateTracer
from .nodes import (
    init_analysis_node,
    iteration_start_node,
    select_model_node,
    prepare_context_node,
    llm_tool_loop_node,
    iteration_end_node,
    check_exit_node,
    save_output_node,
)


def build_optimizer_graph(tracer: StateTracer | None = None) -> NodeGraph:
    """Build and return the complete optimizer graph.

    All 8 nodes are wired with deterministic and conditional edges.
    """
    graph = NodeGraph(tracer=tracer)

    # ── Nodes ─────────────────────────────────────────────────────
    graph.add_node(NodeName.INIT_ANALYSIS, init_analysis_node)
    graph.add_node(NodeName.ITERATION_START, iteration_start_node)
    graph.add_node(NodeName.SELECT_MODEL, select_model_node)
    graph.add_node(NodeName.PREPARE_CONTEXT, prepare_context_node)
    graph.add_node(NodeName.LLM_TOOL_LOOP, llm_tool_loop_node)
    graph.add_node(NodeName.ITERATION_END, iteration_end_node)
    graph.add_node(NodeName.CHECK_EXIT, check_exit_node)
    graph.add_node(NodeName.SAVE_OUTPUT, save_output_node)

    # ── Edges ─────────────────────────────────────────────────────
    # init_analysis -> condition (after_init)
    graph.add_edge(NodeName.INIT_ANALYSIS, after_init)
    # iteration_start -> select_model (deterministic)
    graph.add_edge(NodeName.ITERATION_START, NodeName.SELECT_MODEL)
    # select_model -> prepare_context (deterministic)
    graph.add_edge(NodeName.SELECT_MODEL, NodeName.PREPARE_CONTEXT)
    # prepare_context -> llm_tool_loop (deterministic)
    graph.add_edge(NodeName.PREPARE_CONTEXT, NodeName.LLM_TOOL_LOOP)
    # llm_tool_loop -> iteration_end (deterministic)
    graph.add_edge(NodeName.LLM_TOOL_LOOP, NodeName.ITERATION_END)
    # iteration_end -> check_exit (deterministic)
    graph.add_edge(NodeName.ITERATION_END, NodeName.CHECK_EXIT)
    # check_exit -> condition (after_check_exit)
    graph.add_edge(NodeName.CHECK_EXIT, after_check_exit)
    # save_output -> end (deterministic)
    graph.add_edge(NodeName.SAVE_OUTPUT, NodeName.END)

    return graph


__all__ = [
    "OptimizerState",
    "TimingState",
    "IterationState",
    "ModelState",
    "CostState",
    "ContextState",
    "ControlState",
    "StrategyState",
    "StepState",
    "WnsMilestone",
    "PhaseEntry",
    "FlowControlRecord",
    "NodeDeps",
    "NodeGraph",
    "NodeName",
    "StateTracer",
    "build_optimizer_graph",
]
