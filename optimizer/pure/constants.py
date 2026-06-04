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
    "rapidwright_optimize_pin_swapping": "pin_swapping_strategy",
    "rapidwright_flatten_lut_cascade": "lut_cascade_flattening",
    "rapidwright_replicate_critical_cells": "critical_path_cell_replication_strategy",
    "rapidwright_execute_register_retiming": "execute_register_retiming",
    "rapidwright_analyze_register_retiming": "analyze_register_retiming",
    "rapidwright_smart_retiming": "smart_retiming",
    "rapidwright_execute_opt_design_strategy": "opt_design_strategy",
    "rapidwright_analyze_congestion": "analyze_congestion",
}
SKILL_NAME_TO_TOOL: dict[str, str] = {v: k for k, v in SKILL_TOOL_MAP.items()}


# ── EXECUTE phase strategy-to-tool mapping ─────────────────────────
# Maps strategy names (as selected by LLM in SELECT_STRATEGY) to the
# corresponding MCP tool to call in the EXECUTE phase.
# Used by phase_execute.py (runtime constraint injection) and
# prepare_context.py (LLM prompt generation).  Keep in sync.
EXECUTE_STRATEGY_TOOL_MAP: dict[str, str] = {
    "PBLOCK": "rapidwright_execute_pblock_strategy",
    "PhysOpt": "vivado_physopt_and_route",
    "Fanout": "rapidwright_execute_fanout_strategy",
    "LUTCascade": "rapidwright_flatten_lut_cascade",
    "PinSwap": "rapidwright_optimize_pin_swapping",
    "CellReplication": "rapidwright_replicate_critical_cells",
    "RegisterRetiming": "rapidwright_execute_register_retiming",
    "CongestionSpreading": "rapidwright_execute_congestion_spreading",
    "NetSwap": "rapidwright_execute_net_swapping",
    "PhysOpt+RegisterRetiming": "vivado_physopt_and_route",
    "OptDesign": "rapidwright_execute_opt_design_strategy",
    # Aliases: LLM may use alternative names for the same strategy
    "LogicOptimization": "rapidwright_execute_opt_design_strategy",
    # New strategies for NN/datapath designs
    "LogicResynthesis": "vivado_run_tcl",
    "PhysOptAggressive": "vivado_physopt_and_route",
}


# ── Threshold constants ──────────────────────────────────────────

WORKER_UPGRADE_THRESHOLD = 2       # Cumulative failures before upgrade
WORKER_DOWNGRADE_THRESHOLD = 3     # Worker consecutive successes before downgrade
GLOBAL_NO_IMPROVEMENT_LIMIT = 4    # Global no-improvement limit (increased from 2 for harder benchmarks)
WNS_TARGET_THRESHOLD = 0.0         # WNS target (0.0 ns = timing convergence)
WNS_ROLLBACK_THRESHOLD: float = 0.050  # 50ps: trigger rollback when latest_wns falls this far below best_wns
MAX_STRATEGY_CYCLES = 5            # Max strategy cycles per iteration (increased from 3 for more strategy exploration)

# ── RapidWright directional pre-check ──────────────────────────────
# Level 1 pre-filter: before running the expensive Vivado P&R chain,
# use RapidWright's timing estimation (~2.5s, ~2% error) to check
# whether the skill's modification directionally improved WNS.
#
# RapidWright timing is reliable for *directional* comparison
# (which of two placements is better), but NOT for absolute WNS
# values — especially on UltraScale+ multi-SLR designs where route
# congestion dominates and RW can't predict it without actual routing.
#
# See the plan at docs/plans/p-r-rollback-abundant-puffin.md for
# the three-level funnel design (RW pre-check → Place-Only → Full P&R).
RAPIDWRIGHT_PRECHECK_ENABLED: bool = True
# Directional regression threshold: when post-skill RapidWright WNS
# estimate falls this far below the pre-skill Vivado WNS baseline,
# treat the change as likely harmful and skip the P&R chain.
# Not an absolute accuracy claim — only used for direction detection.
RAPIDWRIGHT_PRECHECK_REGRESS_THRESHOLD: float = 0.080  # Widened from 50ps to 80ps to avoid rejecting strategies that help after full P&R


# ── Vivado Place-Only intermediate check (Level 2) ─────────────────
# After a skill passes the RapidWright directional pre-check (Level 1),
# the skill's design DCP is loaded into Vivado and place_design runs.
# At this point we can get a *placement-level* timing estimate via
# vivado_report_timing_summary — more reliable than RW timing because
# Vivado knows the actual placement, though still without routing.
#
# Skills suitable for place-only check are those whose auto-chain
# includes a vivado_place_design step (pblock, fanout, opt_design).
# Skills without place_design (physopt, register_retiming) skip this.
PLACE_ONLY_CHECK_ENABLED: bool = True

# Threshold for place-only WNS regression. When place-only WNS falls
# this far below the pre-skill baseline, skip the remaining route steps.
# Higher than RAPIDWRIGHT_PRECHECK_REGRESS_THRESHOLD because place-level
# timing has less uncertainty than RW estimation.
PLACE_ONLY_REGRESS_THRESHOLD: float = 0.050  # Widened from 30ps to 50ps for harder designs

# Skills that trigger place-only check in their chain.
PLACE_ONLY_CHECK_SKILLS: frozenset[str] = frozenset({
    "rapidwright_execute_fanout_strategy",
    "rapidwright_execute_opt_design_strategy",
    "rapidwright_execute_pblock_strategy",
})

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
    "vivado_report_route_status": frozenset({"route_status"}),
    "vivado_report_timing_summary": frozenset({"timing_summary", "cdc_paths"}),
    "rapidwright_get_design_info": frozenset({"design_info"}),
}


# ── Per-phase tool rate limits ────────────────────────────────────
# Prevents LLM from repeatedly calling the same read-only tool within
# a single phase. When exceeded, the call returns a rate-limit message
# directing the LLM to use alternatives (dashboard data, batch params).
PHASE_TOOL_RATE_LIMITS: dict[str, int] = {
    "rapidwright_search_cells": 3,
    "vivado_run_tcl": 2,
    "vivado_write_checkpoint": 3,  # prevent excessive checkpoint I/O from LLM
    "rapidwright_analyze_net_detour": 2,  # suppress when consistently returning 0 results
    # REMOVED: vivado_report_route_status, rapidwright_get_design_info,
    # rapidwright_get_device_topology — removed from ANALYZE/EVALUATE
    # allowlists entirely (data already in Dashboard from init_analysis).
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


# ── Heavy chain skills ──────────────────────────────────────────
# Skills whose auto-chain includes place_design + route_design (~180s).
# When post-eval shows UNCHANGED, the chain is skipped in favor of a
# lightweight validation (place_design without pblock). This prevents
# wasting ~3 min on a strategy that didn't modify the netlist.
HEAVY_CHAIN_SKILLS: frozenset[str] = frozenset({
    # NOTE: rapidwright_execute_pblock_strategy is intentionally excluded.
    # The skill is analysis-only (returns pblock_ranges); the actual
    # netlist mutation happens via the SKILL_CHAIN_ACTIONS chain
    # (unplace → create_pblock → place → route). The chain must always
    # run regardless of post-eval verdict.
    "rapidwright_execute_fanout_strategy",
})


SKILL_CHAIN_ACTIONS: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": [
        {"tool": "vivado_place_design", "args": {"directive": "unplace"}},
        {"tool": "vivado_create_and_apply_pblock",
         "args_from_skill": {
             "pblock_name": "pblock_name",
             "ranges": "pblock_ranges",
             "is_soft": "is_soft_recommended",
         }},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}},
    ],
    # Auto-chain: open checkpoint written by retiming, then route so WNS eval triggers.
    "rapidwright_execute_register_retiming": [
        {"tool": "vivado_open_checkpoint",
         "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}},
    ],
    # Auto-chain: open fanout checkpoint, place, then route before WNS eval.
    # Without this, post-eval sees unplaced design and reports false WNS improvement.
    "rapidwright_execute_fanout_strategy": [
        {"tool": "vivado_open_checkpoint",
         "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}},
    ],
    # Auto-chain: after opt_design modifies netlist, must re-place + re-route.
    # opt_design is called by the Vivado MCP tool vivado_opt_design, triggered
    # via the RapidWright wrapper skill rapidwright_execute_opt_design_strategy.
    "rapidwright_execute_opt_design_strategy": [
        {"tool": "vivado_opt_design",
         "args_from_skill": {"directive": "directive", "retarget": "retarget"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}},
    ],
    # Auto-chain: phys_opt_design modifies placement, then evaluate before routing.
    # Split from vivado_physopt_and_route to allow early termination if phys_opt
    # doesn't improve WNS (saves ~40s of unnecessary routing per attempt).
    "rapidwright_execute_physopt_strategy": [
        {"tool": "vivado_phys_opt_design",
         "args_from_skill": {"directive": "directive"}},
        # Post-eval fires here (vivado_phys_opt_design is in POST_EVAL_TOOLS).
        # If UNCHANGED, chain gate (P0) skips remaining steps.
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}},
    ],
}


# ── Optional chain actions (LLM can choose to use or skip) ──────────
# These chains provide validation steps that LLM can insert at will.
# Unlike SKILL_CHAIN_ACTIONS (auto-executed), these are suggestions.
OPTIONAL_CHAIN_VALIDATION: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": {
        "description": "PBLOCK strategy with optional validation steps",
        "validation_before": [
            {"tool": "vivado_check_design_status", "args": {}},
        ],
        "validation_after": [
            {"tool": "vivado_validate_timing", "args": {}},
            {"tool": "rapidwright_compare_designs", "args": {}},
        ],
        "optional_steps": [
            {"tool": "rapidwright_estimate_timing", "args": {}, "skip_if": "direction_regress"},
        ],
    },
    "rapidwright_execute_fanout_strategy": {
        "description": "Fanout strategy with optional validation steps",
        "validation_before": [
            {"tool": "vivado_check_design_status", "args": {}},
        ],
        "validation_after": [
            {"tool": "vivado_validate_timing", "args": {}},
        ],
    },
    "rapidwright_optimize_cell_placement": {
        "description": "Cell placement optimization with optional validation",
        "validation_before": [
            {"tool": "vivado_check_design_status", "args": {}},
        ],
        "validation_after": [
            {"tool": "vivado_validate_timing", "args": {}},
            {"tool": "rapidwright_compare_designs", "args": {}},
        ],
    },
    "rapidwright_smart_retiming": {
        "description": "Register retiming with optional validation",
        "validation_before": [
            {"tool": "vivado_check_design_status", "args": {}},
        ],
        "validation_after": [
            {"tool": "vivado_validate_timing", "args": {}},
            {"tool": "rapidwright_compare_designs", "args": {}},
        ],
    },
}


# ── Design consistency constraints for LLM ──────────────────────────
# These constraints are injected into LLM context to guide tool selection.
DESIGN_CONSISTENCY_CONSTRAINTS = """
## Design Consistency Constraints

### CRITICAL: Competition Requirements
1. **Timing Convergence**: WNS must be ≥ 0
2. **Logic Equivalence**: Design behavior must not change

### Safe Operations (READ-ONLY, always safe):
- vivado_report_timing_summary — read timing
- vivado_extract_critical_path_cells — read paths
- vivado_check_design_status — check placement/routing status
- vivado_validate_timing — validate timing after modifications
- rapidwright_report_timing — estimate timing (direction only)
- rapidwright_analyze_* — analyze design structure
- rapidwright_search_cells — search for cells
- rapidwright_compare_designs — compare designs for consistency

### Risky Operations (MAY modify design logic):
- rapidwright_optimize_* — changes placement/optimization
- rapidwright_smart_retiming — changes register positions
- rapidwright_execute_* — executes strategies
- vivado_place_design — changes placement
- vivado_route_design — changes routing
- vivado_phys_opt_design — changes placement optimization

### Validation Requirements:
1. After ANY risky operation, run vivado_validate_timing
2. Before submission, run rapidwright_compare_designs
3. Always check vivado_check_design_status before timing checks

### RapidWright Accuracy Warning:
For this design (37K+ cells, UltraScale+):
- RapidWright timing error: up to 0.5ns+ for cross-SLR paths
- Only directional comparison (better/worse) is reliable
- Always verify with Vivado for final decisions
"""


# ── Per-tool timeout defaults (seconds) ──────────────────────────
# Quick read-only queries use short timeouts; execution tools use long timeouts.
# All timeouts are multiplied by design_size_factor at call time.
# User-specified timeout in arguments always takes priority.

_TOOL_TIMEOUT_DEFAULTS: dict[str, float] = {
    # Fast read-only queries (< 60s base)
    "vivado_get_wns": 30.0,
    "vivado_search_cells": 60.0,
    "vivado_get_resource_counts": 60.0,
    "vivado_get_cached_high_fanout_nets": 10.0,
    "rapidwright_get_device_topology": 30.0,
    "rapidwright_get_design_info": 30.0,
    "rapidwright_search_cells": 60.0,
    "rapidwright_analyze_critical_path_spread": 60.0,
    "rapidwright_analyze_congestion": 60.0,
    # Medium queries (60-120s base)
    "vivado_report_timing_summary": 120.0,
    "vivado_report_route_status": 120.0,
    "vivado_run_tcl": 120.0,
    "vivado_extract_critical_path_cells": 120.0,
    "vivado_extract_critical_path_pins": 120.0,
    "vivado_get_critical_high_fanout_nets": 120.0,
    "vivado_report_utilization_for_pblock": 120.0,
    "rapidwright_read_checkpoint": 120.0,
    "rapidwright_analyze_pblock_region": 120.0,
    "rapidwright_initialize_rapidwright": 60.0,
    # Heavy execution tools (300-3600s base)
    "vivado_open_checkpoint": 600.0,
    "vivado_place_design": 1800.0,
    "vivado_route_design": 1800.0,
    "vivado_phys_opt_design": 3600.0,
    "vivado_physopt_and_route": 3600.0,
    "vivado_write_checkpoint": 300.0,
    "vivado_write_verilog_simulation": 300.0,
    "vivado_create_and_apply_pblock": 300.0,
    "rapidwright_execute_pblock_strategy": 600.0,
    "rapidwright_execute_fanout_strategy": 300.0,
    "rapidwright_optimize_cell_placement": 300.0,
    "rapidwright_execute_congestion_spreading": 300.0,
    "rapidwright_execute_register_retiming": 300.0,
    "rapidwright_execute_net_swapping": 300.0,
    "rapidwright_optimize_lut_input_cone": 300.0,
    "rapidwright_replicate_critical_cells": 300.0,
    "rapidwright_smart_region_search": 300.0,
    "rapidwright_optimize_fanout_batch": 300.0,
    "rapidwright_write_checkpoint": 300.0,
    "rapidwright_compare_design_structure": 120.0,
    "rapidwright_smart_retiming": 300.0,
    "rapidwright_execute_opt_design_strategy": 120.0,
    "rapidwright_execute_physopt_strategy": 120.0,
    "vivado_opt_design": 600.0,
}

_DEFAULT_TOOL_TIMEOUT: float = 300.0
_TOOL_TIMEOUT_MAX: float = 900.0  # Hard cap: never wait more than 15 min per call


# ── MCP error response detection patterns ──────────────────────────
# MCP servers may return error strings starting with [ERROR] instead of
# raising exceptions. These must be treated as failures: no caching,
# cache invalidation, and the error must propagate to the agent framework.

_MCP_ERROR_PATTERNS: tuple[str, ...] = (
    "[ERROR] Tcl command timed out",
    "[ERROR] Application-level timeout",
    "[ERROR] Multi-line script aborted",
    "[ERROR] Multi-line script validation failed",
    "[ERROR] Vivado process terminated",
    '"error":',      # JSON error from tool_router
)
