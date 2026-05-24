"""Shared constants for the optimizer pure functions.

Extracted from dcp_optimizer.py: L90-122, L135-159, L296-315, L865-868.
"""

from __future__ import annotations


# ── PBLOCK strategy parameters ────────────────────────────────
PBLOCK_DISTANCE_WEIGHT_DEFAULT = 0.3
PBLOCK_CRITICAL_PATHS_TOP_N = 10
PBLOCK_CRITICAL_CELLS_MAX = 50


# ── Task classification ──────────────────────────────────────────

class TaskCategory:
    INFORMATION = "information"
    OPTIMIZATION = "optimization"
    UNKNOWN = "unknown"


INFORMATION_PATTERNS = ["get_", "read", "query", "check", "list", "show", "status", "report"]
OPTIMIZATION_PATTERNS = [
    "optimize", "improve", "place", "route", "synthesize",
    "floorplan", "create", "modify", "fix", "debug", "analyze",
]


# ── Model tier mapping ───────────────────────────────────────────

class ModelTier:
    PLANNER = "planner"
    WORKER = "worker"
    DEFAULT = None


TOOL_MODEL_MAPPING = {
    "place_design": ModelTier.PLANNER,
    "phys_opt_design": ModelTier.PLANNER,
    "route_design": ModelTier.PLANNER,
    "optimize_placement": ModelTier.PLANNER,
    "optimize_routing": ModelTier.PLANNER,
    "synthesize": ModelTier.PLANNER,
    "create_floorplan": ModelTier.PLANNER,
    "debug_timing": ModelTier.PLANNER,
    "fix_timing": ModelTier.PLANNER,
    "get_utilization": ModelTier.WORKER,
    "get_timing": ModelTier.WORKER,
    "report_power": ModelTier.WORKER,
    "list_ports": ModelTier.WORKER,
    "read_checkpoint": ModelTier.WORKER,
}


# ── Routing failure detection ────────────────────────────────────

ROUTING_FAILURE_PHRASES = [
    "routing failed", "route error", "cannot route",
    "unroutable", "exceeds", "congestion",
]


# ── Skill tool mapping ──────────────────────────────────────────

SKILL_TOOL_MAP: dict[str, str] = {
    "rapidwright_analyze_net_detour": "net_detour",
    "rapidwright_optimize_cell_placement": "optimize_cell",
    "rapidwright_smart_region_search": "smart_region",
    "rapidwright_analyze_pblock_region": "pblock_strategy",
    "rapidwright_execute_physopt_strategy": "physopt_strategy",
    "rapidwright_execute_pblock_strategy": "execute_pblock_strategy",
    "rapidwright_execute_fanout_strategy": "fanout_strategy",
    "rapidwright_analyze_congestion_spreading": "analyze_congestion_spreading",
    "rapidwright_execute_congestion_spreading": "execute_congestion_spreading",
    "rapidwright_analyze_net_swapping": "analyze_net_swapping",
    "rapidwright_execute_net_swapping": "execute_net_swapping",
}
SKILL_NAME_TO_TOOL: dict[str, str] = {v: k for k, v in SKILL_TOOL_MAP.items()}


# ── Threshold constants ──────────────────────────────────────────

WORKER_UPGRADE_THRESHOLD = 2       # Cumulative failures before upgrade
WORKER_DOWNGRADE_THRESHOLD = 3     # Worker consecutive successes before downgrade
GLOBAL_NO_IMPROVEMENT_LIMIT = 3    # Global no-improvement limit
WNS_TARGET_THRESHOLD = 0.0         # WNS target (0.0 ns = timing convergence)
WNS_ROLLBACK_THRESHOLD: float = 0.030  # 30ps: trigger rollback when latest_wns falls this far below best_wns

# Context thresholds (derived from model config, but we use safe defaults)
SMALL_OUTPUT_THRESHOLD = 3000      # Bypass summarization below this
TOOL_RESULT_TRUNCATE = 30000       # Truncation limit for tool results
RECENT_TURNS_TO_KEEP = 20         # Recent messages to keep during compression

# Worker context limits (from model_config.yaml, safe defaults)
# These are 60% and 80% of the worker model's max_tokens
WORKER_CONTEXT_WARN_TOKENS = 120_000   # 60% of 200K
WORKER_CONTEXT_FORCE_TOKENS = 160_000  # 80% of 200K


# ── Dashboard freshness tracking ──────────────────────────────

# Maps tool names → dashboard fields they refresh.
# Used to track dashboard data freshness after tool execution.
# Extensible: add new tool→field mappings here when developing new tools.
DASHBOARD_REFRESH_MAP: dict[str, frozenset[str]] = {
    "vivado_report_utilization_for_pblock": frozenset({"resource_utilization"}),
    "vivado_get_critical_high_fanout_nets": frozenset({"high_fanout_nets"}),
    "rapidwright_analyze_critical_path_spread": frozenset({"critical_path_spread"}),
    "vivado_extract_critical_path_pins": frozenset({"critical_path_spread"}),
}


# ── Per-phase tool rate limits ────────────────────────────────────
# Prevents LLM from repeatedly calling the same read-only tool within
# a single phase. When exceeded, the call returns a rate-limit message
# directing the LLM to use alternatives (dashboard data, batch params).
PHASE_TOOL_RATE_LIMITS: dict[str, int] = {
    "rapidwright_search_cells": 3,
    "vivado_run_tcl": 5,
}

# ── Skill chain actions ──────────────────────────────────────────
# After a skill completes, auto-execute the listed MCP tools in order.
# Each step: {"tool": str, "args": dict, "args_from_skill": {param: skill_result_key}}
# args_from_skill extracts values from the skill's returned data dict.
# ── LLM extra_body builder (with prompt caching) ────────────────

def build_llm_extra_body(
    reasoning_config: dict | None,
    model: str,
    planner_model: str,
    worker_model: str,
) -> dict:
    """Build extra_body for LLM API calls with prompt caching + reasoning config.

    Always enables OpenRouter prompt caching so the system prompt prefix
    is cached across repeated calls, reducing token waste.
    """
    extra: dict = {"cache": {"prompt": True}}

    if reasoning_config:
        tier_key = None
        if model == planner_model:
            tier_key = "planner"
        elif model == worker_model:
            tier_key = "worker"
        if tier_key:
            cfg = reasoning_config.get(tier_key)
            if cfg and cfg.get("enabled"):
                reasoning_payload: dict[str, bool | int] = {"enabled": True}
                max_output = cfg.get("max_output_tokens")
                if max_output is not None:
                    reasoning_payload["max_output_tokens"] = max_output
                extra["reasoning"] = reasoning_payload

    return extra


SKILL_CHAIN_ACTIONS: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": [
        {"tool": "vivado_place_design", "args": {"directive": "unplace"}},
        {"tool": "vivado_create_and_apply_pblock",
         "args_from_skill": {
             "pblock_name": "pblock_name",
             "ranges": "pblock_ranges",
             "is_soft": "is_soft_recommended",
         }},
        {"tool": "vivado_place_design", "args": {}},
        {"tool": "vivado_route_design", "args": {}},
    ],
}
