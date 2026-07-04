"""Rollback node: restore design to the best-known checkpoint.

Triggered when EVALUATE detects WNS regression or LLM requests ROLLBACK.
Opens the saved best DCP, verifies WNS, restores timing state, then
routes to ITERATION_START.

Rollback also performs a comprehensive state reset: clears tool caches,
marks all dashboard fields stale, clears in-memory analysis data from
the failed iteration, and resets design-data snapshot state so subsequent
phases work from a clean slate.
"""

from __future__ import annotations

import logging

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.tool_router import call_tool as call_tool_fn
from ..color import green, yellow
from .subgraphs.phase_handoff import reset_design_fingerprint

logger = logging.getLogger(__name__)


async def rollback_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Restore design from best-known checkpoint.

    Actions:
        1. Open best_checkpoint.dcp via MCP
        2. Verify WNS via vivado_get_wns
        3. Restore latest_tns/latest_failing_endpoints from best values
        4. Route to ITERATION_START

    Returns:
        NodeName.ITERATION_START (always).
    """
    ckpt = state.control.best_checkpoint_path
    if ckpt is None or not ckpt.exists():
        logger.error(f"[ROLLBACK] No checkpoint at {ckpt}, routing directly to iteration")
        return NodeName.ITERATION_START

    try:
        logger.warning(yellow(f"[ROLLBACK] Opening checkpoint: {ckpt}"))
        result = await call_tool_fn(
            "vivado_open_checkpoint",
            {"dcp_path": str(ckpt.resolve())},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        if "error" in result.lower():
            logger.error(f"[ROLLBACK] Failed to open checkpoint: {result[:200]}")
            return NodeName.ITERATION_START
        state.control.current_dcp_path = ckpt.resolve()

        # Verify WNS after restore
        wns_result = await call_tool_fn(
            "vivado_get_wns", {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        if wns_result and wns_result.strip() not in ("", "(no output)", "PARSE_ERROR"):
            try:
                verified_wns = float(wns_result.strip())
                state.timing.latest_wns = verified_wns
                logger.info(f"[ROLLBACK] Verified WNS: {verified_wns:.3f}ns")
            except ValueError:
                logger.warning(f"[ROLLBACK] WNS parse failed, using cached best")
                state.timing.latest_wns = state.timing.best_wns
        else:
            state.timing.latest_wns = state.timing.best_wns

        # Restore TNS/failing endpoints from cached best values
        if state.timing.best_wns_tns is not None:
            state.timing.latest_tns = state.timing.best_wns_tns
        if state.timing.best_wns_failing_endpoints is not None:
            state.timing.latest_failing_endpoints = state.timing.best_wns_failing_endpoints

        logger.info(green(
            f"[ROLLBACK] State restored: WNS={state.timing.latest_wns:.3f}ns, "
            f"TNS={state.timing.latest_tns}, endpoints={state.timing.latest_failing_endpoints}"
        ))

        # Inject ROLLBACK NOTIFICATION into LLM context so the next iteration
        # is aware the design was physically restored to its best checkpoint.
        if deps.compat is not None:
            best_iter = state.timing.best_wns_iteration or "?"
            best_wns_str = f"{state.timing.best_wns:.3f}" if state.timing.best_wns != float('-inf') else "N/A"
            tns_str = f"{state.timing.latest_tns:.3f}" if state.timing.latest_tns is not None else "N/A"
            fe_str = str(state.timing.latest_failing_endpoints) if state.timing.latest_failing_endpoints is not None else "N/A"
            notification = (
                f"[SYSTEM — Rollback Occurred]\n"
                f"The design was restored from best_checkpoint.dcp.\n"
                f"  → Best WNS: {best_wns_str}ns (from iteration {best_iter})\n"
                f"  → TNS: {tns_str}\n"
                f"  → Failing endpoints: {fe_str}\n"
                f"The previous iteration's degraded state was discarded. "
                f"Continuing analysis from the best-known design state."
            )
            deps.compat.add_message("user", notification)
            logger.info("[ROLLBACK] Rollback notification injected into LLM context")

        # Clear entity registry after rollback: checkpoint may have different
        # cell topology (merged/split/renamed), so stale names must be re-fetched.
        state.entity_registry.clear()
        logger.info("[ROLLBACK] Entity registry cleared — cells must be re-fetched")

        # ── Comprehensive state reset: prevent stale data from the failed
        #    iteration from leaking into the next ANALYZE/SELECT_STRATEGY ──

        # 1. Clear tool cache — cached results from the failed iteration
        #    would return wrong data for the restored checkpoint.
        state.context.tool_cache.clear()
        logger.info("[ROLLBACK] Tool cache cleared")

        # 2. Mark all dashboard fields stale — the design was physically
        #    restored to a different checkpoint, so every field must be
        #    re-acquired. This triggers auto-refresh in ANALYZE/SELECT_STRATEGY
        #    entry for timing_summary (other fields are marked stale and
        #    re-fetched on LLM demand).
        for field in state.timing.field_freshness:
            state.timing.field_freshness[field] = "stale"
        state.timing.critical_paths_stale = True
        logger.info("[ROLLBACK] All field_freshness marked stale")

        # 3. Clear in-memory analysis data from the failed iteration.
        #    WNS/TNS/failing_endpoints were already re-verified above —
        #    everything else comes from the old (wrong) design state.
        state.timing.critical_paths = []
        state.timing.congestion_data = None
        state.timing.high_fanout_nets = None
        state.timing.route_status = None
        state.timing.design_info = None
        state.timing.critical_path_spread = None
        state.timing.resource_utilization = None
        state.timing.cdc_paths = None
        state.timing.qor_suggestions = None
        state.timing.failing_endpoint_names = []
        logger.info("[ROLLBACK] Cleared in-memory analysis data (critical_paths, congestion, etc.)")

        # 4. Reset design-data snapshot state — allow new snapshot at same
        #    iteration number since the design has been physically changed.
        state.context.design_data.last_snapshot_iteration = -1
        state.context.design_data.design_data_path = None
        state.context.design_data.last_snapshot_fingerprint = ""
        logger.info("[ROLLBACK] DesignData snapshot state reset (fingerprint cleared)")

        # 5. Clear raw tool outputs buffer — the failed iteration's raw
        #    outputs refer to a different design state and should not be
        #    queryable by the LLM after rollback.
        state.context.raw_tool_outputs.clear()
        logger.info("[ROLLBACK] Raw tool outputs buffer cleared")

        # 6. Reset phase-handoff design fingerprint — ensures the next
        #    phase transition clears tool cache (since the design checkpoint
        #    changed, cached results from the previous state are invalid).
        reset_design_fingerprint()
        logger.info("[ROLLBACK] Phase-handoff design fingerprint reset")

    except Exception as e:
        logger.error(f"[ROLLBACK] Exception during rollback: {e}")

    return NodeName.ITERATION_START
