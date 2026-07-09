"""State dataclasses for the state-machine-driven optimizer.

All state is captured in typed dataclass sub-slices, composed into
OptimizerState. Nodes modify state in-place (mutable pattern).
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .pure.entities import EntityRegistry

logger = logging.getLogger(__name__)


class DesignState:
    """Design physical implementation state (from Vivado Design State string).

    Used to determine reliability of WNS/TNS data for LLM strategy selection
    and RapidWright pre-check gating. Degrades from fully accurate (ROUTED)
    to wireload estimate (UNPLACED).
    """
    UNPLACED = "unplaced"     # Synthesized only — no placement, WNS is wireload estimate
    PLACED = "placed_only"    # Placed but not routed — placement-level WNS, no routing
    ROUTED = "routed"         # Fully placed and routed — full timing accuracy


def parse_design_state(timing_report: Optional[str]) -> Optional[str]:
    """Parse Design State from a Vivado timing report.

    Args:
        timing_report: Raw text output from vivado_report_timing_summary.

    Returns:
        One of DesignState.UNPLACED, DesignState.PLACED, DesignState.ROUTED,
        or None when the field cannot be parsed. Returning None (instead of
        silently defaulting to UNPLACED) lets callers preserve the last known
        good state — a false UNPLACED flips the dashboard into a misleading
        "WNS based on wireload estimates" warning even when the WNS is a real
        post-route number (observed after vivado_physopt_and_route, whose
        report lacks a "Design State" header).
    """
    match = re.search(
        r"Design\s+State\s*(?:\||:)\s*([^\|\n\r\t]+)",
        timing_report or "",
        re.IGNORECASE,
    )
    if match:
        state = match.group(1).strip().lower()
        if "routed" in state:
            return DesignState.ROUTED
        if "placed" in state:
            return DesignState.PLACED
        if "optimized" in state:
            return DesignState.UNPLACED
    return None


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
    best_wns_at_entry: Optional[float] = None  # best_wns when entering this phase (for true delta)
    timestamp: float = field(default_factory=time.time)


@dataclass
class PathNode:
    """Single node (cell or net) on a timing path with its delay contribution.

    Populated by D1: structured per-node delay breakdown extracted from
    Vivado report_timing Data Path Delay section, replacing the old
    aggregate-only logic_delay/net_delay summing.
    """
    kind: str = ""              # "cell" | "net"
    name: str = ""              # cell name (pin suffix stripped) or net name
    cell_type: str = ""         # cell type: LUT6/CARRY8/FDRE/MUXF7/DSP_A_B_DATA... (cell only)
    location: str = ""          # SLICE_X91Y106 / DSP48E2_X10Y46 (cell only)
    incr_delay: Optional[float] = None   # incremental delay (ns) — the key diagnostic field
    cumul_delay: Optional[float] = None  # cumulative arrival time at this node (ns)
    fanout: Optional[int] = None         # net fanout (net only, from "fo=N")
    net_status: str = ""                 # "routed" | "unset" | "" (net only)


@dataclass
class ClockDomainInfo:
    """Clock-domain context for a single timing path.

    Populated by D2: extracted from report_timing header fields (Source,
    Destination, Path Group, Clock Path Skew, Clock Uncertainty), replacing
    the old string-guessing of clock domain from cell names.
    """
    source_clock: str = ""          # launch clock name
    dest_clock: str = ""            # capture clock name
    path_group: str = ""            # Path Group
    path_type: str = ""             # "Setup (Max at Slow Process Corner)" etc.
    requirement: Optional[float] = None     # clock requirement (ns)
    clock_skew: Optional[float] = None      # Clock Path Skew (ns) = DCD - SCD + CPR
    clock_uncertainty: Optional[float] = None  # Clock Uncertainty (ns)
    source_clock_delay: Optional[float] = None # SCD (ns)
    dest_clock_delay: Optional[float] = None   # DCD (ns)
    is_cross_clock: bool = False    # source_clock != dest_clock


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
    # D1: per-node delay breakdown (replaces aggregate-only summing)
    nodes: list[PathNode] = field(default_factory=list)
    startpoint: str = ""                                     # launch cell/pin (was discarded)
    endpoint_pin: str = ""                                   # capture cell/pin (full, not just cell name)
    arrival_time: Optional[float] = None                     # final arrival time (ns)
    required_time: Optional[float] = None                    # required time (ns)
    top_delay_nodes: list[PathNode] = field(default_factory=list)  # top contributors by incr_delay
    # D2: clock-domain context (replaces string-guessing in _convert_critical_path)
    clock: ClockDomainInfo = field(default_factory=ClockDomainInfo)


@dataclass
class PathCluster:
    """A cluster of similar timing paths derived from violation analysis."""
    cluster_id: str = ""                 # e.g. "logic_deep_aes_core"
    cluster_type: str = ""               # "logic_dominated" | "route_dominated" | "mixed"
    module: str = ""                       # Primary module name
    path_count: int = 0                   # Number of paths in this cluster
    worst_slack: Optional[float] = None   # Worst slack in cluster
    best_slack: Optional[float] = None     # Best (least negative) slack in cluster
    avg_logic_delay_pct: Optional[float] = None
    avg_logic_levels: Optional[float] = None
    representative_cells: list[str] = field(default_factory=list)  # Up to 6 cells of worst path


@dataclass
class ViolationSummary:
    """Aggregated violation distribution for high-density LLM context.

    Provides a structured overview of timing violations without requiring
    per-path expansion. Populated from critical_paths + timing summary.
    """
    total_failing_endpoints: Optional[int] = None
    severity_distribution: dict[str, int] = field(default_factory=dict)
    delay_profile_breakdown: dict[str, int] = field(default_factory=dict)
    logic_level_distribution: dict[str, int] = field(default_factory=dict)
    top_violating_modules: dict[str, dict] = field(default_factory=dict)
    path_clusters: list[PathCluster] = field(default_factory=list)


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
    baseline_wns: Optional[float] = None  # WNS of iteration baseline DCP, refreshed on strategy switch reload
    latest_tns: Optional[float] = None
    latest_failing_endpoints: Optional[int] = None
    prev_best_wns: Optional[float] = None
    prev_best_tns: Optional[float] = None
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
    # Human-readable reason for why critical_paths_stale was set (e.g.
    # "place/route changed", "checkpoint reloaded", "strategy switch",
    # "rollback"). Rendered in the dashboard so the LLM can distinguish a
    # real layout change from a benign checkpoint reload. Reset to "" when
    # critical_paths_stale is cleared.
    critical_paths_stale_reason: str = ""
    resource_utilization: Optional[dict] = None
    baseline_resource_utilization: Optional[dict] = None
    field_freshness: dict[str, str] = field(default_factory=dict)
    # Tracks freshness status per dashboard field: "fresh" | "stale".
    # Initialized by init_analysis, updated by DASHBOARD_REFRESH_MAP on tool call,
    # set to "stale" on design modification. Keys match DASHBOARD_REFRESH_MAP values.
    # Hold timing (parsed from init_analysis, stored for dashboard Module 1)
    hold_wns: Optional[float] = None
    hold_tns: Optional[float] = None
    hold_failing: Optional[int] = None
    # Device capacity for utilization percentage calculation
    device_capacity: Optional[dict] = None  # {"LUT": N, "FF": N, "DSP": N, "BRAM": N, "URAM": N}
    # Congestion analysis result (populated when analyze_congestion tool runs)
    congestion_data: Optional[dict] = None
    # Route status (populated during init_analysis from report_route_status)
    route_status: Optional[dict] = None
    # Control sets (populated during init_analysis from report_control_sets)
    control_sets: Optional[dict] = None
    # Cross-domain paths count from CDC analysis (populated during init_analysis)
    cross_domain_paths_count: int = 0
    # Design info from RapidWright get_design_info (populated during init_analysis)
    design_info: Optional[dict] = None
    # Constraints environment info (populated during init_analysis)
    constraints_info: Optional[dict] = None
    # PVT corner extracted from timing report header (populated during init_analysis)
    pvt_corner: Optional[str] = None
    # Phase checkpoint: which init_analysis steps have completed (for skip-on-restart)
    # Steps: timing_done, clocks_done, hold_done, util_done, route_done,
    #        constraints_done (false/multicycle/IO), cdc_done
    analysis_checkpoints: dict[str, bool] = field(default_factory=lambda: {
        "timing_done": False,
        "clocks_done": False,
        "hold_done": False,
        "util_done": False,
        "route_done": False,
        "constraints_done": False,
        "cdc_done": False,
    })
    # Adaptive timeout factor based on design cell count.
    # Set after initial size probe in init_analysis.
    # 1.0 for <50K cells, 1.5 for 50K-150K, 3.0 for >150K.
    design_size_factor: float = 1.0
    # Aggregated violation distribution (populated from critical_paths + timing summary)
    violation_summary: Optional[ViolationSummary] = None
    # Top failing endpoint names (last cell of each critical path), derived from critical_paths
    failing_endpoint_names: list[str] = field(default_factory=list)
    # Design physical implementation state (detected from timing report Design State).
    # UNPLACED → synthesized only, no placement; WNS is wireload estimate (highly unreliable).
    # PLACED   → placed but not routed; WNS has placement info but estimated routing (moderate).
    # ROUTED   → fully placed and routed; WNS has full accuracy.
    # Updated by _post_eval_hook / _track_wns_from_result every time a timing report is parsed.
    design_state: str = "unplaced"


@dataclass
class IterationState:
    """Iteration counter, no-improvement tracking, tool errors, narratives."""
    current: int = 0
    global_no_improvement: int = 0
    tool_errors: list[dict] = field(default_factory=list)
    narratives: list[dict] = field(default_factory=list)
    strategy_sequence: list[str] = field(default_factory=list)
    blocked_strategies: list[str] = field(default_factory=list)
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
    budget_injected: bool = False
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
    total_cache_read_tokens: int = 0   # tokens served from prompt cache (OpenRouter/Anthropic)
    total_cache_creation_tokens: int = 0  # tokens written to prompt cache
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
class FailedStrategyRecord:
    """Record of a failed strategy attempt with reason classification."""
    strategy: str = ""          # PBLOCK, PhysOpt, Fanout, etc.
    reason: str = "unknown"     # tool_error | strategy_ineffective | no_improvement
    tool: str = ""              # Tool name(s) involved
    iteration: int = 0
    detail: str = ""            # Human-readable detail (truncated to 200 chars)
    blocked_until_iter: int = 0  # TTL: strategy unblocks when iteration.current >= this value


@dataclass
class OptimizationAppliedRecord:
    """Record of a strategy successfully applied and persisted in best_checkpoint."""
    strategy: str = ""
    params: str = ""           # short param summary (truncated 200 chars)
    wns_before: float = 0.0
    wns_after: float = 0.0
    iteration: int = 0
    checkpoint_path: str = ""


@dataclass
class DesignDataState:
    """Tracks what design data has been persisted to disk."""
    last_snapshot_iteration: int = -1  # -1 = no snapshot taken yet
    stored_iterations: list[int] = field(default_factory=list)
    design_data_path: Optional[str] = None  # current iteration's design_data dir path
    last_snapshot_fingerprint: str = ""  # fingerprint of data at last snapshot; when data changes within same iteration, mismatch triggers re-storage


@dataclass
class ContextState:
    """Compression metrics, raw tool outputs, repetition detection."""
    compression_count: int = 0
    raw_tool_outputs: dict[tuple[int, str, int, str], str] = field(default_factory=dict)
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
    # Failed strategy tracking (canonical source in V2, replaces MemoryManager._failed_strategies)
    failed_strategies: list[FailedStrategyRecord] = field(default_factory=list)
    # History of successfully applied optimizations (persisted in best_checkpoint)
    optimization_history: list[OptimizationAppliedRecord] = field(default_factory=list)
    # Consecutive iterations without best_wns improvement (for exit gating)
    consecutive_no_progress: int = 0
    # Tool result cache: tool_name:args_hash -> (round, result). Cleared on phase transition.
    tool_cache: dict[str, tuple[int, str]] = field(default_factory=dict)
    # Per-phase tool call counters for rate limiting. Reset at each phase entry.
    tool_phase_call_counts: dict[str, int] = field(default_factory=dict)
    # Consecutive empty LLM responses (no content AND no tool calls).
    # Reset to 0 on any non-empty response. Used for early phase exit.
    consecutive_empty_responses: int = 0
    # PBLOCK multiplier tracking: records (iteration, multiplier) for variation
    previous_pblock_multipliers: list[tuple[int, float]] = field(default_factory=list)
    # Frozen PBLOCK planning state shared between analysis/selection/execute.
    pending_pblock_plan: dict[str, Any] | None = None
    pending_pblock_candidates: list[dict[str, Any]] = field(default_factory=list)
    attempted_pblock_candidate_ids: list[str] = field(default_factory=list)
    # Design data persistence tracking (for DesignDataManager)
    design_data: DesignDataState = field(default_factory=DesignDataState)


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
    post_rollback_analyze: bool = False  # set by EVALUATE when rollback detected, used by next ANALYZE
    # Iteration-level checkpoints for multi-granularity rollback.
    # Saved before each EXECUTE phase; capped at 3 entries.
    # Each entry: (iteration_number, dcp_path).
    iteration_checkpoints: list = field(default_factory=list)  # list of tuple[int, Path]


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


def _ttl_for_reason(reason: str, current: int) -> int:
    """Compute blocked_until_iter based on failure reason.

    TTL scheme (per reason):
      - strategy_ineffective → short cooldown (1 iteration)
      - strategy_not_applicable → medium cooldown (2 iterations)
      - no_improvement → longer cooldown (3 iterations)
      - tool_error / anything else → no TTL (immediate retriable)
    """
    if reason == "strategy_ineffective":
        return current + 1
    elif reason == "strategy_not_applicable":
        return current + 2
    elif reason == "no_improvement":
        return current + 3
    else:  # tool_error, data_quality_error, unknown, etc.
        return current


def record_strategy_failure(
    state: OptimizerState,
    strategy: str,
    reason: str = "unknown",
    tool: str = "",
    detail: str = "",
) -> None:
    """Record a failed strategy attempt to state.context.failed_strategies.

    Deduplicates by strategy name: same strategy is only recorded once.
    Replaces MemoryManager.record_failure() / DCPOptimizerCompat.record_failure()
    as the canonical V2 path.

    TTL (per-reason cooldown): see _ttl_for_reason for the complete scheme.
    """
    existing = [f for f in state.context.failed_strategies if f.strategy == strategy]
    if existing:
        # Refresh reason and blocked_until_iter on re-failure so TTL
        # restarts and the entry stays blocked if still ineffective.
        entry = existing[0]
        new_ttl = _ttl_for_reason(reason, state.iteration.current)
        # Preserve a more-restrictive (longer cooldown) classification recorded
        # at execution time when a later recorder (notably iteration_end's
        # independent empty-result re-scan) tries to downgrade it. EXECUTE
        # records strategy_not_applicable/strategy_ineffective from real chain
        # context; iteration_end may reclassify the same attempt as tool_error
        # (TTL=0, instantly retriable) from a raw-output regex match. Allowing
        # that downgrade would contradict the execution-time applicability
        # verdict and make a strategy that should cool down immediately
        # retriable. Only overwrite when the new reason is equally or more
        # restrictive (new TTL >= existing TTL).
        if entry.blocked_until_iter > new_ttl:
            logger.info(
                "[FAILED_STRATEGY] Preserved stricter reason for %s "
                "(existing=%s blocked_until_iter=%d, ignored downgrade to %s ttl=%d)",
                strategy, entry.reason, entry.blocked_until_iter, reason, new_ttl,
                extra={"strategy": strategy, "reason": entry.reason,
                       "ignored_reason": reason},
            )
            return
        entry.reason = reason
        entry.tool = tool
        entry.iteration = state.iteration.current
        entry.detail = (detail or "")[:200]
        entry.blocked_until_iter = new_ttl
        logger.warning(
            "[FAILED_STRATEGY] Updated: %s (reason=%s, tool=%s, blocked_until_iter=%d)",
            strategy, reason, tool, entry.blocked_until_iter,
            extra={"strategy": strategy, "reason": reason, "tool": tool,
                   "blocked_until": entry.blocked_until_iter},
        )
        return
    blocked_until_iter = _ttl_for_reason(reason, state.iteration.current)
    entry = FailedStrategyRecord(
        strategy=strategy,
        reason=reason,
        tool=tool,
        iteration=state.iteration.current,
        detail=detail[:200],
        blocked_until_iter=blocked_until_iter,
    )
    state.context.failed_strategies.append(entry)
    logger.warning(
        "[FAILED_STRATEGY] Recorded: %s (reason=%s, tool=%s, total failed: %d, unblock_iter=%d)",
        strategy, reason, tool, len(state.context.failed_strategies), entry.blocked_until_iter,
        extra={"strategy": strategy, "reason": reason, "tool": tool,
               "failed_count": len(state.context.failed_strategies)},
    )


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
    # Entity registry: canonical, compression-resistant cell-name SSOT.
    # Rebuilt into the Pinned context layer each turn (never enters
    # MessageStore), so cell names survive compression. Validated at the
    # LLM->tool boundary by tool_router. See optimizer/pure/entities.py.
    entity_registry: "EntityRegistry" = field(default_factory=lambda: _new_registry())

    @property
    def registry(self) -> "EntityRegistry":
        """Alias for entity_registry (concise access)."""
        return self.entity_registry


def _new_registry():
    """Lazy factory to avoid importing entities.py at module top (circular)."""
    from .pure.entities import EntityRegistry
    return EntityRegistry()


# ── Dashboard StateSpace (6-module canonical representation) ────────
# These are OUTPUT-ONLY dataclasses built by optimizer/pure/state_space.py
# from OptimizerState. They are consumed by dashboard/serializer.py (Web UI)
# and optimizer/pure/context_snapshot.py (LLM context injection).

@dataclass
class DashboardGlobalState:
    """Module 1: Global state and optimization targets."""
    current_stage: str = ""            # SYNTHESIS|PLACEMENT|ROUTING|POST_ROUTE
    iteration_count: int = 0
    target_frequency: float = 0.0      # MHz
    wns_setup: Optional[float] = None
    baseline_wns: Optional[float] = None  # iteration baseline WNS (start point for current strategy)
    tns_setup: Optional[float] = None
    whs_hold: Optional[float] = None
    ths_hold: Optional[float] = None
    lut_utilization: Optional[float] = None    # 0.0~1.0
    ff_utilization: Optional[float] = None
    bram_utilization: Optional[float] = None
    dsp_utilization: Optional[float] = None
    # Best values across iterations (for progress tracking)
    best_wns: Optional[float] = None
    best_wns_iteration: Optional[int] = None
    # Design scale (from design_info)
    cell_count: int = 0
    net_count: int = 0
    # Design physical implementation state (from timing report Design State)
    # UNPLACED | PLACED | ROUTED — determines WNS accuracy and pre-check gating
    design_state: str = "unplaced"


@dataclass
class DashboardViolationSummary:
    """Aggregated violation distribution for high-density LLM context.

    Provides severity distribution, delay profile breakdown, logic level
    distribution, and top violating modules — all derived from
    TimingState.critical_paths without additional Vivado calls.
    """
    total_failing_endpoints: Optional[int] = None
    severity_distribution: dict[str, int] = field(default_factory=dict)
    delay_profile_breakdown: dict[str, int] = field(default_factory=dict)
    logic_level_distribution: dict[str, int] = field(default_factory=dict)
    top_violating_modules: dict[str, dict] = field(default_factory=dict)
    path_clusters: list[DashboardPathCluster] = field(default_factory=list)


@dataclass
class DashboardTimingPath:
    """Module 2: Single violating timing path endpoint entry."""
    endpoint_name: str = ""
    source_clock: str = ""
    dest_clock: str = ""
    slack: Optional[float] = None
    logic_delay_pct: Optional[float] = None   # 0.0~1.0
    route_delay_pct: Optional[float] = None   # 0.0~1.0
    logic_levels: Optional[int] = None
    path_group: str = ""                       # clock domain group label
    # D1/D2: diagnostic detail (populated from CriticalPathEntry)
    startpoint: str = ""                  # launch cell/pin (was missing)
    clock_skew: Optional[float] = None    # D2: Clock Path Skew (ns)
    clock_uncertainty: Optional[float] = None  # D2: Clock Uncertainty (ns)
    is_cross_clock: bool = False          # D2: source_clock != dest_clock
    delay_hotspots: list[dict] = field(default_factory=list)
    # e.g. [{"name":"u/carry7","type":"CARRY8","incr":0.082,"pct_of_path":0.16,"location":"SLICE_X.."}]
    # Cell type chain for the full path, e.g. "LUT6→LUT5→MUXF7→FDRE".
    # Populated from CriticalPathEntry.cells using heuristic cell type detection.
    # Only shown in ANALYZE / SELECT_STRATEGY phases.
    cell_type_chain: str = ""
    # Summary counts: e.g. {LUT: 3, MUXF: 2, FF: 1}
    cell_type_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class DashboardPathCluster:
    """A cluster of similar timing paths, with a representative path and statistics."""
    cluster_id: str = ""                 # e.g. "logic_deep_aes_core"
    representative_path_idx: int = 0      # Index into top_violating_paths
    path_count: int = 0                   # Number of paths in this cluster
    slack_range: str = ""                 # e.g. "-1.200ns to -0.850ns"
    avg_logic_delay_pct: Optional[float] = None
    avg_logic_levels: Optional[float] = None
    module: str = ""                      # Primary module for this cluster


@dataclass
class DashboardTimingClusters:
    """Module 2 container: Top-N violating path endpoints + violation summary."""
    top_violating_paths: list[DashboardTimingPath] = field(default_factory=list)
    violation_summary: Optional[DashboardViolationSummary] = None
    path_clusters: list[DashboardPathCluster] = field(default_factory=list)
    failing_endpoint_names: list[str] = field(default_factory=list)


@dataclass
class DashboardCongestionHotspot:
    """Module 3: A congestion hotspot bounding box."""
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    severity: float = 0.0              # 0.0~1.0
    dominant_module: str = ""


@dataclass
class DashboardPhysicalCongestion:
    """Module 3: Physical and congestion metrics."""
    global_congestion_score: Optional[float] = None   # 0.0~1.0, >0.85 critical
    avg_wirelength: Optional[float] = None
    long_route_nets_count: Optional[int] = None       # None=unknown, 0=analyzed+none
    congestion_hotspots: list[DashboardCongestionHotspot] = field(default_factory=list)
    pblock_overflow_count: Optional[int] = None        # None=not_measured, 0=measured+none
    congestion_level: Optional[str] = None             # LOW|MEDIUM|HIGH|CRITICAL from report_route_status
    total_wirelength: Optional[float] = None           # total route wirelength from report_route_status
    max_wirelength: Optional[float] = None             # max single-net wirelength from report_route_status
    timing_violated_nets: Optional[int] = None         # nets with timing violations from report_route_status


@dataclass
class DashboardHighFanoutNet:
    """Module 4: A single high-fanout net entry."""
    net_name: str = ""
    fanout_count: int = 0
    is_replicated: bool = False


@dataclass
class DashboardNetlistQuality:
    """Module 4: Netlist architecture quality metrics."""
    total_control_sets: Optional[int] = None  # None=not_extracted, 0=parsed zero
    avg_control_sets_per_slice: Optional[float] = None
    high_fanout_nets: list[DashboardHighFanoutNet] = field(default_factory=list)
    failed_inferences: list[str] = field(default_factory=list)
    cross_domain_paths_count: int = 0
    cell_type_summary: str = ""  # top cell types from design_info (e.g. "LUT6:1200, FDRE:800, ...")


@dataclass
class DashboardConstraints:
    """Module 5: Timing constraints environment."""
    clock_definitions: dict[str, float] = field(default_factory=dict)  # name -> freq_mhz
    false_paths_count: Optional[int] = None     # None=not_extracted, 0=parsed zero
    multicycle_paths_count: Optional[int] = None  # None=not_extracted, 0=parsed zero
    io_delay_defined_pct: Optional[float] = None   # 0.0~1.0
    total_io_ports: Optional[int] = None             # None=parse failed, 0=no ports
    pvt_corner: Optional[str] = None


@dataclass
class DashboardDynamicGradient:
    """Module 6: Iteration-over-iteration delta data."""
    delta_wns: Optional[float] = None
    delta_tns: Optional[float] = None
    delta_congestion: Optional[float] = None
    last_action_taken: str = ""
    action_status: str = ""            # Success|Failed|Timeout


@dataclass
class DashboardModuleEntry:
    """Single module with critical path statistics for Module 7."""
    name: str = ""
    critical_path_hits: int = 0
    cell_distribution_pct: float = 0.0
    sub_modules: list[str] = field(default_factory=list)


@dataclass
class DashboardArchitectureOverview:
    """Module 7: Module-level architecture insights inferred from cell names.

    Extracted from CriticalPathEntry cell names which encode hierarchy
    (e.g. \"design_i/aes_core/sbox/LUT6\"). Zero-cost: no additional
    Vivado Tcl or RapidWright calls required.
    """
    top_modules: list[DashboardModuleEntry] = field(default_factory=list)
    cross_module_paths: int = 0       # paths spanning >=2 modules
    intra_module_paths: int = 0       # paths within a single module
    deepest_module: Optional[str] = None  # module with highest logic depth
    total_cells_analyzed: int = 0


@dataclass
class StateSpace:
    """Canonical 7-module dashboard state, built from OptimizerState."""
    global_state: DashboardGlobalState = field(default_factory=DashboardGlobalState)
    timing_clusters: DashboardTimingClusters = field(default_factory=DashboardTimingClusters)
    physical_congestion: DashboardPhysicalCongestion = field(default_factory=DashboardPhysicalCongestion)
    netlist_quality: DashboardNetlistQuality = field(default_factory=DashboardNetlistQuality)
    constraints_env: DashboardConstraints = field(default_factory=DashboardConstraints)
    dynamic_gradient: DashboardDynamicGradient = field(default_factory=DashboardDynamicGradient)
    architecture_overview: DashboardArchitectureOverview = field(default_factory=DashboardArchitectureOverview)
