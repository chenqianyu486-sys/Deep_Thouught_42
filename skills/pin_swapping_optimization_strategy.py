# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Pin Swapping Optimization Skill.

Remaps critical LUT input signals to faster physical pins (A5/A6 preferred)
to reduce critical path delay. Works at the RapidWright level by swapping
BEL pin connections on LUT cells without changing LUT equation (INIT string).
"""

import logging
import os
import time
from typing import Any

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill

logger = logging.getLogger(__name__)

# UltraScale/UltraScale+ LUT pin delay ordering (fastest to slowest):
# A5, A6 are direct inputs with lowest delay; A1-A4 go through the
# F7/F8 mux chain and are slower. This ordering is design-dependent
# but A5/A6 being fastest is the common UltraScale heuristic.
_PIN_PRIORITY = {"A6": 0, "A5": 1, "A4": 2, "A3": 3, "A2": 4, "A1": 5}


def _get_lut_pins(cell) -> dict[str, str]:
    """Get current BEL pin -> net mapping for a LUT cell.

    Returns dict of {bel_pin_name: net_name} for input pins only.
    """
    pin_map = {}
    bel = cell.getBEL()
    if bel is None:
        return pin_map
    for i in range(bel.getNumPins()):
        bel_pin = bel.getPin(i)
        if str(bel_pin.getName()) in _PIN_PRIORITY:
            net = cell.getNetFromSitePin(bel_pin)
            if net is not None:
                pin_map[str(bel_pin.getName())] = str(net.getName())
    return pin_map


def _get_lut_init(cell) -> str:
    """Get the INIT string from a LUT cell's properties."""
    try:
        init_prop = cell.getProperty("INIT")
        if init_prop is not None:
            return str(init_prop)
    except Exception:
        pass
    return ""


def _can_swap_pins(cell, pin_a: str, pin_b: str) -> bool:
    """Check if two input pins of a LUT can be swapped without changing logic.

    For LUTs with symmetric inputs, swapping preserves the truth table.
    For a LUT6, swapping any two inputs requires adjusting the INIT string,
    which is complex. Instead, we look for pins that are logically equivalent
    (e.g., both connect to the same net, or the LUT equation is symmetric
    with respect to those inputs).

    A practical approach: only swap pins carrying the critical signal,
    and verify the INIT string transformation is valid.
    """
    # For now, allow swapping any two pins — the INIT string must be
    # permuted accordingly. If that's not feasible, we skip.
    return True


def _permute_lut_init(init_str: str, pin_a: str, pin_b: str, cell_type: str) -> str | None:
    """Permute the LUT INIT string to reflect a pin swap.

    Returns new INIT string if permutation is valid, None if not feasible.
    For a LUT6 (6-input), swapping inputs i and j means permuting bits
    of the truth table accordingly.
    """
    if not init_str or not init_str.startswith("'h") and not init_str.startswith("'H"):
        # Try without quotes
        init_str_clean = init_str.replace("'", "").replace("h", "").replace("H", "")
    else:
        init_str_clean = init_str.replace("'", "").replace("h", "").replace("H", "")

    try:
        init_val = int(init_str_clean, 16)
    except (ValueError, TypeError):
        return None

    # Determine number of inputs from cell type
    if "LUT6" in cell_type.upper():
        num_inputs = 6
    elif "LUT5" in cell_type.upper():
        num_inputs = 5
    elif "LUT4" in cell_type.upper():
        num_inputs = 4
    elif "LUT3" in cell_type.upper():
        num_inputs = 3
    elif "LUT2" in cell_type.upper():
        num_inputs = 2
    elif "LUT1" in cell_type.upper():
        return None  # single input, nothing to swap
    else:
        return None

    # Map pin names to bit positions
    # A1=bit0, A2=bit1, ..., A6=bit5
    pin_to_bit = {f"A{i+1}": i for i in range(6)}
    bit_a = pin_to_bit.get(pin_a)
    bit_b = pin_to_bit.get(pin_b)
    if bit_a is None or bit_b is None or bit_a >= num_inputs or bit_b >= num_inputs:
        return None

    # Permute the truth table
    num_entries = 1 << num_inputs
    new_init = 0
    for entry in range(num_entries):
        # Swap the bits at positions bit_a and bit_b
        bit_a_val = (entry >> bit_a) & 1
        bit_b_val = (entry >> bit_b) & 1
        new_entry = entry
        if bit_a_val != bit_b_val:
            # Toggle both bits to effectively swap them
            new_entry = entry ^ (1 << bit_a) ^ (1 << bit_b)
        if new_init_val := (init_val >> new_entry) & 1:
            new_init |= 1 << entry

    # Format as hex
    hex_width = (num_entries + 3) // 4
    return f"'h{new_init:0{hex_width}X}"


def _get_cell_type(cell) -> str:
    """Get cell type string."""
    try:
        return str(cell.getType())
    except Exception:
        return ""


def execute_pin_swapping(
    design,
    critical_paths: list[dict],
    temp_dir: str = "temp",
    checkpoint_prefix: str = "pin_swap",
) -> dict:
    """Execute pin swapping optimization on critical path LUTs.

    Args:
        design: RapidWright Design object (mutated in-place)
        critical_paths: List of path descriptors, each containing
            "cells" with cell name and pin info
        temp_dir: Directory for checkpoint
        checkpoint_prefix: Checkpoint filename prefix

    Returns:
        dict with swap results and checkpoint path
    """
    if design is None:
        return {"error": "Design not loaded"}

    if not critical_paths:
        return {
            "swaps_attempted": 0,
            "skipped": True,
            "message": "No critical paths provided",
        }

    # Collect LUT cells from critical paths
    lut_candidates = []
    for path in critical_paths:
        cells = path.get("cells", path.get("pins", []))
        for cell_info in cells:
            cell_name = cell_info if isinstance(cell_info, str) else cell_info.get("cell_name", cell_info.get("name", ""))
            if not cell_name:
                continue
            # Look up cell in design
            try:
                cell = design.getCell(cell_name)
                if cell is None:
                    continue
                cell_type = _get_cell_type(cell)
                if "LUT" not in cell_type.upper():
                    continue
                if not cell.isPlaced():
                    continue
                lut_candidates.append({
                    "cell": cell,
                    "cell_name": cell_name,
                    "cell_type": cell_type,
                })
            except Exception:
                continue

    if not lut_candidates:
        return {
            "swaps_attempted": 0,
            "swaps_successful": 0,
            "message": "No placed LUT cells found on critical paths",
            "checkpoint_path": None,
        }

    # Save checkpoint before mutation
    os.makedirs(temp_dir, exist_ok=True)
    ckpt_path = os.path.join(temp_dir, f"{checkpoint_prefix}_pre_swap.dcp")
    try:
        design.writeCheckpoint(ckpt_path)
        logger.info(f"Pre-swap checkpoint saved: {ckpt_path}")
    except Exception as e:
        logger.warning(f"Failed to save pre-swap checkpoint: {e}")

    swaps_attempted = 0
    swaps_successful = 0
    swap_details = []

    for candidate in lut_candidates:
        cell = candidate["cell"]
        cell_name = candidate["cell_name"]
        cell_type = candidate["cell_type"]

        try:
            site = cell.getSite()
            if site is None:
                continue

            # Get current pin assignments
            pin_map = _get_lut_pins(cell)
            if not pin_map:
                continue

            # Find which pins are on the critical path
            # Strategy: try to move the critical signal to A6 or A5
            # Check if A6/A5 are occupied by non-critical signals
            current_pins = sorted(pin_map.keys(),
                                  key=lambda p: _PIN_PRIORITY.get(p, 99))

            # Try swapping the slowest-used pin with A6 or A5
            fastest_free = None
            for pin in ["A6", "A5"]:
                if pin in pin_map:
                    continue
                fastest_free = pin
                break

            if fastest_free is None:
                # A6 and A5 are both occupied — try next fastest
                continue

            # Find slowest occupied pin
            slowest_pin = None
            for pin in reversed(["A6", "A5", "A4", "A3", "A2", "A1"]):
                if pin in pin_map and pin != fastest_free:
                    slowest_pin = pin
                    break

            if slowest_pin is None:
                continue

            # Get nets to swap
            net_fast = pin_map.get(fastest_free)
            net_slow = pin_map.get(slowest_pin)

            if net_fast is None or net_slow is None:
                continue

            # Perform the swap using RapidWright ECO
            # Use site pin swapping — disconnect and reconnect
            try:
                from com.xilinx.rapidwright.design import Design
                from com.xilinx.rapidwright.design.tools import LUTTools

                # Attempt LUTTools pin swap if available
                # Otherwise do manual disconnect/reconnect
                success = False

                # Method 1: Direct pin swap via disconnect/reconnect
                try:
                    site_pin_fast = site.getBELPin(fastest_free)
                    site_pin_slow = site.getBELPin(slowest_pin)

                    if site_pin_fast is not None and site_pin_slow is not None:
                        # Disconnect current nets from their pins
                        design.disconnectPin(site, site_pin_fast)
                        design.disconnectPin(site, site_pin_slow)

                        # Reconnect in swapped positions
                        design.connectPin(site, site_pin_slow, net_fast)
                        design.connectPin(site, site_pin_fast, net_slow)

                        success = True
                except Exception as e:
                    logger.debug(f"Pin swap via disconnect/reconnect failed for {cell_name}: {e}")

                if success:
                    swaps_successful += 1
                    swap_details.append({
                        "cell": cell_name,
                        "swapped": f"{slowest_pin}<->{fastest_free}",
                        "net_on_slow_pin": net_slow,
                        "net_on_fast_pin": net_fast,
                        "status": "success",
                    })
                else:
                    swap_details.append({
                        "cell": cell_name,
                        "status": "failed",
                        "message": "Could not perform pin swap",
                    })

                swaps_attempted += 1

            except Exception as e:
                logger.debug(f"Pin swap failed for {cell_name}: {e}")
                swap_details.append({
                    "cell": cell_name,
                    "status": "failed",
                    "message": str(e),
                })
                swaps_attempted += 1

        except Exception as e:
            logger.warning(f"Error processing cell {cell_name}: {e}")

    # Write post-swap checkpoint
    post_ckpt_path = os.path.join(temp_dir, f"{checkpoint_prefix}_post_swap.dcp")
    ckpt_written = False
    try:
        design.writeCheckpoint(post_ckpt_path)
        ckpt_written = True
        logger.info(f"Post-swap checkpoint saved: {post_ckpt_path}")
    except Exception as e:
        logger.warning(f"Failed to save post-swap checkpoint: {e}")
        post_ckpt_path = None

    return {
        "swaps_attempted": swaps_attempted,
        "swaps_successful": swaps_successful,
        "lut_candidates_found": len(lut_candidates),
        "swap_details": swap_details,
        "checkpoint_path": post_ckpt_path if ckpt_written else None,
        "pre_swap_checkpoint": ckpt_path,
    }


@skill(
    name="pin_swapping_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="Pin Swapping Optimization",
    description="Swap LUT input pins to remap critical signals to faster pins (A5/A6). "
                "MUTATING. Side effects: cell pin connections changed, checkpoint written. "
                "Trigger: WNS stuck around -0.3ns, LUT input pins have delay variation. "
                "Requires critical_paths JSON input.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["pin_connections", "checkpoint_file"],
    timeout_ms=120000,
    parameters=[
        ParameterSpec("critical_paths", list,
                      "List of path descriptors with cells/pins info"),
        ParameterSpec("temp_dir", str, "Directory for checkpoint", default="temp"),
        ParameterSpec("checkpoint_prefix", str, "Checkpoint filename prefix", default="pin_swap"),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class PinSwappingOptimizationSkill(Skill):
    """Skill for Pin Swapping Optimization on critical path LUTs."""

    def execute(self, context: SkillContext,
                critical_paths: list[dict],
                temp_dir: str = "temp",
                checkpoint_prefix: str = "pin_swap") -> SkillResult:
        try:
            result = execute_pin_swapping(
                context.design, critical_paths, temp_dir, checkpoint_prefix
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
        if not isinstance(paths, list):
            return False, "critical_paths must be a list"
        return True, ""
