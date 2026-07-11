"""Shared constants for the optimizer pure functions.

Extracted from dcp_optimizer.py: L90-122, L135-159, L296-315, L865-868.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tool-layer exports are progressively moving into dedicated modules.
# ``constants.py`` remains a compatibility barrel while consumers migrate.


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


# ── Strategy map (single source of truth) ─────────────────────────
# Consolidates four previously independent mappings:
#   EXECUTE_STRATEGY_TOOL_MAP (strategy → execute tool)
#   STRATEGY_TO_PRIMARY_TOOL (strategy → primary tool)
#   STRATEGY_SKILL_MAP (strategy → skill identifier)
#   SKILL_TOOL_MAP / SKILL_NAME_TO_TOOL (bidirectional tool ↔ skill)

@dataclass
class StrategyEntry:
    """Mapping entry for a single strategy.

    Fields:
        skill_id:   Skill identifier (used in telemetry, logging).
        execute_tool: MCP tool called during the EXECUTE phase.
    """
    skill_id: str
    execute_tool: str


STRATEGY_MAP: dict[str, StrategyEntry] = {
    "PBLOCK": StrategyEntry("pblock_strategy", "rapidwright_execute_pblock_strategy"),
    "PhysOpt": StrategyEntry("physopt_strategy", "vivado_physopt_and_route"),
    "Fanout": StrategyEntry("fanout_strategy", "rapidwright_execute_fanout_strategy"),
    "LUTCascade": StrategyEntry("lut_cascade_flattening", "rapidwright_flatten_lut_cascade"),
    "PinSwap": StrategyEntry("pin_swapping_strategy", "rapidwright_optimize_pin_swapping"),
    "CellReplication": StrategyEntry("critical_path_cell_replication", "rapidwright_replicate_critical_cells"),
    "CongestionSpreading": StrategyEntry("execute_congestion_spreading", "rapidwright_execute_congestion_spreading"),
    "NetSwap": StrategyEntry("execute_net_swapping", "rapidwright_execute_net_swapping"),
    "OptDesign": StrategyEntry("opt_design_strategy", "rapidwright_execute_opt_design_strategy"),
    "RegisterRetiming": StrategyEntry("execute_register_retiming", "rapidwright_execute_register_retiming"),
    "SmartRetiming": StrategyEntry("smart_retiming", "rapidwright_smart_retiming"),
    "LogicResynthesis": StrategyEntry("opt_design_strategy", "rapidwright_execute_opt_design_strategy"),
    "PhysOptAggressive": StrategyEntry("physopt_strategy", "vivado_physopt_and_route"),
    "CombinationalRebalance": StrategyEntry("combinational_rebalancing_strategy", "rapidwright_execute_combinational_rebalancing_strategy"),
    "LUTMUXFRepack": StrategyEntry("lut_muxf_repack_strategy", "rapidwright_execute_lut_muxf_repack_strategy"),
    "MUXFTreeReorder": StrategyEntry("muxf_tree_reorder_strategy", "rapidwright_execute_muxf_tree_reorder_strategy"),
    # Aliases: LLM may use alternative names for the same strategy
    "LogicOptimization": StrategyEntry("opt_design_strategy", "rapidwright_execute_opt_design_strategy"),
    # Combined strategies
    "PhysOpt+RegisterRetiming": StrategyEntry("physopt_strategy", "vivado_physopt_and_route"),
    # Vivado-only directive-exploration strategies (no RapidWright skill).
    # Keep in sync with tool_catalog.STRATEGY_MAP.
    "PlaceRouteDirectiveExplore": StrategyEntry("place_route_directive_explore", "vivado_place_design"),
    "CongestionRouteExplore": StrategyEntry("congestion_route_explore", "vivado_route_design"),
}

# Backward-compatible reverse mapping: tool → skill_id.
# Auto-derived from STRATEGY_MAP so it never drifts.
SKILL_TOOL_MAP: dict[str, str] = {v.execute_tool: v.skill_id for v in STRATEGY_MAP.values()}

# ── Contract layer centralized derivations ──────────────────────────
# Single source of truth: all tool sets derive from STRATEGY_MAP.

PRIMARY_STRATEGY_TOOL_NAMES: frozenset[str] = frozenset(
    v.execute_tool for v in STRATEGY_MAP.values()
)


def get_strategy_primary_tool(strategy: str) -> str | None:
    entry = STRATEGY_MAP.get(strategy)
    return entry.execute_tool if entry else None


# ── Auto-derived strategy tool names (must precede DESIGN_MODIFICATION_TOOLS) ─

# Names of tools that perform the actual optimization work (strategy tools).
# Used by EVALUATE cooling logic to distinguish strategy-tool errors (which
# mean the strategy didn't execute fairly) from auxiliary-tool errors (which
# mean the strategy ran but auxiliary analysis tools failed). When only
# auxiliary tools have errors, the strategy still got a fair execution chance
# and should be cooled down if it showed no improvement.
# Auto-derived from STRATEGY_MAP with supplementary execution tools appended.
STRATEGY_TOOL_NAMES: frozenset[str] = frozenset(
    PRIMARY_STRATEGY_TOOL_NAMES
    | {
        "vivado_physopt_and_route",
        "vivado_phys_opt_design",
        "vivado_opt_design",
        "vivado_place_design",
        "vivado_route_design",
        "rapidwright_optimize_cell_placement",
        "rapidwright_optimize_lut_input_cone",
    }
)

# Core tools always available during EXECUTE phase.
EXECUTE_CORE_TOOLS: frozenset[str] = frozenset(
    PRIMARY_STRATEGY_TOOL_NAMES
    | {
        "vivado_physopt_and_route",
        "vivado_phys_opt_design",
        "vivado_opt_design",
        "vivado_place_design",
        "vivado_route_design",
        "vivado_write_checkpoint",
        "vivado_open_checkpoint",
        "vivado_report_timing_summary",
        "report_step_state",
    }
)

# Tools whose execution triggers a post-eval timing comparison.
POST_EVAL_TOOLS: frozenset[str] = frozenset({
    "vivado_route_design",
    "vivado_phys_opt_design",
    "vivado_physopt_and_route",
    "rapidwright_execute_pblock_strategy",
    "rapidwright_execute_muxf_tree_reorder_strategy",
})


# ── Threshold constants ──────────────────────────────────────────

WORKER_UPGRADE_THRESHOLD = 2       # Cumulative failures before upgrade
WORKER_DOWNGRADE_THRESHOLD = 3     # Worker consecutive successes before downgrade
GLOBAL_NO_IMPROVEMENT_LIMIT = 3    # Global no-improvement limit (balanced: give enough time for optimization)
PR_DIRECTIVE_COMBINATIONS = [
    ("Explore", "Explore", "default_after_physopt"),
    ("ExtraTimingOpt", "NoTimingRelaxation", "logic_depth_limited"),
    ("AltSpreadLogic_medium", "HigherDelayCost", "congestion_medium"),
    ("AltSpreadLogic_high", "Explore", "congestion_high"),
    ("ExtraPostPlacementOpt", "Default", "wns_stuck"),
    ("AltSpreadLogic_high", "Explore", "spread_needed"),
    ("Default", "HigherDelayCost", "route_critical"),
    ("Explore", "NoTimingRelaxation", "aggressive_setup"),
]

# Per-strategy default (place, route) directive profile — tier-2 fallback.
# When the LLM omits place_directive/route_directive in a skill call, the
# auto-chain executor applies these strategy-appropriate defaults instead of
# the universal "Explore" (tier 3). LLM-provided directives (tier 1) still
# take precedence. Scenarios sourced from PR_DIRECTIVE_COMBINATIONS above.
# All values are within PLACE_SAFE_DIRECTIVES / ROUTE_SAFE_DIRECTIVES
# (enforced at the VivadoMCP server level).
# place=None means the chain has no place_design step for that strategy.
STRATEGY_DEFAULT_DIRECTIVES: dict[str, tuple[str | None, str | None]] = {
    # pblock: clusters critical-path cells into a tight region; route must
    # not relax timing targets.
    "rapidwright_execute_pblock_strategy": ("Explore", "NoTimingRelaxation"),
    # physopt: incremental placement optimization; balanced re-route
    # (matches PR_DIRECTIVE_COMBINATIONS "default_after_physopt").
    "rapidwright_execute_physopt_strategy": ("Explore", "Explore"),
    # opt_design: logic-depth reduction → ExtraTimingOpt place + hold timing.
    # (matches "logic_depth_limited")
    "rapidwright_execute_opt_design_strategy": ("ExtraTimingOpt", "NoTimingRelaxation"),
    # combinational rebalancing: logic-depth → same as opt_design.
    "rapidwright_execute_combinational_rebalancing_strategy": ("ExtraTimingOpt", "NoTimingRelaxation"),
    # lut_muxf repack: logic-depth/structure optimization → same profile.
    "rapidwright_execute_lut_muxf_repack_strategy": ("ExtraTimingOpt", "NoTimingRelaxation"),
    # muxf tree reorder: phys_opt + route only (no place step) → place unused.
    "rapidwright_execute_muxf_tree_reorder_strategy": (None, "Explore"),
    # fanout: net-delay reduction; route holds timing targets.
    "rapidwright_execute_fanout_strategy": ("Explore", "NoTimingRelaxation"),
    # flatten lut cascade: logic-depth reduction → same as opt_design.
    "rapidwright_flatten_lut_cascade": ("ExtraTimingOpt", "NoTimingRelaxation"),
}

# Directives known to fail due to licensing or tool limitations.
# When an LLM provides one of these, the auto-chain silently falls back to
# the strategy's default directive instead of passing the failing directive
# through to Vivado, saving a full P&R cycle.
KNOWN_BROKEN_DIRECTIVES: frozenset[str] = frozenset({
    "Performance_ExtraTimingOpt",  # Requires Extra Timing license not in contest env
})

NETLIST_MODIFYING_STRATEGIES = frozenset({
    "Fanout", "LUTCascade", "CellReplication", "NetSwap",
    "OptDesign", "LogicResynthesis", "CombinationalRebalance",
    "LUTMUXFRepack", "MUXFTreeReorder",
})

PLACEMENT_ONLY_STRATEGIES = frozenset({"PBLOCK", "CongestionSpreading", "PinSwap"})

PHYS_OPT_ONLY_STRATEGIES = frozenset({"PhysOpt", "PhysOptAggressive", "CongestionRouteExplore"})

HOLD_VIOLATION_THRESHOLD_NS = -0.050
PULSE_WIDTH_VIOLATION_THRESHOLD_NS = -0.050
EQUIVALENCE_FF_CHANGE_THRESHOLD = 0.05
EQUIVALENCE_LUT_CHANGE_THRESHOLD = 0.10
MAX_ACCEPTABLE_ROUTE_ERRORS = 0

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
RAPIDWRIGHT_PRECHECK_ENABLED: bool = False  # Disabled: trust LLM strategy selection, run full P&R
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
PLACE_ONLY_CHECK_ENABLED: bool = False  # Disabled: trust LLM strategy selection, always run full P&R chain

# Threshold for place-only WNS regression. When place-only WNS falls
# this far below the pre-skill baseline, skip the remaining route steps.
# Higher than RAPIDWRIGHT_PRECHECK_REGRESS_THRESHOLD because place-level
# timing has less uncertainty than RW estimation.
PLACE_ONLY_REGRESS_THRESHOLD: float = 0.030

# Skills that trigger place-only check in their chain.
PLACE_ONLY_CHECK_SKILLS: frozenset[str] = frozenset({
    "rapidwright_execute_fanout_strategy",
    "rapidwright_execute_opt_design_strategy",
    "rapidwright_execute_combinational_rebalancing_strategy",
    "rapidwright_execute_lut_muxf_repack_strategy",
    # PBLOCK excluded: its chain uses local unplace_cells (critical cells only),
    # so after place_design the moved cells' nets are temporarily unrouted and
    # place-only WNS is an artifactual regression — the check would wrongly skip
    # route_design. PBLOCK relies on post-chain re-eval + EVALUATE rollback instead.
})

# Context thresholds (derived from model config, but we use safe defaults)
SMALL_OUTPUT_THRESHOLD = 3000              # Bypass summarization below this (chars)
TOOL_RESULT_TRUNCATE = 30000               # Truncation limit for tool results (chars)
RAW_OUTPUT_DIRECT_THRESHOLD = 50000        # Bypass summarization for vivado_get_raw_tool_output below this (chars)
RAW_OUTPUT_SMART_TRUNCATE = 50000          # Head+tail truncation for large raw outputs (chars)
RECENT_TURNS_TO_KEEP = 20         # Recent messages to keep during compression

# Worker context limits (from model_config.yaml, safe defaults)
# Used for worker→planner upgrade decisions (not compression thresholds)
WORKER_CONTEXT_WARN_TOKENS = 180_000   # ~72% of 250K max_tokens (planner promotion when context is full)
WORKER_CONTEXT_FORCE_TOKENS = 200_000  # ~80% of 250K max_tokens (force planner at hard limit)

# Design data persistence
DESIGN_DATA_DIR = "design_data"               # Subdirectory under run_dir
DESIGN_DATA_MAX_FILES = 500                   # Max JSON files per iteration (safety limit)


# ── Dashboard freshness tracking ──────────────────────────────

# Maps tool names → dashboard fields they refresh.
# Used to track dashboard data freshness after tool execution.
# Extensible: add new tool→field mappings here when developing new tools.
DASHBOARD_REFRESH_MAP: dict[str, frozenset[str]] = {
    "vivado_report_utilization_for_pblock": frozenset({"resource_utilization"}),
    "vivado_get_critical_high_fanout_nets": frozenset({"high_fanout_nets"}),
    "rapidwright_analyze_critical_path_spread": frozenset({"critical_path_spread"}),
    "vivado_extract_critical_path_pins": frozenset({"critical_path_spread"}),
    "vivado_report_route_status": frozenset({"route_status", "route_status_detail"}),
    "vivado_report_timing_summary": frozenset({"timing_summary", "cdc_paths"}),
    "rapidwright_get_design_info": frozenset({"design_info"}),
    "vivado_extract_critical_path_cells": frozenset({"critical_path_cells"}),
    "vivado_report_qor_suggestions": frozenset({"qor_suggestions"}),
    "vivado_report_high_fanout_nets": frozenset({"high_fanout_nets"}),
    "vivado_get_wns": frozenset({"timing_summary"}),
    "rapidwright_analyze_congestion": frozenset({"congestion_data"}),
}

# ── Design modification tools ──────────────────────────────────
# Tools that modify the design (place, route, phys_opt, RW strategies).
# When called, all dashboard field_freshness entries are marked "stale"
# so the LLM knows which data may no longer be current.
# Auto-derived from STRATEGY_TOOL_NAMES with extra modification tools appended.
DESIGN_MODIFICATION_TOOLS: frozenset[str] = frozenset(
    STRATEGY_TOOL_NAMES
    | {
        "vivado_create_and_apply_pblock",
        "vivado_unplace_cells",
        "vivado_open_checkpoint",
        "rapidwright_optimize_fanout_batch",
        "rapidwright_execute_physopt_strategy",
        "rapidwright_smart_retiming",
        "vivado_opt_design",
    }
)

# Tools with side effects on the design — reuses DESIGN_MODIFICATION_TOOLS
# to prevent drift between the two definitions.
SIDE_EFFECT_TOOLS: frozenset[str] = DESIGN_MODIFICATION_TOOLS


# ── TCL modification detection (for vivado_run_tcl) ───────────────
# vivado_run_tcl is NOT in DESIGN_MODIFICATION_TOOLS because the LLM also
# uses it for read-only queries (get_property, report_*). Instead, the
# stale-marking sites inspect the TCL command via is_modifying_tcl() so
# only design-modifying commands invalidate field_freshness — closing the
# false-freshness gap without over-aggressively stale-marking read-only TCL.
MODIFYING_TCL_VERBS: frozenset[str] = frozenset({
    "place_design", "route_design", "phys_opt_design", "opt_design",
    "unplace", "unplace_cells", "set_property", "create_pblock",
    "resize_pblock", "add_cells_to_pblock", "remove_cells_from_pblock",
    "delete_pblock", "place_cell", "unroute_net", "route_net",
    "lock_design", "unlock_design", "replace_cell", "move_cell",
    "retarget", "remap", "synth_design", "link_design",
})


def is_modifying_tcl(command: str) -> bool:
    """Return True if a vivado_run_tcl command string contains design-modifying verbs.

    Matches the first word of each non-comment line (Vivado TCL is line-oriented),
    so read-only commands like ``report_timing_summary`` or ``get_property`` do
    not trigger stale-marking, while ``place_design`` / ``set_property ...`` do.
    """
    if not command or not isinstance(command, str):
        return False
    for line in command.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if not tokens:
            continue
        first_word = tokens[0].lower().split("::")[-1]
        if first_word in MODIFYING_TCL_VERBS:
            return True
    return False


# ── Per-phase tool rate limits ────────────────────────────────────
# Prevents LLM from repeatedly calling the same read-only tool within
# a single phase. When exceeded, the call returns a rate-limit message
# directing the LLM to use alternatives (dashboard data, batch params).
PHASE_TOOL_RATE_LIMITS: dict[str, int] = {
    "rapidwright_search_cells": 3,
    "vivado_run_tcl": 2,
    "vivado_write_checkpoint": 3,  # prevent excessive checkpoint I/O from LLM
    "rapidwright_analyze_net_detour": 2,  # suppress when consistently returning 0 results
    "vivado_get_cached_high_fanout_nets": 2,  # data is in Dashboard M4; 2 covers verify + re-check
    "vivado_check_design_status": 3,  # design state is in Dashboard M1; 3 covers pre/post-EXECUTE checks
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

    Prompt caching strategy (two-tier):
    - Tier 1 (extra_body.cache): Sets ``{"cache": {"prompt": True}}`` for OpenRouter
      prompt caching. This caches the messages array prefix so repeated system
      messages + early conversation turns avoid re-encoding across calls.
      The cache TTL is ~5min on OpenRouter (matches Anthropic prompt cache TTL).

    - Tier 2 (extra_body.system): The static system prompt is further passed as a
      top-level ``system`` parameter (not as a message in the messages array).
      This is set by each phase's _call_phase_llm after extracting the system
      message from the formatted API messages. Separating the static system
      content from the dynamic conversation history maximizes cache prefix
      stability — the messages array becomes shorter and changes less between
      calls, so more of its prefix survives in cache.

    Cache metrics (cache_read_input_tokens, cache_creation_input_tokens) are
    extracted from the OpenRouter usage response by _track_cost() and logged
    by LLMCallLogger, providing visibility into cache effectiveness.
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
# Disabled: trust LLM strategy selection, always run the full P&R chain
# so strategies get real P&R validation rather than being skipped on a
# post-eval UNCHANGED verdict.
HEAVY_CHAIN_SKILLS: frozenset[str] = frozenset()

# place_design/route_design steps accept an optional directive override via
# args_from_skill (keys: place_directive / route_directive in the skill result).
# When absent, the hardcoded "Explore" in args is used as fallback.
#
# route_design routing reuse: Vivado automatically preserves routing for
# unchanged nets when route_design is called on a partially-routed design, so
# no explicit flag is needed. (A previous `reuse: True` arg emitted an invalid
# `-reuse` flag that Vivado rejected with "Unknown option '-reuse'", causing
# every PBLOCK/physopt chain to fail at the route step.)
SKILL_CHAIN_ACTIONS: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": [
        # Local unplace: only critical-path cells (vs the old global
        # place_design -unplace which tore down the whole design). Keeps the
        # rest of the design placed/routed so the next place+route is
        # incremental — Vivado automatically reuses prior routing for unchanged nets.
        {"tool": "vivado_unplace_cells",
         "args_from_skill": {"cells": "critical_path_cells"}},
        {"tool": "vivado_create_and_apply_pblock",
         "args_from_skill": {
             "pblock_name": "pblock_name",
             "ranges": "pblock_ranges",
             "is_soft": "is_soft_recommended",
             "cells": "critical_path_cells",
         }},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    # Auto-chain: open checkpoint written by retiming, then route so WNS eval triggers.
    "rapidwright_execute_register_retiming": [
        {"tool": "vivado_open_checkpoint",
         "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    # Auto-chain: open fanout checkpoint, place, then route before WNS eval.
    # Without this, post-eval sees unplaced design and reports false WNS improvement.
    "rapidwright_execute_fanout_strategy": [
        {"tool": "vivado_open_checkpoint",
         "args_from_skill": {"dcp_path": "checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    # Auto-chain: after opt_design modifies netlist, must re-place + re-route.
    # opt_design is called by the Vivado MCP tool vivado_opt_design, triggered
    # via the RapidWright wrapper skill rapidwright_execute_opt_design_strategy.
    "rapidwright_execute_opt_design_strategy": [
        {"tool": "vivado_opt_design",
         "args_from_skill": {"directive": "directive", "retarget": "retarget"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}},
    ],
    # Auto-chain: combinational rebalancing — same as opt_design chain.
    # RapidWright identifies deep combinational segments; Vivado opt_design -remap
    # performs logic-equivalent resynthesis (NO FF insert, latency preserved).
    "rapidwright_execute_combinational_rebalancing_strategy": [
        {"tool": "vivado_opt_design",
         "args_from_skill": {"directive": "directive", "retarget": "retarget"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}},
    ],
    # Auto-chain: LUT6+MUXF co-repack — same as opt_design chain (AddRemap directive).
    "rapidwright_execute_lut_muxf_repack_strategy": [
        {"tool": "vivado_opt_design",
         "args_from_skill": {"directive": "directive", "retarget": "retarget"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}},
    ],
    # Auto-chain: MUXF tree reorder — phys_opt_design (NO -retime) + route.
    # phys_opt_design requires a placed design; no place_design step needed.
    "rapidwright_execute_muxf_tree_reorder_strategy": [
        {"tool": "vivado_phys_opt_design",
         "args_from_skill": {"directive": "directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
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
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
    # Auto-chain: LUT cascade flattening mutates the RW netlist and writes a
    # post-flatten DCP. Open it in Vivado and re-place + re-route to get a real
    # post-route WNS (RW estimate_timing is ~0.2ns pessimistic and unreliable
    # for strategy evaluation). Skipped automatically when the skill returns
    # status="skipped"/"no_action" (no cascades or wide input cones).
    "rapidwright_flatten_lut_cascade": [
        {"tool": "vivado_open_checkpoint",
         "args_from_skill": {"dcp_path": "post_checkpoint_path"}},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
        {"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}},
    ],
}


def get_skill_chain_actions(tool_name: str) -> list[dict] | None:
    return SKILL_CHAIN_ACTIONS.get(tool_name)


def has_skill_chain(tool_name: str) -> bool:
    return tool_name in SKILL_CHAIN_ACTIONS


# Chain-behavior contracts consumed by EXECUTE.
# PBLOCK is analysis-first: the skill itself computes ranges and the Vivado
# chain applies them, so RW directional pre-check is not meaningful there.
RW_PRECHECK_EXEMPT_CHAIN_TOOLS: frozenset[str] = frozenset({
    "rapidwright_execute_pblock_strategy",
})

# Some chain tools legitimately return sparse payloads but still require the
# Vivado chain to materialize real timing impact.
EMPTY_RESULT_CHAIN_EXEMPT_TOOLS: frozenset[str] = frozenset({
    "rapidwright_execute_pblock_strategy",
    "rapidwright_flatten_lut_cascade",
})


def tool_uses_rw_precheck(tool_name: str) -> bool:
    """Return True when a chained tool should run the RW directional pre-check."""
    return has_skill_chain(tool_name) and tool_name not in RW_PRECHECK_EXEMPT_CHAIN_TOOLS


def should_skip_chain_for_empty_result(
    tool_name: str,
    skill_result_data: dict | None,
) -> tuple[bool, str | None]:
    """Return whether the auto-chain should be skipped for a sparse skill result."""
    if not has_skill_chain(tool_name) or not isinstance(skill_result_data, dict):
        return False, None

    status = skill_result_data.get("status")
    is_skipped = status in ("skipped", "no_action", "unchanged")
    # Plan-style results (status "ready"/"planned" with non-empty steps) carry
    # an executable Vivado chain even without optimized_cells/critical_paths.
    # run-20260710_190708: LUTMUXFRepack/MUXFTreeReorder returned ready plans
    # but were misjudged as "no data produced", so their opt_design/phys_opt
    # chains never ran. `steps` (not analysis_summary) is the signal: skipped
    # plans also attach analysis_summary but never steps.
    has_ready_plan = (
        status in ("ready", "planned")
        and bool(skill_result_data.get("steps"))
    )
    has_empty_payload = (
        not has_ready_plan
        and not skill_result_data.get("optimized_cells")
        and not skill_result_data.get("critical_paths")
        # Fanout reports effect via successful_count (not optimized_cells/steps);
        # >0 means it split nets, so don't treat as empty (run-20260711_232113:
        # 16 splits were orphaned when the chain was wrongly skipped).
        and (skill_result_data.get("successful_count") or 0) <= 0
    )

    if is_skipped:
        return True, "skipped"
    if has_empty_payload and tool_name not in EMPTY_RESULT_CHAIN_EXEMPT_TOOLS:
        return True, "no data produced"
    return False, None


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
    "rapidwright_execute_combinational_rebalancing_strategy": 120.0,
    "rapidwright_execute_lut_muxf_repack_strategy": 120.0,
    "rapidwright_execute_muxf_tree_reorder_strategy": 120.0,
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


# Cost control constants
COST_PER_MHZ_TARGET = 0.01         # Target cost per MHz gained ($)
COST_HARD_STOP_FRACTION = 0.95     # Stop at 95% of cost budget
COST_WARN_FRACTION = 0.70          # Warn at 70% of cost budget
MIN_LLM_EFFICIENCY_SCORE = 5.0     # Min score per dollar of LLM cost
