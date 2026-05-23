"""State dataclasses for the state-machine-driven optimizer.

All state is captured in typed dataclass sub-slices, composed into
OptimizerState. Nodes modify state in-place (mutable pattern).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Control signals ─────────────────────────────────────────────

@dataclass
class StepState:
    """Process control signal from the LLM's report_step_state tool call."""
    step_id: Optional[int] = None
    result_status: Optional[str] = None   # SUCCESS | PARTIAL | FAIL
    flow_control: Optional[str] = None    # CONTINUE | SWITCH_STRATEGY | NEXT_ITERATION | DONE | RETRY | ROLLBACK | EXHAUSTED
    has_tool_calls: bool = False
    raw_content: str = ""
    strategy_phase: Optional[str] = None  # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    strategy_name: Optional[str] = None   # PBLOCK | PhysOpt | Fanout | ...


@dataclass
class PhaseEntry:
    """Record of a strategy phase transition with timestamp."""
    phase: str = ""                          # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    strategy: str = ""                       # strategy name (PBLOCK, PhysOpt, etc.)
    iteration: int = 0
    tool_round: int = 0
    wns_at_entry: Optional[float] = None     # WNS when entering this phase
    timestamp: float = field(default_factory=time.time)


@dataclass
class CriticalPathEntry:
    """Single critical path with cell list and per-path timing detail."""
    cells: list[str] = field(default_factory=list)
    path_length: int = 0
    iteration: int = 0
    slack: Optional[float] = None        # per-path slack (ns)
    logic_delay: Optional[float] = None   # total logic delay (ns)
    net_delay: Optional[float] = None     # total net delay (ns)
    levels: Optional[int] = None          # logic levels/depth


@dataclass
class WnsMilestone:
    """Verified WNS milestone with context."""
    achieved_wns: float = 0.0
    iteration: int = 0
    strategy_label: str = ""
    dcp_path: Optional[str] = None
    tns: Optional[float] = None
    failing_endpoints: Optional[int] = None
    timing_raw_hash: str = ""
    timestamp: float = field(default_factory=time.time)
    verified: bool = True


# ── State sub-slices ────────────────────────────────────────────

@dataclass
class TimingState:
    """WNS/TNS/failing endpoints, best values, milestones."""
    initial_wns: Optional[float] = None
    initial_tns: Optional[float] = None
    initial_failing_endpoints: Optional[int] = None
    best_wns: float = float('-inf')
    best_wns_iteration: Optional[int] = None
    best_wns_tns: Optional[float] = None
    best_wns_failing_endpoints: Optional[int] = None
    latest_wns: Optional[float] = None
    latest_tns: Optional[float] = None
    latest_failing_endpoints: Optional[int] = None
    prev_best_wns: Optional[float] = None
    wns_milestones: list[WnsMilestone] = field(default_factory=list)
    last_verified_timing_info: Optional[dict] = None
    last_verified_timing_raw: Optional[str] = None
    last_verified_timing_iteration: Optional[int] = None
    clock_period: Optional[float] = None
    high_fanout_nets: list = field(default_factory=list)
    critical_path_spread: Optional[dict] = None
    critical_paths: list[CriticalPathEntry] = field(default_factory=list)
    critical_paths_iteration: int = 0
    critical_paths_stale: bool = False
    resource_utilization: Optional[dict] = None
    refreshed_fields: set[str] = field(default_factory=set)
    # Tracks which dashboard fields have been refreshed since init.
    # Values are field names from DASHBOARD_REFRESH_MAP values.


@dataclass
class IterationState:
    """Iteration counter, no-improvement tracking, tool errors, narratives."""
    current: int = 0
    global_no_improvement: int = 0
    tool_errors: list[dict] = field(default_factory=list)
    narratives: list[dict] = field(default_factory=list)
    strategy_sequence: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)  # Tool names called this iteration
    tool_round: int = 0
    max_iterations: int = 50
    max_tool_rounds: int = 80
    no_improvement_limit: int = 3


@dataclass
class ModelState:
    """Model selection, fallback tracking, tier state."""
    current_model: str = ""
    planner_model: str = ""
    worker_model: str = ""
    last_used_model: Optional[str] = None
    previous_tier: Optional[str] = None
    next_iteration_model: Optional[str] = None
    iteration_handoff_prompt: str = ""
    iteration_handoff_injected: bool = False
    format_guard_injected: bool = False
    worker_consecutive_success: int = 0
    worker_consecutive_failures: int = 0
    worker_fallback_models: list[str] = field(default_factory=list)
    planner_fallback_models: list[str] = field(default_factory=list)
    worker_fallback_index: int = 0
    planner_fallback_index: int = 0
    exhausted_worker_fallbacks: set[str] = field(default_factory=set)
    exhausted_planner_fallbacks: set[str] = field(default_factory=set)
    model_usage_history: list[str] = field(default_factory=list)
    llm_call_count: int = 0
    task_type_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    current_task_type: str = ""


@dataclass
class CostState:
    """Token usage and cost tracking."""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost: float = 0.0
    cost_hard_limit: float = 1.0
    api_call_details: list[dict] = field(default_factory=list)


@dataclass
class ToolCallRecord:
    """Single tool call trace entry for dashboard tracking."""
    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict = field(default_factory=dict)
    summary: str = ""
    result_chars: int = 0
    elapsed_seconds: float = 0.0
    iteration: int = 0
    tool_round: int = 0
    status: str = "completed"    # completed | error
    timestamp: float = field(default_factory=time.time)


@dataclass
class FlowControlRecord:
    """Single flow control decision trace entry for trajectory tracking."""
    signal: str = ""          # DONE | SWITCH_STRATEGY | NEXT_ITERATION | EXHAUSTED | ROLLBACK | ANALYZE_DONE | EXEC_DONE | STRATEGY_SELECTED | CONTINUE | SYSTEM_EXIT
    iteration: int = 0
    tool_round: int = 0
    done_reason: str = ""     # wns_target_met | switch_strategy | strategies_exhausted | iteration_success | ...
    phase: str = ""           # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE | SYSTEM
    strategy: str = ""        # Current strategy at decision time
    result_status: str = ""   # From StepState: SUCCESS | PARTIAL | FAIL
    wns_at_decision: Optional[float] = None
    wns_best: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class LLMCallRecord:
    """Single LLM call record for Dashboard history display."""
    timestamp: float = 0.0
    phase: str = ""            # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    model: str = ""
    iteration: int = 0
    user_prompt: str = ""
    assistant_response: str = ""


@dataclass
class ContextState:
    """Compression metrics, raw tool outputs, repetition detection."""
    compression_count: int = 0
    raw_tool_outputs: dict[tuple[int, int], tuple[str, str]] = field(default_factory=dict)
    raw_tool_output_max: int = 50
    # LLM message log for dashboard (not used by compression logic)
    latest_user_prompt: str = ""
    latest_assistant_response: str = ""
    llm_call_history: list[LLMCallRecord] = field(default_factory=list)
    llm_call_history_max: int = 50
    # Tool call trace for dashboard (bounded FIFO)
    tool_call_trace: list[ToolCallRecord] = field(default_factory=list)
    tool_call_trace_max: int = 100
    # Consecutive rounds where report_step_state was missing
    step_state_misses: int = 0
    # Flow control decision log for trajectory tracking (bounded FIFO)
    flow_control_log: list[FlowControlRecord] = field(default_factory=list)
    flow_control_log_max: int = 100


@dataclass
class ControlState:
    """Exit conditions, checkpoint state, user signals, file paths."""
    is_done: bool = False
    done_reason: Optional[str] = None
    needs_save: bool = False
    user_exit_requested: bool = False
    wall_clock_timeout: float = 3600.0
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    step_state: Optional[StepState] = None
    validation_enabled: bool = False
    validation_interval: int = 5
    # File paths
    input_dcp: Optional[Path] = None
    output_dcp: Optional[Path] = None
    run_dir: Optional[Path] = None
    best_checkpoint_path: Optional[Path] = None  # DCP saved when best_wns last improved, for rollback
    current_dcp_path: Optional[Path] = None  # DCP path currently loaded in Vivado


@dataclass
class StrategyState:
    """Strategy lifecycle tracking for the 4-phase decision cycle:
    ANALYZE -> SELECT_STRATEGY -> EXECUTE_STRATEGY -> EVALUATE.
    """
    current_phase: str = ""                  # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    current_strategy: str = ""               # PBLOCK, PhysOpt, Fanout, etc.
    phase_history: list[PhaseEntry] = field(default_factory=list)
    analysis_summary: str = ""               # current analysis findings
    strategy_rationale: str = ""             # why the current strategy was chosen
    evaluation_wns_delta: float = 0.0        # WNS change after execution
    evaluation_result: str = "PENDING"       # IMPROVED | REGRESSION | UNCHANGED | PENDING
    last_handoff_text: str = ""              # PhaseHandoff formatted text for merged dashboard injection


# ── Flow control helpers ────────────────────────────────────────


def record_flow_signal(
    state: OptimizerState,
    signal: str,
    reason: str = "",
    *,
    phase: str = "",
    strategy: str = "",
    result_status: str = "",
) -> FlowControlRecord:
    """Record a flow control decision to the state's flow_control_log.

    Shared across all phases for consistent observability.
    Returns the created FlowControlRecord for optional further use.
    """
    record = FlowControlRecord(
        signal=signal,
        iteration=state.iteration.current,
        tool_round=state.iteration.tool_round,
        done_reason=reason,
        phase=phase or state.strategy.current_phase,
        strategy=strategy or state.strategy.current_strategy,
        result_status=result_status,
        wns_at_decision=state.timing.latest_wns,
        wns_best=state.timing.best_wns,
    )
    state.context.flow_control_log.append(record)
    if len(state.context.flow_control_log) > state.context.flow_control_log_max:
        state.context.flow_control_log = state.context.flow_control_log[-state.context.flow_control_log_max:]

    # Unified console log for real-time flow control tracking
    _phase = record.phase or ""
    _sig = record.signal
    _wns = f"{record.wns_at_decision:.3f}" if record.wns_at_decision is not None else "?"
    _best = f"{record.wns_best:.3f}" if record.wns_best is not None and record.wns_best > float('-inf') else "?"
    logger.info(
        f"[FC] {_sig:20s} phase={_phase:18s} iter={record.iteration} "
        f"round={record.tool_round} wns={_wns} best={_best} reason={reason}"
    )
    return record


# ── Composite state ─────────────────────────────────────────────

@dataclass
class OptimizerState:
    """Complete optimizer state. Nodes modify in-place."""
    timing: TimingState = field(default_factory=TimingState)
    iteration: IterationState = field(default_factory=IterationState)
    model: ModelState = field(default_factory=ModelState)
    cost: CostState = field(default_factory=CostState)
    context: ContextState = field(default_factory=ContextState)
    control: ControlState = field(default_factory=ControlState)
    strategy: StrategyState = field(default_factory=StrategyState)
