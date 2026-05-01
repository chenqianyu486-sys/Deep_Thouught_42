# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Smart Region Search Skill

Provides intelligent pblock region search based on target resource counts
and reference location. Uses greedy expansion to find optimal contiguous
region that satisfies capacity requirements while avoiding delay-heavy columns.
"""

from dataclasses import dataclass
from typing import Optional

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill


@dataclass
class RegionSearchResult:
    """Result of smart region search."""
    col_min: int
    col_max: int
    row_min: int
    row_max: int
    center_col: int
    center_row: int
    reference_col: int
    reference_row: int
    pblock_ranges: str
    estimated_luts: int
    estimated_ffs: int
    estimated_dsps: int
    estimated_brams: int
    target_luts: int
    target_ffs: int
    target_dsps: int
    target_brams: int
    columns_used: int
    rows_used: int
    status: str
    message: str


def _is_delay_heavy_column(tile_type_name: str) -> bool:
    """Check if a tile type is delay-heavy (should be avoided)."""
    delay_patterns = ['URAM', 'HPIO', 'HDIO', 'HRIO', 'IO_LAGUNA']
    return any(pattern in tile_type_name for pattern in delay_patterns)


def _get_column_resource_density(device, col: int) -> dict:
    """
    Get resource density for a specific column.

    Returns dict with counts of SLICE, DSP, BRAM, URAM sites.
    """
    density = {"SLICE": 0, "DSP": 0, "BRAM": 0, "URAM": 0, "delay_heavy": False}

    try:
        tiles = device.getAllTiles()
        for tile in tiles:
            if tile.getColumn() != col:
                continue

            tile_type_name = str(tile.getTileTypeEnum().name())
            if _is_delay_heavy_column(tile_type_name):
                density["delay_heavy"] = True

            sites = tile.getSites()
            if sites:
                for site in sites:
                    site_type_name = str(site.getSiteTypeEnum().name())
                    if site_type_name in ['SLICEL', 'SLICEM']:
                        density["SLICE"] += 1
                    elif 'DSP48E2' in site_type_name:
                        density["DSP"] += 1
                    elif 'RAMB18' in site_type_name:
                        density["BRAM"] += 1
                    elif 'URAM288' in site_type_name:
                        density["URAM"] += 1
    except Exception:
        pass

    return density


def _compute_center_of_mass(design) -> tuple[int, int]:
    """
    Compute center of mass of placed cells in the design.

    Returns:
        Tuple of (col, row) representing the centroid
    """
    placed_cols = []
    placed_rows = []

    try:
        for cell in design.getCells():
            if cell.isPlaced():
                site = cell.getSite()
                if site:
                    tile = site.getTile()
                    if tile:
                        placed_cols.append(tile.getColumn())
                        placed_rows.append(tile.getRow())
    except Exception:
        pass

    if not placed_cols:
        return 0, 0

    return sum(placed_cols) // len(placed_cols), sum(placed_rows) // len(placed_rows)


def smart_region_search(
    design,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    reference_col: Optional[int] = None,
    reference_row: Optional[int] = None
) -> RegionSearchResult:
    """
    Find optimal contiguous region for pblock using greedy expansion.

    Algorithm:
    1. Use reference point (or center of mass of placed cells) as start
    2. Greedily expand in 4 directions (left, right, up, down)
    3. Prefer columns with higher resource density
    4. Skip delay-heavy columns (URAM, HPIO, etc.)
    5. Stop when all target resources are satisfied

    Args:
        design: RapidWright Design object (must be loaded)
        target_lut_count: Required number of LUTs
        target_ff_count: Required number of FFs
        target_dsp_count: Required number of DSPs (default 0)
        target_bram_count: Required number of BRAMs (default 0)
        reference_col: Reference column (optional, uses design center if None)
        reference_row: Reference row (optional, uses design center if None)

    Returns:
        RegionSearchResult with coordinates and pblock description
    """
    if design is None:
        return RegionSearchResult(
            col_min=0, col_max=0, row_min=0, row_max=0,
            center_col=0, center_row=0, reference_col=0, reference_row=0,
            pblock_ranges="", estimated_luts=0, estimated_ffs=0,
            estimated_dsps=0, estimated_brams=0, target_luts=0, target_ffs=0,
            target_dsps=0, target_brams=0, columns_used=0, rows_used=0,
            status="error", message="Design not loaded"
        )

    try:
        device = design.getDevice()
    except Exception as e:
        return RegionSearchResult(
            col_min=0, col_max=0, row_min=0, row_max=0,
            center_col=0, center_row=0, reference_col=0, reference_row=0,
            pblock_ranges="", estimated_luts=0, estimated_ffs=0,
            estimated_dsps=0, estimated_brams=0, target_luts=0, target_ffs=0,
            target_dsps=0, target_brams=0, columns_used=0, rows_used=0,
            status="error", message=f"Failed to get device: {str(e)}"
        )

    # Use center of mass as reference if not provided
    if reference_col is None or reference_row is None:
        reference_col, reference_row = _compute_center_of_mass(design)

    # Analyze device to find good columns (non-delay-heavy with resources)
    all_tiles = list(device.getAllTiles())
    if not all_tiles:
        return RegionSearchResult(
            col_min=0, col_max=0, row_min=0, row_max=0,
            center_col=reference_col, center_row=reference_row,
            reference_col=reference_col, reference_row=reference_row,
            pblock_ranges="", estimated_luts=0, estimated_ffs=0,
            estimated_dsps=0, estimated_brams=0, target_lut_count=target_lut_count,
            target_ff_count=target_ff_count, target_dsps=target_dsp_count,
            target_brams=target_bram_count, columns_used=0, rows_used=0,
            status="error", message="No tiles found in device"
        )

    min_col = min(t.getColumn() for t in all_tiles)
    max_col = max(t.getColumn() for t in all_tiles)
    min_row = min(t.getRow() for t in all_tiles)
    max_row = max(t.getRow() for t in all_tiles)

    # Pre-compute column resource density
    column_density = {}
    for col in range(min_col, max_col + 1):
        column_density[col] = _get_column_resource_density(device, col)

    # Find usable columns (non-delay-heavy)
    usable_columns = []
    for col in range(min_col, max_col + 1):
        if not column_density[col]["delay_heavy"]:
            usable_columns.append(col)

    if not usable_columns:
        usable_columns = list(range(min_col, max_col + 1))

    # Greedy expansion from reference point
    # Track current expansion window
    left_bound = reference_col
    right_bound = reference_col
    top_bound = reference_row
    bottom_bound = reference_row

    # Sort usable columns by resource density (prioritize high-density columns)
    column_priority = sorted(usable_columns, key=lambda c: -(
        column_density[c]["SLICE"] + column_density[c]["DSP"] * 2 + column_density[c]["BRAM"] * 3
    ))

    # Resource estimation: each SLICE has ~4 LUTs and ~8 FFs
    def estimate_resources(col_min: int, col_max: int, row_min: int, row_max: int) -> dict:
        """Estimate resources in a rectangular region."""
        luts = 0
        ffs = 0
        dsps = 0
        brams = 0

        for col in range(max(col_min, min_col), min(col_max + 1, max_col + 1)):
            for row in range(max(row_min, min_row), min(row_max + 1, max_row + 1)):
                try:
                    tile = device.getTile(row, col)
                    if tile:
                        sites = tile.getSites()
                        if sites:
                            for site in sites:
                                stype = str(site.getSiteTypeEnum().name())
                                if stype in ['SLICEL', 'SLICEM']:
                                    luts += 4
                                    ffs += 8
                                elif 'DSP48E2' in stype:
                                    dsps += 1
                                elif 'RAMB18' in stype or 'RAMB36' in stype:
                                    brams += 1
                except Exception:
                    pass

        return {"luts": luts, "ffs": ffs, "dsps": dsps, "brams": brams}

    def resources_sufficient(est: dict) -> bool:
        return (est["luts"] >= target_lut_count and
                est["ffs"] >= target_ff_count and
                est["dsps"] >= target_dsp_count and
                est["brams"] >= target_bram_count)

    # Expand symmetrically from reference point
    max_iterations = 100
    iteration = 0

    while not resources_sufficient(estimate_resources(left_bound, right_bound, bottom_bound, top_bound)):
        if iteration >= max_iterations:
            break
        iteration += 1

        # Find next best column to add
        best_expansion = None
        best_score = -1

        # Try expanding left
        next_left = left_bound - 1
        if next_left >= min_col:
            # Score: resource density / distance from reference
            dist = reference_col - next_left
            density = column_density.get(next_left, {})
            if not density.get("delay_heavy", False):
                score = (density.get("SLICE", 0) * 4) / (dist + 1)
                if score > best_score:
                    best_score = score
                    best_expansion = ("left", next_left, top_bound, bottom_bound)

        # Try expanding right
        next_right = right_bound + 1
        if next_right <= max_col:
            dist = next_right - reference_col
            density = column_density.get(next_right, {})
            if not density.get("delay_heavy", False):
                score = (density.get("SLICE", 0) * 4) / (dist + 1)
                if score > best_score:
                    best_score = score
                    best_expansion = ("right", next_right, top_bound, bottom_bound)

        # Try expanding down (toward lower row indices)
        next_bottom = bottom_bound - 1
        if next_bottom >= min_row:
            # For rows, we include all usable columns in the expansion
            # Score based on average column density
            score = 50 / (abs(reference_row - next_bottom) + 1)  # Prefer closer expansions
            if score > best_score:
                best_score = score
                best_expansion = ("down", left_bound, right_bound, next_bottom)

        # Try expanding up (toward higher row indices)
        next_top = top_bound + 1
        if next_top <= max_row:
            score = 50 / (abs(reference_row - next_top) + 1)
            if score > best_score:
                best_score = score
                best_expansion = ("up", left_bound, right_bound, next_top)

        if best_expansion is None:
            break

        direction = best_expansion[0]
        if direction == "left":
            left_bound = best_expansion[1]
        elif direction == "right":
            right_bound = best_expansion[1]
        elif direction == "down":
            bottom_bound = best_expansion[3]
        elif direction == "up":
            top_bound = best_expansion[3]

    # Compute final estimates
    final_estimate = estimate_resources(left_bound, right_bound, bottom_bound, top_bound)

    # Generate pblock ranges
    pblock_ranges = ""
    try:
        from rapidwright_tools import convert_fabric_region_to_pblock_ranges
        pblock_result = convert_fabric_region_to_pblock_ranges(
            col_min=left_bound,
            col_max=right_bound,
            row_min=bottom_bound,
            row_max=top_bound,
            device_name=str(device.getName())
        )
        if pblock_result.get("status") == "success":
            pblock_ranges = pblock_result.get("pblock_ranges", "")
    except Exception:
        pblock_ranges = f"SLICE column range: {left_bound} to {right_bound}, row range: {bottom_bound} to {top_bound}"

    return RegionSearchResult(
        col_min=left_bound,
        col_max=right_bound,
        row_min=bottom_bound,
        row_max=top_bound,
        center_col=(left_bound + right_bound) // 2,
        center_row=(bottom_bound + top_bound) // 2,
        reference_col=reference_col,
        reference_row=reference_row,
        pblock_ranges=pblock_ranges,
        estimated_luts=final_estimate["luts"],
        estimated_ffs=final_estimate["ffs"],
        estimated_dsps=final_estimate["dsps"],
        estimated_brams=final_estimate["brams"],
        target_luts=target_lut_count,
        target_ffs=target_ff_count,
        target_dsps=target_dsp_count,
        target_brams=target_bram_count,
        columns_used=right_bound - left_bound + 1,
        rows_used=top_bound - bottom_bound + 1,
        status="success",
        message=f"Found region: cols {left_bound}-{right_bound}, rows {bottom_bound}-{top_bound}"
    )


@skill(
    name="smart_region",
    namespace="placement",
    version="1.0.0",
    display_name="Smart Region Search",
    description="Find optimal pblock region using greedy expansion from reference point. "
                "READ-ONLY. "
                "Input: target resource counts (LUT/FF/DSP/BRAM) and optional reference coordinates. "
                "Output: optimal rectangular region and pblock description in one call. "
                "Avoids delay-heavy columns (URAM, HPIO) and prioritizes high-density columns.",
    category=SkillCategory.PLACEMENT,
    idempotency="safe",
    side_effects=[],
    timeout_ms=60000,
    parameters=[
        ParameterSpec("target_lut_count", int, "Required number of LUTs"),
        ParameterSpec("target_ff_count", int, "Required number of FFs"),
        ParameterSpec("target_dsp_count", int, "Required number of DSPs", default=0),
        ParameterSpec("target_bram_count", int, "Required number of BRAMs", default=0),
        ParameterSpec("reference_col", int, "Reference column coordinate (optional)", default=None),
        ParameterSpec("reference_row", int, "Reference row coordinate (optional)", default=None)
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class SmartRegionSearchSkill(Skill):
    """Skill for intelligent pblock region search."""

    def execute(self, context: SkillContext,
                target_lut_count: int, target_ff_count: int,
                target_dsp_count: int = 0, target_bram_count: int = 0,
                reference_col: int | None = None, reference_row: int | None = None) -> SkillResult:
        try:
            result = smart_region_search(
                context.design,
                target_lut_count, target_ff_count, target_dsp_count, target_bram_count,
                reference_col, reference_row
            )

            # Convert to dict for serialization
            result_dict = {
                "col_min": result.col_min,
                "col_max": result.col_max,
                "row_min": result.row_min,
                "row_max": result.row_max,
                "center_col": result.center_col,
                "center_row": result.center_row,
                "reference_col": result.reference_col,
                "reference_row": result.reference_row,
                "pblock_ranges": result.pblock_ranges,
                "estimated_luts": result.estimated_luts,
                "estimated_ffs": result.estimated_ffs,
                "estimated_dsps": result.estimated_dsps,
                "estimated_brams": result.estimated_brams,
                "target_luts": result.target_luts,
                "target_ffs": result.target_ffs,
                "target_dsps": result.target_dsps,
                "target_brams": result.target_brams,
                "columns_used": result.columns_used,
                "rows_used": result.rows_used,
                "status": result.status,
                "message": result.message
            }

            return SkillResult(success=(result.status == "success"), data=result_dict, error=None if result.status == "success" else result.message)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "target_lut_count" not in kwargs:
            return False, "target_lut_count is required"
        if "target_ff_count" not in kwargs:
            return False, "target_ff_count is required"

        target_lut = kwargs.get("target_lut_count", 0)
        target_ff = kwargs.get("target_ff_count", 0)

        if not isinstance(target_lut, int) or target_lut <= 0:
            return False, "target_lut_count must be a positive integer"
        if not isinstance(target_ff, int) or target_ff <= 0:
            return False, "target_ff_count must be a positive integer"

        return True, ""