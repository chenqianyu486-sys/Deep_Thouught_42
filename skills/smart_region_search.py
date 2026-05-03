# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Smart Region Search Skill

Provides intelligent pblock region search based on target resource counts.
Uses column-level device slice index with sliding-window max-rectangle search
for fast, reliable large-region discovery. Falls back to full-device assessment
and multi-region split suggestions when single contiguous region is insufficient.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill


# ---------------------------------------------------------------------------
# Module-level device slice index cache
# ---------------------------------------------------------------------------
# Keyed by device name. Each entry is a dict:
#   columns: list of per-column dicts sorted by col_idx
#     {col_idx, slice_sites, dsp_sites, bram_sites, min_row, max_row, has_delay_heavy}
#   total_slice_sites, total_luts, total_ffs, total_dsps, total_brams
#   min_col, max_col, min_row, max_row, total_rows
_device_slice_index: Dict[str, dict] = {}


def _clear_resource_cache():
    """Clear cached device resource indices (called when design changes)."""
    _device_slice_index.clear()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

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
    # New fields for capacity diagnostics
    capacity_ok: bool = True
    deficit_luts: int = 0
    deficit_ffs: int = 0
    advice: List[str] = field(default_factory=list)
    multi_region_suggestions: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_delay_heavy_column(tile_type_name: str) -> bool:
    """Check if a tile type is delay-heavy (should be avoided)."""
    delay_patterns = ['URAM', 'HPIO', 'HDIO', 'HRIO', 'IO_LAGUNA']
    return any(pattern in tile_type_name for pattern in delay_patterns)


# ---------------------------------------------------------------------------
# Device slice index (one-pass precomputation cache)
# ---------------------------------------------------------------------------

def _build_device_slice_index(device) -> dict:
    """Single-pass enumeration of all tiles to build a column-level slice index.

    Returns dict with:
        columns: [{col_idx, slice_sites, dsp_sites, bram_sites, min_row, max_row,
                    has_delay_heavy}, ...] sorted by col_idx
        total_slice_sites, total_luts, total_ffs, total_dsps, total_brams
        min_col, max_col, min_row, max_row, total_rows
    """
    device_name = str(device.getName())
    if device_name in _device_slice_index:
        return _device_slice_index[device_name]

    tiles = list(device.getAllTiles())
    # Aggregate per column
    col_data: Dict[int, dict] = {}
    g_min_row = float('inf')
    g_max_row = float('-inf')

    for tile in tiles:
        col = tile.getColumn()
        row = tile.getRow()
        if row < g_min_row:
            g_min_row = row
        if row > g_max_row:
            g_max_row = row

        if col not in col_data:
            col_data[col] = {
                "col_idx": col,
                "slice_sites": 0,
                "dsp_sites": 0,
                "bram_sites": 0,
                "min_row": float('inf'),
                "max_row": float('-inf'),
                "has_delay_heavy": False,
            }

        cd = col_data[col]
        tile_type_name = str(tile.getTileTypeEnum().name())
        if _is_delay_heavy_column(tile_type_name):
            cd["has_delay_heavy"] = True

        sites = tile.getSites()
        if sites:
            for site in sites:
                stype = str(site.getSiteTypeEnum().name())
                if stype in ('SLICEL', 'SLICEM'):
                    cd["slice_sites"] += 1
                    if row < cd["min_row"]:
                        cd["min_row"] = row
                    if row > cd["max_row"]:
                        cd["max_row"] = row
                elif 'DSP' in stype:
                    cd["dsp_sites"] += 1
                elif 'RAMB' in stype:
                    cd["bram_sites"] += 1

    # Clean up columns with no SLICE sites: set min/max_row to 0
    for cd in col_data.values():
        if cd["slice_sites"] == 0:
            cd["min_row"] = 0
            cd["max_row"] = 0
        else:
            cd["min_row"] = int(cd["min_row"])
            cd["max_row"] = int(cd["max_row"])

    g_min_row = int(g_min_row)
    g_max_row = int(g_max_row)

    columns_sorted = sorted(col_data.values(), key=lambda c: c["col_idx"])
    min_col = columns_sorted[0]["col_idx"] if columns_sorted else 0
    max_col = columns_sorted[-1]["col_idx"] if columns_sorted else 0

    total_slice = sum(c["slice_sites"] for c in columns_sorted)
    total_dsp = sum(c["dsp_sites"] for c in columns_sorted)
    total_bram = sum(c["bram_sites"] for c in columns_sorted)

    index = {
        "columns": columns_sorted,
        "total_slice_sites": total_slice,
        "total_luts": total_slice * 4,
        "total_ffs": total_slice * 8,
        "total_dsps": total_dsp,
        "total_brams": total_bram,
        "min_col": min_col,
        "max_col": max_col,
        "min_row": g_min_row,
        "max_row": g_max_row,
        "total_rows": g_max_row - g_min_row + 1,
    }
    _device_slice_index[device_name] = index
    return index


# ---------------------------------------------------------------------------
# Sliding-window contiguous region search
# ---------------------------------------------------------------------------

def _find_contiguous_region_sliding_window(
    columns: list,
    required_slices: int,
    required_dsps: int,
    required_brams: int,
    reference_col: int,
    exclude_delay_heavy: bool = True,
) -> dict | None:
    """Sliding-window O(N) search for minimal-width column interval satisfying
    required slice / DSP / BRAM counts.

    Args:
        columns: List of per-column dicts sorted by col_idx.
        required_slices: Required SLICE site count.
        required_dsps: Required DSP site count.
        required_brams: Required BRAM site count.
        reference_col: Preferred center column (for tie-breaking).
        exclude_delay_heavy: Skip columns marked has_delay_heavy.

    Returns:
        None if no window satisfies, or dict with:
          left_idx, right_idx, col_min, col_max, total_slices, total_dsps,
          total_brams, common_min_row, common_max_row
    """
    N = len(columns)

    # Score function for tie-breaking: prefer narrower windows closer to reference
    def _score(left: int, right: int) -> float:
        width = right - left + 1
        center_col = (columns[left]["col_idx"] + columns[right]["col_idx"]) / 2.0
        dist = abs(center_col - reference_col)
        return float(width) + dist * 0.001  # width dominates, dist breaks ties

    best_window = None
    best_score = float('inf')

    left = 0
    cur_slices = 0
    cur_dsps = 0
    cur_brams = 0
    cur_min_row = float('inf')
    cur_max_row = float('-inf')

    for right in range(N):
        col = columns[right]
        if not (exclude_delay_heavy and col.get("has_delay_heavy")):
            cur_slices += col["slice_sites"]
            cur_dsps += col["dsp_sites"]
            cur_brams += col["bram_sites"]
            if col["slice_sites"] > 0:
                cur_min_row = min(cur_min_row, col["min_row"])
                cur_max_row = max(cur_max_row, col["max_row"])

        # Shrink from left while still satisfying
        while left <= right:
            left_col = columns[left]
            can_drop = True
            if exclude_delay_heavy and left_col.get("has_delay_heavy"):
                pass  # already excluded from counts, safe to drop
            else:
                test_slices = cur_slices - left_col["slice_sites"]
                test_dsps = cur_dsps - left_col["dsp_sites"]
                test_brams = cur_brams - left_col["bram_sites"]
                if test_slices >= required_slices and test_dsps >= required_dsps and test_brams >= required_brams:
                    # Recompute min/max row after dropping
                    cur_slices = test_slices
                    cur_dsps = test_dsps
                    cur_brams = test_brams
                    left += 1
                    # Recompute row bounds for current window
                    cur_min_row = float('inf')
                    cur_max_row = float('-inf')
                    for i in range(left, right + 1):
                        c = columns[i]
                        if c["slice_sites"] > 0:
                            cur_min_row = min(cur_min_row, c["min_row"])
                            cur_max_row = max(cur_max_row, c["max_row"])
                    continue
                else:
                    can_drop = False
            if not can_drop:
                break
            left += 1

        if cur_slices >= required_slices and cur_dsps >= required_dsps and cur_brams >= required_brams:
            s = _score(left, right)
            if s < best_score:
                best_score = s
                best_window = {
                    "left_idx": left,
                    "right_idx": right,
                    "col_min": columns[left]["col_idx"],
                    "col_max": columns[right]["col_idx"],
                    "total_slices": cur_slices,
                    "total_dsps": cur_dsps,
                    "total_brams": cur_brams,
                    "common_min_row": int(cur_min_row) if cur_min_row != float('inf') else 0,
                    "common_max_row": int(cur_max_row) if cur_max_row != float('-inf') else 0,
                }

    return best_window


# ---------------------------------------------------------------------------
# Multi-region split suggestion
# ---------------------------------------------------------------------------

def _suggest_multi_region_split(
    columns: list,
    required_slices: int,
    required_dsps: int,
    required_brams: int,
    index: dict,
    exclude_delay_heavy: bool = True,
) -> list[dict]:
    """When single contiguous region is insufficient, suggest splitting target
    across 2-3 independent column groups.

    Strategy: partition available columns into groups, assign proportional
    portion of target to each. Returns list of region suggestions.
    """
    usable = [c for c in columns if not (exclude_delay_heavy and c.get("has_delay_heavy"))]
    if len(usable) < 2:
        return []

    total_slices = sum(c["slice_sites"] for c in usable)
    total_dsps = sum(c["dsp_sites"] for c in usable)
    total_brams = sum(c["bram_sites"] for c in usable)

    if total_slices < required_slices * 0.5:
        return []  # Not enough even with split

    # Partition into 2 roughly equal groups
    mid = len(usable) // 2
    group_a = usable[:mid]
    group_b = usable[mid:]

    suggestions = []
    for idx, group in enumerate([group_a, group_b]):
        if not group:
            continue
        g_slices = sum(c["slice_sites"] for c in group)
        g_dsps = sum(c["dsp_sites"] for c in group)
        g_brams = sum(c["bram_sites"] for c in group)

        # Proportional target
        frac = g_slices / max(total_slices, 1)
        tgt_luts = int(required_slices * frac * 4)
        tgt_ffs = int(required_slices * frac * 8)

        common_min_row = min(c["min_row"] for c in group if c["slice_sites"] > 0)
        common_max_row = max(c["max_row"] for c in group if c["slice_sites"] > 0)

        suggestions.append({
            "group": idx + 1,
            "cols": [group[0]["col_idx"], group[-1]["col_idx"]],
            "rows": [common_min_row, common_max_row],
            "estimated_luts": g_slices * 4,
            "estimated_ffs": g_slices * 8,
            "estimated_dsps": g_dsps,
            "estimated_brams": g_brams,
            "suggested_target_luts": tgt_luts,
            "suggested_target_ffs": tgt_ffs,
            "note": (
                f"Assign ~{frac:.0%} of cells to pblock group {idx + 1}. "
                f"Requires manual cell partitioning."
            ),
        })

    return suggestions


# ---------------------------------------------------------------------------
# Public API: backward-compatible functions
# ---------------------------------------------------------------------------

def _get_column_resource_density(device, col: int) -> dict:
    """Get resource density for a specific column (uses cached index)."""
    index = _build_device_slice_index(device)
    for c in index["columns"]:
        if c["col_idx"] == col:
            return {
                "SLICE": c["slice_sites"],
                "DSP": c["dsp_sites"],
                "BRAM": c["bram_sites"],
                "URAM": 0,
                "delay_heavy": c["has_delay_heavy"],
            }
    return {"SLICE": 0, "DSP": 0, "BRAM": 0, "URAM": 0, "delay_heavy": False}


def _estimate_region_resources(index: dict, col_min: int, col_max: int,
                                row_min: int, row_max: int) -> dict:
    """Estimate resources in a rectangular region using column index.

    Uses row-fraction approximation. O(cols_in_region).
    """
    total_rows = index["total_rows"]
    if total_rows <= 0:
        return {"luts": 0, "ffs": 0, "dsps": 0, "brams": 0}

    actual_rows = row_max - row_min + 1
    row_fraction = min(1.0, actual_rows / total_rows)

    luts = 0
    ffs = 0
    dsps = 0
    brams = 0

    for col_entry in index["columns"]:
        c = col_entry["col_idx"]
        if c < col_min or c > col_max:
            continue
        luts += int(col_entry["slice_sites"] * row_fraction * 4)
        ffs += int(col_entry["slice_sites"] * row_fraction * 8)
        dsps += int(col_entry["dsp_sites"] * row_fraction)
        brams += int(col_entry["bram_sites"] * row_fraction)

    return {"luts": luts, "ffs": ffs, "dsps": dsps, "brams": brams}


def estimate_full_device_resources(device) -> dict:
    """Return total device resources using cached index."""
    index = _build_device_slice_index(device)
    return {
        "luts": index["total_luts"],
        "ffs": index["total_ffs"],
        "dsps": index["total_dsps"],
        "brams": index["total_brams"],
        "col_range": [index["min_col"], index["max_col"]],
        "row_range": [index["min_row"], index["max_row"]],
    }


def expand_region_to_capacity(device, region_bounds: dict,
                               required_lut: int, required_ff: int,
                               required_dsp: int = 0, required_bram: int = 0) -> dict:
    """Fallback: expand region outward until capacity met or device edge reached.

    Uses the new column index for fast estimation. Expands columns first
    (alternating), then rows.
    """
    index = _build_device_slice_index(device)
    col_min = region_bounds["col_min"]
    col_max = region_bounds["col_max"]
    row_min = region_bounds["row_min"]
    row_max = region_bounds["row_max"]

    def _sufficient(est: dict) -> bool:
        return (est["luts"] >= required_lut and est["ffs"] >= required_ff and
                est["dsps"] >= required_dsp and est["brams"] >= required_bram)

    est = _estimate_region_resources(index, col_min, col_max, row_min, row_max)
    if _sufficient(est):
        return {
            "col_min": col_min, "col_max": col_max,
            "row_min": row_min, "row_max": row_max,
            "estimated_luts": est["luts"], "estimated_ffs": est["ffs"],
            "estimated_dsps": est["dsps"], "estimated_brams": est["brams"],
            "capacity_met": True, "expanded": False,
        }

    # Phase 1: Expand columns outward
    expand_left = True
    while not _sufficient(est):
        expanded = False
        if expand_left and col_min > index["min_col"]:
            col_min -= 1
            expanded = True
        elif not expand_left and col_max < index["max_col"]:
            col_max += 1
            expanded = True
        elif col_min > index["min_col"]:
            col_min -= 1
            expanded = True
        elif col_max < index["max_col"]:
            col_max += 1
            expanded = True
        expand_left = not expand_left
        if not expanded:
            break
        est = _estimate_region_resources(index, col_min, col_max, row_min, row_max)

    # Phase 2: Expand rows
    expand_down = True
    while not _sufficient(est):
        expanded = False
        if expand_down and row_min > index["min_row"]:
            row_min -= 1
            expanded = True
        elif not expand_down and row_max < index["max_row"]:
            row_max += 1
            expanded = True
        elif row_min > index["min_row"]:
            row_min -= 1
            expanded = True
        elif row_max < index["max_row"]:
            row_max += 1
            expanded = True
        expand_down = not expand_down
        if not expanded:
            break
        est = _estimate_region_resources(index, col_min, col_max, row_min, row_max)

    return {
        "col_min": col_min, "col_max": col_max,
        "row_min": row_min, "row_max": row_max,
        "estimated_luts": est["luts"], "estimated_ffs": est["ffs"],
        "estimated_dsps": est["dsps"], "estimated_brams": est["brams"],
        "capacity_met": _sufficient(est),
        "expanded": True,
    }


# ---------------------------------------------------------------------------
# Center-of-mass computation
# ---------------------------------------------------------------------------

def _compute_center_of_mass(design) -> tuple[int, int]:
    """Compute column center of placed cells. Returns (col, row)."""
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


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

def smart_region_search(
    design,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    reference_col: Optional[int] = None,
    reference_row: Optional[int] = None,
) -> RegionSearchResult:
    """Find optimal contiguous region for pblock using sliding-window search.

    Algorithm:
    1. Precompute column-level slice index (cached, one-pass O(tiles))
    2. Convert target LUT/FF to required SLICE site count
       (each SLICE ≈ 4 LUTs + 8 FFs)
    3. Sliding window over columns to find minimal-width interval satisfying
       required slices, DSPs, BRAMs
    4. If insufficient even at full device: return full-device stats with
       deficit and multi-region split suggestions

    Args:
        design: RapidWright Design object
        target_lut_count: Required number of LUTs
        target_ff_count: Required number of FFs
        target_dsp_count: Required number of DSPs (default 0)
        target_bram_count: Required number of BRAMs (default 0)
        reference_col: Reference column (optional, uses design center if None)
        reference_row: Reference row (optional, uses design center if None)

    Returns:
        RegionSearchResult with coordinates, estimates, and capacity diagnostics.
    """
    if design is None:
        return RegionSearchResult(
            col_min=0, col_max=0, row_min=0, row_max=0,
            center_col=0, center_row=0, reference_col=0, reference_row=0,
            pblock_ranges="", estimated_luts=0, estimated_ffs=0,
            estimated_dsps=0, estimated_brams=0, target_luts=0, target_ffs=0,
            target_dsps=0, target_brams=0, columns_used=0, rows_used=0,
            status="error", message="Design not loaded",
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
            status="error", message=f"Failed to get device: {str(e)}",
        )

    # Build/retrieve cached index
    index = _build_device_slice_index(device)

    # Reference point
    if reference_col is None or reference_row is None:
        reference_col, reference_row = _compute_center_of_mass(design)
    if reference_col is None:
        reference_col = (index["min_col"] + index["max_col"]) // 2
    if reference_row is None:
        reference_row = (index["min_row"] + index["max_row"]) // 2

    # Convert target LUT/FF to required SLICE site count
    required_slices = max(
        (target_lut_count + 3) // 4,   # ceil(luts / 4)
        (target_ff_count + 7) // 8,    # ceil(ffs / 8)
        1,
    )

    # --- Phase 1: Sliding-window search on usable columns ---
    columns = index["columns"]
    window = _find_contiguous_region_sliding_window(
        columns, required_slices, target_dsp_count, target_bram_count,
        reference_col=reference_col, exclude_delay_heavy=True,
    )

    if window is None:
        # Retry without excluding delay-heavy columns
        window = _find_contiguous_region_sliding_window(
            columns, required_slices, target_dsp_count, target_bram_count,
            reference_col=reference_col, exclude_delay_heavy=False,
        )

    if window is not None:
        # Found a contiguous region
        col_min = window["col_min"]
        col_max = window["col_max"]
        row_min = index["min_row"]
        row_max = index["max_row"]

        # Use common row range of selected columns for more accurate estimate
        if window["common_min_row"] > 0 and window["common_max_row"] > 0:
            row_min = window["common_min_row"]
            row_max = window["common_max_row"]

        est = _estimate_region_resources(index, col_min, col_max, row_min, row_max)

        capacity_ok = (est["luts"] >= target_lut_count and
                       est["ffs"] >= target_ff_count and
                       est["dsps"] >= target_dsp_count and
                       est["brams"] >= target_bram_count)

        # Generate pblock_ranges
        pblock_ranges = _generate_pblock_ranges(device, col_min, col_max, row_min, row_max)

        return RegionSearchResult(
            col_min=col_min, col_max=col_max,
            row_min=row_min, row_max=row_max,
            center_col=(col_min + col_max) // 2,
            center_row=(row_min + row_max) // 2,
            reference_col=reference_col, reference_row=reference_row,
            pblock_ranges=pblock_ranges,
            estimated_luts=est["luts"], estimated_ffs=est["ffs"],
            estimated_dsps=est["dsps"], estimated_brams=est["brams"],
            target_luts=target_lut_count, target_ffs=target_ff_count,
            target_dsps=target_dsp_count, target_brams=target_bram_count,
            columns_used=col_max - col_min + 1,
            rows_used=row_max - row_min + 1,
            status="success",
            message=(f"Found region: cols {col_min}-{col_max}, "
                     f"rows {row_min}-{row_max}. "
                     f"Estimated LUTs={est['luts']:,}, FFs={est['ffs']:,}. "
                     f"Capacity {'OK' if capacity_ok else 'INSUFFICIENT'}."),
            capacity_ok=capacity_ok,
            deficit_luts=max(0, target_lut_count - est["luts"]),
            deficit_ffs=max(0, target_ff_count - est["ffs"]),
            advice=_build_advice(est, target_lut_count, target_ff_count,
                                  target_dsp_count, target_bram_count),
        )

    # --- Phase 2: No single region found — return full device assessment ---
    full_est = {
        "luts": index["total_luts"],
        "ffs": index["total_ffs"],
        "dsps": index["total_dsps"],
        "brams": index["total_brams"],
    }
    col_min = index["min_col"]
    col_max = index["max_col"]
    row_min = index["min_row"]
    row_max = index["max_row"]
    pblock_ranges = _generate_pblock_ranges(device, col_min, col_max, row_min, row_max)

    deficit_luts = max(0, target_lut_count - full_est["luts"])
    deficit_ffs = max(0, target_ff_count - full_est["ffs"])
    capacity_ok = (deficit_luts == 0 and deficit_ffs == 0 and
                   full_est["dsps"] >= target_dsp_count and
                   full_est["brams"] >= target_bram_count)

    # Generate multi-region split suggestions
    multi = _suggest_multi_region_split(
        columns, required_slices, target_dsp_count, target_bram_count,
        index, exclude_delay_heavy=True,
    )

    return RegionSearchResult(
        col_min=col_min, col_max=col_max,
        row_min=row_min, row_max=row_max,
        center_col=(col_min + col_max) // 2,
        center_row=(row_min + row_max) // 2,
        reference_col=reference_col, reference_row=reference_row,
        pblock_ranges=pblock_ranges,
        estimated_luts=full_est["luts"], estimated_ffs=full_est["ffs"],
        estimated_dsps=full_est["dsps"], estimated_brams=full_est["brams"],
        target_luts=target_lut_count, target_ffs=target_ff_count,
        target_dsps=target_dsp_count, target_brams=target_bram_count,
        columns_used=col_max - col_min + 1,
        rows_used=row_max - row_min + 1,
        status="success" if capacity_ok else "capacity_insufficient",
        message=(f"Full device region: cols {col_min}-{col_max}, "
                 f"rows {row_min}-{row_max}. "
                 f"Estimated LUTs={full_est['luts']:,}, FFs={full_est['ffs']:,}. "
                 f"Capacity {'OK' if capacity_ok else 'INSUFFICIENT'}."),
        capacity_ok=capacity_ok,
        deficit_luts=deficit_luts,
        deficit_ffs=deficit_ffs,
        advice=_build_advice(full_est, target_lut_count, target_ff_count,
                              target_dsp_count, target_bram_count),
        multi_region_suggestions=multi,
    )


def _generate_pblock_ranges(device, col_min, col_max, row_min, row_max) -> str:
    """Generate Vivado pblock_ranges string for a region."""
    try:
        from rapidwright_tools import convert_fabric_region_to_pblock_ranges
        pb = convert_fabric_region_to_pblock_ranges(
            col_min=col_min, col_max=col_max,
            row_min=row_min, row_max=row_max,
            device_name=str(device.getName()),
        )
        if pb.get("status") == "success":
            return pb.get("pblock_ranges", "")
    except Exception:
        pass
    return (f"SLICE column range: {col_min} to {col_max}, "
            f"row range: {row_min} to {row_max}")


def _build_advice(est: dict, target_lut: int, target_ff: int,
                   target_dsp: int, target_bram: int) -> list[str]:
    """Build diagnostic advice based on capacity assessment."""
    advice = []
    lut_short = max(0, target_lut - est["luts"])
    ff_short = max(0, target_ff - est["ffs"])

    if lut_short == 0 and ff_short == 0:
        advice.append(
            "Region capacity is sufficient for target resources. "
            "You can safely proceed with pblock creation and placement."
        )
        return advice

    if lut_short > 0 or ff_short > 0:
        advice.append(
            f"Target resource exceeds device capacity by "
            f"LUTs={lut_short:,}, FFs={ff_short:,}."
        )
        advice.append(
            "Consider reducing resource_multiplier, splitting the design "
            "across multiple pblocks, or upgrading to a larger device."
        )

    if target_dsp > est.get("dsps", 0):
        advice.append(f"DSP target ({target_dsp}) exceeds available ({est['dsps']}).")
    if target_bram > est.get("brams", 0):
        advice.append(f"BRAM target ({target_bram}) exceeds available ({est['brams']}).")

    advice.append(
        "If you continue with an undersized pblock, Vivado place_design will "
        "likely fail with resource errors or produce unroutable results."
    )
    return advice


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------

@skill(
    name="smart_region",
    namespace="placement",
    version="1.0.0",
    display_name="Smart Region Search",
    description="Find optimal pblock region using sliding-window column search. "
                "READ-ONLY. "
                "Input: target resource counts (LUT/FF/DSP/BRAM) and optional reference coordinates. "
                "Output: optimal rectangular region with capacity diagnostics and "
                "multi-region split suggestions when single region is insufficient. "
                "Avoids delay-heavy columns and prioritizes minimal-width regions.",
    category=SkillCategory.PLACEMENT,
    idempotency="safe",
    side_effects=[],
    timeout_ms=600000,
    parameters=[
        ParameterSpec("target_lut_count", int, "Required number of LUTs"),
        ParameterSpec("target_ff_count", int, "Required number of FFs"),
        ParameterSpec("target_dsp_count", int, "Required number of DSPs", default=0),
        ParameterSpec("target_bram_count", int, "Required number of BRAMs", default=0),
        ParameterSpec("reference_col", int, "Reference column coordinate (optional)", default=None),
        ParameterSpec("reference_row", int, "Reference row coordinate (optional)", default=None),
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
                reference_col, reference_row,
            )

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
                "message": result.message,
                "capacity_ok": result.capacity_ok,
                "deficit_luts": result.deficit_luts,
                "deficit_ffs": result.deficit_ffs,
                "advice": result.advice,
                "multi_region_suggestions": result.multi_region_suggestions,
            }

            return SkillResult(
                success=(result.status in ("success", "capacity_insufficient")),
                data=result_dict,
                error=None if result.status in ("success", "capacity_insufficient")
                else result.message,
            )
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
