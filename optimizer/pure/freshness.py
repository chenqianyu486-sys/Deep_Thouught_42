"""Unified freshness management — single write path for dashboard data trust.

This module consolidates two previously-independent staleness representations
that tracked the same fact and drifted when callers forgot to update both:

  1. ``field_freshness: dict[str, str]``  — per-dashboard-field ``"fresh"``/``"stale"``
  2. ``critical_paths_stale: bool`` + ``critical_paths_stale_reason: str``
     — a boolean shadow of ``field_freshness["critical_path_cells"]`` plus a
     human-readable reason rendered in the dashboard.

The drift (e.g. ``critical_paths_stale=False`` while
``field_freshness["critical_path_cells"]="stale"`` → dashboard shows the
contradictory ``stale=false [stale]``) caused multiple P1 bugs (F4/F5 and
related).  Rather than replacing ``dict[str, str]`` with a heavier per-field
``DataCurrency`` dataclass (which would touch ~30 read/write sites, the
serializer, and tests for 8/9 fields that carry no reason), this module
centralizes the *write path* into three helpers.  Every site that previously
wrote the two representations independently now calls one helper, so they
can never disagree.

R2 (data-driven phase-entry auto-refresh) lives here too: a declarative
``RefreshSpec`` table + ``run_phase_entry_refresh`` replace the hardcoded
per-field refresh blocks that were copy-pasted across phase entry points.
Adding a new auto-refreshed field is now one table entry instead of a new
~25-line block.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from optimizer.pure.tool_runtime_policy import DASHBOARD_REFRESH_MAP

if TYPE_CHECKING:
    from optimizer.deps import NodeDeps
    from optimizer.pure.tool_filter import LoopPhase
    from optimizer.state import OptimizerState, TimingState

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# R1: Unified freshness write helpers
# ═══════════════════════════════════════════════════════════════════════

def mark_all_fields_stale(timing: "TimingState", *, reason: str) -> None:
    """Mark every dashboard field stale + critical_paths_stale=True.

    Centralizes the dual-write that previously appeared at 5 sites
    (strategy switch, design-modification tool, rollback, EVALUATE guard,
    SELECT guard).  All ``field_freshness`` entries → ``"stale"`` and the
    ``critical_paths_stale`` bool + reason are set in one call so the two
    representations never drift.

    Does NOT touch ``entity_registry`` — registry staleness is a separate
    SSOT (call ``registry.mark_stale()`` or ``registry.clear()`` at the
    call site as appropriate).
    """
    for _f in timing.field_freshness:
        timing.field_freshness[_f] = "stale"
    # Ensure the critical_path_cells key exists even before init_analysis
    # populates the full dict (matches the explicit set at the original sites).
    timing.field_freshness["critical_path_cells"] = "stale"
    timing.critical_paths_stale = True
    timing.critical_paths_stale_reason = reason


def mark_critical_paths_stale(timing: "TimingState", *, reason: str) -> None:
    """Mark only critical_paths stale (bool + field_freshness + reason).

    Fixes a latent drift at chain-step sites (phase_execute place_design /
    create_pblock) that set ``critical_paths_stale=True`` + reason but did
    NOT sync ``field_freshness["critical_path_cells"]``, leaving the
    dashboard with ``stale=true [fresh]`` for the critical-path field.
    """
    timing.critical_paths_stale = True
    timing.critical_paths_stale_reason = reason
    timing.field_freshness["critical_path_cells"] = "stale"


def mark_critical_paths_fresh(timing: "TimingState") -> None:
    """Mark critical_paths fresh (bool + field_freshness + clear reason).

    Centralizes the dual-write at critical-path extraction sites
    (update_critical_paths, _auto_refresh_critical_paths).  The
    ``field_freshness`` key is only set when it already exists, matching
    the original guard at phase_execute.py:2551 (the key may be absent
    before init_analysis populates the full dict).
    """
    timing.critical_paths_stale = False
    timing.critical_paths_stale_reason = ""
    if "critical_path_cells" in timing.field_freshness:
        timing.field_freshness["critical_path_cells"] = "fresh"


# ═══════════════════════════════════════════════════════════════════════
# R2: Data-driven phase-entry auto-refresh
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class RefreshSpec:
    """Declarative spec for one stale-field auto-refresh at phase entry.

    Attributes:
        field_name: ``field_freshness`` key to check for staleness.
        tool: MCP tool to call when the field is stale.
        args: Tool arguments dict.
        post_process: Sync callback ``(state, result_str) -> bool`` that
            parses the tool result and mutates state.  Returns True on
            success (the runner then marks all ``DASHBOARD_REFRESH_MAP[tool]``
            fields fresh); False on parse failure (field stays stale,
            matching the original per-block success-gated behavior).
        condition: Optional gate ``(state) -> bool``; when it returns
            False the spec is skipped entirely (e.g. design_info only
            refreshes post-rollback).
    """
    field_name: str
    tool: str
    args: dict = field(default_factory=dict)
    post_process: Callable[["OptimizerState", str], bool] | None = None
    condition: Callable[["OptimizerState"], bool] | None = None


# ── Post-process callbacks (extracted from the former inline blocks) ──

def _timing_summary_post(state: "OptimizerState", result: str, *,
                         adopt_wns: bool = True) -> bool:
    """Parse a timing-summary report and update latest WNS/TNS/FE.

    When ``adopt_wns=False`` (SELECT_STRATEGY entry), a non-routed design's
    WNS is NOT adopted — it is an optimistic wireload estimate that would
    pollute ``latest_wns`` and mislead strategy selection
    (run-20260710_002051).  The last known WNS is preserved instead.
    """
    from optimizer.pure.timing import parse_timing_summary
    from optimizer.state import parse_design_state, DesignState

    parsed = parse_timing_summary(result)
    if not parsed or "wns" not in parsed:
        return False
    if not adopt_wns:
        ds = parse_design_state(result)
        if ds is not None and ds != DesignState.ROUTED:
            logger.warning(
                f"[phase-entry] Skipping stale WNS refresh: design state "
                f"is {ds} (not routed); preserving last known WNS."
            )
            # Do NOT mark fresh — matches original SELECT block behavior: the
            # WNS was not adopted, so the field stays stale. SELECT has no
            # vivado_report_timing_summary tool to manually refresh.
            return False
        if ds is not None:
            state.timing.design_state = ds
    state.timing.latest_wns = parsed["wns"]
    state.timing.latest_tns = parsed.get("tns")
    state.timing.latest_failing_endpoints = parsed.get("failing_endpoints")
    return True


def _design_info_post(state: "OptimizerState", result: str) -> bool:
    """design_info has no state mutation beyond freshness (stored by tool)."""
    if not result or "error" in result.lower():
        return False
    return True


def _resource_utilization_post(state: "OptimizerState", result: str) -> bool:
    from optimizer.pure.timing import parse_resource_utilization
    util = parse_resource_utilization(result)
    if util is None:
        return False
    state.timing.resource_utilization = util
    return True


def _high_fanout_nets_post(state: "OptimizerState", result: str) -> bool:
    from optimizer.pure.timing import parse_high_fanout_nets
    parsed = parse_high_fanout_nets(result)
    if not parsed:
        return False
    state.timing.high_fanout_nets = parsed
    return True


def _route_status_post(state: "OptimizerState", result: str) -> bool:
    from optimizer.pure.timing import parse_route_status
    rs = parse_route_status(result)
    if rs is None:
        return False
    state.timing.route_status = rs
    return True


def _congestion_post(state: "OptimizerState", result: str) -> bool:
    """Parse congestion JSON and store the global score.

    Mirrors init_analysis.py:384-391 — the tool returns a JSON string (or
    dict) with ``congested_ratio`` and ``severity``; we store a compact
    ``{"global_score": ...}`` dict.
    """
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, dict) or "error" in data:
        return False
    state.timing.congestion_data = {
        "global_score": data.get("congested_ratio", 0.0),
    }
    return True


# ── Spec tables ────────────────────────────────────────────────────────

def _is_post_rollback(state: "OptimizerState") -> bool:
    """design_info refresh only after rollback (critical_paths cleared)."""
    return bool(
        state.timing.critical_paths_stale and not state.timing.critical_paths
    )


# ANALYZE entry: refresh stale high-value fields so the LLM starts with
# current data instead of re-discovering it via tool calls.
#   - timing_summary: WNS/TNS/FE (always refresh when stale)
#   - design_info: structural data (post-rollback only — fast ~30s, gives
#     the LLM something to work with before it re-extracts critical paths)
#   - resource_utilization: LUT/FF/DSP/BRAM (strategy-selection input)
#   - high_fanout_nets: verified net list for Fanout strategy
#   - route_status: routed/unrouted + congestion level (NEW — was a gap,
#     LLM had to notice [stale] and manually refresh)
#   - congestion_data: RapidWright congestion score (NEW — was a gap)
# critical_path_cells is deliberately NOT here: re-extraction is expensive
# and targeting-dependent; the LLM should decide when to call
# vivado_extract_critical_path_cells (matches the original design intent).
ANALYZE_REFRESH_SPECS: list[RefreshSpec] = [
    RefreshSpec(
        field_name="timing_summary",
        tool="vivado_report_timing_summary",
        post_process=lambda s, r: _timing_summary_post(s, r, adopt_wns=True),
    ),
    RefreshSpec(
        field_name="design_info",
        tool="rapidwright_get_design_info",
        post_process=_design_info_post,
        condition=_is_post_rollback,
    ),
    RefreshSpec(
        field_name="resource_utilization",
        tool="vivado_report_utilization_for_pblock",
        post_process=_resource_utilization_post,
    ),
    RefreshSpec(
        field_name="high_fanout_nets",
        tool="vivado_get_critical_high_fanout_nets",
        args={"num_paths": 50, "min_fanout": 50},
        post_process=_high_fanout_nets_post,
    ),
    RefreshSpec(
        field_name="route_status",
        tool="vivado_report_route_status",
        post_process=_route_status_post,
    ),
    RefreshSpec(
        field_name="congestion_data",
        tool="rapidwright_analyze_congestion",
        post_process=_congestion_post,
    ),
]

# SELECT_STRATEGY entry: only WNS needs auto-refresh (strategy selection
# depends on current WNS).  adopt_wns=False applies the routed-design guard
# — within an iteration the design may be mid-modification, so an unrouted
# report's wireload estimate must not pollute latest_wns.
SELECT_REFRESH_SPECS: list[RefreshSpec] = [
    RefreshSpec(
        field_name="timing_summary",
        tool="vivado_report_timing_summary",
        post_process=lambda s, r: _timing_summary_post(s, r, adopt_wns=False),
    ),
]


_PHASE_SPECS: dict[str, list[RefreshSpec]] = {
    "ANALYZE": ANALYZE_REFRESH_SPECS,
    "SELECT_STRATEGY": SELECT_REFRESH_SPECS,
}


async def run_phase_entry_refresh(
    state: "OptimizerState",
    deps: "NodeDeps",
    phase: "LoopPhase",
) -> None:
    """Auto-refresh stale dashboard fields at phase entry (data-driven).

    Iterates the ``RefreshSpec`` table for ``phase``.  For each spec whose
    ``field`` is ``"stale"`` (and whose optional ``condition`` passes),
    calls the refresh tool, runs ``post_process``, and — on success — marks
    all ``DASHBOARD_REFRESH_MAP[tool]`` fields fresh.  Per-spec try/except
    isolates failures: one field's refresh error does not block the others.

    Replaces the 4 hardcoded blocks in phase_analyze.py and the 1 block in
    phase_select_strategy.py.  Adding a new auto-refreshed field is now one
    ``RefreshSpec`` entry instead of a new ~25-line copy-pasted block.
    """
    from optimizer.pure.tool_router import call_tool as call_tool_fn

    phase_name = phase.value if hasattr(phase, "value") else str(phase)
    specs = _PHASE_SPECS.get(phase_name)
    if not specs:
        return

    ff = state.timing.field_freshness
    for spec in specs:
        if ff.get(spec.field_name) != "stale":
            continue
        if spec.condition is not None and not spec.condition(state):
            continue
        try:
            result = await call_tool_fn(
                spec.tool,
                spec.args,
                rapidwright_session=deps.rapidwright_session,
                vivado_session=deps.vivado_session,
                raw_tool_outputs=state.context.raw_tool_outputs,
                iteration=state.iteration.current,
                tool_round=0,
                tool_cache=state.context.tool_cache,
                design_size_factor=state.timing.design_size_factor,
                entity_registry=state.entity_registry,
            )
            ok = True
            if spec.post_process is not None:
                ok = spec.post_process(state, result)
            if ok:
                for _f in DASHBOARD_REFRESH_MAP.get(spec.tool, frozenset()):
                    ff[_f] = "fresh"
                logger.info(
                    f"[{phase_name}] Auto-refreshed stale {spec.field_name} "
                    f"via {spec.tool}"
                )
        except Exception as e:
            logger.warning(
                f"[{phase_name}] Auto-refresh {spec.field_name} via {spec.tool} "
                f"failed: {e}"
            )
