"""Pure stateless functions for the optimizer.

Unit-testable without MCP sessions or state.
"""

from .timing import (
    parse_timing_summary,
    parse_high_fanout_nets,
    parse_resource_utilization,
    is_valid_wns,
    compute_timing_hash,
    compute_violation_summary,
)
from .constants import (
    TaskCategory,
    INFORMATION_PATTERNS,
    OPTIMIZATION_PATTERNS,
    ModelTier,
    TOOL_MODEL_MAPPING,
    ROUTING_FAILURE_PHRASES,
    SKILL_TOOL_MAP,
    SKILL_NAME_TO_TOOL,
    WORKER_UPGRADE_THRESHOLD,
    WORKER_DOWNGRADE_THRESHOLD,
    GLOBAL_NO_IMPROVEMENT_LIMIT,
    WNS_TARGET_THRESHOLD,
    SMALL_OUTPUT_THRESHOLD,
    TOOL_RESULT_TRUNCATE,
    RECENT_TURNS_TO_KEEP,
    build_llm_extra_body,
)
from .model_select import (
    classify_task,
    get_task_capability_score,
    estimate_context_complexity,
    compute_model_scores,
    select_model,
)
from .tool_summary import (
    summarize_tool_result,
    filter_tool_result,
)
from .iteration_logic import (
    update_iteration_counters,
    infer_strategy_from_tools,
    build_iteration_narrative,
)
from .context_snapshot import (
    build_context_snapshot,
    inject_context_snapshot,
    inject_context_snapshot_at_end,
    inject_merged_dashboard,
    PHASE_DASHBOARD_SECTIONS,
)
from .handoff import (
    build_handoff_prompt,
    build_situation_summary,
    build_status_signal,
)
from .tool_router import (
    call_tool,
    is_routing_failure,
)
from .step_state import (
    extract_step_state,
)
from .trajectory import (
    format_trajectory_summary,
)

__all__ = [
    # timing
    "parse_timing_summary", "parse_high_fanout_nets", "parse_resource_utilization",
    "is_valid_wns", "compute_timing_hash", "compute_violation_summary",
    # constants
    "TaskCategory", "INFORMATION_PATTERNS", "OPTIMIZATION_PATTERNS",
    "ModelTier", "TOOL_MODEL_MAPPING", "ROUTING_FAILURE_PHRASES",
    "SKILL_TOOL_MAP", "SKILL_NAME_TO_TOOL",
    "WORKER_UPGRADE_THRESHOLD", "WORKER_DOWNGRADE_THRESHOLD",
    "GLOBAL_NO_IMPROVEMENT_LIMIT", "WNS_TARGET_THRESHOLD",
    "SMALL_OUTPUT_THRESHOLD", "TOOL_RESULT_TRUNCATE", "RECENT_TURNS_TO_KEEP",
    "build_llm_extra_body",
    # model_select
    "classify_task", "get_task_capability_score", "estimate_context_complexity",
    "compute_model_scores", "select_model",
    # tool_summary
    "summarize_tool_result", "filter_tool_result",
    # iteration_logic
    "update_iteration_counters", "infer_strategy_from_tools", "build_iteration_narrative",
    # context_snapshot
    "build_context_snapshot", "inject_context_snapshot", "inject_context_snapshot_at_end",
    # handoff
    "build_handoff_prompt", "build_situation_summary", "build_status_signal",
    # tool_router
    "call_tool", "is_routing_failure",
    # step_state
    "extract_step_state",
    # trajectory
    "format_trajectory_summary",
]
