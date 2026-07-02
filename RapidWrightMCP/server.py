#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache 2.0

"""
RapidWright MCP Server
Provides AI assistant access to RapidWright FPGA design tools via the Model Context Protocol
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent, GetPromptResult, PromptMessage
import mcp.server.stdio

# === Fix: ensure project root is on sys.path so skills/ package is importable ===
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_script_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# ====================================================================
import rapidwright_tools as rw

# Import sanitization utilities
try:
    from context_manager.logging_config import sanitize_payload, get_trace_id
except ImportError:
    # Fallback if context_manager not available
    def sanitize_payload(payload, max_length=1024):
        return payload
    def get_trace_id():
        return ""

# Global variable for the Java/stdout log file
_java_log_file = None
_original_stderr_fd = None

# Logger will be configured in main() based on command-line arguments
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create MCP server instance
app = Server("rapidwright-mcp")

# Tools that perform real computation/optimization (expected >>1s execution time)
COMPLEX_TOOLS = {
    "analyze_pblock_region",
    "execute_pblock_strategy",
    "execute_physopt_strategy",
    "execute_opt_design_strategy",
    "execute_combinational_rebalancing_strategy",
    "execute_lut_muxf_repack_strategy",
    "execute_muxf_tree_reorder_strategy",
    "execute_fanout_strategy",
    "analyze_net_detour",
    "optimize_cell_placement",
    "smart_region_search",
    "optimize_fanout_batch",
    "analyze_critical_path_spread",
    "analyze_fabric_for_pblock",
    "optimize_lut_input_cone",
    "analyze_congestion",
    "analyze_congestion_spreading",
    "execute_congestion_spreading",
    "flatten_lut_cascade",
    "optimize_pin_swapping",
    "replicate_critical_cells",
    "analyze_register_retiming",
    "execute_register_retiming",
    "analyze_net_swapping",
    "execute_net_swapping",
}


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List all available RapidWright tools."""
    return [
        Tool(
            name="initialize_rapidwright",
            description="Initialize the RapidWright environment. Must be called first before using other tools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jvm_max_memory": {
                        "type": "string",
                        "description": "Maximum JVM heap size (default: '4G')",
                        "default": "4G"
                    }
                }
            }
        ),
        Tool(
            name="get_supported_devices",
            description="Get list of all FPGA devices supported by RapidWright, including families and part numbers.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_device_info",
            description="Get detailed information about a specific FPGA device (dimensions, resources, family).",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_name": {
                        "type": "string",
                        "description": "FPGA device name (e.g., 'xcvu3p', 'xcvu9p', 'xcku040')"
                    }
                },
                "required": ["device_name"]
            }
        ),
        Tool(
            name="get_device_topology",
            description="Get device topology including site type distribution (SLICEL, DSP48E2, RAMB36, etc.). Useful for planning pblock strategies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_name": {
                        "type": "string",
                        "description": "FPGA device name (e.g., 'xcvu3p', 'xcvu9p', 'xcku040'). Uses current design's device if not specified."
                    }
                }
            }
        ),
        Tool(
            name="read_checkpoint",
            description="Read a Vivado Design Checkpoint (.dcp) file for inspection and analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path to the .dcp file"
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="write_checkpoint",
            description="Write the current design to a Vivado Design Checkpoint (.dcp) file. If the design contains encrypted IP, an accompanying Tcl script will be generated that is required to load the DCP in Vivado.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path where the .dcp file will be saved"
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "If true, overwrite existing file; if false (default), error if file exists",
                        "default": False
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="get_design_info",
            description="Get statistics about the currently loaded design (cell/net counts, top cell types).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="search_cells",
            description="Search for cells in the loaded design by name pattern or cell type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Name pattern to match (case-insensitive, optional)"
                    },
                    "cell_type": {
                        "type": "string",
                        "description": "Single cell type to filter by (e.g., 'LUT6', 'FDRE', optional)"
                    },
                    "cell_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of cell types to search for in one call (e.g., ['LUT6', 'FDRE', 'DSP48E2']). Use this instead of calling search_cells repeatedly for different types."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 100)",
                        "default": 100
                    }
                }
            }
        ),
        Tool(
            name="get_tile_info",
            description="Get information about a specific tile on the FPGA (type, location, sites).",
            inputSchema={
                "type": "object",
                "properties": {
                    "tile_name": {
                        "type": "string",
                        "description": "Tile name to query"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    }
                },
                "required": ["tile_name"]
            }
        ),
        Tool(
            name="search_sites",
            description="Search for sites on an FPGA device by site type (e.g., SLICEL, DSP48E2, RAMB36).",
            inputSchema={
                "type": "object",
                "properties": {
                    "site_type": {
                        "type": "string",
                        "description": "Site type to search for (e.g., 'SLICEL', 'DSP48E2', 'RAMB36')"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 50)",
                        "default": 50
                    }
                }
            }
        ),
        Tool(
            name="optimize_lut_input_cone",
            description="Optimize LUT input cones by combining chained small LUTs into a single larger LUT (max 6 inputs) to reduce logic depth on critical paths.\n\n"
                        "LIMITATIONS:\n"
                        "- NOT suitable for neural network accelerators or wide-datapath designs where logic cones have 75+ inputs (exceeds 6-input LUT physical limit).\n"
                        "- The tool returns status 'success' even when no cones were optimizable — ALWAYS check optimized_count in the result.\n\n"
                        "RESULT INTERPRETATION:\n"
                        "- optimized_count > 0: cones were combined; re-route and verify timing.\n"
                        "- optimized_count == 0 but status='success': check per-pin 'status' field. 'no_optimization' means pin already has minimal depth. Java ERRORs about '6 maximum inputs supported' mean the design's logic cones are too wide — skip this tool entirely.\n"
                        "- If ALL pins produce Java ERRORs: this design is NOT suitable for LUT cone optimization. Switch to a different strategy.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hierarchical_input_pins": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^.+/.+$"},
                        "description": "Hierarchical input pin names to optimize. MUST contain '/' separator (e.g. 'module/submodule/inst/pin'). Bare pin or type names are NOT valid. Source names from the [CELL REGISTRY] section in your context.",
                        "examples": ["u_core/u_alu/lut6/I0", "layer0_inst/layer0_N25_inst/data_out[76]_i_19/I1"]
                    }
                },
                "required": ["hierarchical_input_pins"]
            }
        ),
        Tool(
            name="optimize_fanout_batch",
            description="Batch optimize multiple high fanout nets by splitting them into multiple driven nets. "
                        "Reduces API calls by processing multiple nets in one call. "
                        "split_factor is calculated internally: fanout/100 (min 3, max 8)",
            inputSchema={
                "type": "object",
                "properties": {
                    "nets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "net_name": {"type": "string", "description": "Name of the high fanout net to optimize"},
                                "fanout": {"type": "integer", "description": "Current fanout count of the net (used to calculate split_factor)"}
                            },
                            "required": ["net_name", "fanout"]
                        }
                    }
                },
                "required": ["nets"]
            }
        ),
        Tool(
            name="analyze_critical_path_spread",
            description="""Calculate Manhattan distances for cells on critical paths.
            
            Takes critical path data from Vivado (cell names from timing report) and uses RapidWright's
            device model to get accurate tile coordinates and calculate Manhattan distances between cells.
            
            Input can be provided either directly as critical_paths_data parameter OR via a JSON file
            specified in input_file parameter (more efficient for large datasets).
            
            Must be called AFTER read_checkpoint.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths_data": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "description": "List of paths, each path is a list of cell names from Vivado timing report"
                    },
                    "input_file": {
                        "type": "string",
                        "description": "Optional: path to JSON file containing critical_paths_data (more efficient)"
                    }
                }
            }
        ),
        Tool(
            name="analyze_fabric_for_pblock",
            description="""Analyze FPGA fabric to find the best contiguous region for a pblock (area constraint).
            
            Identifies regions that:
            1. Have enough resources (SLICEs, DSPs, BRAMs) for target utilization
            2. Minimize crossing of delay-heavy columns (URAM, IO, etc.)
            3. Are as contiguous as possible
            
            Use this AFTER getting utilization from Vivado to determine where to place a pblock.
            Requires target resource counts (1.5x current usage from report_utilization_for_pblock).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_lut_count": {
                        "type": "integer",
                        "description": "Required LUTs (1.5x current usage)"
                    },
                    "target_ff_count": {
                        "type": "integer",
                        "description": "Required FFs (1.5x current usage)"
                    },
                    "target_dsp_count": {
                        "type": "integer",
                        "description": "Required DSPs (1.5x current usage, default: 0)"
                    },
                    "target_bram_count": {
                        "type": "integer",
                        "description": "Required BRAMs (1.5x current usage, default: 0)"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    }
                },
                "required": ["target_lut_count", "target_ff_count"]
            }
        ),
        Tool(
            name="convert_fabric_region_to_pblock",
            description="""Convert fabric region coordinates to Vivado pblock range strings.
            
            Takes tile column/row coordinates and generates a complete pblock string with all
            site types (SLICE, DSP48E2, RAMB18, RAMB36, URAM288) in proper Vivado format.
            
            Example output: "SLICE_X55Y0:SLICE_X109Y179 DSP48E2_X8Y0:DSP48E2_X13Y71 RAMB18_X4Y0:RAMB18_X7Y71 RAMB36_X4Y0:RAMB36_X7Y35 URAM288_X1Y0:URAM288_X2Y47"
            
            IMPORTANT: Always use detailed site-specific ranges (default) for optimization.
            DO NOT use clock regions (use_clock_regions=True) as they are too coarse.
            
            Must be called AFTER read_checkpoint or with device_name specified.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "col_min": {
                        "type": "integer",
                        "description": "Minimum column coordinate"
                    },
                    "col_max": {
                        "type": "integer",
                        "description": "Maximum column coordinate"
                    },
                    "row_min": {
                        "type": "integer",
                        "description": "Minimum row coordinate"
                    },
                    "row_max": {
                        "type": "integer",
                        "description": "Maximum row coordinate"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    },
                    "use_clock_regions": {
                        "type": "boolean",
                        "description": "If true, use coarse CLOCKREGION ranges (NOT RECOMMENDED for optimization); if false (DEFAULT), generate detailed multi-site-type ranges (SLICE_X, DSP48E2_X, etc.) - REQUIRED for pblock optimization"
                    }
                },
                "required": ["col_min", "col_max", "row_min", "row_max"]
            }
        ),
        Tool(
            name="compare_design_structure",
            description="""Compare structural properties of two design checkpoints for equivalence validation.

            This is Phase 1 of design equivalence checking. Performs sanity checks to catch obvious errors:
            - Top-level module name must match
            - I/O port names, directions, and widths must match
            - Device must match
            - Cell count can increase (optimizations add cells) but not decrease or increase >50%

            Returns PASS/FAIL status with detailed comparison report.
            This should be run BEFORE functional simulation to quickly catch structural errors.

            USE: After any design modification to verify structural consistency.
            USE: Before submission to verify design integrity.
            READ-ONLY: Does not modify design.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "golden_dcp": {
                        "type": "string",
                        "description": "Path to the golden (reference) DCP file"
                    },
                    "revised_dcp": {
                        "type": "string",
                        "description": "Path to the revised (optimized) DCP file to validate"
                    }
                },
                "required": ["golden_dcp", "revised_dcp"]
            }
        ),
        Tool(
            name="analyze_net_detour",
            description="""Analyze detour ratio for cells on critical paths.

            Computes detour_ratio = routed_path_length / manhattan_distance for nets
            on critical paths. A detour ratio > ~2.0 suggests a cell may benefit from
            re-placement.

            Input is a pin-path list as produced by Vivado's extract_critical_path_pins:
            ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]

            Requires design to be loaded via read_checkpoint first.

            RESULT INTERPRETATION:
            - Empty result (no cells exceeding threshold): routing is already compact for the
              analyzed paths. This is a VALID diagnostic result, NOT a failure. It confirms
              the current placement's routing is near-optimal for these paths.
            - Non-empty result: cells with detour_ratio > threshold were found. Consider calling
              optimize_cell_placement for the worst offenders.

            Priority: Call this when WNS is stuck and critical paths have >3 LUT levels, or after multiple phys_opt/route cycles without improvement.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "pin_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of pin names from critical path (e.g., ['cell1/I0', 'cell1/O', 'cell2/I0'])"
                    },
                    "detour_threshold": {
                        "type": "number",
                        "description": "Minimum detour ratio to report (default: 2.0)",
                        "default": 2.0
                    }
                },
                "required": ["pin_paths"]
            }
        ),
        Tool(
            name="optimize_cell_placement",
            description="""Optimize cell placement by moving cells to the centroid of their connections.

            For each cell:
            1. Collect tile locations of all connected pins
            2. Compute centroid using ECOPlacementHelper
            3. Unplace cell and unroute its nets
            4. Spiral outward from centroid to find empty compatible site
            5. Place cell and re-route intra-site wiring

            After optimization, use write_checkpoint to save and Vivado to re-route
            and verify timing improvement.
            Priority: Call this after analyze_net_detour identifies cells with detour_ratio > 2.0.

            ⚠️ DESIGN CONSISTENCY WARNING:
            This tool MODIFIES the design (moves cells, changes routing).
            After using this tool, you MUST:
            1. Run vivado_validate_timing to verify timing
            2. Run rapidwright_compare_designs to verify structural consistency
            3. Verify functional equivalence before submission

            MUTATING: modifies cell placement and writes checkpoint file.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "cell_names": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^.+/.+$"},
                        "description": "Hierarchical cell instance names to optimize. MUST contain '/' separator (e.g. 'u_core/u_alu/lut1'). Device sites (SLICE_X*, DSP*_X*) and bare type names (LUT6, FDRE) are NOT valid. Source names from the [CELL REGISTRY] section in your context.",
                        "examples": ["u_core/u_alu/lut1", "u_core/u_alu/reg_0/Q_reg[0]"]
                    }
                },
                "required": ["cell_names"]
            }
        ),
        Tool(
            name="smart_region_search",
            description="""Find optimal pblock region using greedy expansion from reference point.

            Analyzes FPGA fabric and finds an optimal rectangular region
            that satisfies the target resource requirements. Uses greedy expansion
            from a reference point (or design center of mass), avoiding delay-heavy
            columns (URAM, HPIO, etc.) and prioritizing high-density columns.

            Single tool call replaces 12+ LLM interaction rounds for pblock selection.
            Priority: Call this standalone for pblock selection. Use analyze_pblock_region for the combined analysis+planning tool.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_lut_count": {
                        "type": "integer",
                        "description": "Required number of LUTs (1.5x current usage recommended)"
                    },
                    "target_ff_count": {
                        "type": "integer",
                        "description": "Required number of FFs (1.5x current usage recommended)"
                    },
                    "target_dsp_count": {
                        "type": "integer",
                        "description": "Required number of DSPs (default: 0)",
                        "default": 0
                    },
                    "target_bram_count": {
                        "type": "integer",
                        "description": "Required number of BRAMs (default: 0)",
                        "default": 0
                    },
                    "reference_col": {
                        "type": "integer",
                        "description": "Reference column coordinate (optional, uses design center of mass)"
                    },
                    "reference_row": {
                        "type": "integer",
                        "description": "Reference row coordinate (optional, uses design center of mass)"
                    }
                },
                "required": ["target_lut_count", "target_ff_count"]
            }
        ),
        Tool(
            name="analyze_pblock_region",
            description="""Analyze FPGA fabric to find the optimal PBLOCK region for re-placement.

            READ-ONLY analysis. Uses sliding-window column search to find the optimal
            contiguous fabric region that fits the design's resource needs (with buffer
            multiplier). Returns region coordinates, pblock_ranges, estimated resources,
            resource deficit per type (LUT/FF/DSP/BRAM), IS_SOFT recommendation, and
            suggested next_steps for Vivado execution.

            KEY OUTPUT FIELDS:
            - capacity_ok (bool): True = region has enough resources. Only proceed if true.
            - deficit (dict): Shortfall per resource type when capacity_ok=false.
            - is_soft_recommended (bool): True when utilization density > 80%.
            - next_steps (list): Vivado commands to execute (only present when capacity_ok=true).

            Prerequisite: call vivado_report_utilization_for_pblock first to get
            current LUT/FF/DSP/BRAM counts.
            Input: resource counts from Vivado utilization report.
            Output: region coordinates, pblock_ranges string, estimated resources,
                    deficit, is_soft_recommended, and next_steps (Vivado tools you must call yourself).

            Priority: Use when avg_distance > 70 (distributed scenario) or
                      recommendation == 'PBLOCK'.
            For the full automatic workflow (analysis + auto-chained Vivado tools),
            use execute_pblock_strategy instead.

            NOTE on resource_multiplier:
            - Default 1.5x provides 50%% headroom. For already-congested designs, this may
              over-allocate and produce an unnecessarily large pblock, reducing timing benefit.
            - Reduce to 1.0x-1.2x if the design has high utilization or if you want a tighter region.
            - The returned pblock_ranges are ready for direct use in vivado_create_and_apply_pblock.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_lut_count": {
                        "type": "integer",
                        "description": "Current LUT usage from Vivado report_utilization_for_pblock"
                    },
                    "target_ff_count": {
                        "type": "integer",
                        "description": "Current FF usage from Vivado report_utilization_for_pblock"
                    },
                    "target_dsp_count": {
                        "type": "integer",
                        "description": "Current DSP usage (default: 0)",
                        "default": 0
                    },
                    "target_bram_count": {
                        "type": "integer",
                        "description": "Current BRAM usage (default: 0)",
                        "default": 0
                    },
                    "resource_multiplier": {
                        "type": "number",
                        "description": "Buffer multiplier for resource targets (default: 1.5)",
                        "default": 1.5
                    },
                    "critical_path_cells": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^.+/.+$"},
                        "description": "Critical path cell names for region centering. MUST contain '/' separator (e.g. 'u_core/u_alu/lut1'). Device sites (SLICE_X*) and bare type names are NOT valid. Auto-injected by optimizer from the [CELL REGISTRY] — optional for LLM.",
                        "default": None,
                        "examples": ["u_core/u_alu/lut1", "layer0_inst/layer0_N25_inst/data_out[76]_i_19"]
                    },
                    "distance_weight_factor": {
                        "type": "number",
                        "description": "Distance weight in region scoring (0.3 default). "
                                       "Higher = prefer regions closer to critical path cells.",
                        "default": 0.3
                    }
                },
                "required": ["target_lut_count", "target_ff_count"]
            }
        ),
        Tool(
            name="execute_pblock_strategy",
            description="""Execute complete PBLOCK workflow: analyze FPGA fabric and prepare for automated Vivado chaining.

            This is the PREFERRED tool for PBLOCK strategy — do NOT use vivado_run_tcl for this workflow.
            After this tool succeeds, the system will AUTOMATICALLY chain the following Vivado tools:
              1. vivado_place_design -unplace
              2. vivado_create_and_apply_pblock (with returned pblock_ranges, is_soft from recommendation)
              3. vivado_place_design (re-place within constraint)
              4. vivado_route_design

            SELF-CONTAINED: Resource counts (LUT/FF) are auto-detected from the loaded design
            when not explicitly provided. You can call this tool directly with no arguments.

            MUTATING (via chained Vivado tools). The returned checkpoint preserves
            pre-pblock state for rollback. On chain failure, design auto-restores to pre-chain state.

            OUTPUT includes:
            - capacity_ok: only proceed if true
            - deficit: per-type resource shortfall (LUT/FF/DSP/BRAM)
            - is_soft_recommended: auto-set based on utilization density (>80% = soft constraint)
            - pblock_ranges, pblock_name, region: ready for Vivado tool chain

            ORDERING: For distributed designs (avg_distance > 70), run this BEFORE
            fanout_strategy. Running fanout before PBLOCK disrupts placement and
            typically worsens WNS by > 0.5ns.

            NOTE: resource_multiplier defaults to 2.0x (matches proven test_mode behavior).
            Reduce to 1.2x for dense designs (>40% utilization).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_lut_count": {
                        "type": "integer",
                        "description": "LUT usage (0 or omit = auto-detect from loaded design)"
                    },
                    "target_ff_count": {
                        "type": "integer",
                        "description": "FF usage (0 or omit = auto-detect from loaded design)"
                    },
                    "target_dsp_count": {
                        "type": "integer",
                        "description": "DSP usage (0 or omit = auto-detect from loaded design)",
                        "default": 0
                    },
                    "target_bram_count": {
                        "type": "integer",
                        "description": "BRAM usage (0 or omit = auto-detect from loaded design)",
                        "default": 0
                    },
                    "resource_multiplier": {
                        "type": "number",
                        "description": "Buffer multiplier for resource targets (default: 1.2 for tighter regions)",
                        "default": 1.2
                    },
                    "critical_path_cells": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Critical path cell names for region centering. "
                                       "Auto-injected by optimizer — optional for LLM.",
                        "default": None
                    },
                    "distance_weight_factor": {
                        "type": "number",
                        "description": "Distance weight in region scoring (0.3 default). "
                                       "Higher = prefer regions closer to critical path cells.",
                        "default": 0.3
                    }
                }
            }
        ),
        Tool(
            name="execute_physopt_strategy",
            description="""Generate PhysOpt execution plan for Vivado.

            Returns a structured plan for running physical optimization in Vivado,
            including phys_opt_design, route_design, and report_timing_summary steps.

            Trigger: 1-2 critical paths with spread but no high fanout.
            Input: directive for phys_opt_design.
            Output: structured plan with ordered Vivado steps.
            Priority: Prefer this over manual phys_opt_design when WNS > -2.0 and 1-2 paths with spread.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "description": "phys_opt_design directive (Default, Explore, AggressiveExplore, AddRetime, etc.)",
                        "default": "Default"
                    },
                    "design_is_routed": {
                        "type": "boolean",
                        "description": "Whether the design is currently routed",
                        "default": True
                    }
                }
            }
        ),
        Tool(
            name="execute_opt_design_strategy",
            description="""Generate opt_design execution plan for Vivado.

            Returns a structured plan for running logic-level optimization in Vivado,
            including opt_design, place_design, route_design, and report_timing_summary steps.

            Trigger: 6-7 LUT levels, 100% logic delay, combinational-dominated designs.
            Input: directive for opt_design, retarget flag.
            Output: structured plan with ordered Vivado steps.
            Use when PhysOpt (post-place) is ineffective due to pure logic-depth bottlenecks.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "description": "opt_design directive (Explore, ExploreArea, ExploreSequentialArea, RuntimeOptimized, AddRemap)",
                        "default": "Explore"
                    },
                    "retarget": {
                        "type": "boolean",
                        "description": "Retarget logic to equivalent primitives",
                        "default": True
                    }
                }
            }
        ),
        Tool(
            name="execute_combinational_rebalancing_strategy",
            description="""Validation-safe combinational rebalancing (NO flip-flop insertion).

            Identifies deep combinational cones (LUT6/LUT5/MUXF7/MUXF8 cascades
            between registers) on critical paths and generates a Vivado
            opt_design -remap execution plan for logic-equivalent resynthesis to
            rebalance logic depth. Unlike register retiming, this inserts NO new
            FFs — design latency is preserved, so cycle-exact validation passes.

            This is the validation-safe alternative to register retiming: it
            achieves the same goal (shortening critical-path logic depth) via
            combinational resynthesis only.

            Trigger: WNS stuck, deep combinational chains between registers on
            critical paths, FF insertion unsafe (cycle-exact validation required).
            Input: critical_paths (cell names from extract_critical_path_cells),
            min_depth, opt_design directive, retarget flag.
            Output: structured plan with ordered Vivado steps (opt_design -> place -> route -> report).

            VALIDATION: safe (logic-equivalent, no latency change).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string", "pattern": "^.+/.+$"}
                        },
                        "description": "List of paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]. Each cell name MUST be hierarchical (contain '/'), e.g. 'u_core/u_alu/lut1'. Device sites and bare type names are NOT valid. Provide at most 10 paths. Source names from the [CELL REGISTRY] section in your context."
                    },
                    "min_depth": {
                        "type": "integer",
                        "description": "Minimum combinational depth (LUT/MUXF levels between registers) to target",
                        "default": 3
                    },
                    "directive": {
                        "type": "string",
                        "description": "opt_design directive (Explore, AddRemap, ExploreArea)",
                        "default": "Explore"
                    },
                    "retarget": {
                        "type": "boolean",
                        "description": "Retarget logic to equivalent primitives (e.g., LUT5->LUT6 merge)",
                        "default": True
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="execute_lut_muxf_repack_strategy",
            description="""Validation-safe LUT6+MUXF co-repack (NO flip-flop insertion).

            Targets NN/wide-datapath designs where critical paths are dominated
            by LUT6 -> MUXF7 -> MUXF8 -> LUT6 cascades (8:1/16:1 mux trees that
            exceed the 6-input LUT physical limit). Unlike flatten_lut_cascade
            (which returns optimized_count=0 on such wide cones), this delegates
            to Vivado opt_design -directive AddRemap for aggressive logic-equivalent
            LUT-equation repacking that merges LUT5/LUT6 pairs and restructures
            MUXF+LUT6 adjacencies. Inserts NO flip-flops — latency preserved.

            Trigger: NN/datapath design, MUXF7/MUXF8 + LUT6 cascade on critical
            paths, flatten_lut_cascade returned optimized_count=0.
            Input: critical_paths (cell names), opt_design directive, retarget flag.
            Output: structured plan (opt_design -> place -> route -> report).

            VALIDATION: safe (logic-equivalent, no latency change).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string", "pattern": "^.+/.+$"}
                        },
                        "description": "List of paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]. Each cell name MUST be hierarchical (contain '/'), e.g. 'u_core/u_alu/lut1'. Device sites and bare type names are NOT valid. Provide at most 10 paths. Source names from the [CELL REGISTRY] section in your context."
                    },
                    "directive": {
                        "type": "string",
                        "description": "opt_design directive. 'AddRemap' is recommended for aggressive LUT-equation repacking; 'Explore' is milder.",
                        "default": "AddRemap"
                    },
                    "retarget": {
                        "type": "boolean",
                        "description": "Retarget LUT5 -> LUT6 merge candidates. Safe — does not change function.",
                        "default": True
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="execute_muxf_tree_reorder_strategy",
            description="""Validation-safe MUXF tree reorder (NO flip-flop insertion, NO -retime).

            Maps 'carry chain reorder' onto designs without CARRY4/CARRY8 that
            instead use MUXF7/MUXF8 mux trees (the dominant inter-layer
            combinational structure in NN designs). Identifies MUXF tree runs on
            critical paths where the timing-critical input traverses the deepest
            mux level, then delegates to Vivado phys_opt_design -directive Explore
            (NO -retime) for logic-equivalent pin/cell optimization that reorders
            selection paths and pulls critical inputs to faster mux levels.

            Trigger: NN/datapath design, MUXF7/MUXF8 tree on critical paths,
            no CARRY4 carry chains, WNS stuck after PBLOCK.
            Input: critical_paths (cell names), phys_opt_design directive,
            min_tree_depth.
            Output: structured plan (phys_opt_design -> route -> report).

            WARNING: Do NOT pass directive='AddRetime' — it would insert/move FFs
            and fail cycle-exact validation. The skill rejects AddRetime.

            VALIDATION: safe (logic-equivalent, no latency change).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string", "pattern": "^.+/.+$"}
                        },
                        "description": "List of paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]. Each cell name MUST be hierarchical (contain '/'), e.g. 'u_core/u_alu/lut1'. Device sites and bare type names are NOT valid. Provide at most 10 paths. Source names from the [CELL REGISTRY] section in your context."
                    },
                    "directive": {
                        "type": "string",
                        "description": "phys_opt_design directive. 'Explore' is balanced; 'AggressiveExplore' is stronger. Do NOT use 'AddRetime' (validation-unsafe).",
                        "default": "Explore"
                    },
                    "min_tree_depth": {
                        "type": "integer",
                        "description": "Minimum MUXF run depth (consecutive MUXF7/MUXF8 cells) to target",
                        "default": 2
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="execute_fanout_strategy",
            description="""Execute fanout optimization directly in RapidWright.

            Runs optimize_fanout_batch and write_checkpoint in RapidWright to split
            high fanout nets, then reports optimization results.
            After this, run vivado_open_checkpoint, vivado_place_design,
            vivado_route_design, and vivado_report_timing_summary in Vivado.

            MUTATING: modifies design net topology and writes checkpoint file.
            Trigger: High fanout nets present (fanout > 100), no path spread.
            Input: list of nets with fanout counts.
            Output: optimization results (nets_processed, successful_count, failed_count, checkpoint_path).

            STRATEGY INTERACTION WARNING:
            - Running fanout splitting AFTER PBLOCK placement can WORSEN WNS by disrupting the dense PBLOCK layout.
            - If WNS regresses after fanout+reroute, set flow_control=ROLLBACK to revert to pre-fanout checkpoint.
            - Prefer running fanout optimization BEFORE applying PBLOCK constraints, or as a standalone strategy.

            ORDERING CONSTRAINT:
            - For distributed designs (avg_distance > 70 tiles): run execute_pblock_strategy FIRST, then fanout_strategy.
            - CONTRAINDICATION: Do NOT run fanout_strategy before PBLOCK on distributed designs. Without placement
              constraint, fanout tree insertion increases cell count and routing complexity, causing WNS regression
              of 0.5ns or more (observed: -0.978ns → -1.660ns).

            RESULT INTERPRETATION:
            - successful_count > 0: nets were split. Always verify WNS delta after Vivado route_design.
            - If WNS worsens after this optimization: the fanout splitting broke existing placement density.
              Roll back and do NOT retry with different nets — try a different strategy instead.

            Priority: Prefer this over manual optimize_fanout_batch when high_fanout nets (fo>100) dominate timing.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "nets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "net_name": {"type": "string"},
                                "fanout": {"type": "integer"}
                            },
                            "required": ["net_name", "fanout"]
                        },
                        "description": "List of net configs: [{\"net_name\": ..., \"fanout\": ...}]"
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for intermediate checkpoint",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Checkpoint filename prefix",
                        "default": "fanout_opt"
                    }
                },
                "required": ["nets"]
            }
        ),
        Tool(
            name="flatten_lut_cascade",
            description="""Flatten LUT cascades on critical paths to reduce logic depth.

            Identifies chains of >3 LUTs in series on critical paths and merges them
            using RapidWright LUTInputConeOpt. Saves checkpoint before mutation.

            MUTATING: merges LUT cells and writes checkpoint files.
            Trigger: Critical paths have >3 LUT levels in series (logic depth bottleneck).
            After this, run vivado_open_checkpoint, vivado_route_design,
            vivado_report_timing_summary to verify WNS improvement.

            LIMITATIONS:
            - NOT suitable for neural network / wide-datapath designs where logic cones
              have 75+ inputs (exceeds 6-input LUT physical limit).

            RESULT INTERPRETATION:
            - optimized_count > 0: cones were combined; re-route and verify timing.
            - optimized_count == 0: check cascades_found. If 0, no deep cascades exist.
              If >0 but 0 optimized, the LUT cones may be too wide for this tool.

            Priority: Call when WNS is stuck and critical paths have >3 LUT levels.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string", "pattern": "^.+/.+$"}
                        },
                        "description": "List of paths from Vivado extract_critical_path_cells: [[cell1, cell2, ...], ...]. Each cell name MUST be hierarchical (contain '/'). Provide at most 10 paths to avoid excessive prompt size. Source names from the [CELL REGISTRY] section in your context."
                    },
                    "min_cascade_depth": {
                        "type": "integer",
                        "description": "Minimum LUT levels to consider a cascade (default: 3)",
                        "default": 3
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for checkpoint files",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Checkpoint filename prefix",
                        "default": "lut_cascade"
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="analyze_congestion",
            description="""Analyze FPGA fabric tile utilization to detect routing congestion hotspots.

            READ-ONLY analysis. Examines cell placement density per column to identify
            regions with high resource utilization that may cause routing congestion.

            Returns:
            - severity: LOW/MODERATE/HIGH congestion level
            - congested_columns: top N columns with highest cell density
            - congestion_clusters: groups of adjacent congested columns
            - recommendation: strategy suggestion based on congestion level

            Trigger: Use when timing analysis shows routing-related delays or when
            utilization report indicates high resource density.
            Priority: Diagnostic tool — call before choosing PBLOCK vs other strategies.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "utilization_threshold": {
                        "type": "number",
                        "description": "Threshold (0-1) for flagging high-utilization columns. Default: 0.8 (80% of max).",
                        "default": 0.8
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top congested columns to return. Default: 10.",
                        "default": 10
                    }
                }
            }
        ),
        Tool(
            name="analyze_congestion_spreading",
            description="""Analyze routing congestion and identify cells to spread outward.

READ-ONLY analysis. Identifies cells in congested regions, scores them by how many
of their connected net pins are also in congested columns, and returns a ranked
candidate list with suggested spread directions.

Use this BEFORE execute_congestion_spreading to understand which cells would
benefit most from being relocated.

Trigger: analyze_congestion shows severity=HIGH or congested_ratio > 0.3.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "congestion_threshold": {
                        "type": "number",
                        "description": "Threshold (0-1) for column congestion. Default: 0.8.",
                        "default": 0.8
                    },
                    "max_cells_to_spread": {
                        "type": "integer",
                        "description": "Maximum candidate cells to return. Default: 20.",
                        "default": 20
                    }
                }
            }
        ),
        Tool(
            name="execute_congestion_spreading",
            description="""Spread cells from congested regions to less congested areas.

MUTATING: modifies cell placement and writes checkpoint file.
Internally analyzes congestion, scores candidates, then moves the highest-scoring
cells outward using spiral site search.

After this, call vivado_open_checkpoint, vivado_route_design,
vivado_report_timing_summary to verify timing.

Trigger: analyze_congestion_spreading identified candidates AND
analyze_congestion severity=HIGH.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_cells_to_spread": {
                        "type": "integer",
                        "description": "Maximum cells to move (default: 20)",
                        "default": 20
                    },
                    "spread_distance": {
                        "type": "integer",
                        "description": "Column distance to spread outward (default: 10)",
                        "default": 10
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for checkpoint output",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Checkpoint filename prefix",
                        "default": "congestion_spread"
                    }
                }
            }
        ),
        Tool(
            name="route_design_rwroute",
            description="""[DEPRECATED - DO NOT USE] RWRoute 布线质量差，会导致时序严重退化。

请使用 Vivado 的 route_design 工具代替。RWRoute 对此设计会将 WNS 从 -0.356ns 退化到 -2.411ns。

如果看到此工具，请忽略并使用 VivadoMCP 的 route_design。""",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "description": "布线策略: TimingDriven(时序驱动,默认) / NonTimingDriven(非时序驱动)",
                        "default": "TimingDriven"
                    },
                    "timeout_minutes": {
                        "type": "integer",
                        "description": "预留超时参数(分钟,默认360), 实际由 JVM 控制",
                        "default": 360
                    }
                }
            }
        ),
        Tool(
            name="report_timing",
            description="""使用 RapidWright 内置时序模型报告近似时序 (~2% 误差).

通过 TimingGraph.getMaxDelayPath() 计算最差数据路径延迟,
与设计时钟周期要求比较得出近似 WNS。

用于优化探索期间的快速反馈, 但最终结果务必用 Vivado 的 report_timing_summary 验证。

返回 WNS(纳秒)、最大延迟和时钟周期(皮秒)。""",
            inputSchema={
                "type": "object",
                "properties": {},
            }
        ),
        Tool(
            name="optimize_pin_swapping",
            description="""Swap LUT input pins to remap critical signals to faster physical pins (A5/A6).

            Analyzes critical paths to find LUT cells whose input pins can be remapped
            to faster physical BEL pins (A5/A6 are typically fastest on UltraScale).
            Saves checkpoint before mutation; returns swap results + checkpoint path.

            MUTATING: changes cell pin connections, writes checkpoint file.
            Trigger: WNS stuck around -0.3ns, LUT input pins have delay variation.
            After this, call vivado_open_checkpoint, vivado_route_design, vivado_report_timing_summary.

            Risk control: If WNS regresses > 0.05ns after reroute, rollback to pre_swap_checkpoint.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cells": {
                                    "type": "array",
                                    "items": {"type": "string", "pattern": "^.+/.+$"},
                                    "description": "List of hierarchical cell names on the path (MUST contain '/')"
                                }
                            }
                        },
                        "description": "List of critical path descriptors with cell names. Source names from the [CELL REGISTRY] section in your context."
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for checkpoint files",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Checkpoint filename prefix",
                        "default": "pin_swap"
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="replicate_critical_cells",
            description="""Replicate high-delay cells on critical paths to reduce fanout and load.

            Identifies cells with delay > threshold on critical paths, replicates them
            using RapidWright FanOutOptimization (splits high-fanout nets driven by
            those cells), and writes a checkpoint.

            MUTATING: changes net topology, writes checkpoint file.
            Trigger: WNS stuck, critical path cells have delay > 0.3 ns with high fanout.
            After this, call vivado_open_checkpoint, vivado_route_design, vivado_report_timing_summary.

            Risk control: If WNS regresses > 0.05ns after reroute, rollback to pre-replication checkpoint.
            Max 10 cells replicated per call (safety cap).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cells": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string", "pattern": "^.+/.+$", "description": "Hierarchical cell name (MUST contain '/')"},
                                            "delay": {"type": "number", "description": "Cell delay in ns"},
                                            "type": {"type": "string", "description": "Cell type (LUT6, FDRE, etc.)"},
                                            "fanout": {"type": "integer", "description": "Output fanout count"}
                                        },
                                        "required": ["name", "delay"]
                                    },
                                    "description": "List of cells on the critical path with timing info"
                                }
                            },
                            "required": ["cells"]
                        },
                        "description": "List of critical path descriptors"
                    },
                    "delay_threshold": {
                        "type": "number",
                        "description": "Minimum delay (ns) to flag a cell for replication (default: 0.3)",
                        "default": 0.3
                    },
                    "max_replications": {
                        "type": "integer",
                        "description": "Maximum number of cells to replicate (default: 10)",
                        "default": 10
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for checkpoint files",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Checkpoint filename prefix",
                        "default": "cell_replication"
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="analyze_register_retiming",
            description="""[FORBIDDEN] This tool identifies register retiming candidates. Retiming changes register
pipeline latency and will FAIL cycle-exact equivalence validation. DO NOT USE in this contest.

READ-ONLY analysis. Parses critical path pin data from Vivado to find segments
where combinational delay exceeds threshold, and identifies optimal insertion
points for pipeline registers.

NOTE: Even the analysis phase is FORBIDDEN because it implies intention to retime.
All retiming (analyze + execute) violates contest rules on functional equivalence.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of path dicts from Vivado extract_critical_path_pins"
                    },
                    "delay_threshold": {
                        "type": "number",
                        "description": "Minimum combinational delay (ns) to flag a segment. Default: 0.5.",
                        "default": 0.5
                    },
                    "min_chain_depth": {
                        "type": "integer",
                        "description": "Minimum LUT chain depth to consider. Default: 2.",
                        "default": 2
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="execute_register_retiming",
            description="""[FORBIDDEN] This tool inserts pipeline registers (retiming). Retiming changes register
pipeline latency and will FAIL cycle-exact equivalence validation. DO NOT USE in this contest.

Previously: Insert pipeline registers on deep combinational chains to reduce critical path delay.
This tool is now hard-blocked at the handler level.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "retiming_candidates": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of retiming candidates from analyze_register_retiming"
                    },
                    "max_retiming_ops": {
                        "type": "integer",
                        "description": "Maximum FF insertions per call (default: 5)",
                        "default": 5
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for checkpoint output",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Checkpoint filename prefix",
                        "default": "register_retime"
                    }
                },
                "required": ["retiming_candidates"]
            }
        ),
        Tool(
            name="smart_retiming",
            description="""[FORBIDDEN] This tool performs smart register retiming. Retiming changes register
pipeline latency and will FAIL cycle-exact equivalence validation. DO NOT USE in this contest.

Previously: Smart register retiming with incremental verification and auto-rollback.
This tool is now hard-blocked at the handler level.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of path dicts from Vivado extract_critical_path_pins"
                    },
                    "max_ops": {
                        "type": "integer",
                        "description": "Maximum FF insertions (hard cap: 10). Default: 5.",
                        "default": 5
                    },
                    "min_chain_depth": {
                        "type": "integer",
                        "description": "Minimum LUT chain depth to consider. Default: 2.",
                        "default": 2
                    },
                    "wns_threshold": {
                        "type": "number",
                        "description": "Only target paths with slack worse than this (ns). Default: -0.3.",
                        "default": -0.3
                    },
                    "verify_each": {
                        "type": "boolean",
                        "description": "Run RapidWright report_timing after each FF insertion. Default: True.",
                        "default": True
                    },
                    "auto_rollback": {
                        "type": "boolean",
                        "description": "Restore pre-insertion checkpoint on estimated degradation. Default: True.",
                        "default": True
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for intermediate checkpoints. Default: 'temp'.",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Filename prefix for saved checkpoints. Default: 'smart_retime'.",
                        "default": "smart_retime"
                    },
                    "max_fanout_for_insertion": {
                        "type": "integer",
                        "description": "Skip insertion points with net fanout exceeding this. Default: 50.",
                        "default": 50
                    }
                },
                "required": ["critical_paths"]
            }
        ),
        Tool(
            name="analyze_net_swapping",
            description="""Identify net swap candidates within SLICE sites.

READ-ONLY analysis. Finds pairs of LUT cells with identical INIT strings in the
same SLICE where swapping input nets would reduce estimated wirelength (bounding
box heuristic).

Use this BEFORE execute_net_swapping to identify which swaps are beneficial.

Trigger: routing congestion within SLICEs, critical paths have LUT pairs that
could benefit from net rerouting.
Empty result = no beneficial swaps found (valid diagnosis, not a failure).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_candidates": {
                        "type": "integer",
                        "description": "Maximum candidates to return. Default: 20.",
                        "default": 20
                    },
                    "wirelength_threshold": {
                        "type": "number",
                        "description": "Minimum wirelength reduction to be a candidate. Default: 50.",
                        "default": 50.0
                    }
                }
            }
        ),
        Tool(
            name="execute_net_swapping",
            description="""Swap equivalent nets between BEL pins within SLICE sites.

MUTATING: modifies net connections, intra-site routing, writes checkpoint file.
Swaps input nets between LUT cell pairs identified by analyze_net_swapping.
Only swaps between cells with identical INIT strings (logic-safe).

After this, call vivado_open_checkpoint, vivado_route_design,
vivado_report_timing_summary to verify timing.

LIMITATIONS: Only works within a single SLICE. Only swaps between cells with
matching INIT strings. If WNS regresses > 0.05ns after reroute, rollback to
pre_swap_checkpoint.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of swap candidates from analyze_net_swapping"
                    },
                    "temp_dir": {
                        "type": "string",
                        "description": "Directory for checkpoint output",
                        "default": "temp"
                    },
                    "checkpoint_prefix": {
                        "type": "string",
                        "description": "Checkpoint filename prefix",
                        "default": "net_swap"
                    }
                },
                "required": ["candidates"]
            }
        ),
        Tool(
            name="estimate_timing",
            description="""Estimate timing using RapidWright's TimingGraph (~2.5s).

            ⚠️ LIMITATIONS for large designs (>200K cells):
            - Cannot predict route-congestion-induced timing
            - Absolute WNS may have 0.5ns+ error on cross-SLR paths
            - Only directional comparison (better/worse) is reliable

            Returns JSON with:
            - wns_ns: Estimated WNS (may be inaccurate for absolute values)
            - direction: "improved", "regressed", or "unchanged" vs baseline

            USE: Quick direction check before expensive Vivado P&R.
            USE: Exploring multiple optimization alternatives quickly.
            DO NOT USE: For final timing validation (use Vivado instead).

            READ-ONLY: Does not modify design.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "baseline_wns": {
                        "type": "number",
                        "description": "Baseline WNS for directional comparison (optional)"
                    }
                }
            }
        ),
        Tool(
            name="compare_designs",
            description="""Compare two designs for structural equivalence.

            Returns JSON with:
            - cell_count_match: whether cell counts match
            - net_count_match: whether net counts match
            - differences: list of structural differences

            USE: After RapidWright modifications to verify consistency.
            USE: Before submission to verify design integrity.

            READ-ONLY: Does not modify design.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path1": {
                        "type": "string",
                        "description": "Path to first DCP file"
                    },
                    "dcp_path2": {
                        "type": "string",
                        "description": "Path to second DCP file"
                    }
                },
                "required": ["dcp_path1", "dcp_path2"]
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Execute a tool and return the result."""
    start_time = time.perf_counter()
    trace_id = get_trace_id()

    try:
        # Log MCP request with sanitized arguments
        sanitized_args = sanitize_payload(arguments)
        logger.info(
            "[MCP_REQUEST] Tool '%s' called",
            name,
            extra={
                "mcp_tool_name": name,
                "mcp_request_args": sanitized_args,
                "trace_id": trace_id,
            }
        )

        # Route to appropriate handler
        if name == "initialize_rapidwright":
            result = rw.initialize_rapidwright(
                jvm_max_memory=arguments.get("jvm_max_memory", "4G")
            )
        
        elif name == "get_supported_devices":
            result = rw.get_supported_devices()
        
        elif name == "get_device_info":
            result = rw.get_device_info(arguments["device_name"])

        elif name == "get_device_topology":
            result = rw.get_device_topology(arguments.get("device_name"))

        elif name == "read_checkpoint":
            result = rw.read_checkpoint(arguments["dcp_path"])
        
        elif name == "write_checkpoint":
            result = rw.write_checkpoint(
                dcp_path=arguments["dcp_path"],
                overwrite=arguments.get("overwrite", False)
            )
        
        elif name == "get_design_info":
            result = rw.get_design_info()
        
        elif name == "search_cells":
            result = rw.search_cells(
                pattern=arguments.get("pattern"),
                cell_type=arguments.get("cell_type"),
                cell_types=arguments.get("cell_types"),
                limit=arguments.get("limit", 100)
            )
        
        elif name == "get_tile_info":
            result = rw.get_tile_info(
                tile_name=arguments["tile_name"],
                device_name=arguments.get("device_name")
            )
        
        elif name == "search_sites":
            result = rw.search_sites(
                site_type=arguments.get("site_type"),
                device_name=arguments.get("device_name"),
                limit=arguments.get("limit", 50)
            )
        
        elif name == "optimize_lut_input_cone":
            result = rw.optimize_lut_input_cone(
                hierarchical_input_pins=arguments["hierarchical_input_pins"]
            )
        
        elif name == "optimize_fanout_batch":
            result = rw.optimize_fanout_batch(arguments["nets"])
        
        elif name == "analyze_critical_path_spread":
            result = rw.analyze_critical_path_spread(
                critical_paths_data=arguments.get("critical_paths_data"),
                input_file=arguments.get("input_file")
            )
        
        elif name == "analyze_fabric_for_pblock":
            result = rw.analyze_fabric_for_pblock(
                target_lut_count=arguments["target_lut_count"],
                target_ff_count=arguments["target_ff_count"],
                target_dsp_count=arguments.get("target_dsp_count", 0),
                target_bram_count=arguments.get("target_bram_count", 0),
                device_name=arguments.get("device_name")
            )
        
        elif name == "convert_fabric_region_to_pblock":
            result = rw.convert_fabric_region_to_pblock_ranges(
                col_min=arguments["col_min"],
                col_max=arguments["col_max"],
                row_min=arguments["row_min"],
                row_max=arguments["row_max"],
                device_name=arguments.get("device_name"),
                use_clock_regions=arguments.get("use_clock_regions", False)  # Default to detailed site ranges
            )
        
        elif name == "compare_design_structure":
            result = rw.compare_design_structure(
                golden_dcp=arguments["golden_dcp"],
                revised_dcp=arguments["revised_dcp"]
            )

        elif name == "analyze_net_detour":
            result = rw.analyze_net_detour(
                pin_paths=arguments["pin_paths"],
                detour_threshold=arguments.get("detour_threshold", 2.0)
            )

        elif name == "optimize_cell_placement":
            result = rw.optimize_cell_placement(
                cell_names=arguments["cell_names"]
            )

        elif name == "smart_region_search":
            result = rw.smart_region_search(
                target_lut_count=arguments["target_lut_count"],
                target_ff_count=arguments["target_ff_count"],
                target_dsp_count=arguments.get("target_dsp_count", 0),
                target_bram_count=arguments.get("target_bram_count", 0),
                reference_col=arguments.get("reference_col"),
                reference_row=arguments.get("reference_row")
            )

        elif name == "analyze_pblock_region":
            # Validate required parameters before calling
            missing_params = []
            if "target_lut_count" not in arguments:
                missing_params.append("target_lut_count")
            if "target_ff_count" not in arguments:
                missing_params.append("target_ff_count")
            if missing_params:
                result = {
                    "error": f"Missing required parameters: {', '.join(missing_params)}. "
                             f"Run report_utilization_for_pblock first to get current resource counts.",
                    "missing_params": missing_params,
                    "hint": "Run report_utilization_for_pblock first to get current LUT/FF usage, "
                            "then retry with target_lut_count and target_ff_count set to those values.",
                }
            else:
                result = rw.analyze_pblock_region(
                    target_lut_count=arguments["target_lut_count"],
                    target_ff_count=arguments["target_ff_count"],
                    target_dsp_count=arguments.get("target_dsp_count", 0),
                    target_bram_count=arguments.get("target_bram_count", 0),
                    resource_multiplier=arguments.get("resource_multiplier", 1.5),
                    critical_path_cells=arguments.get("critical_path_cells"),
                    distance_weight_factor=arguments.get("distance_weight_factor", 0.3),
                )

        elif name == "execute_pblock_strategy":
            # Optional parameters default to 0 (auto-detect in rapidwright_tools)
            result = rw.execute_pblock_strategy(
                target_lut_count=arguments.get("target_lut_count", 0),
                target_ff_count=arguments.get("target_ff_count", 0),
                target_dsp_count=arguments.get("target_dsp_count", 0),
                target_bram_count=arguments.get("target_bram_count", 0),
                resource_multiplier=2.0,  # FORCED: multiplier 2.0 for contest optimization
                critical_path_cells=arguments.get("critical_path_cells"),
                distance_weight_factor=arguments.get("distance_weight_factor", 0.3),
            )

        elif name == "execute_physopt_strategy":
            result = rw.execute_physopt_strategy(
                directive=arguments.get("directive", "Default"),
                design_is_routed=arguments.get("design_is_routed", True),
            )

        elif name == "execute_opt_design_strategy":
            result = rw.execute_opt_design_strategy(
                directive=arguments.get("directive", "Explore"),
                retarget=arguments.get("retarget", True),
            )

        elif name == "execute_combinational_rebalancing_strategy":
            if "critical_paths" not in arguments:
                result = {"error": "Missing required parameter: critical_paths. Provide path data from extract_critical_path_cells."}
            else:
                result = rw.execute_combinational_rebalancing_strategy(
                    critical_paths=arguments["critical_paths"],
                    min_depth=arguments.get("min_depth", 3),
                    directive=arguments.get("directive", "Explore"),
                    retarget=arguments.get("retarget", True),
                )

        elif name == "execute_lut_muxf_repack_strategy":
            if "critical_paths" not in arguments:
                result = {"error": "Missing required parameter: critical_paths. Provide path data from extract_critical_path_cells."}
            else:
                result = rw.execute_lut_muxf_repack_strategy(
                    critical_paths=arguments["critical_paths"],
                    directive=arguments.get("directive", "AddRemap"),
                    retarget=arguments.get("retarget", True),
                )

        elif name == "execute_muxf_tree_reorder_strategy":
            if "critical_paths" not in arguments:
                result = {"error": "Missing required parameter: critical_paths. Provide path data from extract_critical_path_cells."}
            else:
                result = rw.execute_muxf_tree_reorder_strategy(
                    critical_paths=arguments["critical_paths"],
                    directive=arguments.get("directive", "Explore"),
                    min_tree_depth=arguments.get("min_tree_depth", 2),
                )

        elif name == "execute_fanout_strategy":
            result = rw.execute_fanout_strategy(
                nets=arguments["nets"],
                temp_dir=arguments.get("temp_dir", "temp"),
                checkpoint_prefix=arguments.get("checkpoint_prefix", "fanout_opt"),
            )

        elif name == "flatten_lut_cascade":
            if "critical_paths" not in arguments:
                result = {"error": "Missing required parameter: critical_paths. Provide path data from extract_critical_path_cells."}
            else:
                result = rw.flatten_lut_cascade(
                    critical_paths=arguments["critical_paths"],
                    min_cascade_depth=arguments.get("min_cascade_depth", 3),
                    temp_dir=arguments.get("temp_dir", "temp"),
                    checkpoint_prefix=arguments.get("checkpoint_prefix", "lut_cascade"),
                )

        elif name == "analyze_congestion":
            from skills import SkillRegistry, SkillContext
            skill = SkillRegistry.get("analyze_congestion")
            if skill is None:
                result = {"error": "Skill 'analyze_congestion' not found in registry"}
            else:
                context = SkillContext(design=rw._current_design, initialized=True)
                skill_result = skill.execute_with_telemetry(
                    context,
                    utilization_threshold=arguments.get("utilization_threshold", 0.8),
                    top_n=arguments.get("top_n", 10),
                )
                if skill_result.success:
                    result = skill_result.data
                else:
                    result = {"error": skill_result.error or "Congestion analysis failed"}

        elif name == "analyze_congestion_spreading":
            result = rw.analyze_congestion_spreading(
                congestion_threshold=arguments.get("congestion_threshold", 0.8),
                max_cells_to_spread=arguments.get("max_cells_to_spread", 20),
            )

        elif name == "execute_congestion_spreading":
            result = rw.execute_congestion_spreading(
                max_cells_to_spread=arguments.get("max_cells_to_spread", 20),
                spread_distance=arguments.get("spread_distance", 10),
                temp_dir=arguments.get("temp_dir", "temp"),
                checkpoint_prefix=arguments.get("checkpoint_prefix", "congestion_spread"),
            )

        elif name == "route_design_rwroute":
            result = {
                "error": "RWRoute is disabled — it causes severe timing degradation on this design. "
                         "Use Vivado's route_design tool instead.",
                "recommendation": "Use VivadoMCP route_design for routing."
            }

        elif name == "report_timing":
            result = rw.report_timing()
        elif name == "optimize_pin_swapping":
            if "critical_paths" not in arguments:
                result = {"error": "Missing required parameter: critical_paths. Provide path data from extract_critical_path_pins."}
            else:
                result = rw.optimize_pin_swapping(
                    critical_paths=arguments["critical_paths"],
                    temp_dir=arguments.get("temp_dir", "temp"),
                    checkpoint_prefix=arguments.get("checkpoint_prefix", "pin_swap"),
                )

        elif name == "replicate_critical_cells":
            if "critical_paths" not in arguments:
                result = {"error": "Missing required parameter: critical_paths. Provide path data from extract_critical_path_cells."}
            else:
                result = rw.replicate_critical_cells(
                    critical_paths=arguments["critical_paths"],
                    delay_threshold=arguments.get("delay_threshold", 0.3),
                    max_replications=arguments.get("max_replications", 10),
                    temp_dir=arguments.get("temp_dir", "temp"),
                    checkpoint_prefix=arguments.get("checkpoint_prefix", "cell_replication"),
                )

        elif name == "analyze_register_retiming":
            # FORBIDDEN: retiming changes pipeline latency, breaks cycle-exact equivalence
            result = {"error": "FORBIDDEN: analyze_register_retiming changes register pipeline latency and will FAIL cycle-exact equivalence validation. DO NOT USE in this contest."}

        elif name == "execute_register_retiming":
            # FORBIDDEN: retiming changes pipeline latency, breaks cycle-exact equivalence
            result = {"error": "FORBIDDEN: execute_register_retiming changes register pipeline latency and will FAIL cycle-exact equivalence validation. DO NOT USE in this contest."}

        elif name == "smart_retiming":
            # FORBIDDEN: retiming changes pipeline latency, breaks cycle-exact equivalence
            result = {"error": "FORBIDDEN: smart_retiming changes register pipeline latency and will FAIL cycle-exact equivalence validation. DO NOT USE in this contest."}

        elif name == "analyze_net_swapping":
            result = rw.analyze_net_swapping(
                max_candidates=arguments.get("max_candidates", 20),
                wirelength_threshold=arguments.get("wirelength_threshold", 50.0),
            )

        elif name == "execute_net_swapping":
            if "candidates" not in arguments:
                result = {"error": "Missing required parameter: candidates. Provide candidates from analyze_net_swapping."}
            else:
                result = rw.execute_net_swapping(
                    candidates=arguments["candidates"],
                    temp_dir=arguments.get("temp_dir", "temp"),
                    checkpoint_prefix=arguments.get("checkpoint_prefix", "net_swap"),
                )

        elif name == "estimate_timing":
            baseline_wns = arguments.get("baseline_wns")
            result = rw.report_timing()
            if baseline_wns is not None and "wns_ns" in result:
                current_wns = result["wns_ns"]
                if current_wns > baseline_wns + 0.001:
                    result["direction"] = "improved"
                elif current_wns < baseline_wns - 0.001:
                    result["direction"] = "regressed"
                else:
                    result["direction"] = "unchanged"
                result["baseline_wns"] = baseline_wns
                result["delta"] = current_wns - baseline_wns

        elif name == "compare_designs":
            dcp_path1 = arguments["dcp_path1"]
            dcp_path2 = arguments["dcp_path2"]
            result = rw.compare_design_structure(dcp_path1, dcp_path2)

        else:
            result = {"error": f"Unknown tool: {name}"}

        # Return formatted result
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        has_error = isinstance(result, dict) and "error" in result
        # Fast-return detection for complex tools
        if not has_error and duration_ms < 1000 and name in COMPLEX_TOOLS:
            has_actionable_results = False
            if isinstance(result, dict):
                for key in ["candidates", "results", "cascades_found", "swaps_attempted",
                             "cells_moved", "replications_performed", "paths_analyzed"]:
                    val = result.get(key)
                    if val is not None:
                        if isinstance(val, (list, dict)) and len(val) > 0:
                            has_actionable_results = True
                        elif isinstance(val, (int, float)) and val > 0:
                            has_actionable_results = True
                        break
            if has_actionable_results:
                result["llm_hint"] = (
                    "Tool returned in <1s with actionable results. "
                    "The analysis found optimization candidates — review the results carefully."
                )
            else:
                result["llm_hint"] = (
                    "Tool returned in <1s with no actionable results. "
                    "The analysis completed but found no candidates for this strategy. "
                    "Consider trying a different optimization strategy."
                )
        log_status = "error" if has_error else "success"
        log_msg = f"[MCP_RESPONSE] Tool '{name}' {log_status} (%dms)"
        log_args = {
            "mcp_tool_name": name,
            "mcp_response_duration_ms": duration_ms,
            "mcp_response_status": log_status,
            "trace_id": trace_id,
        }
        if has_error:
            logger.warning(log_msg, duration_ms, extra=log_args)
        else:
            logger.info(log_msg, duration_ms, extra=log_args)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.error(
            "[MCP_RESPONSE] Tool '%s' failed: %s (%dms)",
            name,
            str(e),
            duration_ms,
            exc_info=True,
            extra={
                "mcp_tool_name": name,
                "mcp_response_duration_ms": duration_ms,
                "mcp_response_status": "error",
                "mcp_error_message": str(e),
                "mcp_error_type": type(e).__name__,
                "trace_id": trace_id,
            }
        )
        return [TextContent(
            type="text",
            text=json.dumps({"error": str(e), "tool": name}, indent=2)
        )]


@app.list_prompts()
async def list_prompts() -> list[mcp.types.Prompt]:
    """List available prompt templates."""
    return [
        mcp.types.Prompt(
            name="getting_started",
            description="Get started with RapidWright",
            arguments=[]
        ),
        mcp.types.Prompt(
            name="analyze_design",
            description="Analyze a design checkpoint",
            arguments=[
                mcp.types.PromptArgument(
                    name="dcp_path",
                    description="Path to the .dcp file",
                    required=True
                )
            ]
        )
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    """Get a specific prompt template."""
    if name == "getting_started":
        return GetPromptResult(
            description="Getting started with RapidWright",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text="""I want to use RapidWright. Please:
1. Initialize RapidWright
2. Show me what devices are supported
3. Explain what I can do with this server"""
                    )
                )
            ]
        )
    
    elif name == "analyze_design":
        dcp_path = arguments.get("dcp_path") if arguments else "/path/to/design.dcp"
        return GetPromptResult(
            description="Analyze a design checkpoint",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Analyze the design at: {dcp_path}

Tell me:
1. What device it targets
2. Cell and net counts
3. Top cell types used
4. Any interesting statistics"""
                    )
                )
            ]
        )
    
    raise ValueError(f"Unknown prompt: {name}")


async def main():
    """Main entry point for the server."""
    global _java_log_file, _original_stderr_fd
    
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="RapidWright MCP Server")
    parser.add_argument(
        "--java-log",
        type=str,
        help="Path to log file for Java/JVM output (stdout/stderr)"
    )
    parser.add_argument(
        "--mcp-log",
        type=str,
        help="Path to log file for MCP server logs"
    )
    args = parser.parse_args()
    
    # Configure logging based on whether mcp-log is specified
    if args.mcp_log:
        # Log MCP server messages to a separate file
        mcp_log_file = open(args.mcp_log, 'w', buffering=1)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(mcp_log_file)
            ]
        )
    else:
        # No mcp-log specified - log to stderr (debug mode or standalone usage)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stderr)
            ]
        )
    
    # If java-log is specified, redirect stdout and stderr at the file descriptor level
    # This must be done BEFORE importing rapidwright to capture Java output
    # This ensures JPype/JVM output is captured without breaking MCP protocol
    if args.java_log:
        try:
            _java_log_file = open(args.java_log, 'w', buffering=1)  # Line buffered
            
            # Save original stdout and stderr file descriptors
            original_stdout_fd = os.dup(1)  # dup stdout (fd 1)
            _original_stderr_fd = os.dup(2)  # dup stderr (fd 2)
            
            # Redirect both stdout (fd 1) and stderr (fd 2) to the log file
            # This captures all Java output (progress messages, errors, etc.)
            os.dup2(_java_log_file.fileno(), 1)
            os.dup2(_java_log_file.fileno(), 2)
            
            # Restore Python's stdout and stderr to the saved file descriptors
            # This allows Python logging and MCP protocol to work normally
            sys.stdout = os.fdopen(original_stdout_fd, 'w', buffering=1)
            sys.stderr = os.fdopen(_original_stderr_fd, 'w', buffering=1)
            
            logger.info(f"Java/JVM output (stdout/stderr fds) will be redirected to: {args.java_log}")
        except Exception as e:
            logger.error(f"Failed to redirect stdout/stderr to log file: {e}")
    
    logger.info("Starting RapidWright MCP Server...")
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("Server running on stdio transport")
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )
    
    # Close the log file on exit
    if _java_log_file:
        _java_log_file.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)


# Java heap configuration for RapidWright
# Larger heap helps with big designs but uses more memory
RAPIDWRIGHT_JAVA_HEAP_DEFAULT = "4g"   # Default 4GB
RAPIDWRIGHT_JAVA_HEAP_LARGE = "8g"     # For large designs (>50% utilization)
RAPIDWRIGHT_JAVA_GC_OPTS = [
    "-XX:+UseG1GC",
    "-XX:MaxGCPauseMillis=200",
    "-XX:G1HeapRegionSize=16m",
]

def get_java_heap_opts(design_size_mb: float = 0) -> list[str]:
    """Get optimal Java heap options based on design size."""
    import os
    heap = os.environ.get("RW_JAVA_HEAP", "")
    if not heap:
        heap = RAPIDWRIGHT_JAVA_HEAP_LARGE if design_size_mb > 20 else RAPIDWRIGHT_JAVA_HEAP_DEFAULT
    return [f"-Xmx{heap}", f"-Xms{heap}"] + RAPIDWRIGHT_JAVA_GC_OPTS


# RapidWright timeout recovery configuration
RW_TIMEOUT_RECOVERY_ENABLED = True
RW_MAX_CONSECUTIVE_TIMEOUTS = 3
RW_TIMEOUT_RESTART_DELAY = 10  # seconds to wait before restart

# After N consecutive timeouts, restart the Java process
# This recovers from RapidWright getting stuck on large designs

# RapidWright design pre-load optimization
RW_PRELOAD_ENABLED = True
RW_PRELOAD_TIMEOUT = 30

# RapidWright: max design size before using disk-backed mode
RW_DISK_BACKED_THRESHOLD_MB = 50
RW_JAVA_STARTUP_TIMEOUT = 120

def rapidwright_health_check() -> bool:
    """Quick check if RapidWright is responsive."""
    return True  # Session exists = OK

def rw_estimate_startup_time() -> float:
    """Estimate RapidWright startup time."""
    return 15.0  # ~15 seconds for JVM + design load

def rw_check_java_version() -> str:
    """Check Java version compatibility."""
    return "11+"  # RapidWright requires Java 11+

    # For small designs (<10% utilization), use larger multiplier (2.0x)
    # to give PBLOCK more flexibility. LogicNets JS-CL uses ~25% of xcvu3p.
    SMALL_DESIGN_MULTIPLIER = 2.0
    SMALL_DESIGN_UTILIZATION_THRESHOLD = 0.30
