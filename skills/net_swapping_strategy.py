# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Net Swapping Strategy Skill.

Two-phase skill for swapping equivalent nets between BEL pins within a SLICE
to reduce routing congestion:
1. Analyze: Identify net swap candidates within SLICE sites
2. Execute: Perform the swaps using RapidWright ECO APIs

Unlike pin_swapping_optimization_strategy (which swaps pins on the SAME cell),
this skill swaps nets between DIFFERENT cells within the same SLICE. Only swaps
between cells with identical INIT strings are considered to preserve logic.
"""

import logging
import os
from typing import Any

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill

logger = logging.getLogger(__name__)

# LUT input BEL pin names
_LUT_INPUT_PINS = ["A1", "A2", "A3", "A4", "A5", "A6"]
# Skip GND/VCC constant nets
_CONST_NET_NAMES = {"GLOBAL_LOGIC0", "GLOBAL_LOGIC1", "GND", "VCC"}


def _get_cell_init(cell) -> str:
    """Get the INIT string from a LUT cell."""
    try:
        init_prop = cell.getProperty("INIT")
        if init_prop is not None:
            return str(init_prop).strip()
    except Exception:
        pass
    return ""


def _get_lut_input_pin_map(cell) -> dict[str, str]:
    """Get BEL pin name -> net name mapping for a LUT cell's input pins.

    Returns dict like {"A1": "net_foo", "A2": "net_bar", ...}
    Only includes pins that have a net connected. BEL pin names are
    position-independent (A1-A6); the matching site pin is <pos><digit>
    where <pos> is the LUT position from the BEL name (e.g. "B6LUT"->"B").
    Uses SitePinInst.getNet() (verified API) instead of the non-existent
    Cell.getNetFromSitePin().
    """
    pin_map = {}
    site_inst = cell.getSiteInst()
    if site_inst is None:
        return pin_map
    bel = cell.getBEL()
    if bel is None:
        return pin_map
    bel_name = str(bel.getName())
    pos_letter = bel_name[0] if (bel_name and bel_name[0] in "ABCDEFGH") else "A"

    for pin_name in _LUT_INPUT_PINS:
        digit = pin_name[1]  # "A1" -> "1"
        try:
            site_pin = site_inst.getSitePinInst(f"{pos_letter}{digit}")
        except Exception:
            continue
        if site_pin is None:
            continue
        net = site_pin.getNet()
        if net is not None:
            pin_map[pin_name] = str(net.getName())
    return pin_map


def _get_all_pin_nets(net) -> list[tuple[int, int]]:
    """Get all (column, row) positions of a net's pins for bounding box estimation."""
    positions = []
    try:
        for pin in net.getPins():
            tile = pin.getTile()
            if tile is not None:
                positions.append((tile.getColumn(), tile.getRow()))
    except Exception:
        pass
    return positions


def _estimate_wirelength_reduction(
    net_a_name: str, net_b_name: str,
    site_col: int, site_row: int,
    design,
) -> float:
    """Estimate wirelength reduction from swapping two nets at a SLICE location.

    Uses Manhattan distance bounding box heuristic:
    - Compute average pin distance of net_a to the SLICE location
    - Compute average pin distance of net_b to the SLICE location
    - Estimate how distances change if nets are swapped

    Returns positive value if swapping reduces estimated wirelength.
    """
    try:
        net_a = design.getNet(net_a_name)
        net_b = design.getNet(net_b_name)
        if net_a is None or net_b is None:
            return 0.0

        pins_a = _get_all_pin_nets(net_a)
        pins_b = _get_all_pin_nets(net_b)

        if not pins_a or not pins_b:
            return 0.0

        # Average Manhattan distance from net pins to the SLICE
        avg_dist_a = sum(abs(c - site_col) + abs(r - site_row) for c, r in pins_a) / len(pins_a)
        avg_dist_b = sum(abs(c - site_col) + abs(r - site_row) for c, r in pins_b) / len(pins_b)

        # Heuristic: if net_a's pins are far from this SLICE and net_b's are close,
        # swapping could help. The reduction is proportional to the distance difference.
        reduction = avg_dist_a - avg_dist_b
        return reduction

    except Exception:
        return 0.0


def _is_swap_feasible(cell_i, cell_j, pin_a: str, pin_b: str) -> bool:
    """Check if swapping net from cell_i.pin_a with net from cell_j.pin_b is safe.

    Feasibility constraints:
    1. Both cells must be LUT type
    2. Both cells must have the same INIT string (ensures logic equivalence)
    3. Neither net should be GND/VCC
    """
    # Check cell types
    type_i = str(cell_i.getType()) if hasattr(cell_i, "getType") else ""
    type_j = str(cell_j.getType()) if hasattr(cell_j, "getType") else ""

    if "LUT" not in type_i.upper() or "LUT" not in type_j.upper():
        return False

    # Check INIT strings match
    init_i = _get_cell_init(cell_i)
    init_j = _get_cell_init(cell_j)
    if not init_i or not init_j:
        return False
    if init_i != init_j:
        return False

    return True


def _find_lut_cells_in_site(site_inst) -> list[dict]:
    """Enumerate all placed LUT cells in a site instance.

    Returns list of dicts with cell, bel_name, pin_net_map.
    """
    cells = []
    try:
        for cell in site_inst.getCells():
            if not cell.isPlaced():
                continue
            cell_type = str(cell.getType()) if hasattr(cell, "getType") else ""
            if "LUT" not in cell_type.upper():
                continue
            bel_name = str(cell.getBELName()) if hasattr(cell, "getBELName") else ""
            pin_map = _get_lut_input_pin_map(cell)
            if pin_map:
                cells.append({
                    "cell": cell,
                    "cell_name": str(cell.getName()),
                    "cell_type": cell_type,
                    "bel_name": bel_name,
                    "pin_net_map": pin_map,
                    "init": _get_cell_init(cell),
                })
    except Exception as e:
        logger.debug(f"Error enumerating LUT cells in site: {e}")
    return cells


def analyze_net_swapping(
    design,
    max_candidates: int = 20,
    wirelength_threshold: float = 50.0,
) -> dict:
    """Analyze design to find net swap candidates within SLICE sites.

    READ-ONLY: does not modify the design.

    For each SLICE site, examines pairs of LUT cells with identical INIT strings
    and identifies pin swaps that would reduce estimated wirelength (bounding box
    heuristic).

    Args:
        design: RapidWright Design object
        max_candidates: Maximum candidates to return
        wirelength_threshold: Minimum wirelength reduction to be a candidate

    Returns:
        dict with candidates list and summary
    """
    if design is None:
        return {"error": "Design not loaded"}

    candidates = []
    sites_scanned = 0
    sites_with_candidates = 0

    try:
        device = design.getDevice()
        if device is None:
            return {"error": "Device not available"}

        # Iterate placed cells grouped by site instead of device.getAllSites(),
        # which enumerates the entire fabric (incl. empty sites). On a 37K-cell
        # design this is the difference between completing and timing out at 60s
        # (P0-2, run-20260711_193102: 2x timeout). Only SLICE sites with placed
        # LUT cells can yield swap candidates anyway.
        site_map = {}  # site_name -> (site, site_inst)
        for cell in design.getCells():
            site_inst = cell.getSiteInst()
            site = cell.getSite()
            if site_inst is None or site is None:
                continue
            sname = str(site.getName())
            if sname not in site_map:
                site_map[sname] = (site, site_inst)

        # Early-exit cap: collect a small pool then stop scanning, so
        # max_candidates actually bounds runtime (the old code scanned every
        # site before truncating). The final sort still picks the best of pool.
        collect_cap = max(max_candidates * 2, 20)

        for site_name, (site, site_inst) in site_map.items():
            site_type = str(site.getSiteTypeEnum()) if hasattr(site, "getSiteTypeEnum") else ""
            if "SLICE" not in site_type.upper():
                continue

            sites_scanned += 1
            lut_cells = _find_lut_cells_in_site(site_inst)
            if len(lut_cells) < 2:
                continue

            site_col = site.getInstanceX()
            site_row = site.getInstanceY()
            found_candidate_in_site = False

            # Check all pairs of LUT cells in this SLICE
            for i in range(len(lut_cells)):
                for j in range(i + 1, len(lut_cells)):
                    cell_i_info = lut_cells[i]
                    cell_j_info = lut_cells[j]
                    cell_i = cell_i_info["cell"]
                    cell_j = cell_j_info["cell"]

                    # Skip if different INIT (different logic)
                    if cell_i_info["init"] != cell_j_info["init"]:
                        continue

                    # Enumerate pin pairs
                    for pin_a, net_a in cell_i_info["pin_net_map"].items():
                        if net_a in _CONST_NET_NAMES:
                            continue
                        for pin_b, net_b in cell_j_info["pin_net_map"].items():
                            if net_b in _CONST_NET_NAMES:
                                continue
                            if net_a == net_b:
                                continue  # Same net, trivial

                            # Estimate wirelength reduction
                            reduction = _estimate_wirelength_reduction(
                                net_a, net_b, site_col, site_row, design
                            )

                            if reduction > wirelength_threshold:
                                candidates.append({
                                    "site_name": site_name,
                                    "site_col": site_col,
                                    "site_row": site_row,
                                    "cell_i": cell_i_info["cell_name"],
                                    "cell_j": cell_j_info["cell_name"],
                                    "bel_i": cell_i_info["bel_name"],
                                    "bel_j": cell_j_info["bel_name"],
                                    "pin_a": pin_a,
                                    "pin_b": pin_b,
                                    "net_a": net_a,
                                    "net_b": net_b,
                                    "init": cell_i_info["init"],
                                    "wirelength_reduction": round(reduction, 2),
                                })
                                found_candidate_in_site = True

            if found_candidate_in_site:
                sites_with_candidates += 1

            # Early-exit once we have a large enough candidate pool.
            if len(candidates) >= collect_cap:
                break

        # Sort by wirelength reduction descending
        candidates.sort(key=lambda x: x["wirelength_reduction"], reverse=True)
        candidates = candidates[:max_candidates]

        return {
            "candidates": candidates,
            "summary": {
                "sites_scanned": sites_scanned,
                "sites_with_candidates": sites_with_candidates,
                "total_candidates": len(candidates),
                "wirelength_threshold": wirelength_threshold,
                "recommended_action": (
                    f"Execute {len(candidates)} net swaps to reduce wirelength"
                    if candidates
                    else "No beneficial net swaps found"
                ),
            },
        }

    except Exception as e:
        logger.error(f"Net swapping analysis failed: {e}")
        return {"error": str(e)}


def execute_net_swapping(
    design,
    candidates: list[dict],
    temp_dir: str = "temp",
    checkpoint_prefix: str = "net_swap",
) -> dict:
    """Execute net swaps within SLICE sites and write checkpoint.

    MUTATING: modifies net connections and intra-site routing in-place.

    For each candidate, swaps the nets between two BEL pins on different cells
    within the same SLICE using disconnect/reconnect ECO operations.

    Args:
        design: RapidWright Design object (mutated in-place)
        candidates: List of swap candidates from analyze_net_swapping
        temp_dir: Directory for checkpoint output
        checkpoint_prefix: Checkpoint filename prefix

    Returns:
        dict with swaps_performed, swaps_failed, results, checkpoint_path
    """
    if design is None:
        return {"error": "Design not loaded"}

    if not candidates:
        return {
            "swaps_performed": 0,
            "swaps_failed": 0,
            "message": "No candidates provided",
        }

    try:
        from com.xilinx.rapidwright.design.tools import LUTTools
    except ImportError:
        logger.warning("LUTTools not available, falling back to manual ECO")
        LUTTools = None

    # Save pre-swap checkpoint
    os.makedirs(temp_dir, exist_ok=True)
    pre_ckpt = os.path.join(temp_dir, f"{checkpoint_prefix}_pre_swap.dcp")
    try:
        design.writeCheckpoint(pre_ckpt)
        logger.info(f"Pre-swap checkpoint saved: {pre_ckpt}")
    except Exception as e:
        logger.warning(f"Failed to save pre-swap checkpoint: {e}")
        pre_ckpt = None

    swaps_performed = 0
    swaps_failed = 0
    results = []

    for idx, cand in enumerate(candidates):
        cell_i_name = cand["cell_i"]
        cell_j_name = cand["cell_j"]
        pin_a = cand["pin_a"]
        pin_b = cand["pin_b"]
        net_a_name = cand["net_a"]
        net_b_name = cand["net_b"]
        site_name = cand["site_name"]

        try:
            cell_i = design.getCell(cell_i_name)
            cell_j = design.getCell(cell_j_name)
            if cell_i is None or cell_j is None:
                results.append({
                    "candidate_idx": idx,
                    "status": "skipped",
                    "message": f"Cell not found: {cell_i_name if cell_i is None else cell_j_name}",
                })
                swaps_failed += 1
                continue

            site = cell_i.getSite()
            if site is None or str(site.getName()) != site_name:
                results.append({
                    "candidate_idx": idx,
                    "status": "skipped",
                    "message": "Site mismatch or not placed",
                })
                swaps_failed += 1
                continue

            net_a = design.getNet(net_a_name)
            net_b = design.getNet(net_b_name)
            if net_a is None or net_b is None:
                results.append({
                    "candidate_idx": idx,
                    "status": "skipped",
                    "message": f"Net not found: {net_a_name if net_a is None else net_b_name}",
                })
                swaps_failed += 1
                continue

            site_inst = design.getSiteInstFromSite(site)
            if site_inst is None:
                results.append({
                    "candidate_idx": idx,
                    "status": "skipped",
                    "message": "No SiteInstance",
                })
                swaps_failed += 1
                continue

            # Perform the swap using disconnect/reconnect
            # Swap net_a (on cell_i.pin_a) with net_b (on cell_j.pin_b)
            success = False

            # Get BEL pins for the swap
            bel_pin_a = site.getBELPin(pin_a)
            bel_pin_b = site.getBELPin(pin_b)

            if bel_pin_a is None or bel_pin_b is None:
                results.append({
                    "candidate_idx": idx,
                    "status": "failed",
                    "message": f"BEL pin not found: {pin_a if bel_pin_a is None else pin_b}",
                })
                swaps_failed += 1
                continue

            # Disconnect old connections
            design.disconnectPin(site, bel_pin_a)
            design.disconnectPin(site, bel_pin_b)

            # Reconnect in swapped positions
            # cell_i.pin_a gets net_b, cell_j.pin_b gets net_a
            design.connectPin(site, bel_pin_b, net_a)
            design.connectPin(site, bel_pin_a, net_b)

            success = True

            if success:
                swaps_performed += 1
                results.append({
                    "candidate_idx": idx,
                    "status": "success",
                    "site": site_name,
                    "cell_i": cell_i_name,
                    "cell_j": cell_j_name,
                    "swapped": f"{cell_i_name}.{pin_a}({net_a_name}) <-> {cell_j_name}.{pin_b}({net_b_name})",
                    "wirelength_reduction": cand.get("wirelength_reduction", 0),
                })
            else:
                swaps_failed += 1
                results.append({
                    "candidate_idx": idx,
                    "status": "failed",
                    "message": "Swap operation failed",
                })

        except Exception as e:
            swaps_failed += 1
            results.append({
                "candidate_idx": idx,
                "status": "error",
                "message": str(e),
            })
            logger.debug(f"Net swap failed for candidate {idx}: {e}")

    # Update intra-site routing for affected sites
    affected_sites = set()
    for r in results:
        if r.get("status") == "success":
            affected_sites.add(r.get("site"))

    for site_name in affected_sites:
        try:
            site = design.getDevice().getSite(site_name)
            if site is not None:
                site_inst = design.getSiteInstFromSite(site)
                if site_inst is not None:
                    site_inst.routeSite()
        except Exception as e:
            logger.debug(f"Failed to re-route site {site_name}: {e}")

    # Write post-swap checkpoint
    post_ckpt = os.path.join(temp_dir, f"{checkpoint_prefix}_post_swap.dcp")
    ckpt_written = False
    try:
        design.writeCheckpoint(post_ckpt)
        ckpt_written = True
        logger.info(f"Post-swap checkpoint saved: {post_ckpt}")
    except Exception as e:
        logger.warning(f"Failed to save post-swap checkpoint: {e}")
        post_ckpt = None

    return {
        "swaps_performed": swaps_performed,
        "swaps_failed": swaps_failed,
        "candidates_total": len(candidates),
        "results": results,
        "checkpoint_path": post_ckpt if ckpt_written else None,
        "pre_swap_checkpoint": pre_ckpt,
        "post_actions": [
            "vivado_open_checkpoint",
            "vivado_route_design",
            "vivado_report_timing_summary",
        ],
    }


@skill(
    name="analyze_net_swapping",
    namespace="analysis",
    version="1.0.0",
    display_name="Net Swapping Analysis",
    description="Identify net swap candidates within SLICE sites. "
                "READ-ONLY. Scans placed LUT cells grouped by site and finds pairs "
                "with identical INIT strings where swapping input nets would reduce "
                "wirelength. Use before execute_net_swapping. NOTE: max_candidates "
                "bounds both returned results and scan early-exit (a larger value "
                "scans more sites); on very large designs this may still exceed 60s "
                "and time out - if so, pick a different strategy.",
    category=SkillCategory.ANALYSIS,
    idempotency="safe",
    side_effects=[],
    timeout_ms=120000,
    parameters=[
        ParameterSpec(
            name="max_candidates",
            type=int,
            description="Maximum candidates to return. Default: 20.",
            default=20,
        ),
        ParameterSpec(
            name="wirelength_threshold",
            type=float,
            description="Minimum wirelength reduction to be a candidate. Default: 50.",
            default=50.0,
        ),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class AnalyzeNetSwappingSkill(Skill):
    """Net Swapping Analysis Skill (READ-ONLY)."""

    def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        design = context.design
        if design is None:
            return SkillResult(
                success=False,
                error="Design not loaded in context",
                error_code="INVALID_PARAMETER",
            )

        max_candidates = kwargs.get("max_candidates", 20)
        wirelength_threshold = kwargs.get("wirelength_threshold", 50.0)

        result = analyze_net_swapping(
            design,
            max_candidates=max_candidates,
            wirelength_threshold=wirelength_threshold,
        )

        if "error" in result:
            return SkillResult(success=False, error=result["error"])

        return SkillResult(success=True, data=result)

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        mc = kwargs.get("max_candidates", 20)
        if not isinstance(mc, int) or mc <= 0:
            return False, "max_candidates must be a positive integer"
        wt = kwargs.get("wirelength_threshold", 50.0)
        if not isinstance(wt, (int, float)):
            return False, "wirelength_threshold must be a number"
        return True, ""


@skill(
    name="execute_net_swapping",
    namespace="optimization",
    version="1.0.0",
    display_name="Net Swapping Execution",
    description="Swap equivalent nets between BEL pins within SLICE sites. "
                "MUTATING. Side effects: pin connections, intra-site routing, checkpoint file. "
                "Trigger: analyze_net_swapping identified candidates with wirelength reduction. "
                "Requires candidates list from analyze_net_swapping.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["pin_connections", "intra_site_routing", "checkpoint_file"],
    timeout_ms=120000,
    parameters=[
        ParameterSpec(
            name="candidates",
            type=list,
            description="List of swap candidates from analyze_net_swapping.",
        ),
        ParameterSpec(
            name="temp_dir",
            type=str,
            description="Directory for checkpoint output.",
            default="temp",
        ),
        ParameterSpec(
            name="checkpoint_prefix",
            type=str,
            description="Checkpoint filename prefix.",
            default="net_swap",
        ),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class ExecuteNetSwappingSkill(Skill):
    """Net Swapping Execution Skill (MUTATING)."""

    def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        candidates = kwargs.get("candidates", [])
        if not candidates:
            return SkillResult(
                success=False,
                error="candidates is required and must be non-empty",
                error_code="INVALID_PARAMETER",
            )

        try:
            result = execute_net_swapping(
                context.design,
                candidates=candidates,
                temp_dir=kwargs.get("temp_dir", "temp"),
                checkpoint_prefix=kwargs.get("checkpoint_prefix", "net_swap"),
            )
            if "error" in result:
                return SkillResult(success=False, data=result, error=result["error"])
            return SkillResult(success=True, data=result)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "candidates" not in kwargs:
            return False, "candidates is required"
        cands = kwargs["candidates"]
        if not isinstance(cands, list):
            return False, "candidates must be a list"
        return True, ""
