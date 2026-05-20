"""State dataclasses for the state-machine-driven optimizer.

All state is captured in typed dataclass sub-slices, composed into
OptimizerState. Nodes modify state in-place (mutable pattern).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── Control signals ─────────────────────────────────────────────

@dataclass
class StepState:
    """Process control signal from the LLM's report_step_state tool call."""
    step_id: Optional[int] = None
    result_status: Optional[str] = None   # SUCCESS | PARTIAL | FAIL
    flow_control: Optional[str] = None    # CONTINUE | SWITCH_STRATEGY | DONE | RETRY | ROLLBACK | EXHAUSTED
    has_tool_calls: bool = False
    raw_content: str = ""


@dataclass
class CriticalPathEntry:
    """Single critical path with cell list."""
    cells: list[str] = field(default_factory=list)
    path_length: int = 0
    iteration: int = 0


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
class ContextState:
    """Compression metrics, raw tool outputs, repetition detection."""
    compression_count: int = 0
    raw_tool_outputs: dict[tuple[int, int], tuple[str, str]] = field(default_factory=dict)
    raw_tool_output_max: int = 50


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
