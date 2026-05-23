# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Critical Path Cell Replication Strategy Skill.

Replicates high-delay cells on critical paths to reduce fanout and load,
improving WNS. Uses RapidWright ECO tools (FanOutOptimization) to replicate
driver cells and redistribute loads to copies.
"""

import os
import logging

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill

logger = logging.getLogger(__name__)


def execute_cell_replication(
    design,
    critical_paths: list[dict],
    delay_threshold: float = 0.3,
    max_replications: int = 10,
    temp_dir: str = "temp",
    checkpoint_prefix: str = "cell_replication",
) -> dict:
    """Execute critical path cell replication and return results.

    Identifies high-delay cells on critical paths (delay > threshold),
    replicates them using RapidWright FanOutOptimization, and writes
    a checkpoint. The caller must re-route and verify timing in Vivado.

    Args:
        design: RapidWright Design object (mutated in-place)
        critical_paths: List of path dicts, each with:
            {"cells": [{"name": str, "delay": float, "type": str, "fanout": int}, ...]}
        delay_threshold: Minimum delay (ns) to flag a cell for replication
        max_replications: Maximum number of cells to replicate (safety cap)
        temp_dir: Directory for checkpoint output
        checkpoint_prefix: Checkpoint filename prefix

    Returns:
        dict with replication results, checkpoint path, and per-cell details
    """
    if design is None:
        return {"error": "Design not loaded"}

    if not critical_paths:
        return {
            "replications_performed": 0,
            "skipped": True,
            "message": "No critical paths provided",
        }

    # Step 1: Identify high-delay cells across all paths
    high_delay_cells = _identify_high_delay_cells(
        critical_paths, delay_threshold, max_replications
    )

    if not high_delay_cells:
        return {
            "replications_performed": 0,
            "skipped": True,
            "message": f"No cells with delay > {delay_threshold} ns found on critical paths",
            "delay_threshold": delay_threshold,
        }

    logger.info(
        f"Found {len(high_delay_cells)} high-delay cells for replication "
        f"(threshold={delay_threshold} ns)"
    )

    # Step 2: Replicate cells using FanOutOptimization
    replication_results = []
    successful_count = 0
    failed_count = 0

    for cell_info in high_delay_cells:
        cell_name = cell_info["name"]
        cell_fanout = cell_info.get("fanout", 0)
        cell_delay = cell_info.get("delay", 0.0)

        try:
            # Find all nets driven by this cell with fanout > 1
            nets_to_split = _find_nets_for_cell(design, cell_name, cell_fanout)

            if not nets_to_split:
                replication_results.append({
                    "cell_name": cell_name,
                    "delay": cell_delay,
                    "status": "skipped",
                    "message": "No splittable nets found for cell",
                })
                continue

            # Split each high-fanout net
            cell_success = True
            for net_info in nets_to_split:
                net_name = net_info["net_name"]
                fanout = net_info["fanout"]
                split_factor = max(2, min(4, fanout // 50))

                net = design.getNet(net_name)
                if net is None:
                    continue

                from com.xilinx.rapidwright.eco import FanOutOptimization
                FanOutOptimization.cutFanOutOfRoutedNet(design, net, split_factor)
                logger.info(
                    f"Split net '{net_name}' (fanout={fanout}) into {split_factor} parts "
                    f"for cell '{cell_name}'"
                )

            replication_results.append({
                "cell_name": cell_name,
                "delay": cell_delay,
                "nets_split": len(nets_to_split),
                "status": "success",
                "message": f"Replicated cell via {len(nets_to_split)} net splits",
            })
            successful_count += 1

        except Exception as e:
            logger.error(f"Error replicating cell {cell_name}: {e}")
            replication_results.append({
                "cell_name": cell_name,
                "delay": cell_delay,
                "status": "error",
                "message": str(e),
            })
            failed_count += 1

    # Step 3: Write checkpoint
    os.makedirs(temp_dir, exist_ok=True)
    checkpoint_path = os.path.join(
        temp_dir, f"{checkpoint_prefix}_post_replication.dcp"
    )

    checkpoint_error = None
    try:
        from rapidwright_tools import write_checkpoint
        ckpt_result = write_checkpoint(dcp_path=checkpoint_path, overwrite=True)
        if isinstance(ckpt_result, dict) and "error" in ckpt_result:
            checkpoint_error = ckpt_result["error"]
    except Exception as e:
        checkpoint_error = str(e)

    if checkpoint_error:
        return {
            "error": f"Checkpoint write failed: {checkpoint_error}",
            "checkpoint_path": checkpoint_path,
            "replications_performed": successful_count,
            "results": replication_results,
        }

    return {
        "replications_performed": successful_count,
        "failed_count": failed_count,
        "total_candidates": len(high_delay_cells),
        "delay_threshold": delay_threshold,
        "checkpoint_path": checkpoint_path,
        "results": replication_results,
    }


def _identify_high_delay_cells(
    critical_paths: list[dict],
    delay_threshold: float,
    max_replications: int,
) -> list[dict]:
    """Identify cells with delay above threshold across all critical paths.

    Deduplicates by cell name, keeping the highest delay occurrence.
    Returns sorted list (highest delay first), capped at max_replications.
    """
    seen = {}  # cell_name -> {name, delay, type, fanout}

    for path in critical_paths:
        cells = path.get("cells", [])
        for cell in cells:
            delay = cell.get("delay", 0.0)
            if delay < delay_threshold:
                continue
            name = cell.get("name", "")
            if not name:
                continue
            # Keep highest delay per cell
            if name not in seen or delay > seen[name]["delay"]:
                seen[name] = {
                    "name": name,
                    "delay": delay,
                    "type": cell.get("type", "unknown"),
                    "fanout": cell.get("fanout", 0),
                }

    # Sort by delay descending, cap at max
    ranked = sorted(seen.values(), key=lambda c: c["delay"], reverse=True)
    return ranked[:max_replications]


def _find_nets_for_cell(design, cell_name: str, hint_fanout: int) -> list[dict]:
    """Find nets driven by the given cell that have fanout > 1.

    Returns list of {"net_name": str, "fanout": int}.
    """
    results = []

    try:
        cell = design.getCell(cell_name)
        if cell is None:
            # Try searching by iterating (cell name may be hierarchical)
            for c in design.getCells():
                if str(c.getName()) == cell_name:
                    cell = c
                    break
        if cell is None:
            return results

        # Find output nets of this cell
        for pin in cell.getPinMappingsP2L().keySet():
            pin_str = str(pin)
            # Look for output pins (O, Q, etc.)
            if pin_str in ("O", "Q", "O5", "O6"):
                # Get the physical net connected to this pin
                site_inst = cell.getSiteInst()
                if site_inst is not None:
                    site_pin = site_inst.getSitePinInst(pin_str)
                    if site_pin is not None:
                        net = site_pin.getNet()
                        if net is not None:
                            fanout = net.getFanOut()
                            if fanout > 1:
                                results.append({
                                    "net_name": str(net.getName()),
                                    "fanout": fanout,
                                })
    except Exception as e:
        logger.warning(f"Error finding nets for cell {cell_name}: {e}")

    # If no nets found via pin mapping, try net iteration
    if not results and hint_fanout > 1:
        try:
            for net in design.getNets():
                net_name = str(net.getName())
                import re
                pattern = re.compile(re.escape(cell_name) + r'(?:_|$)')
                if pattern.search(net_name):
                    fanout = net.getFanOut()
                    if fanout > 1:
                        results.append({
                            "net_name": net_name,
                            "fanout": fanout,
                        })
        except Exception:
            pass

    return results


@skill(
    name="critical_path_cell_replication_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="Critical Path Cell Replication",
    description="Replicate high-delay cells on critical paths to reduce fanout/load. "
                "MUTATING. Side effects: net topology changes, checkpoint file written. "
                "Trigger: WNS stuck, critical path cells have delay > 0.3 ns with high fanout. "
                "After this, run vivado_open_checkpoint, vivado_route_design, vivado_report_timing_summary.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["net_topology", "checkpoint_file"],
    timeout_ms=300000,
    parameters=[
        ParameterSpec("critical_paths", list,
                      "List of path dicts: [{\"cells\": [{\"name\": str, \"delay\": float, "
                      "\"type\": str, \"fanout\": int}, ...]}, ...]"),
        ParameterSpec("delay_threshold", float,
                      "Minimum delay (ns) to flag a cell for replication", default=0.3),
        ParameterSpec("max_replications", int,
                      "Maximum number of cells to replicate", default=10),
        ParameterSpec("temp_dir", str,
                      "Directory for intermediate checkpoint", default="temp"),
        ParameterSpec("checkpoint_prefix", str,
                      "Checkpoint filename prefix", default="cell_replication"),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND",
                  "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class CriticalPathCellReplicationSkill(Skill):
    """Skill for replicating high-delay cells on critical paths."""

    def execute(self, context: SkillContext,
                critical_paths: list[dict],
                delay_threshold: float = 0.3,
                max_replications: int = 10,
                temp_dir: str = "temp",
                checkpoint_prefix: str = "cell_replication") -> SkillResult:
        try:
            result = execute_cell_replication(
                context.design,
                critical_paths,
                delay_threshold,
                max_replications,
                temp_dir,
                checkpoint_prefix,
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
        for i, path in enumerate(paths):
            if not isinstance(path, dict) or "cells" not in path:
                return False, f"critical_paths[{i}]: each entry must have a 'cells' key"
        return True, ""
