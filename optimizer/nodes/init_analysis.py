"""Init analysis node: run initial FPGA design analysis.

Reads the input DCP, extracts WNS/TNS/resource utilization,
and populates state.timing with initial values.

Reference: dcp_optimizer.py perform_initial_analysis() (L4068-4259).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ..state import OptimizerState, DesignState, parse_design_state
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.critical_path import parse_critical_path_cells, update_critical_paths
from ..pure.timing import (
    parse_timing_summary, parse_high_fanout_nets, parse_resource_utilization,
    parse_hold_timing, parse_route_status, parse_control_sets,
    parse_cdc_paths, parse_design_info, parse_pvt_corner,
)
from ..pure.tool_router import call_tool as call_tool_fn
from config_loader import get_worker_model_config

logger = logging.getLogger(__name__)


def _checkpoint_done(state: OptimizerState, step: str) -> bool:
    """Check if an init_analysis step is already completed (for skip-on-restart)."""
    return state.timing.analysis_checkpoints.get(step, False)


def _mark_checkpoint(state: OptimizerState, step: str) -> None:
    """Atomically mark a step as completed after validate-then-commit."""
    state.timing.analysis_checkpoints[step] = True
    logger.info(f"[init_analysis] Checkpoint committed: {step}")


# Design size bins for adaptive timeout scaling.
# Base: <50K cells (small designs like logicnets_jscl at 37K).
# 1.5x: 50K-150K (medium designs like corundum at 197K — actually 197K > 150K).
# 3.0x: >150K (large designs like boom_soc at ~1M).
DESIGN_SIZE_BINS: list[tuple[int, int, float]] = [
    (0, 50000, 1.0),
    (50000, 150000, 1.5),
    (150000, 2**31, 3.0),
]
MAX_TIMEOUT: float = 900.0  # Hard cap: never wait more than 15 min per call


def _compute_size_factor(cell_count: int) -> float:
    """Map cell count to timeout multiplier using DESIGN_SIZE_BINS."""
    for lo, hi, factor in DESIGN_SIZE_BINS:
        if lo <= cell_count < hi:
            return factor
    return 3.0


def scaled_timeout(base: float, state: OptimizerState) -> float:
    """Apply design size factor to a base timeout, capped at MAX_TIMEOUT."""
    return min(base * state.timing.design_size_factor, MAX_TIMEOUT)


    # OPTIMIZATION NOTE: Several init analysis steps can be deferred
    # or skipped if the design was recently analyzed. The checkpoint
    # system handles most of this automatically, but explicit skip
    # checks can further reduce startup time for re-opened DCPs.

async def _probe_design_size(state: OptimizerState, deps: NodeDeps) -> int:
    """Quickly probe design cell count via lightweight Tcl.

    Runs `llength [get_cells -hier]` which completes in ~1-2s even
    for 200K-cell designs. Sets state.timing.design_size_factor for
    adaptive timeout scaling.

    Returns:
        Cell count (int), or 0 if probe fails.
    """
    try:
        result = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "llength [get_cells -hier -quiet]", "timeout": 30},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        count_str = result.strip().split()[0] if result.strip() else "0"
        cell_count = int(count_str) if count_str.lstrip("-").isdigit() else 0
        factor = _compute_size_factor(cell_count)
        state.timing.design_size_factor = factor
        logger.info(
            f"[init_analysis] Design size probe: {cell_count} cells, "
            f"timeout factor={factor}"
        )
        return cell_count
    except Exception as e:
        logger.warning(f"[init_analysis] Size probe failed, using default factor: {e}")
        state.timing.design_size_factor = 1.0
        return 0


async def init_analysis_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Run initial analysis on the input DCP design.

    Actions:
        1. Record start_time
        2. Initialize RapidWright + open Vivado checkpoint (parallel)
        3. Run Vivado analysis pipeline (sequential) || RapidWright pipeline (sequential)
        4. Run cross-server analysis (critical path spread + congestion)
        5. Populate state.timing with all extracted data

    Returns:
        Next node name (edge after_init decides based on WNS).
    """
    state.control.start_time = time.time()

    input_dcp = state.control.input_dcp
    if input_dcp is None:
        logger.error("[init_analysis] No input_dcp path set")
        state.control.is_done = True
        state.control.done_reason = "no_input_dcp"
        return NodeName.SAVE_OUTPUT

    logger.info(f"[init_analysis] Analyzing {input_dcp}")

    try:
        # ════════════════════════════════════════════════════════════
        # Phase A: Initialize both MCP servers in parallel
        # ════════════════════════════════════════════════════════════

        async def _init_vivado():
            result = await call_tool_fn(
                "vivado_open_checkpoint",
                {"dcp_path": str(input_dcp.resolve()), "timeout": 600},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            if "error" in result.lower() and "opened successfully" not in result.lower():
                raise RuntimeError(f"Failed to open checkpoint: {result}")
            state.control.current_dcp_path = input_dcp.resolve()
            # Freshly loaded input DCP - memory matches the file (clean).
            state.control.live_design_dirty = False
            logger.info("[init_analysis] Vivado checkpoint opened")

        async def _init_rapidwright():
            result = await call_tool_fn(
                "rapidwright_initialize_rapidwright", {},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            if "error" in result.lower() and "success" not in result.lower():
                raise RuntimeError(f"Failed to initialize RapidWright: {result}")
            logger.info("[init_analysis] RapidWright initialized")

        await asyncio.gather(_init_vivado(), _init_rapidwright())

        # Probe design size for adaptive timeout scaling
        await _probe_design_size(state, deps)

        # ════════════════════════════════════════════════════════════
        # Phase B: Parallel pipelines — Vivado (sequential) || RapidWright (sequential)
        # ════════════════════════════════════════════════════════════
        # Each pipeline returns a dict of cross-pipeline data instead of
        # using nonlocal variables, eliminating implicit data flow.

        async def _vivado_pipeline() -> dict:
            """Run Vivado analysis pipeline. Returns extracted data for Phase C."""
            result: dict = {
                "timing_report": None,
                "cell_names_for_spread": None,
            }

            # Step B1: Report timing summary
            if _checkpoint_done(state, "timing_done"):
                logger.info("[init_analysis] Skipping timing step (checkpoint done)")
            else:
                timing_report = await call_tool_fn(
                    "vivado_report_timing_summary",
                    {"timeout": scaled_timeout(300, state)},
                    deps.rapidwright_session, deps.vivado_session,
                    design_size_factor=state.timing.design_size_factor,
                )
                result["timing_report"] = timing_report
                timing_info = parse_timing_summary(timing_report)

                # Parse design physical implementation state from Design State field.
                # At init, a None (unparseable) genuinely means unknown → UNPLACED.
                parsed_ds = parse_design_state(timing_report)
                state.timing.design_state = parsed_ds if parsed_ds is not None else DesignState.UNPLACED
                logger.info(
                    f"[init_analysis] Design state: {state.timing.design_state}"
                )

                state.timing.initial_wns = timing_info["wns"]
                state.timing.initial_tns = timing_info["tns"]
                state.timing.initial_failing_endpoints = timing_info["failing_endpoints"]
                state.timing.latest_tns = timing_info["tns"]
                state.timing.latest_failing_endpoints = timing_info["failing_endpoints"]
                state.timing.best_wns = timing_info["wns"] if timing_info["wns"] is not None else float('-inf')
                state.timing.latest_wns = timing_info["wns"]
                state.timing.best_wns_iteration = 0
                state.timing.best_wns_tns = timing_info["tns"]
                state.timing.best_wns_failing_endpoints = timing_info["failing_endpoints"]
                logger.info(
                    f"[init_analysis] Timing: WNS={state.timing.initial_wns}, "
                    f"TNS={state.timing.initial_tns}, FE={state.timing.initial_failing_endpoints}"
                )

                # PVT corner from timing report header
                state.timing.pvt_corner = parse_pvt_corner(timing_report)
                _mark_checkpoint(state, "timing_done")

            # Step B2: Get clock period
            if _checkpoint_done(state, "clocks_done"):
                logger.info("[init_analysis] Skipping clock period step (checkpoint done)")
            else:
                await _extract_clock_period(state, deps)
                _mark_checkpoint(state, "clocks_done")

            # Step B3: Hold timing check
            if _checkpoint_done(state, "hold_done"):
                logger.info("[init_analysis] Skipping hold timing step (checkpoint done)")
            else:
                await _extract_hold_timing(state, deps)
                _mark_checkpoint(state, "hold_done")

            # Step B4: High fanout nets
            nets_report = await call_tool_fn(
                "vivado_get_critical_high_fanout_nets",
                {"num_paths": 50, "min_fanout": 50},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            state.timing.high_fanout_nets = parse_high_fanout_nets(nets_report)
            logger.info(f"[init_analysis] Found {len(state.timing.high_fanout_nets)} high fanout nets")

            # Step B5: Resource utilization
            if _checkpoint_done(state, "util_done"):
                logger.info("[init_analysis] Skipping utilization step (checkpoint done)")
            else:
                util_report = await call_tool_fn(
                    "vivado_report_utilization_for_pblock", {},
                    deps.rapidwright_session, deps.vivado_session,
                    design_size_factor=state.timing.design_size_factor,
                )
                state.timing.resource_utilization = parse_resource_utilization(util_report)
                logger.info(f"[init_analysis] Resource utilization: {state.timing.resource_utilization}")
                state.timing.baseline_resource_utilization = state.timing.resource_utilization.copy() if state.timing.resource_utilization else None
                if state.timing.baseline_resource_utilization:
                    logger.info(f"i[init_analysis] Baseline resources saved: {state.timing.baseline_resource_utilization}")
                _mark_checkpoint(state, "util_done")

            # Step B6: Critical path cells (for spread analysis later)
            cells_json = await call_tool_fn(
                "vivado_extract_critical_path_cells",
                {"num_paths": 50},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            cell_paths = parse_critical_path_cells(cells_json)
            if cell_paths:
                update_critical_paths(state, cell_paths, iteration=0)
            result["cell_names_for_spread"] = [p["cells"] for p in cell_paths] if cell_paths else None

            # Step B7: Route status (M3: avg_wirelength, long_route_nets)
            if _checkpoint_done(state, "route_done"):
                logger.info("[init_analysis] Skipping route status step (checkpoint done)")
            else:
                try:
                    route_report = await call_tool_fn(
                        "vivado_report_route_status",
                        {"timeout": scaled_timeout(300, state)},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                    state.timing.route_status = parse_route_status(route_report)
                    logger.info(
                        f"[init_analysis] Route status: "
                        f"nets={state.timing.route_status.get('total_nets')}, "
                        f"long_routes={state.timing.route_status.get('long_route_nets_count')}"
                    )
                    _mark_checkpoint(state, "route_done")
                except Exception as e:
                    logger.warning(f"[init_analysis] Route status failed: {e}")

            # Step B8: Control sets (M4)
            # Step B9: Constraints — false paths, multicycle paths, IO delay (M5)
            if _checkpoint_done(state, "constraints_done"):
                logger.info("[init_analysis] Skipping constraints step (checkpoint done)")
            else:
                await _extract_control_sets(state, deps)
                await _extract_constraints(state, deps)
                _mark_checkpoint(state, "constraints_done")

            # Step B10: CDC analysis (M4: cross_domain_paths_count)
            if _checkpoint_done(state, "cdc_done"):
                logger.info("[init_analysis] Skipping CDC step (checkpoint done)")
            else:
                await _extract_cdc_paths(state, deps)
                _mark_checkpoint(state, "cdc_done")

            return result

        async def _rapidwright_pipeline() -> dict:
            """Run RapidWright analysis pipeline. Returns extracted data for Phase C."""
            # Step B-R1: Read checkpoint in RapidWright
            result = await call_tool_fn(
                "rapidwright_read_checkpoint",
                {"dcp_path": str(input_dcp.resolve())},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            if "error" in result.lower() and "success" not in result.lower():
                logger.warning(f"[init_analysis] Could not load design in RapidWright: {result}")

            # Step B-R2: Device topology → capacity
            await _extract_device_capacity(state, deps)

            # Step B-R3: Design info (M4: cell type statistics)
            await _extract_design_info(state, deps)

            return {}

        # Phase B: Parallel execution with explicit return values (no nonlocal)
        vivado_result, rw_result = await asyncio.gather(
            _vivado_pipeline(), _rapidwright_pipeline()
        )

        # ════════════════════════════════════════════════════════════
        # Phase C: Cross-server analysis (sequential on RW)
        # ════════════════════════════════════════════════════════════

        # Step C1: Critical path spread (uses explicit return value from Vivado pipeline)
        cell_names_for_spread = vivado_result.get("cell_names_for_spread")
        if cell_names_for_spread:
            try:
                spread_result = await call_tool_fn(
                    "rapidwright_analyze_critical_path_spread",
                    {"critical_paths_data": cell_names_for_spread},
                    deps.rapidwright_session, deps.vivado_session,
                    design_size_factor=state.timing.design_size_factor,
                )
                spread_data = json.loads(spread_result)
                state.timing.critical_path_spread = {
                    "max_distance": spread_data.get("max_distance_found", 0),
                    "avg_distance": spread_data.get("avg_max_distance", 0),
                    "paths_analyzed": spread_data.get("paths_analyzed", 0),
                }
                logger.info(
                    f"[init_analysis] Critical path spread: "
                    f"max={state.timing.critical_path_spread['max_distance']} tiles"
                )
            except Exception as e:
                logger.warning(f"[init_analysis] Could not analyze critical path spread: {e}")

        # Step C2: Routing congestion
        try:
            congestion_result = await call_tool_fn(
                "rapidwright_analyze_congestion", {},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            if isinstance(congestion_result, str):
                congestion_data = json.loads(congestion_result)
            else:
                congestion_data = congestion_result
            if "error" not in congestion_data:
                state.timing.congestion_data = {
                    "global_score": congestion_data.get("congested_ratio", 0.0),
                }
                logger.info(
                    f"[init_analysis] Congestion: ratio={congestion_data.get('congested_ratio', 0.0):.3f}, "
                    f"severity={congestion_data.get('severity', 'UNKNOWN')}"
                )
        except Exception as e:
            logger.warning(f"[init_analysis] Could not analyze congestion: {e}")

    except Exception as e:
        logger.error(f"[init_analysis] Analysis failed: {e}")
        state.control.is_done = True
        state.control.done_reason = f"init_analysis_failed: {e}"
        return NodeName.SAVE_OUTPUT

    # Load cost_hard_limit from config (shared planner+worker budget)
    state.cost.cost_hard_limit = get_worker_model_config().cost_hard_limit
    logger.info(f"[init_analysis] Cost hard limit: ${state.cost.cost_hard_limit:.2f}")

    # Save initial checkpoint so rollback always has a restore point.
    # Without this, best_checkpoint_path stays None until WNS improves,
    # and rollback_node logs "No checkpoint at None" and skips restore.
    if (state.control.run_dir is not None
            and deps.vivado_session is not None
            and state.timing.best_wns != float('-inf')):
        try:
            ckpt_path = state.control.run_dir / "best_checkpoint.dcp"
            await call_tool_fn(
                "vivado_write_checkpoint",
                {"dcp_path": str(ckpt_path.resolve()), "force": True},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            state.control.best_checkpoint_path = ckpt_path
            state.control.needs_save = False
            logger.info(
                f"[init_analysis] Saved initial checkpoint: "
                f"WNS={state.timing.best_wns:.3f}ns -> {ckpt_path}"
            )
        except Exception as e:
            logger.warning(f"[init_analysis] Failed to save initial checkpoint: {e}")

    # Initialize dashboard freshness: mark all fields as fresh since init_analysis
    # collected them. Keys match DASHBOARD_REFRESH_MAP values in constants.py.
    state.timing.field_freshness = {
        "resource_utilization": "fresh",
        "high_fanout_nets": "fresh",
        "critical_path_spread": "fresh",
        "route_status": "fresh",
        "timing_summary": "fresh",
        "cdc_paths": "fresh",
        "design_info": "fresh",
        "congestion_data": "fresh",
        "critical_path_cells": "fresh",
    }

    logger.info(
        f"[init_analysis] Complete: WNS={state.timing.initial_wns}, "
        f"best_wns={state.timing.best_wns:.3f}"
    )

    return NodeName.ITERATION_START


# ═══════════════════════════════════════════════════════════════════
# Extraction helpers
# ═══════════════════════════════════════════════════════════════════


async def _extract_clock_period(state: OptimizerState, deps: NodeDeps) -> None:
    """Extract clk_fpl26contest clock period from Vivado."""
    try:
        tcl_cmd = (
            "set clk [get_clocks -quiet clk_fpl26contest]; "
            "if {$clk ne {}} { "
            "  puts [get_property PERIOD $clk]; "
            "} else { "
            "  puts {NO_CONTEST_CLOCK}; "
            "}"
        )
        clock_result = await call_tool_fn(
            "vivado_run_tcl", {"command": tcl_cmd},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        period = None
        if clock_result and clock_result.strip():
            for token in clock_result.strip().split():
                if token.startswith("ERROR") or token.startswith("WARNING"):
                    continue
                try:
                    val = float(token)
                    if val > 0:
                        period = val
                        break
                except ValueError:
                    continue
        if period is None:
            fallback_cmd = (
                "set tp [get_timing_paths -max_paths 1 -setup]; "
                "if {$tp ne {}} { "
                "  set clk [get_property ENDPOINT_CLOCK $tp]; "
                "  if {$clk ne {}} { "
                "    puts [get_property PERIOD [get_clocks $clk]]; "
                "  } "
                "}"
            )
            fallback_result = await call_tool_fn(
                "vivado_run_tcl", {"command": fallback_cmd},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            if fallback_result and fallback_result.strip():
                for token in fallback_result.strip().split():
                    if token.startswith("ERROR") or token.startswith("WARNING"):
                        continue
                    try:
                        val = float(token)
                        if val > 0:
                            period = val
                            break
                    except ValueError:
                        continue
        if period is not None:
            state.timing.clock_period = period
            logger.info(f"[init_analysis] Clock period: {period:.3f} ns")
        else:
            logger.warning("[init_analysis] Could not determine clock period")
    except Exception as e:
        logger.warning(f"[init_analysis] Could not get clock period: {e}")


async def _extract_hold_timing(state: OptimizerState, deps: NodeDeps) -> None:
    """Check hold timing (competition requirement: hold WNS >= 0)."""
    try:
        hold_report = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "report_timing_summary -delay_type min -max_paths 100"},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        hold = parse_hold_timing(hold_report)
        if hold.get("hold_wns") is not None:
            state.timing.hold_wns = hold["hold_wns"]
            state.timing.hold_tns = hold.get("hold_tns")
            state.timing.hold_failing = hold.get("hold_failing")
            if hold["hold_wns"] < 0:
                logger.warning(
                    f"[init_analysis] HOLD VIOLATED: WNS={hold['hold_wns']:.3f}ns, "
                    f"failing={hold.get('hold_failing')}"
                )
            else:
                logger.info(f"[init_analysis] Hold timing MET: WNS={hold['hold_wns']:.3f}ns")
    except Exception as e:
        logger.warning(f"[init_analysis] Hold timing check failed: {e}")


async def _extract_device_capacity(state: OptimizerState, deps: NodeDeps) -> None:
    """Extract device topology and compute capacity."""
    try:
        topo_result = await call_tool_fn(
            "rapidwright_get_device_topology", {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        topo_data = json.loads(topo_result)
        if topo_data.get("status") == "success":
            logger.info(f"[init_analysis] Device: {topo_data.get('device')}")
            site_dist = topo_data.get("site_type_distribution", [])
            capacity: dict[str, int] = {"LUT": 0, "FF": 0, "DSP": 0, "BRAM": 0, "URAM": 0}
            for entry in site_dist:
                stype = entry.get("type", "")
                count = entry.get("count", 0)
                if "SLICE" in stype.upper():
                    capacity["LUT"] += count * 8
                    capacity["FF"] += count * 16
                elif "DSP" in stype.upper():
                    capacity["DSP"] += count
                elif stype.upper().startswith("RAMB36"):
                    capacity["BRAM"] += count
                elif stype.upper().startswith("RAMB18"):
                    capacity["BRAM"] += count // 2
                elif "URAM" in stype.upper():
                    capacity["URAM"] += count
            state.timing.device_capacity = capacity
            logger.info(f"[init_analysis] Device capacity: {capacity}")
    except Exception as e:
        logger.warning(f"[init_analysis] Could not get device topology: {e}")


async def _extract_design_info(state: OptimizerState, deps: NodeDeps) -> None:
    """Extract design statistics from RapidWright (cell types, net count)."""
    try:
        result = await call_tool_fn(
            "rapidwright_get_design_info", {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        info = parse_design_info(result)
        if info:
            state.timing.design_info = info
            top = info.get("top_cell_types", {})
            top_str = ", ".join(f"{k}:{v}" for k, v in sorted(top.items(), key=lambda x: -x[1])[:8])
            logger.info(
                f"[init_analysis] Design: {info.get('design_name')}, "
                f"cells={info.get('cell_count')}, nets={info.get('net_count')}, "
                f"top_types=[{top_str}]"
            )
    except Exception as e:
        logger.warning(f"[init_analysis] Could not get design info: {e}")


async def _extract_control_sets(state: OptimizerState, deps: NodeDeps) -> None:
    """Extract control set count via Vivado Tcl."""
    try:
        result = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "report_control_sets -return_string"},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        state.timing.control_sets = parse_control_sets(result)
        logger.info(f"[init_analysis] Control sets: {state.timing.control_sets}")
    except Exception as e:
        logger.warning(f"[init_analysis] Control sets extraction failed: {e}")


async def _extract_constraints(state: OptimizerState, deps: NodeDeps) -> None:
    """Extract timing constraints: false paths, multicycle paths, IO delay coverage."""
    constraints: dict = {"false_paths_count": 0, "multicycle_paths_count": 0, "io_delay_defined_pct": None}

    # False paths
    try:
        fp_result = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "llength [get_false_path -quiet]"},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        if fp_result and fp_result.strip():
            val = fp_result.strip().split()[0]
            try:
                constraints["false_paths_count"] = int(val)
            except ValueError:
                pass
    except Exception as e:
        logger.warning(f"[init_analysis] False paths query failed: {e}")

    # Multicycle paths
    try:
        mp_result = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "llength [get_multicycle_path -quiet]"},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        if mp_result and mp_result.strip():
            val = mp_result.strip().split()[0]
            try:
                constraints["multicycle_paths_count"] = int(val)
            except ValueError:
                pass
    except Exception as e:
        logger.warning(f"[init_analysis] Multicycle paths query failed: {e}")

    # IO delay coverage
    try:
        io_result = await call_tool_fn(
            "vivado_run_tcl",
            {"command": (
                "set ports [get_ports -quiet]; "
                "if {$ports ne {}} { "
                "  set with_delay 0; "
                "  set total 0; "
                "  foreach p $ports { "
                "    incr total; "
                "    if {[get_property -quiet HAS_INPUT_DELAY $p] || "
                "        [get_property -quiet HAS_OUTPUT_DELAY $p]} { "
                "      incr with_delay; "
                "    } "
                "  }; "
                "  puts \"$with_delay $total\"; "
                "} else { "
                "  puts \"0 0\"; "
                "}"
            )},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        if io_result and io_result.strip():
            parts = io_result.strip().split()
            if len(parts) >= 2:
                try:
                    with_delay = int(parts[0])
                    total_ports = int(parts[1])
                    constraints["total_io_ports"] = total_ports
                    if total_ports > 0:
                        constraints["io_delay_defined_pct"] = round(with_delay / total_ports, 4)
                except ValueError:
                    pass
    except Exception as e:
        logger.warning(f"[init_analysis] IO delay analysis failed: {e}")

    state.timing.constraints_info = constraints
    logger.info(
        f"[init_analysis] Constraints: false_paths={constraints['false_paths_count']}, "
        f"multicycle={constraints['multicycle_paths_count']}, "
        f"io_delay_pct={constraints['io_delay_defined_pct']}"
    )


async def _extract_cdc_paths(state: OptimizerState, deps: NodeDeps) -> None:
    """Count cross-clock-domain timing paths.

    For single-clock designs CDC=0 is correct (no cross-clock paths possible).
    For multi-clock designs, runs report_clock_interaction and counts
    lines containing 'Unconstrained' as a best-effort unsafe CDC count.
    """
    try:
        # Step 1: count clocks in the design
        clock_result = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "llength [get_clocks]"},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        clock_count = 0
        if clock_result and clock_result.strip():
            try:
                clock_count = int(clock_result.strip().split()[0])
            except ValueError:
                pass

        if clock_count <= 1:
            # Single-clock (or no clock) design — no cross-clock paths possible
            state.timing.cross_domain_paths_count = 0
            logger.info(f"[init_analysis] CDC paths: 0 ({clock_count} clock(s) in design)")
            return

        # Step 2: multi-clock design — use report_clock_interaction
        cdc_report = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "report_clock_interaction -return_string"},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        # Simple count: lines containing "Unconstrained" indicate unsafe CDC paths
        count = 0
        if cdc_report:
            for line in cdc_report.split('\n'):
                if 'Unconstrained' in line:
                    count += 1
        state.timing.cross_domain_paths_count = count
        logger.info(f"[init_analysis] CDC paths: {count} ({clock_count} clocks)")
    except Exception as e:
        logger.warning(f"[init_analysis] CDC analysis failed: {e}")

# Init analysis: minimum required fields before proceeding
REQUIRED_INIT_FIELDS = ["wns", "tns", "design_state", "route_status"]

def estimate_design_complexity(state) -> str:
    """Classify design complexity: simple, moderate, complex."""
    cells = getattr(state.timing, "total_cells", 0)
    if cells < 50000: return "simple"
    if cells < 200000: return "moderate"
    return "complex"

def should_run_full_init(dcp_path: str) -> bool:
    """Decide if full init analysis is needed (vs using cached data)."""
    import os
    if not os.path.exists(dcp_path): return False
    return os.path.getsize(dcp_path) > 1000  # Basic sanity check

def _quick_design_assessment(timing_report: str) -> dict:
    """Rapid design assessment from timing report."""
    import re
    wns_m = re.search(r"WNS\s*:?\s*(-?\d+\.?\d*)", timing_report)
    state_m = re.search(r"Design State\s*[:|]\s*(\w[^\n\r]*)", timing_report)
    return {"wns": float(wns_m.group(1)) if wns_m else None, "state": state_m.group(1).strip() if state_m else "unknown"}
