# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
LUT Cascade Flattening Strategy Skill.

Identifies LUT cascades (>3 levels) on critical paths and flattens them
using RapidWright LUTInputConeOpt to reduce logic depth.
"""

import logging
import os

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill

logger = logging.getLogger(__name__)


# LUT type names in Xilinx UltraScale/UltraScale+
_LUT_TYPES = {"LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6"}


def _is_lut_cell(cell) -> bool:
    """Check if a cell is a LUT type."""
    try:
        return str(cell.getType()) in _LUT_TYPES
    except Exception:
        return False


def _find_lut_cascades(design, critical_paths: list[list[str]],
                       min_depth: int = 3) -> list[dict]:
    """Analyze critical paths to find LUT cascade chains.

    Args:
        design: RapidWright Design object
        critical_paths: List of paths, each path is a list of cell names
        min_depth: Minimum cascade depth to report (default 3)

    Returns:
        List of cascade dicts with pin_name, depth, cell_names
    """
    if design is None:
        return []

    netlist = design.getNetlist()
    cascades = []

    for path_idx, path in enumerate(critical_paths):
        # Walk path and find consecutive LUT sequences
        current_chain = []
        for cell_name in path:
            try:
                cell = design.getCell(cell_name)
                if cell is None:
                    # Try netlist lookup
                    cell = netlist.getCell(cell_name)
            except Exception:
                cell = None

            if cell is not None and _is_lut_cell(cell):
                current_chain.append(cell_name)
            else:
                # Non-LUT cell breaks the chain
                if len(current_chain) > min_depth:
                    # Found a cascade — use the input pin of the last LUT
                    # (the one feeding into the destination)
                    cascades.append({
                        "path_index": path_idx,
                        "depth": len(current_chain),
                        "cell_names": list(current_chain),
                        # The optimization target: first LUT's input drives the cascade
                        "source_cell": current_chain[0],
                    })
                current_chain = []

        # Handle chain ending at path boundary
        if len(current_chain) > min_depth:
            cascades.append({
                "path_index": path_idx,
                "depth": len(current_chain),
                "cell_names": list(current_chain),
                "source_cell": current_chain[0],
            })

    return cascades


def _get_lut_input_pin(design, cell_name: str) -> str | None:
    """Get a hierarchical input pin name for a LUT cell.

    Returns the first input pin in hierarchical format for LUTInputConeOpt.
    """
    try:
        cell = design.getCell(cell_name)
        if cell is None:
            return None

        # Get hierarchical name
        hier_cell = cell.getName()  # Already hierarchical in RapidWright

        # Find first input port instance
        for pip in cell.getPhysicalCell().getSite().getSitePins():
            pass  # Not the right approach

        # Use netlist to find hierarchical port instances
        netlist = design.getNetlist()
        hcell = netlist.getHierCellFromName(cell_name)
        if hcell is None:
            return None

        for port_inst in hcell.getPortInsts():
            if port_inst.isInput():
                return port_inst.getHierarchicalNetlistName()

    except Exception as e:
        logger.debug("Failed to get input pin for %s: %s", cell_name, e)

    return None


def execute_lut_cascade_flattening(
    design,
    critical_paths: list[list[str]],
    min_cascade_depth: int = 3,
    temp_dir: str = "temp",
    checkpoint_prefix: str = "lut_cascade",
) -> dict:
    """Execute LUT cascade flattening optimization.

    Args:
        design: RapidWright Design object (mutated in-place)
        critical_paths: List of paths, each path is a list of cell names
        min_cascade_depth: Minimum LUT levels to consider a cascade (default 3)
        temp_dir: Directory for checkpoint
        checkpoint_prefix: Checkpoint filename prefix

    Returns:
        dict with cascades_found, optimized_count, checkpoint_path, results
    """
    if design is None:
        return {"error": "Design not loaded"}

    if not critical_paths:
        return {
            "cascades_found": 0,
            "skipped": True,
            "message": "No critical paths provided",
        }

    # Step 1: Find LUT cascades
    cascades = _find_lut_cascades(design, critical_paths, min_cascade_depth)

    if not cascades:
        return {
            "cascades_found": 0,
            "skipped": True,
            "message": f"No LUT cascades deeper than {min_cascade_depth} found",
        }

    # Step 2: Save checkpoint before mutation
    os.makedirs(temp_dir, exist_ok=True)
    ckpt_path = os.path.join(temp_dir, f"{checkpoint_prefix}_pre_flatten.dcp")
    try:
        from rapidwright_tools import write_checkpoint
        ckpt_result = write_checkpoint(dcp_path=ckpt_path, overwrite=True)
        if isinstance(ckpt_result, dict) and "error" in ckpt_result:
            return {"error": f"Checkpoint save failed: {ckpt_result['error']}"}
    except Exception as e:
        return {"error": f"Checkpoint save failed: {e}"}

    # Step 3: Collect input pins for optimization
    # For each cascade, find the output pin of the last LUT (the one on the critical path)
    # and optimize that input cone
    pins_to_optimize = []
    for cascade in cascades:
        last_lut = cascade["cell_names"][-1]
        pin = _get_lut_input_pin(design, last_lut)
        if pin:
            pins_to_optimize.append(pin)
        else:
            logger.warning("Could not resolve input pin for cascade cell %s", last_lut)

    if not pins_to_optimize:
        return {
            "cascades_found": len(cascades),
            "optimized_count": 0,
            "checkpoint_path": ckpt_path,
            "message": "Found cascades but could not resolve any input pins",
            "cascades": cascades,
        }

    # Step 4: Optimize using LUTInputConeOpt
    try:
        from rapidwright_tools import optimize_lut_input_cone
        opt_result = optimize_lut_input_cone(pins_to_optimize)
        if isinstance(opt_result, dict) and "error" in opt_result:
            return {
                "error": f"LUT optimization failed: {opt_result['error']}",
                "cascades_found": len(cascades),
                "checkpoint_path": ckpt_path,
            }
    except Exception as e:
        return {"error": f"LUT optimization failed: {e}"}

    optimized_count = opt_result.get("optimized_count", 0)

    # Step 5: Write post-flatten checkpoint
    post_ckpt_path = os.path.join(temp_dir, f"{checkpoint_prefix}_post_flatten.dcp")
    try:
        from rapidwright_tools import write_checkpoint
        write_checkpoint(dcp_path=post_ckpt_path, overwrite=True)
    except Exception as e:
        logger.warning("Post-flatten checkpoint write failed: %s", e)
        post_ckpt_path = None

    return {
        "cascades_found": len(cascades),
        "pins_submitted": len(pins_to_optimize),
        "optimized_count": optimized_count,
        "pre_checkpoint_path": ckpt_path,
        "post_checkpoint_path": post_ckpt_path,
        "cascades": [
            {"path_index": c["path_index"], "depth": c["depth"],
             "cells": c["cell_names"]}
            for c in cascades
        ],
        "per_pin_results": opt_result.get("results", []),
    }


@skill(
    name="lut_cascade_flattening",
    namespace="optimization",
    version="1.0.0",
    display_name="LUT Cascade Flattening",
    description="Identify LUT cascades (>3 levels) on critical paths and flatten them "
                "using RapidWright LUTInputConeOpt. MUTATING. Side effects: LUT merging, "
                "checkpoint files written. "
                "Trigger: Critical paths have >3 LUT levels in series (logic depth bottleneck). "
                "LIMITATIONS: Not suitable for neural network / wide-datapath designs where "
                "logic cones exceed 6-input LUT physical limit.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["lut_merging", "checkpoint_file"],
    timeout_ms=300000,
    parameters=[
        ParameterSpec("critical_paths", list,
                      "List of paths from Vivado extract_critical_path_cells: "
                      "[[cell1, cell2, ...], ...]"),
        ParameterSpec("min_cascade_depth", int,
                      "Minimum LUT levels to consider a cascade", default=3),
        ParameterSpec("temp_dir", str,
                      "Directory for checkpoint files", default="temp"),
        ParameterSpec("checkpoint_prefix", str,
                      "Checkpoint filename prefix", default="lut_cascade"),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND",
                  "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class LutCascadeFlatteningSkill(Skill):
    """Skill for LUT cascade flattening optimization."""

    def execute(self, context: SkillContext,
                critical_paths: list[list[str]],
                min_cascade_depth: int = 3,
                temp_dir: str = "temp",
                checkpoint_prefix: str = "lut_cascade") -> SkillResult:
        try:
            result = execute_lut_cascade_flattening(
                context.design, critical_paths, min_cascade_depth,
                temp_dir, checkpoint_prefix,
            )
            if "error" in result:
                return SkillResult(success=False, data=result, error=result["error"])
            return SkillResult(success=True, data=result)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "critical_paths" not in kwargs:
            return False, "critical_paths is required"
        paths = kwargs["critical_paths"]
        if not isinstance(paths, list) or len(paths) == 0:
            return False, "critical_paths must be a non-empty list"
        return True, ""
