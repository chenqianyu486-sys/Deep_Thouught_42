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
    """Get {bel_pin_name: net_name} for a LUT cell's input pins (A1-A6).

    BEL pin names are position-independent (always A1-A6); the matching site
    pin name is <pos><digit> where <pos> is the LUT position derived from the
    BEL name (e.g. "A6LUT"->"A", "B6LUT"->"B"). Uses Cell.getSiteInst() +
    SitePinInst.getNet() (the verified RapidWright API) instead of the
    non-existent Cell.getNetFromSitePin() that previously threw AttributeError
    on every candidate (run-20260711_041512, 0 swaps).
    """
    pin_map = {}
    bel = cell.getBEL()
    if bel is None:
        return pin_map
    site_inst = cell.getSiteInst()
    if site_inst is None:
        return pin_map
    bel_name = str(bel.getName())
    pos_letter = bel_name[0] if (bel_name and bel_name[0] in "ABCDEFGH") else "A"
    for digit in "123456":
        try:
            site_pin = site_inst.getSitePinInst(f"{pos_letter}{digit}")
        except Exception:
            continue
        if site_pin is None:
            continue
        net = site_pin.getNet()
        if net is not None:
            pin_map[f"A{digit}"] = str(net.getName())
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

            # Pin selection: move the slowest occupied input's net onto a
            # faster FREE pin (A6 fastest, A1 slowest). This is a move, not
            # an exchange -- the free pin has no net, so the previous code's
            # net_fast = pin_map.get(fastest_free) was always None and every
            # candidate was silently skipped (0 swaps). The LUT INIT is
            # permuted to match the new pin assignment so logical equivalence
            # is preserved.
            fastest_free = None
            for pin in ["A6", "A5"]:
                if pin not in pin_map:
                    fastest_free = pin
                    break
            if fastest_free is None:
                # A6 and A5 both occupied - no faster free pin available.
                continue

            # Slowest occupied pin = highest delay rank (A1 slowest).
            slowest_pin = max(
                (p for p in pin_map if p != fastest_free),
                key=lambda p: _PIN_PRIORITY.get(p, 99),
                default=None,
            )
            if slowest_pin is None:
                continue

            net_slow_name = pin_map.get(slowest_pin)
            if net_slow_name is None:
                continue

            # Permute INIT first; skip the swap entirely if permutation is
            # invalid, rather than moving pins without updating the truth
            # table (which would break logical equivalence).
            init_str = _get_lut_init(cell)
            new_init = _permute_lut_init(init_str, slowest_pin, fastest_free, cell_type)
            if new_init is None:
                continue

            # Execute the pin move via RapidWright ECO. Connect the net to the
            # fast pin first, then release the slow pin, so the net is never
            # left dangling if a step fails mid-move.
            success = False
            try:
                bel = cell.getBEL()
                bel_pin_slow = bel.getPin(slowest_pin)
                bel_pin_fast = bel.getPin(fastest_free)
                site_inst = cell.getSiteInst()
                net_obj = design.getNet(net_slow_name)
                if (bel_pin_slow is None or bel_pin_fast is None
                        or site_inst is None or net_obj is None):
                    continue
                design.connectPin(site_inst, bel_pin_fast, net_obj)
                design.disconnectPin(site_inst, bel_pin_slow)
                cell.setProperty("INIT", new_init)
                success = True
            except Exception as e:
                logger.debug(f"Pin swap failed for {cell_name}: {e}")

            swaps_attempted += 1
            if success:
                swaps_successful += 1
                swap_details.append({
                    "cell": cell_name,
                    "swapped": f"{slowest_pin}->{fastest_free}",
                    "net_moved": net_slow_name,
                    "status": "success",
                })
            else:
                swap_details.append({
                    "cell": cell_name,
                    "status": "failed",
                    "message": "Could not perform pin swap",
                })

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
