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

from ..state import OptimizerState
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
                {"dcp_path": str(input_dcp.resolve())},
                deps.rapidwright_session, deps.vivado_session,
            )
            if "error" in result.lower() and "opened successfully" not in result.lower():
                raise RuntimeError(f"Failed to open checkpoint: {result}")
            state.control.current_dcp_path = input_dcp.resolve()
            logger.info("[init_analysis] Vivado checkpoint opened")

        async def _init_rapidwright():
            result = await call_tool_fn(
                "rapidwright_initialize_rapidwright", {},
                deps.rapidwright_session, deps.vivado_session,
            )
            if "error" in result.lower() and "success" not in result.lower():
                raise RuntimeError(f"Failed to initialize RapidWright: {result}")
            logger.info("[init_analysis] RapidWright initialized")

        await asyncio.gather(_init_vivado(), _init_rapidwright())

        # ════════════════════════════════════════════════════════════
        # Phase B: Parallel pipelines — Vivado (sequential) || RapidWright (sequential)
        # ════════════════════════════════════════════════════════════

        # Shared containers for cross-pipeline data
        timing_report = None
        cell_names_for_spread = None

        async def _vivado_pipeline():
            nonlocal timing_report, cell_names_for_spread

            # Step B1: Report timing summary
            timing_report = await call_tool_fn(
                "vivado_report_timing_summary", {},
                deps.rapidwright_session, deps.vivado_session,
            )
            timing_info = parse_timing_summary(timing_report)

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

            # Step B2: Get clock period
            await _extract_clock_period(state, deps)

            # Step B3: Hold timing check
            await _extract_hold_timing(state, deps)

            # Step B4: High fanout nets
            nets_report = await call_tool_fn(
                "vivado_get_critical_high_fanout_nets",
                {"num_paths": 50, "min_fanout": 100},
                deps.rapidwright_session, deps.vivado_session,
            )
            state.timing.high_fanout_nets = parse_high_fanout_nets(nets_report)
            logger.info(f"[init_analysis] Found {len(state.timing.high_fanout_nets)} high fanout nets")

            # Step B5: Resource utilization
            util_report = await call_tool_fn(
                "vivado_report_utilization_for_pblock", {},
                deps.rapidwright_session, deps.vivado_session,
            )
            state.timing.resource_utilization = parse_resource_utilization(util_report)
            logger.info(f"[init_analysis] Resource utilization: {state.timing.resource_utilization}")

            # Step B6: Critical path cells (for spread analysis later)
            cells_json = await call_tool_fn(
                "vivado_extract_critical_path_cells",
                {"num_paths": 50},
                deps.rapidwright_session, deps.vivado_session,
            )
            cell_paths = parse_critical_path_cells(cells_json)
            if cell_paths:
                update_critical_paths(state, cell_paths, iteration=0)
            cell_names_for_spread = [p["cells"] for p in cell_paths]

            # Step B7: Route status (M3: avg_wirelength, long_route_nets)
            try:
                route_report = await call_tool_fn(
                    "vivado_report_route_status", {},
                    deps.rapidwright_session, deps.vivado_session,
                )
                state.timing.route_status = parse_route_status(route_report)
                logger.info(
                    f"[init_analysis] Route status: "
                    f"nets={state.timing.route_status.get('total_nets')}, "
                    f"long_routes={state.timing.route_status.get('long_route_nets_count')}"
                )
            except Exception as e:
                logger.warning(f"[init_analysis] Route status failed: {e}")

            # Step B8: Control sets (M4)
            await _extract_control_sets(state, deps)

            # Step B9: Constraints — false paths, multicycle paths, IO delay (M5)
            await _extract_constraints(state, deps)

            # Step B10: CDC analysis (M4: cross_domain_paths_count)
            await _extract_cdc_paths(state, deps)

        async def _rapidwright_pipeline():
            # Step B-R1: Read checkpoint in RapidWright
            result = await call_tool_fn(
                "rapidwright_read_checkpoint",
                {"dcp_path": str(input_dcp.resolve())},
                deps.rapidwright_session, deps.vivado_session,
            )
            if "error" in result.lower() and "success" not in result.lower():
                logger.warning(f"[init_analysis] Could not load design in RapidWright: {result}")

            # Step B-R2: Device topology → capacity
            await _extract_device_capacity(state, deps)

            # Step B-R3: Design info (M4: cell type statistics)
            await _extract_design_info(state, deps)

        await asyncio.gather(_vivado_pipeline(), _rapidwright_pipeline())

        # ════════════════════════════════════════════════════════════
        # Phase C: Cross-server analysis (sequential on RW)
        # ════════════════════════════════════════════════════════════

        # Step C1: Critical path spread
        if cell_names_for_spread:
            try:
                spread_result = await call_tool_fn(
                    "rapidwright_analyze_critical_path_spread",
                    {"critical_paths_data": cell_names_for_spread},
                    deps.rapidwright_session, deps.vivado_session,
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
            )
            if isinstance(congestion_result, str):
                congestion_data = json.loads(congestion_result)
            else:
                congestion_data = congestion_result
            if "error" not in congestion_data:
                state.timing.congestion_data = {
                    "global_score": congestion_data.get("congested_ratio", 0.0),
                    "pblock_overflow_count": 0,
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
            {"command": "llength [get_false_paths -quiet]"},
            deps.rapidwright_session, deps.vivado_session,
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
            {"command": "llength [get_multicycle_paths -quiet]"},
            deps.rapidwright_session, deps.vivado_session,
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
        )
        if io_result and io_result.strip():
            parts = io_result.strip().split()
            if len(parts) >= 2:
                try:
                    with_delay = int(parts[0])
                    total_ports = int(parts[1])
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
    """Count cross-clock-domain timing paths."""
    try:
        cdc_report = await call_tool_fn(
            "vivado_run_tcl",
            {"command": "report_timing -cross_clock -max_paths 100 -return_string"},
            deps.rapidwright_session, deps.vivado_session,
        )
        state.timing.cross_domain_paths_count = parse_cdc_paths(cdc_report)
        logger.info(f"[init_analysis] CDC paths: {state.timing.cross_domain_paths_count}")
    except Exception as e:
        logger.warning(f"[init_analysis] CDC analysis failed: {e}")
