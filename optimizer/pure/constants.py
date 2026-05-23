"""Shared constants for the optimizer pure functions.

Extracted from dcp_optimizer.py: L90-122, L135-159, L296-315, L865-868.
"""

from __future__ import annotations


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

# ── Skill chain actions ──────────────────────────────────────────
# After a skill completes, auto-execute the listed MCP tools in order.
# Each step: {"tool": str, "args": dict, "args_from_skill": {param: skill_result_key}}
# args_from_skill extracts values from the skill's returned data dict.
SKILL_CHAIN_ACTIONS: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": [
        {"tool": "vivado_place_design", "args": {"directive": "unplace"}},
        {"tool": "vivado_create_and_apply_pblock",
         "args_from_skill": {
             "pblock_name": "pblock_name",
             "pblock_ranges": "pblock_ranges",
             "is_soft": "is_soft_recommended",
         }},
        {"tool": "vivado_place_design", "args": {}},
        {"tool": "vivado_route_design", "args": {}},
    ],
}
