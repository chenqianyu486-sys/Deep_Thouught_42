# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Congestion-Aware Cell Spreading Skill.

Identifies cells in routing-congested regions and spreads them outward
to improve routability. Uses cell density as a proxy for routing congestion.

Two-phase approach:
  1. ANALYSIS (READ-ONLY): Score cells by congestion connectivity
  2. EXECUTION (MUTATING): Move high-score cells outward, write checkpoint
"""

import logging
import os

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill

logger = logging.getLogger(__name__)


def _get_cell_nets(cell):
    """Collect physical nets connected to a cell via its site pins."""
    nets = []
    try:
        for pin in cell.getSitePinInsts():
            net = pin.getNet()
            if net is not None and net not in nets:
                nets.append(net)
    except Exception:
        pass
    return nets


def _count_congested_connections(cell, congested_cols: set) -> tuple[int, float]:
    """Count how many of a cell's connected pin columns are congested.

    Returns:
        (score, avg_connection_col): score = count of congested pin columns,
        avg_connection_col = average column of all connected pins.
    """
    score = 0
    total_pins = 0
    col_sum = 0

    nets = _get_cell_nets(cell)
    for net in nets:
        try:
            for pin in net.getSourcePins():
                tile = pin.getTile()
                if tile is not None:
                    col = tile.getColumn()
                    col_sum += col
                    total_pins += 1
                    if col in congested_cols:
                        score += 1
            for pin in net.getSinkPins():
                tile = pin.getTile()
                if tile is not None:
                    col = tile.getColumn()
                    col_sum += col
                    total_pins += 1
                    if col in congested_cols:
                        score += 1
        except Exception:
            continue

    avg_col = col_sum / total_pins if total_pins > 0 else 0
    return score, avg_col


def analyze_congestion_spreading(
    design,
    congestion_threshold: float = 0.8,
    max_cells_to_spread: int = 20,
) -> dict:
    """Analyze routing congestion and identify cells to spread outward.

    READ-ONLY: does not modify the design.

    Args:
        design: RapidWright Design object
        congestion_threshold: Threshold (0-1) for column congestion detection
        max_cells_to_spread: Maximum candidate cells to return

    Returns:
        dict with congestion_analysis, candidates, and summary
    """
    if design is None:
        return {"error": "Design not loaded"}

    try:
        from skills.congestion_analysis import analyze_congestion
    except ImportError:
        return {"error": "congestion_analysis module not available"}

    # Step 1: Get column-level congestion data
    congestion = analyze_congestion(design, utilization_threshold=congestion_threshold)
    if "error" in congestion:
        return {"error": congestion["error"]}

    # Step 2: Build set of congested column numbers
    congested_cols = {c["column"] for c in congestion.get("congested_columns", [])}
    if not congested_cols:
        return {
            "congestion_analysis": congestion,
            "candidates": [],
            "summary": {
                "total_candidates": 0,
                "message": "No congested columns detected",
            },
        }

    # Step 3: Score each placed cell in a congested column
    candidates = []
    max_fanout_penalty_threshold = 100

    for cell in design.getCells():
        if not cell.isPlaced():
            continue

        site = cell.getSite()
        if site is None:
            continue

        tile = site.getTile()
        if tile is None:
            continue

        col = tile.getColumn()
        if col not in congested_cols:
            continue

        # Score the cell by congestion connectivity
        score, avg_conn_col = _count_congested_connections(cell, congested_cols)
        if score == 0:
            continue

        # Penalize cells on high-fanout nets (risky to move)
        nets = _get_cell_nets(cell)
        max_fanout = 0
        for net in nets:
            try:
                fo = net.getFanOut()
                if fo > max_fanout:
                    max_fanout = fo
            except Exception:
                pass

        if max_fanout > max_fanout_penalty_threshold:
            score = max(0, score - 1)

        # Determine spread direction hint
        if avg_conn_col < col:
            spread_direction = "left"
        elif avg_conn_col > col:
            spread_direction = "right"
        else:
            spread_direction = "center"

        candidates.append({
            "cell_name": cell.getName(),
            "cell_type": cell.getType() if hasattr(cell, "getType") else "",
            "current_column": col,
            "score": score,
            "connection_column_avg": round(avg_conn_col, 1),
            "spread_direction": spread_direction,
            "max_fanout_net": max_fanout,
            "site_name": str(site.getName()),
        })

    # Step 4: Sort by score descending, take top N
    candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = candidates[:max_cells_to_spread]

    # Step 5: Compute summary
    score_range = (candidates[-1]["score"], candidates[0]["score"]) if candidates else (0, 0)

    return {
        "congestion_analysis": congestion,
        "candidates": candidates,
        "summary": {
            "total_candidates": len(candidates),
            "score_range": list(score_range),
            "congested_columns_count": len(congested_cols),
            "recommended_action": (
                f"Spread {len(candidates)} cells outward from congested regions"
                if candidates
                else "No cells need spreading"
            ),
        },
    }


def execute_congestion_spreading(
    design,
    max_cells_to_spread: int = 20,
    spread_distance: int = 10,
    temp_dir: str = "temp",
    checkpoint_prefix: str = "congestion_spread",
) -> dict:
    """Spread cells from congested regions outward and write checkpoint.

    MUTATING: modifies cell placement in-place and writes a DCP file.

    Args:
        design: RapidWright Design object (mutated in-place)
        max_cells_to_spread: Maximum cells to move
        spread_distance: Column distance to spread outward from congested center
        temp_dir: Directory for checkpoint output
        checkpoint_prefix: Checkpoint filename prefix

    Returns:
        dict with cells_moved, cells_failed, density_reduction,
        checkpoint_path, results, post_actions
    """
    if design is None:
        return {"error": "Design not loaded"}

    try:
        from com.xilinx.rapidwright.eco import ECOPlacementHelper
        from com.xilinx.rapidwright.design.tools import DesignTools
    except ImportError:
        return {"error": "RapidWright ECO classes not available"}

    # Step 1: Analyze to get candidates
    analysis = analyze_congestion_spreading(
        design, max_cells_to_spread=max_cells_to_spread
    )
    if "error" in analysis:
        return {"error": analysis["error"]}

    candidates = analysis.get("candidates", [])
    if not candidates:
        return {
            "cells_moved": 0,
            "cells_failed": 0,
            "message": "No cells identified for spreading",
            "congestion_analysis": analysis.get("congestion_analysis"),
        }

    # Step 2: Compute congested center column
    congested_cols = {
        c["column"]
        for c in analysis.get("congestion_analysis", {}).get("congested_columns", [])
    }
    congested_center = sum(congested_cols) // len(congested_cols) if congested_cols else 0

    device = design.getDevice()

    # Step 3: Move each candidate cell
    results = []
    cells_moved = 0
    cells_failed = 0

    # Snapshot column counts before spreading
    col_counts_before = {}
    for cell in design.getCells():
        if cell.isPlaced():
            site = cell.getSite()
            if site is not None:
                tile = site.getTile()
                if tile is not None:
                    c = tile.getColumn()
                    col_counts_before[c] = col_counts_before.get(c, 0) + 1

    for cand in candidates:
        cell_name = cand["cell_name"]
        cell = design.getCell(cell_name)

        if cell is None or not cell.isPlaced():
            results.append({
                "cell_name": cell_name,
                "status": "skipped",
                "message": "Cell not found or not placed",
            })
            cells_failed += 1
            continue

        original_site = str(cell.getSite().getName())

        # Collect connected tiles
        connected_tiles = []
        try:
            nets = _get_cell_nets(cell)
            for net in nets:
                for pin in net.getSourcePins():
                    tile = pin.getTile()
                    if tile:
                        connected_tiles.append((tile.getColumn(), tile.getRow()))
                for pin in net.getSinkPins():
                    tile = pin.getTile()
                    if tile:
                        connected_tiles.append((tile.getColumn(), tile.getRow()))
        except Exception as e:
            results.append({
                "cell_name": cell_name,
                "status": "error",
                "original_site": original_site,
                "message": f"Failed to collect connected tiles: {e}",
            })
            cells_failed += 1
            continue

        if not connected_tiles:
            results.append({
                "cell_name": cell_name,
                "status": "skipped",
                "original_site": original_site,
                "message": "No connected tiles found",
            })
            cells_failed += 1
            continue

        # Compute spread target: push away from congested center
        current_col = cand["current_column"]
        centroid_col = sum(t[0] for t in connected_tiles) // len(connected_tiles)
        centroid_row = sum(t[1] for t in connected_tiles) // len(connected_tiles)

        if current_col <= congested_center:
            target_col = current_col - spread_distance
        else:
            target_col = current_col + spread_distance

        # Use connection centroid row for vertical positioning
        target_row = centroid_row

        # Unroute nets and unplace cell
        nets_unrouted = []
        try:
            nets = _get_cell_nets(cell)
            for net in nets:
                nets_unrouted.append(str(net.getName()))
                net.unroute()
            DesignTools.fullyUnplaceCell(cell)
        except Exception as e:
            results.append({
                "cell_name": cell_name,
                "status": "error",
                "original_site": original_site,
                "message": f"Failed to unplace/unroute: {e}",
            })
            cells_failed += 1
            continue

        # Spiral search for empty compatible site near target
        new_site = None
        try:
            new_site = ECOPlacementHelper.spiralOutFrom(
                device, cell.getSiteTypeEnum(), target_col, target_row
            )
            if new_site and new_site.isOccupied():
                max_spiral_steps = 20
                for _ in range(max_spiral_steps):
                    new_site = ECOPlacementHelper.spiralOutFrom(
                        device,
                        cell.getSiteTypeEnum(),
                        new_site.getInstanceX(),
                        new_site.getInstanceY(),
                    )
                    if new_site and not new_site.isOccupied():
                        break
                else:
                    new_site = None
        except Exception:
            new_site = None

        if new_site is None:
            results.append({
                "cell_name": cell_name,
                "status": "error",
                "original_site": original_site,
                "message": "Could not find empty compatible site",
            })
            cells_failed += 1
            continue

        # Place cell and route intra-site wiring
        try:
            design.placeCell(cell, new_site)
            site_inst = new_site.getSiteInstance()
            if site_inst:
                site_inst.routeSite()

            new_site_name = str(new_site.getName())
            results.append({
                "cell_name": cell_name,
                "status": "success",
                "original_site": original_site,
                "new_site": new_site_name,
                "nets_unrouted": nets_unrouted,
            })
            cells_moved += 1
        except Exception as e:
            results.append({
                "cell_name": cell_name,
                "status": "error",
                "original_site": original_site,
                "message": f"Failed to place at new site: {e}",
            })
            cells_failed += 1

    # Step 4: Compute density reduction
    col_counts_after = {}
    for cell in design.getCells():
        if cell.isPlaced():
            site = cell.getSite()
            if site is not None:
                tile = site.getTile()
                if tile is not None:
                    c = tile.getColumn()
                    col_counts_after[c] = col_counts_after.get(c, 0) + 1

    density_reduction = {}
    for col in sorted(set(list(col_counts_before.keys()) + list(col_counts_after.keys()))):
        before = col_counts_before.get(col, 0)
        after = col_counts_after.get(col, 0)
        if before != after:
            density_reduction[col] = {"before": before, "after": after, "delta": after - before}

    # Step 5: Write checkpoint
    os.makedirs(temp_dir, exist_ok=True)
    checkpoint_path = os.path.join(temp_dir, f"{checkpoint_prefix}_congestion_spread.dcp")

    try:
        design.writeCheckpoint(checkpoint_path)
    except Exception as e:
        return {
            "error": f"Checkpoint write failed: {e}",
            "cells_moved": cells_moved,
            "cells_failed": cells_failed,
            "results": results,
        }

    return {
        "cells_moved": cells_moved,
        "cells_failed": cells_failed,
        "density_reduction": density_reduction,
        "checkpoint_path": checkpoint_path,
        "results": results,
        "post_actions": [
            "vivado_open_checkpoint",
            "vivado_route_design",
            "vivado_report_timing_summary",
        ],
    }


@skill(
    name="analyze_congestion_spreading",
    namespace="analysis",
    version="1.0.0",
    display_name="Congestion-Aware Spreading Analysis",
    description="Identify cells in congested regions and rank them by congestion connectivity. "
                "READ-ONLY. Use before execute_congestion_spreading.",
    category=SkillCategory.ANALYSIS,
    idempotency="safe",
    side_effects=[],
    timeout_ms=60000,
    parameters=[
        ParameterSpec(
            name="congestion_threshold",
            type=float,
            description="Threshold (0-1) for column congestion. Default: 0.8.",
            default=0.8,
        ),
        ParameterSpec(
            name="max_cells_to_spread",
            type=int,
            description="Maximum candidate cells to return. Default: 20.",
            default=20,
        ),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class AnalyzeCongestionSpreadingSkill(Skill):
    """Congestion-Aware Spreading Analysis Skill (READ-ONLY)."""

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        design = context.design
        if design is None:
            return SkillResult(
                success=False,
                error="Design not loaded in context",
                error_code="INVALID_PARAMETER",
            )

        congestion_threshold = kwargs.get("congestion_threshold", 0.8)
        max_cells_to_spread = kwargs.get("max_cells_to_spread", 20)

        result = analyze_congestion_spreading(
            design,
            congestion_threshold=congestion_threshold,
            max_cells_to_spread=max_cells_to_spread,
        )

        if "error" in result:
            return SkillResult(success=False, error=result["error"])

        return SkillResult(success=True, data=result)


@skill(
    name="execute_congestion_spreading",
    namespace="optimization",
    version="1.0.0",
    display_name="Congestion-Aware Cell Spreading",
    description="Spread cells from congested regions outward to improve routability. "
                "MUTATING. Side effects: cell placement changes, checkpoint file written. "
                "Trigger: analyze_congestion severity=HIGH, PBLOCK/PhysOpt ineffective.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["cell_placement", "checkpoint_file"],
    timeout_ms=300000,
    parameters=[
        ParameterSpec(
            name="max_cells_to_spread",
            type=int,
            description="Maximum cells to move. Default: 20.",
            default=20,
        ),
        ParameterSpec(
            name="spread_distance",
            type=int,
            description="Column distance to spread outward. Default: 10.",
            default=10,
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
            default="congestion_spread",
        ),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class ExecuteCongestionSpreadingSkill(Skill):
    """Congestion-Aware Cell Spreading Skill (MUTATING)."""

    def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        try:
            result = execute_congestion_spreading(
                context.design,
                max_cells_to_spread=kwargs.get("max_cells_to_spread", 20),
                spread_distance=kwargs.get("spread_distance", 10),
                temp_dir=kwargs.get("temp_dir", "temp"),
                checkpoint_prefix=kwargs.get("checkpoint_prefix", "congestion_spread"),
            )
            if "error" in result:
                return SkillResult(success=False, data=result, error=result["error"])
            return SkillResult(success=True, data=result)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        max_cells = kwargs.get("max_cells_to_spread", 20)
        if not isinstance(max_cells, int) or max_cells <= 0:
            return False, "max_cells_to_spread must be a positive integer"
        spread_dist = kwargs.get("spread_distance", 10)
        if not isinstance(spread_dist, int) or spread_dist <= 0:
            return False, "spread_distance must be a positive integer"
        return True, ""
