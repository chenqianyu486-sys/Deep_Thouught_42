"""Init analysis node: run initial FPGA design analysis.

Reads the input DCP, extracts WNS/TNS/resource utilization,
and populates state.timing with initial values.

Reference: dcp_optimizer.py perform_initial_analysis() (L4068-4259).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.timing import parse_timing_summary, parse_high_fanout_nets, parse_resource_utilization, parse_hold_timing
from ..pure.tool_router import call_tool as call_tool_fn
from config_loader import get_worker_model_config

logger = logging.getLogger(__name__)


async def init_analysis_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Run initial analysis on the input DCP design.

    Actions:
        1. Record start_time
        2. Initialize RapidWright
        3. Open checkpoint in Vivado
        4. Report timing summary -> parse WNS/TNS
        5. Get high fanout nets
        6. Get resource utilization
        7. Load design in RapidWright
        8. Get device topology
        9. Extract critical path cells and analyze spread

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
        # Step 1: Initialize RapidWright
        logger.info("[init_analysis] Initializing RapidWright...")
        result = await call_tool_fn(
            "rapidwright_initialize_rapidwright", {},
            deps.rapidwright_session, deps.vivado_session,
        )
        if "error" in result.lower() and "success" not in result.lower():
            raise RuntimeError(f"Failed to initialize RapidWright: {result}")

        # Step 2: Open checkpoint in Vivado
        logger.info(f"[init_analysis] Opening checkpoint: {input_dcp}")
        result = await call_tool_fn(
            "vivado_open_checkpoint",
            {"dcp_path": str(input_dcp.resolve())},
            deps.rapidwright_session, deps.vivado_session,
        )
        if "error" in result.lower() and "opened successfully" not in result.lower():
            raise RuntimeError(f"Failed to open checkpoint: {result}")

        # Step 3: Report timing summary
        logger.info("[init_analysis] Analyzing timing...")
        timing_report = await call_tool_fn(
            "vivado_report_timing_summary", {},
            deps.rapidwright_session, deps.vivado_session,
        )
        timing_info = parse_timing_summary(timing_report)

        # Populate state.timing
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
            f"TNS={state.timing.initial_tns}, "
            f"FE={state.timing.initial_failing_endpoints}"
        )

        # Step 4: Get clock period via run_tcl (clk_fpl26contest is the contest clock)
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
                # Fallback: worst-path clock
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

        # Check hold timing (competition requirement: hold WNS >= 0)
        try:
            hold_report = await call_tool_fn(
                "vivado_run_tcl",
                {"command": "report_timing_summary -delay_type min -max_paths 100"},
                deps.rapidwright_session, deps.vivado_session,
            )
            hold = parse_hold_timing(hold_report)
            if hold.get("hold_wns") is not None:
                if hold["hold_wns"] < 0:
                    logger.warning(
                        f"[init_analysis] HOLD VIOLATED: WNS={hold['hold_wns']:.3f}ns, "
                        f"failing={hold.get('hold_failing')}"
                    )
                else:
                    logger.info(f"[init_analysis] Hold timing MET: WNS={hold['hold_wns']:.3f}ns")
        except Exception as e:
            logger.warning(f"[init_analysis] Hold timing check failed: {e}")

        # Step 5: Get critical high fanout nets
        logger.info("[init_analysis] Identifying high fanout nets...")
        nets_report = await call_tool_fn(
            "vivado_get_critical_high_fanout_nets",
            {"num_paths": 50, "min_fanout": 100},
            deps.rapidwright_session, deps.vivado_session,
        )
        state.timing.high_fanout_nets = parse_high_fanout_nets(nets_report)
        logger.info(f"[init_analysis] Found {len(state.timing.high_fanout_nets)} high fanout nets")

        # Step 6: Get resource utilization
        logger.info("[init_analysis] Getting resource utilization...")
        util_report = await call_tool_fn(
            "vivado_report_utilization_for_pblock", {},
            deps.rapidwright_session, deps.vivado_session,
        )
        state.timing.resource_utilization = parse_resource_utilization(util_report)

        # Step 7: Load design in RapidWright
        logger.info("[init_analysis] Loading design in RapidWright...")
        result = await call_tool_fn(
            "rapidwright_read_checkpoint",
            {"dcp_path": str(input_dcp.resolve())},
            deps.rapidwright_session, deps.vivado_session,
        )
        if "error" in result.lower() and "success" not in result.lower():
            logger.warning(f"[init_analysis] Could not load design in RapidWright: {result}")

        # Step 8: Get device topology
        try:
            topo_result = await call_tool_fn(
                "rapidwright_get_device_topology", {},
                deps.rapidwright_session, deps.vivado_session,
            )
            topo_data = json.loads(topo_result)
            if topo_data.get("status") == "success":
                logger.info(f"[init_analysis] Device: {topo_data.get('device')}")
        except Exception as e:
            logger.warning(f"[init_analysis] Could not get device topology: {e}")

        # Step 9: Extract critical path cells and analyze spread
        try:
            run_dir = state.control.run_dir or Path("/tmp")
            temp_path = run_dir / "initial_critical_paths.json"

            cells_json = await call_tool_fn(
                "vivado_extract_critical_path_cells",
                {"num_paths": 50, "output_file": str(temp_path)},
                deps.rapidwright_session, deps.vivado_session,
            )

            spread_result = await call_tool_fn(
                "rapidwright_analyze_critical_path_spread",
                {"input_file": str(temp_path)},
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

    except Exception as e:
        logger.error(f"[init_analysis] Analysis failed: {e}")
        state.control.is_done = True
        state.control.done_reason = f"init_analysis_failed: {e}"
        return NodeName.SAVE_OUTPUT

    # Sync initial values to MemoryManager for compression context
    if deps.compat is not None:
        try:
            if state.timing.initial_wns is not None:
                deps.compat.set_initial_wns(state.timing.initial_wns)
            if state.timing.clock_period is not None:
                deps.compat.set_clock_period(state.timing.clock_period)
        except Exception as e:
            logger.warning(f"[init_analysis] Failed to sync initial values to MemoryManager: {e}")

    # Load cost_hard_limit from config (shared planner+worker budget)
    state.cost.cost_hard_limit = get_worker_model_config().cost_hard_limit
    logger.info(f"[init_analysis] Cost hard limit: ${state.cost.cost_hard_limit:.2f}")

    logger.info(
        f"[init_analysis] Complete: WNS={state.timing.initial_wns}, "
        f"best_wns={state.timing.best_wns:.3f}"
    )

    # The edge function after_init will decide the next node
    return NodeName.ITERATION_START
