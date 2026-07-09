# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
PBLOCK Region Analysis Skill.

Analyzes FPGA fabric using RapidWright to find optimal pblock region,
generates pblock ranges, and returns analysis data (region coordinates,
pblock_ranges string, estimated resources) with suggested next steps.
READ-ONLY — no design modification.
"""
import logging

from optimizer.pure.pblock_plan import (
    PBLOCK_GLOBAL_MODE,
    PBLOCK_LOCAL_MODE,
    PBLOCK_UNPLACE_GLOBAL,
    PBLOCK_UNPLACE_LOCAL,
    PblockExecutionPlan,
    recommend_pblock_plan,
)
from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill



def compute_adaptive_resource_multiplier(
    target_lut_count: int,
    target_ff_count: int,
    device_lut_capacity: int = 394000,
    device_ff_capacity: int = 788000,
    base_multiplier: float = 1.5,
) -> float:
    """Compute adaptive resource multiplier based on design size.

    Small designs (<10% device): use higher multiplier (1.8x) for better placement freedom
    Medium designs (10-30% device): use default multiplier (1.5x)
    Large designs (>30% device): use lower multiplier (1.2x) to avoid resource conflicts

    Args:
        target_lut_count: Current LUT usage
        target_ff_count: Current FF usage
        device_lut_capacity: Total device LUT capacity (default: xcvu3p = 394K)
        device_ff_capacity: Total device FF capacity (default: xcvu3p = 788K)
        base_multiplier: Base multiplier to adjust

    Returns:
        Adjusted resource multiplier
    """
    # Calculate utilization ratio
    lut_ratio = target_lut_count / device_lut_capacity if device_lut_capacity > 0 else 0
    ff_ratio = target_ff_count / device_ff_capacity if device_ff_capacity > 0 else 0
    max_ratio = max(lut_ratio, ff_ratio)

    if max_ratio < 0.10:
        # Small design: more freedom for placement
        return max(base_multiplier, 1.8)
    elif max_ratio < 0.30:
        # Medium design: use default
        return base_multiplier
    else:
        # Large design: tighter constraints to avoid resource conflicts
        return min(base_multiplier, 1.2)


def _describe_multiplier_transform(
    target_lut_count: int,
    target_ff_count: int,
    base_multiplier: float,
    adaptive_multiplier: float,
    device_lut_capacity: int = 394000,
) -> str:
    """Explain why the multiplier changed from base to adaptive.

    Mirrors the branching in compute_adaptive_resource_multiplier so the
    LLM can understand why its input_multiplier was adjusted.
    """
    ratio = target_lut_count / device_lut_capacity if device_lut_capacity > 0 else 0
    if adaptive_multiplier > base_multiplier:
        return (
            f"adaptive: {base_multiplier}→{adaptive_multiplier} "
            f"(small design, {ratio:.0%} of device, raised for placement freedom)"
        )
    if adaptive_multiplier < base_multiplier:
        return (
            f"adaptive: {base_multiplier}→{adaptive_multiplier} "
            f"(large design, {ratio:.0%} of device, lowered to avoid resource conflicts)"
        )
    return f"adaptive: {base_multiplier} (unchanged, {ratio:.0%} of device)"


logger = logging.getLogger(__name__)


def _should_use_whole_design_fallback(
    *,
    sizing_basis: str,
    columns_used: int,
    utilization_density: float,
    target_lut_count: int,
    target_ff_count: int,
    bound_resources: dict | None,
) -> tuple[bool, str | None]:
    """Detect over-tight local-pblock plans that should fall back to a wider region.

    The local-pblock model is useful when the bound cells already describe a
    concentrated hotspot. On distributed designs, though, sizing a pblock for
    only a few dozen LUT/FF sites can collapse to a single SLICE column with a
    very low density score; binding those cells to that region then becomes
    overly restrictive and benchmark results regress.
    """
    if sizing_basis != "bound_cells" or not bound_resources:
        return False, None
    if columns_used > 1:
        return False, None
    if utilization_density >= 0.10:
        return False, None
    if target_lut_count < 5000:
        return False, None

    bound_luts = max(int(bound_resources.get("luts", 0)), 0)
    bound_ffs = max(int(bound_resources.get("ffs", 0)), 0)
    lut_ratio = bound_luts / max(target_lut_count, 1)
    ff_ratio = bound_ffs / max(target_ff_count, 1)
    if lut_ratio >= 0.05 or ff_ratio >= 0.10:
        return False, None

    return (
        True,
        "local bound-cell sizing collapsed to a single-column, ultra-low-density "
        "region for a much larger design; using a wider whole-design fallback",
    )


def _build_deficit(estimated: dict, required_lut: int, required_ff: int,
                   required_dsp: int = 0, required_bram: int = 0) -> dict:
    """Compute resource deficit per type (positive = shortfall).

    Now also computes DSP and BRAM deficits (not just LUT/FF).
    """
    return {
        "luts": max(0, required_lut - estimated.get("luts", 0)),
        "ffs": max(0, required_ff - estimated.get("ffs", 0)),
        "dsps": max(0, required_dsp - estimated.get("dsps", 0)),
        "brams": max(0, required_bram - estimated.get("brams", 0)),
    }


def _build_advice_insufficient(deficit: dict, full_device: dict,
                                required_lut: int, required_ff: int,
                                resource_multiplier: float,
                                multi_region: list | None = None) -> list[str]:
    """Build advice array for insufficient capacity scenario.

    Covers LUT/FF and DSP/BRAM deficits, resource_multiplier adjustment,
    full-device capacity check, and multi-region split guidance.
    """
    advice = []
    lut_def = deficit.get("luts", 0)
    ff_def = deficit.get("ffs", 0)

    if resource_multiplier > 1.0:
        advice.append(
            f"Resource multiplier is {resource_multiplier}x — consider reducing it "
            f"(e.g., 1.0x-1.2x) to lower the required resource target."
        )

    if lut_def > 0 or ff_def > 0:
        advice.append(
            f"Target resource exceeds region capacity by LUTs={lut_def:,}, FFs={ff_def:,}. "
            f"Consider reducing target_lut_count / target_ff_count to match available resources."
        )

    dsp_def = deficit.get("dsps", 0)
    bram_def = deficit.get("brams", 0)
    if dsp_def > 0 or bram_def > 0:
        advice.append(
            f"Target resources exceed region capacity by DSPs={dsp_def:,}, "
            f"BRAMs={bram_def:,}. "
            f"Consider reducing target_dsp_count / target_bram_count or selecting "
            f"a region with more DSP/BRAM columns."
        )

    full_luts = full_device.get("luts", 0)
    full_ffs = full_device.get("ffs", 0)
    if required_lut > full_luts or required_ff > full_ffs:
        advice.append(
            f"Required resources (LUTs={required_lut:,}, FFs={required_ff:,}) exceed "
            f"entire device capacity (LUTs={full_luts:,}, FFs={full_ffs:,}). "
            f"Consider upgrading to a larger device or splitting the design across multiple FPGAs."
        )
    else:
        advice.append(
            "Consider allowing cross-clock-region placement (non-contiguous region), "
            "splitting the design into multiple pblocks, or relaxing timing constraints "
            "to allow a wider placement spread."
        )

    if multi_region:
        advice.append(
            f"Multi-region split: {len(multi_region)} pblock groups suggested. "
            f"See multi_region_suggestions for column assignments and per-group targets."
        )

    advice.append(
        "If you continue with an undersized pblock, Vivado place_design will likely "
        "fail with resource errors or produce unroutable results."
    )
    return advice


def _build_advice_sufficient(utilization_density: float) -> list[str]:
    """Build advice array for sufficient capacity scenario, graded by density.

    capacity_ok only means the region fits the resources — it does NOT mean
    placement will succeed. At high density place_design congests and worsens
    timing, so the advice must warn the LLM rather than green-light blindly.
    """
    if utilization_density > 0.90:
        return [
            f"Region nearly full (utilization density {utilization_density:.1%}); "
            f"place_design will likely congest and worsen timing. Consider a higher "
            f"resource_multiplier (larger region) or fewer target cells before proceeding."
        ]
    if utilization_density > 0.80:
        return [
            f"Density high (utilization density {utilization_density:.1%}); "
            f"IS_SOFT=1 recommended. Monitor post-place WNS closely — timing may regress."
        ]
    return [
        f"Region capacity is sufficient (utilization density {utilization_density:.1%}). "
        f"You can safely proceed with pblock creation and placement."
    ]


def _estimate_bound_cell_resources(design, cell_names: list[str]) -> dict | None:
    """Estimate LUT/FF/DSP/BRAM resources consumed by a set of bound cells.

    Sizes the pblock region around the cells that will actually be bound to
    it (local pblock), rather than the whole design. Classifies each cell by
    type via the RapidWright API (same scheme as rapidwright_tools auto-detect,
    plus MUXF):
      LUT*        -> luts
      FD*         -> ffs   (FDPE/FDRE/FDSE/FDCE)
      MUXF*       -> luts  (MUXF7/MUXF8 occupy the SLICE F7/F8MUX site;
                            count as LUT-equivalent for SLICE site demand)
      DSP*        -> dsps
      RAMB*/BRAM* -> brams

    Returns {luts, ffs, dsps, brams, matched, total}, or None when no cell
    resolves (caller falls back to whole-design sizing).
    """
    if not cell_names:
        return None
    luts = ffs = dsps = brams = 0
    matched = 0
    for name in cell_names:
        try:
            cell = design.getCell(name)
            if cell is None:
                continue
            ctype = str(cell.getType())
        except Exception:
            continue
        matched += 1
        if ctype.startswith("LUT"):
            luts += 1
        elif ctype.startswith("FD") or ctype in ("FDPE", "FDRE", "FDSE", "FDCE"):
            ffs += 1
        elif ctype.startswith("MUXF"):
            luts += 1
        elif ctype.startswith("DSP"):
            dsps += 1
        elif ctype.startswith("RAMB") or ctype.startswith("BRAM"):
            brams += 1
    if matched == 0:
        logger.warning(
            "[PBLOCK] _estimate_bound_cell_resources: 0/%d cells resolved in "
            "design; falling back to whole-design sizing.", len(cell_names),
        )
        return None
    if matched < len(cell_names) * 0.5:
        logger.warning(
            "[PBLOCK] _estimate_bound_cell_resources: low match rate %d/%d; "
            "bound-resource estimate may undercount.",
            matched, len(cell_names),
        )
    logger.info(
        "[PBLOCK] Bound cell resources: matched %d/%d -> LUT=%d FF=%d DSP=%d BRAM=%d",
        matched, len(cell_names), luts, ffs, dsps, brams,
    )
    return {"luts": luts, "ffs": ffs, "dsps": dsps, "brams": brams,
            "matched": matched, "total": len(cell_names)}


def _get_reference_points(design, critical_path_cells: list[str] | None) -> dict[str, tuple[int, int]]:
    """Build deterministic global-replacement reference points."""
    from skills.smart_region_search import (
        _build_device_slice_index,
        _compute_center_of_mass,
        _compute_critical_path_center_from_cells,
    )

    index = _build_device_slice_index(design.getDevice())
    min_col = int(index["min_col"])
    max_col = int(index["max_col"])
    min_row = int(index["min_row"])
    max_row = int(index["max_row"])
    span = max(max_col - min_col, 1)
    cp_center = None
    if critical_path_cells:
        cp_center = _compute_critical_path_center_from_cells(design, critical_path_cells)
    design_center = _compute_center_of_mass(design)
    default_row = (min_row + max_row) // 2
    cp_col = cp_center[0] if cp_center else None
    cp_row = cp_center[1] if cp_center else None
    if cp_col is None:
        cp_col = design_center[0] if design_center else (min_col + max_col) // 2
    if cp_row is None:
        cp_row = design_center[1] if design_center else default_row
    left_col = min_col + span // 4
    right_col = min_col + (3 * span) // 4
    _dc = (min_col + max_col) // 2
    return {
        "global_cp_center": (int(_dc), int(cp_row)),
        "global_left_bias": (int(_dc), int(cp_row)),
        "global_right_bias": (int(_dc), int(cp_row)),
    }


def _search_candidate_region(
    design,
    *,
    required_lut: int,
    required_ff: int,
    required_dsp: int,
    required_bram: int,
    distance_weight_factor: float,
    critical_path_cells: list[str] | None = None,
    reference_col: int | None = None,
    reference_row: int | None = None,
) -> dict:
    """Search a region and expand it when the first fit is insufficient."""
    from skills.smart_region_search import expand_region_to_capacity, smart_region_search

    region_result = smart_region_search(
        design,
        target_lut_count=required_lut,
        target_ff_count=required_ff,
        target_dsp_count=required_dsp,
        target_bram_count=required_bram,
        reference_col=reference_col,
        reference_row=reference_row,
        critical_path_cells=critical_path_cells,
        distance_weight_factor=distance_weight_factor,
    )
    if region_result is None:
        return {
            "status": "error",
            "message": "smart_region_search returned no region",
        }

    region = {
        "col_min": region_result.col_min,
        "col_max": region_result.col_max,
        "row_min": region_result.row_min,
        "row_max": region_result.row_max,
        "center_col": region_result.center_col,
        "center_row": region_result.center_row,
        "columns_used": region_result.columns_used,
        "rows_used": region_result.rows_used,
    }
    estimated = {
        "luts": region_result.estimated_luts,
        "ffs": region_result.estimated_ffs,
        "dsps": region_result.estimated_dsps,
        "brams": region_result.estimated_brams,
    }
    capacity_ok = region_result.capacity_ok
    capacity_basis = "initial_region"
    region_selection_reason = "smart_region_search"
    pblock_ranges = region_result.pblock_ranges
    multi_region = region_result.multi_region_suggestions
    expanded = False

    if not capacity_ok:
        try:
            device = design.getDevice()
            expanded_result = expand_region_to_capacity(
                device, region, required_lut, required_ff, required_dsp, required_bram,
            )
            region = {
                "col_min": expanded_result["col_min"],
                "col_max": expanded_result["col_max"],
                "row_min": expanded_result["row_min"],
                "row_max": expanded_result["row_max"],
                "center_col": (expanded_result["col_min"] + expanded_result["col_max"]) // 2,
                "center_row": (expanded_result["row_min"] + expanded_result["row_max"]) // 2,
                "columns_used": expanded_result["col_max"] - expanded_result["col_min"] + 1,
                "rows_used": expanded_result["row_max"] - expanded_result["row_min"] + 1,
            }
            estimated = {
                "luts": expanded_result["estimated_luts"],
                "ffs": expanded_result["estimated_ffs"],
                "dsps": expanded_result["estimated_dsps"],
                "brams": expanded_result["estimated_brams"],
            }
            if expanded_result.get("capacity_met"):
                capacity_ok = True
                expanded = True
                capacity_basis = "fallback_expansion"
                region_selection_reason = "fallback_expanded_to_capacity"
                from rapidwright_tools import convert_fabric_region_to_pblock_ranges
                pb_result = convert_fabric_region_to_pblock_ranges(
                    col_min=region["col_min"],
                    col_max=region["col_max"],
                    row_min=region["row_min"],
                    row_max=region["row_max"],
                    device_name=str(device.getName()),
                )
                if pb_result.get("status") == "success":
                    pblock_ranges = pb_result.get("pblock_ranges", "")
        except Exception as e:
            logger.warning("PBLOCK candidate expansion failed: %s", e)

    return {
        "status": "success",
        "region": region,
        "estimated": estimated,
        "capacity_ok": capacity_ok,
        "capacity_basis": capacity_basis,
        "region_selection_reason": region_selection_reason,
        "pblock_ranges": pblock_ranges,
        "multi_region": multi_region,
        "expanded": expanded,
        "reference_col": region_result.reference_col,
        "reference_row": region_result.reference_row,
    }


def _search_global_replacement_region(
    design,
    *,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int,
    target_bram_count: int,
    resource_multiplier: float,
    reference_col: int | None,
    reference_row: int | None,
) -> dict:
    """Build a bounded rectangular global-replacement pblock candidate.

    Global replacement is meant to disturb placement enough to escape a poor
    solution. The proven strong-baseline winner (run-20260706_165117,
    SLICE_X0Y0:X54Y299, target/capacity ~0.24, full height) was a LOOSE
    ~1/3-device window: wide enough for the placer to solve timing, narrow
    enough to force spatial compaction. Near-full windows (density >0.85)
    congest and leave the placer no freedom; whole-device windows constrain
    nothing. This path searches rectangles around the reference point and
    ranks loose windows first.
    """
    from skills.smart_region_search import _build_device_slice_index, _estimate_region_resources

    device = design.getDevice()
    index = _build_device_slice_index(device)
    min_col = int(index["min_col"])
    max_col = int(index["max_col"])
    min_row = int(index["min_row"])
    max_row = int(index["max_row"])
    total_cols = max_col - min_col + 1
    total_rows = max_row - min_row + 1
    if total_cols <= 0 or total_rows <= 0:
        return {"status": "error", "message": "invalid device bounds"}

    ref_col = int(reference_col if reference_col is not None else (min_col + max_col) // 2)
    ref_row = int(reference_row if reference_row is not None else (min_row + max_row) // 2)

    # The execution multiplier remains part of the plan, but the physical
    # replacement window should fit the current design with headroom, not force
    # a 2x whole-design region that expands into a nearly full-device pblock.
    effective_lut = max(1, int(target_lut_count))
    effective_ff = max(1, int(target_ff_count))
    effective_dsp = max(0, target_dsp_count)
    effective_bram = max(0, target_bram_count)

    width_seed = _replacement_width_seed(target_lut_count)
    height_seed = _replacement_height_seed(total_rows)
    width_options = _unique_ints(
        width_seed,
        int(width_seed * 1.25),
        int(width_seed * 1.5),
        55,
        65,
        min(total_cols, 80),
        # Loose replacement windows (fractions of the device width). The
        # ranking below prefers whichever lands closest to the proven
        # ~0.25 target/capacity density.
        int(total_cols * 0.25),
        int(total_cols * 0.33),
        int(total_cols * 0.45),
        int(total_cols * 0.60),
    )
    height_options = _unique_ints(
        height_seed,
        int(height_seed * 1.25),
        int(height_seed * 1.5),
        min(total_rows, 195),
        min(total_rows, 240),
        # Full-height window — matches the winning baseline geometry.
        total_rows,
    )

    candidates: list[dict] = []
    for width in width_options:
        if width <= 0:
            continue
        col_min, col_max = _centered_interval(ref_col, min_col, max_col, width)
        for height in height_options:
            if height <= 0:
                continue
            row_min, row_max = _centered_interval(ref_row, min_row, max_row, height)
            estimated = _estimate_region_resources(index, col_min, col_max, row_min, row_max)
            lut_density = target_lut_count / max(estimated.get("luts", 1), 1)
            ff_density = target_ff_count / max(estimated.get("ffs", 1), 1)
            density = max(lut_density, ff_density)
            capacity_ok = (
                estimated.get("luts", 0) >= effective_lut
                and estimated.get("ffs", 0) >= effective_ff
                and estimated.get("dsps", 0) >= effective_dsp
                and estimated.get("brams", 0) >= effective_bram
            )
            deficit = (
                max(0, effective_lut - estimated.get("luts", 0))
                + max(0, effective_ff - estimated.get("ffs", 0))
                + max(0, effective_dsp - estimated.get("dsps", 0))
                + max(0, effective_bram - estimated.get("brams", 0))
            )
            center_col = (col_min + col_max) // 2
            center_row = (row_min + row_max) // 2
            candidates.append(
                {
                    "region": {
                        "col_min": col_min,
                        "col_max": col_max,
                        "row_min": row_min,
                        "row_max": row_max,
                        "center_col": center_col,
                        "center_row": center_row,
                        "columns_used": col_max - col_min + 1,
                        "rows_used": row_max - row_min + 1,
                    },
                    "estimated": estimated,
                    "capacity_ok": capacity_ok,
                    "density": density,
                    "deficit": deficit,
                    "distance": abs(center_col - ref_col) + abs(center_row - ref_row),
                }
            )

    if not candidates:
        return {"status": "error", "message": "no bounded global candidates generated"}

    def _rank(candidate: dict) -> tuple[int, int, float, int, int]:
        density = float(candidate["density"])
        # Loose windows (~1/5 to 1/2 full) give the placer freedom to solve
        # timing while still forcing compaction; near-full windows congest
        # (place-only WNS stays flat); near-empty windows constrain nothing.
        if 0.15 <= density <= 0.60:
            density_bucket = 0
        elif 0.10 <= density <= 0.85:
            density_bucket = 1
        else:
            density_bucket = 2
        region = candidate["region"]
        area = int(region["columns_used"]) * int(region["rows_used"])
        full_height = 0 if int(region["rows_used"]) >= int(total_rows) else 1
        return (
            0 if candidate["capacity_ok"] else 1,
            full_height,
            density_bucket,
            abs(density - 0.50),
            int(candidate["deficit"]),
            area + int(candidate["distance"]),
        )

    best = sorted(candidates, key=_rank)[0]
    region = best["region"]
    try:
        from rapidwright_tools import convert_fabric_region_to_pblock_ranges
        pb_result = convert_fabric_region_to_pblock_ranges(
            col_min=region["col_min"],
            col_max=region["col_max"],
            row_min=region["row_min"],
            row_max=region["row_max"],
            device_name=str(device.getName()),
        )
        pblock_ranges = pb_result.get("pblock_ranges", "") if pb_result.get("status") == "success" else ""
    except Exception:
        pblock_ranges = ""
    if not pblock_ranges:
        pblock_ranges = (
            f"SLICE column range: {region['col_min']} to {region['col_max']}, "
            f"row range: {region['row_min']} to {region['row_max']}"
        )

    return {
        "status": "success",
        "region": region,
        "estimated": best["estimated"],
        "capacity_ok": bool(best["capacity_ok"]),
        "capacity_basis": "bounded_global_replacement",
        "region_selection_reason": (
            "bounded_global_replacement_rect "
            f"(resource_multiplier={resource_multiplier:.2f}, "
            f"density={best['density']:.3f})"
        ),
        "pblock_ranges": pblock_ranges,
        "multi_region": [],
        "expanded": False,
        "reference_col": ref_col,
        "reference_row": ref_row,
        "effective_target_lut": effective_lut,
        "effective_target_ff": effective_ff,
        "effective_target_dsp": effective_dsp,
        "effective_target_bram": effective_bram,
        "utilization_density": float(best["density"]),
    }


def _replacement_width_seed(target_lut_count: int) -> int:
    if target_lut_count < 20_000:
        return 35
    if target_lut_count < 50_000:
        return 55
    return 70


def _replacement_height_seed(total_rows: int) -> int:
    return max(1, min(total_rows, max(90, int(total_rows * 0.65))))


def _centered_interval(center: int, min_value: int, max_value: int, size: int) -> tuple[int, int]:
    size = max(1, min(size, max_value - min_value + 1))
    start = center - size // 2
    end = start + size - 1
    if start < min_value:
        end += min_value - start
        start = min_value
    if end > max_value:
        start -= end - max_value
        end = max_value
    return max(min_value, start), min(max_value, end)


def _unique_ints(*values: int) -> list[int]:
    return sorted({int(v) for v in values if int(v) > 0})


def _build_execution_plan(
    *,
    candidate_id: str,
    plan_mode: str,
    pblock_ranges: str,
    resource_multiplier: float,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int,
    target_bram_count: int,
    bind_cells_to_pblock: bool,
    unplace_mode: str,
    is_soft: bool,
    place_directive: str,
    route_directive: str,
    reference_col: int | None,
    reference_row: int | None,
    selection_reason: str,
    fallback_reason: str | None,
    critical_path_cells_snapshot: list[str] | None,
    capacity_ok: bool,
    estimated_resources: dict,
    region: dict,
    utilization_density: float | None,
    bound_resources: dict | None = None,
) -> PblockExecutionPlan:
    return PblockExecutionPlan(
        plan_mode=plan_mode,
        candidate_id=candidate_id,
        pblock_name="pblock_tight",
        pblock_ranges=pblock_ranges,
        resource_multiplier=resource_multiplier,
        target_lut_count=target_lut_count,
        target_ff_count=target_ff_count,
        target_dsp_count=target_dsp_count,
        target_bram_count=target_bram_count,
        bind_cells_to_pblock=bind_cells_to_pblock,
        unplace_mode=unplace_mode,
        is_soft=is_soft,
        place_directive=place_directive,
        route_directive=route_directive,
        reference_col=reference_col,
        reference_row=reference_row,
        selection_reason=selection_reason,
        fallback_reason=fallback_reason,
        critical_path_cells_snapshot=list(critical_path_cells_snapshot or []),
        capacity_ok=capacity_ok,
        estimated_resources=dict(estimated_resources or {}),
        region=dict(region or {}),
        utilization_density=utilization_density,
        bound_resources=dict(bound_resources or {}),
    )


def _legacy_generate_pblock_plan(
    design,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    resource_multiplier: float = 1.5,
    critical_path_cells: list[str] | None = None,
    distance_weight_factor: float = 0.3,
    max_utilization_density: float = 0.90,
) -> dict:
    """Analyze FPGA fabric to find optimal PBLOCK region with capacity gating.

    Args:
        design: RapidWright Design object
        target_lut_count: Current LUT usage (from Vivado report)
        target_ff_count: Current FF usage
        target_dsp_count: Current DSP usage
        target_bram_count: Current BRAM usage
        resource_multiplier: Buffer multiplier for resource targets (default 1.5x)

    Returns:
        Dict with status, region, pblock_ranges, estimated_resources,
        target_resources, capacity_ok, deficit (LUT/FF/DSP/BRAM),
        advice (including IS_SOFT recommendation), multi_region_suggestions,
        is_soft_recommended, next_steps. next_steps is non-null ONLY
        when capacity_ok == true.
    """
    if design is None:
        logger.warning("generate_pblock_plan: design is None")
        return {
            "status": "error",
            "message": "Design not loaded",
            "error_details": "context.design is None",
        }

    if target_lut_count <= 0:
        return {
            "status": "skipped",
            "message": (
                f"Invalid resource targets: LUT={target_lut_count} "
                f"(must be positive). Run report_utilization_for_pblock first to get "
                f"actual resource counts."
            ),
            "target_resources": {
                "luts": target_lut_count,
                "ffs": target_ff_count,
                "dsps": target_dsp_count,
                "brams": target_bram_count,
            },
        }

    # Apply adaptive resource multiplier based on design size
    input_multiplier = resource_multiplier
    adaptive_multiplier = compute_adaptive_resource_multiplier(
        target_lut_count, target_ff_count, base_multiplier=resource_multiplier
    )
    multiplier_transform = _describe_multiplier_transform(
        target_lut_count, target_ff_count, input_multiplier, adaptive_multiplier
    )
    logger.info(
        "Adaptive resource multiplier: base=%.1f, adaptive=%.1f (LUT=%d, FF=%d) — %s",
        resource_multiplier, adaptive_multiplier, target_lut_count, target_ff_count,
        multiplier_transform,
    )
    # Size the region for the BOUND cells (local pblock) when critical path
    # cells are provided and resolve in the design; otherwise fall back to
    # whole-design sizing. The 2026-07-04 chain refactor binds only
    # critical_path_cells to the pblock, so sizing the region for the whole
    # design produced a huge no-op region (see dcp_optimizer_run-20260705_130916).
    # adaptive_multiplier still uses target_lut_count (whole design) for the
    # small/medium/large classification — that is a whole-design property.
    bound_resources = (
        _estimate_bound_cell_resources(design, critical_path_cells)
        if critical_path_cells else None
    )
    if bound_resources and bound_resources["matched"] >= max(1, len(critical_path_cells) // 2):
        sizing_basis = "bound_cells"
        base_lut = bound_resources["luts"]
        base_ff = bound_resources["ffs"]
        base_dsp = bound_resources["dsps"]
        base_bram = bound_resources["brams"]
    else:
        sizing_basis = "whole_design"
        base_lut = target_lut_count
        base_ff = target_ff_count
        base_dsp = target_dsp_count
        base_bram = target_bram_count

    required_lut = int(base_lut * adaptive_multiplier)
    required_ff = int(base_ff * adaptive_multiplier)
    required_dsp = int(base_dsp * resource_multiplier)
    required_bram = int(base_bram * resource_multiplier)

    logger.info(
        "analyze_pblock_region: sizing_basis=%s | base LUT=%d FF=%d DSP=%d BRAM=%d | "
        "multiplier=%.1fx | required LUT=%d FF=%d DSP=%d BRAM=%d",
        sizing_basis, base_lut, base_ff, base_dsp, base_bram,
        resource_multiplier, required_lut, required_ff, required_dsp, required_bram,
    )

    # Step 1: smart_region_search (sliding-window algorithm, O(N) fast)
    region_result = None
    region_error = None
    try:
        from skills.smart_region_search import smart_region_search
        region_result = smart_region_search(
            design,
            target_lut_count=required_lut,
            target_ff_count=required_ff,
            target_dsp_count=required_dsp,
            target_bram_count=required_bram,
            critical_path_cells=critical_path_cells,
            distance_weight_factor=distance_weight_factor,
        )
    except Exception as e:
        region_error = str(e)

    if region_error or region_result is None:
        return {
            "status": "error",
            "message": f"Smart region search failed: {region_error or 'unknown error'}",
            "error_details": region_error,
            "target_resources": {
                "luts": required_lut, "ffs": required_ff,
                "dsps": required_dsp, "brams": required_bram,
            },
            "resource_multiplier": adaptive_multiplier,
        }

    # Step 2: Extract region — use smart_region_search's own capacity assessment
    region = {
        "col_min": region_result.col_min,
        "col_max": region_result.col_max,
        "row_min": region_result.row_min,
        "row_max": region_result.row_max,
        "center_col": region_result.center_col,
        "center_row": region_result.center_row,
        "columns_used": region_result.columns_used,
        "rows_used": region_result.rows_used,
    }
    pblock_ranges = region_result.pblock_ranges
    pblock_name = "pblock_tight"

    estimated = {
        "luts": region_result.estimated_luts,
        "ffs": region_result.estimated_ffs,
        "dsps": region_result.estimated_dsps,
        "brams": region_result.estimated_brams,
    }
    required = {
        "luts": required_lut,
        "ffs": required_ff,
        "dsps": required_dsp,
        "brams": required_bram,
    }

    capacity_ok = region_result.capacity_ok
    capacity_basis = "initial_region"
    region_selection_reason = "smart_region_search (sliding window around critical-path center of mass)"
    multi_region = region_result.multi_region_suggestions

    # Step 3: If insufficient, try fallback expansion
    expanded = False
    if not capacity_ok:
        logger.info(
            "analyze_pblock_region: initial region insufficient — "
            "estimated LUT=%d FF=%d | required LUT=%d FF=%d. "
            "Attempting fallback expansion.",
            estimated["luts"], estimated["ffs"],
            required_lut, required_ff,
        )
        try:
            from skills.smart_region_search import expand_region_to_capacity
            device = design.getDevice()
            expanded_result = expand_region_to_capacity(
                device, region, required_lut, required_ff,
                required_dsp, required_bram,
            )
            if expanded_result.get("capacity_met"):
                capacity_ok = True
                expanded = True
                capacity_basis = "fallback_expansion"
                region_selection_reason = "fallback_expanded_to_capacity (initial region insufficient)"
                region = {
                    "col_min": expanded_result["col_min"],
                    "col_max": expanded_result["col_max"],
                    "row_min": expanded_result["row_min"],
                    "row_max": expanded_result["row_max"],
                    "center_col": (expanded_result["col_min"] + expanded_result["col_max"]) // 2,
                    "center_row": (expanded_result["row_min"] + expanded_result["row_max"]) // 2,
                    "columns_used": expanded_result["col_max"] - expanded_result["col_min"] + 1,
                    "rows_used": expanded_result["row_max"] - expanded_result["row_min"] + 1,
                }
                estimated = {
                    "luts": expanded_result["estimated_luts"],
                    "ffs": expanded_result["estimated_ffs"],
                    "dsps": expanded_result["estimated_dsps"],
                    "brams": expanded_result["estimated_brams"],
                }
                from rapidwright_tools import convert_fabric_region_to_pblock_ranges
                pb_result = convert_fabric_region_to_pblock_ranges(
                    col_min=region["col_min"], col_max=region["col_max"],
                    row_min=region["row_min"], row_max=region["row_max"],
                    device_name=str(device.getName()),
                )
                if pb_result.get("status") == "success":
                    pblock_ranges = pb_result.get("pblock_ranges", "")

                logger.info(
                    "analyze_pblock_region: fallback expansion succeeded — "
                    "expanded to cols %d-%d, rows %d-%d. "
                    "estimated LUT=%d FF=%d.",
                    region["col_min"], region["col_max"],
                    region["row_min"], region["row_max"],
                    estimated["luts"], estimated["ffs"],
                )
            else:
                region = {
                    "col_min": expanded_result["col_min"],
                    "col_max": expanded_result["col_max"],
                    "row_min": expanded_result["row_min"],
                    "row_max": expanded_result["row_max"],
                    "center_col": (expanded_result["col_min"] + expanded_result["col_max"]) // 2,
                    "center_row": (expanded_result["row_min"] + expanded_result["row_max"]) // 2,
                    "columns_used": expanded_result["col_max"] - expanded_result["col_min"] + 1,
                    "rows_used": expanded_result["row_max"] - expanded_result["row_min"] + 1,
                }
                estimated = {
                    "luts": expanded_result["estimated_luts"],
                    "ffs": expanded_result["estimated_ffs"],
                    "dsps": expanded_result["estimated_dsps"],
                    "brams": expanded_result["estimated_brams"],
                }
                logger.warning(
                    "analyze_pblock_region: fallback expansion reached device edge, "
                    "still insufficient. Max region estimated LUT=%d FF=%d.",
                    estimated["luts"], estimated["ffs"],
                )
        except Exception as e:
            logger.warning("Fallback expansion failed: %s", e)

    # Step 4: Compute deficit, get full device resources
    deficit = _build_deficit(estimated, required_lut, required_ff, required_dsp, required_bram) if not capacity_ok else None

    full_device = {}
    try:
        from skills.smart_region_search import estimate_full_device_resources
        full_device = estimate_full_device_resources(design.getDevice())
    except Exception:
        pass

    # Compute utilization density (objective, independent of capacity_ok).
    # capacity_ok only means "fits"; density tells the LLM HOW full the region is,
    # so it can judge congestion risk before committing to place_design.
    # For local pblocks (bound_cells), density is the BOUND cells' occupancy of
    # the region — not the whole design's — so is_soft reflects true constraint
    # pressure (a few bound cells in a sized region → low density → hard pblock).
    est_luts = max(estimated.get("luts", 1), 1)
    if sizing_basis == "bound_cells":
        utilization_density = bound_resources["luts"] / est_luts
    else:
        utilization_density = required_lut / est_luts
    density_warning = utilization_density > max_utilization_density

    bind_critical_path_cells_to_pblock = True
    fallback_mode = "local_bound_cells" if sizing_basis == "bound_cells" else "whole_design"
    fallback_reason = None

    use_whole_design_fallback, fallback_reason = _should_use_whole_design_fallback(
        sizing_basis=sizing_basis,
        columns_used=region["columns_used"],
        utilization_density=utilization_density,
        target_lut_count=target_lut_count,
        target_ff_count=target_ff_count,
        bound_resources=bound_resources,
    )
    if use_whole_design_fallback:
        logger.info(
            "analyze_pblock_region: triggering whole-design fallback | reason=%s",
            fallback_reason,
        )
        try:
            from skills.smart_region_search import smart_region_search
            whole_required_lut = int(target_lut_count * adaptive_multiplier)
            whole_required_ff = int(target_ff_count * adaptive_multiplier)
            whole_required_dsp = int(target_dsp_count * resource_multiplier)
            whole_required_bram = int(target_bram_count * resource_multiplier)
            fallback_result = smart_region_search(
                design,
                target_lut_count=whole_required_lut,
                target_ff_count=whole_required_ff,
                target_dsp_count=whole_required_dsp,
                target_bram_count=whole_required_bram,
                critical_path_cells=critical_path_cells,
                distance_weight_factor=distance_weight_factor,
            )
            if fallback_result is not None:
                required_lut = whole_required_lut
                required_ff = whole_required_ff
                required_dsp = whole_required_dsp
                required_bram = whole_required_bram
                required = {
                    "luts": required_lut,
                    "ffs": required_ff,
                    "dsps": required_dsp,
                    "brams": required_bram,
                }
                region = {
                    "col_min": fallback_result.col_min,
                    "col_max": fallback_result.col_max,
                    "row_min": fallback_result.row_min,
                    "row_max": fallback_result.row_max,
                    "center_col": fallback_result.center_col,
                    "center_row": fallback_result.center_row,
                    "columns_used": fallback_result.columns_used,
                    "rows_used": fallback_result.rows_used,
                }
                pblock_ranges = fallback_result.pblock_ranges
                estimated = {
                    "luts": fallback_result.estimated_luts,
                    "ffs": fallback_result.estimated_ffs,
                    "dsps": fallback_result.estimated_dsps,
                    "brams": fallback_result.estimated_brams,
                }
                capacity_ok = fallback_result.capacity_ok
                capacity_basis = "whole_design_fallback"
                region_selection_reason = (
                    "whole_design_fallback (local bound-cell pblock was too narrow "
                    "for a distributed design)"
                )
                multi_region = fallback_result.multi_region_suggestions
                sizing_basis = "whole_design_fallback"
                deficit = (
                    _build_deficit(
                        estimated, required_lut, required_ff, required_dsp, required_bram
                    )
                    if not capacity_ok else None
                )
                utilization_density = required_lut / max(estimated.get("luts", 1), 1)
                density_warning = utilization_density > max_utilization_density
                bind_critical_path_cells_to_pblock = False
                fallback_mode = "whole_design_soft"
            else:
                fallback_reason = (
                    f"{fallback_reason}; fallback search returned no region, keeping local plan"
                )
        except Exception as e:
            logger.warning("Whole-design PBLOCK fallback failed: %s", e)
            fallback_reason = f"{fallback_reason}; fallback search failed: {e}"

    # Step 5: Build advice (density-graded when sufficient, deficit-driven when not)
    is_soft_recommended = False
    if capacity_ok:
        is_soft_recommended = utilization_density > 0.8
        if not bind_critical_path_cells_to_pblock:
            is_soft_recommended = True
        advice = _build_advice_sufficient(utilization_density)
        advice.append(
            f"IS_SOFT={'1' if is_soft_recommended else '0'} recommended "
            f"(utilization density: {utilization_density:.1%})."
        )
        if not bind_critical_path_cells_to_pblock:
            advice.append(
                "Fallback enabled: apply this pblock without binding cells so the "
                "region stays soft/wide for distributed-design recovery."
            )
        if density_warning:
            advice.append(
                f"WARNING: utilization density {utilization_density:.1%} exceeds "
                f"max_utilization_density {max_utilization_density:.0%} — place_design "
                f"is likely to congest. Consider raising resource_multiplier or "
                f"skipping PBLOCK for this design."
            )
    else:
        advice = _build_advice_insufficient(
            deficit or {}, full_device, required_lut, required_ff,
            resource_multiplier, multi_region,
        )

    # Step 6: Build next_steps — ONLY if capacity is sufficient
    next_steps = None
    if capacity_ok:
        soft_str = "true" if is_soft_recommended else "false"
        next_steps = [
            "vivado: unplace_cells(cells=critical_path_cells)  # local unplace of bound cells only",
        ]
        if bind_critical_path_cells_to_pblock:
            next_steps.append(
                f"vivado: create_and_apply_pblock with pblock_ranges above, "
                f"pblock_name=pblock_tight, is_soft={soft_str}, cells=critical_path_cells"
            )
        else:
            next_steps.append(
                f"vivado: create_and_apply_pblock with pblock_ranges above, "
                f"pblock_name=pblock_tight, is_soft={soft_str}  # whole-design fallback, do not bind cells"
            )
        next_steps.extend([
            "vivado: place_design (re-place unplaced cells within pblock constraint)",
            "vivado: route_design",
            "vivado: report_timing_summary (verify WNS improvement after PBLOCK re-placement)",
        ])

    # Build message
    if capacity_ok:
        qualifier = " (expanded via fallback)" if expanded else ""
        fallback_note = ""
        if not bind_critical_path_cells_to_pblock:
            qualifier = f"{qualifier} (whole-design fallback)".strip()
            fallback_note = " Local bound-cell plan was deemed too narrow; pblock binding is disabled."
        density_tag = f" [WARNING: density {utilization_density:.1%} > {max_utilization_density:.0%}]" if density_warning else ""
        sizing_note = ""
        if sizing_basis == "bound_cells":
            sizing_note = (
                f" (sized on {bound_resources['matched']} bound cells: "
                f"LUT={bound_resources['luts']}, FF={bound_resources['ffs']})"
            )
        elif sizing_basis == "whole_design_fallback":
            sizing_note = " (fell back from bound-cell sizing to wider whole-design region)"
        msg = (
            f"PBLOCK region found{qualifier}: cols {region['col_min']}-{region['col_max']}, "
            f"rows {region['row_min']}-{region['row_max']}. "
            f"Estimated: {estimated['luts']:,} LUTs, {estimated['ffs']:,} FFs, "
            f"{estimated['dsps']} DSPs, {estimated['brams']} BRAMs. "
            f"Capacity OK (target: {required_lut:,} LUTs x{adaptive_multiplier}, "
            f"{required_ff:,} FFs x{adaptive_multiplier}){sizing_note}. "
            f"Utilization density {utilization_density:.1%}{density_tag}. "
            f"Multiplier: {input_multiplier}→{adaptive_multiplier} ({multiplier_transform})."
            f"{fallback_note}"
        )
    else:
        d_lut = deficit.get("luts", 0) if deficit else 0
        d_ff = deficit.get("ffs", 0) if deficit else 0
        msg = (
            f"PBLOCK region insufficient: cols {region['col_min']}-{region['col_max']}, "
            f"rows {region['row_min']}-{region['row_max']}. "
            f"Estimated: {estimated['luts']:,} LUTs, {estimated['ffs']:,} FFs. "
            f"Required: {required_lut:,} LUTs, {required_ff:,} FFs. "
            f"Deficit: LUTs={d_lut:,}, FFs={d_ff:,}. "
            f"Do NOT apply pblock — capacity insufficient."
        )

    logger.info(
        "analyze_pblock_region: region [%d-%d, %d-%d] | "
        "estimated LUT=%d FF=%d | required LUT=%d FF=%d | "
        "capacity_ok=%s | deficit=%s | multi_region=%d",
        region["col_min"], region["col_max"], region["row_min"], region["row_max"],
        estimated["luts"], estimated["ffs"],
        required_lut, required_ff,
        capacity_ok, deficit,
        len(multi_region) if multi_region else 0,
    )

    return {
        "status": "success",
        "message": msg,
        "region": region,
        "pblock_ranges": pblock_ranges,
        "pblock_name": pblock_name,
        "estimated_resources": estimated,
        "target_resources": required,
        # Multiplier transparency: input (LLM-provided) vs final (after adaptive
        # adjustment) plus the transform reason, so the LLM can predict how its
        # resource_multiplier parameter maps to the actual region size.
        "resource_multiplier": adaptive_multiplier,
        "input_multiplier": input_multiplier,
        "final_multiplier": adaptive_multiplier,
        "multiplier_transform": multiplier_transform,
        "capacity_ok": capacity_ok,
        "capacity_basis": capacity_basis,
        "region_selection_reason": region_selection_reason,
        # Objective density metrics: capacity_ok only means "fits"; density tells
        # the LLM how full the region is so it can judge congestion risk.
        "utilization_density": utilization_density,
        "density_warning": density_warning,
        "max_utilization_density": max_utilization_density,
        "deficit": deficit,
        "advice": advice,
        "multi_region_suggestions": multi_region,
        "next_steps": next_steps,
        "is_soft_recommended": is_soft_recommended,
        # Echo back the critical-path cells so the PBLOCK auto-chain can pass
        # them to vivado_unplace_cells (local unplace) and to
        # vivado_create_and_apply_pblock(cells=...) (local pblock). Empty when
        # no critical paths were available — the chain's unplace step will
        # surface that as an error (data quality guard upstream should prevent).
        "critical_path_cells": critical_path_cells or [],
        "bind_critical_path_cells_to_pblock": bind_critical_path_cells_to_pblock,
        # Local-pblock sizing transparency (2026-07-05): when critical_path_cells
        # resolve, the region is sized for those bound cells (not the whole
        # design), density reflects bound-cell occupancy, and is_soft follows
        # true density. Lets the LLM see what drove the region size.
        "sizing_basis": sizing_basis,
        "bound_resources": bound_resources,
        "bound_cell_count": bound_resources["matched"] if bound_resources else 0,
        "pblock_fallback_mode": fallback_mode,
        "pblock_fallback_reason": fallback_reason,
    }


def generate_pblock_plan(
    design,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    resource_multiplier: float = 1.5,
    critical_path_cells: list[str] | None = None,
    distance_weight_factor: float = 0.3,
    max_utilization_density: float = 0.90,
    frozen_pblock_plan: dict | None = None,
) -> dict:
    """Build deterministic PBLOCK candidates and return the frozen recommendation."""
    if design is None:
        logger.warning("generate_pblock_plan: design is None")
        return {
            "status": "error",
            "message": "Design not loaded",
            "error_details": "context.design is None",
        }

    if target_lut_count <= 0:
        return {
            "status": "skipped",
            "message": (
                f"Invalid resource targets: LUT={target_lut_count} "
                f"(must be positive). Run report_utilization_for_pblock first to get "
                f"actual resource counts."
            ),
            "target_resources": {
                "luts": target_lut_count,
                "ffs": target_ff_count,
                "dsps": target_dsp_count,
                "brams": target_bram_count,
            },
        }

    input_multiplier = resource_multiplier
    adaptive_multiplier = compute_adaptive_resource_multiplier(
        target_lut_count, target_ff_count, base_multiplier=resource_multiplier
    )
    multiplier_transform = _describe_multiplier_transform(
        target_lut_count, target_ff_count, input_multiplier, adaptive_multiplier
    )
    critical_snapshot = list(critical_path_cells or [])

    full_device = {}
    try:
        from skills.smart_region_search import estimate_full_device_resources
        full_device = estimate_full_device_resources(design.getDevice())
    except Exception:
        pass

    if frozen_pblock_plan:
        selected_plan = recommend_pblock_plan([frozen_pblock_plan])[0]
        if selected_plan is None:
            return {
                "status": "error",
                "message": "Invalid frozen_pblock_plan payload",
                "error_details": "frozen_pblock_plan could not be parsed",
            }
        ordered_candidates = [selected_plan]
        bound_resources = dict(selected_plan.bound_resources or {})
    else:
        bound_resources = (
            _estimate_bound_cell_resources(design, critical_snapshot)
            if critical_snapshot else None
        )
        local_plan = None
        local_fallback_reason = None
        if (
            bound_resources
            and critical_snapshot
            and bound_resources["matched"] >= max(1, len(critical_snapshot) // 2)
        ):
            local_required_lut = int(bound_resources["luts"] * adaptive_multiplier)
            local_required_ff = int(bound_resources["ffs"] * adaptive_multiplier)
            local_required_dsp = int(bound_resources["dsps"] * resource_multiplier)
            local_required_bram = int(bound_resources["brams"] * resource_multiplier)
            local_result = _search_candidate_region(
                design,
                required_lut=local_required_lut,
                required_ff=local_required_ff,
                required_dsp=local_required_dsp,
                required_bram=local_required_bram,
                critical_path_cells=critical_snapshot,
                distance_weight_factor=distance_weight_factor,
            )
            if local_result.get("status") == "success":
                local_density = bound_resources["luts"] / max(local_result["estimated"].get("luts", 1), 1)
                _, local_fallback_reason = _should_use_whole_design_fallback(
                    sizing_basis="bound_cells",
                    columns_used=local_result["region"]["columns_used"],
                    utilization_density=local_density,
                    target_lut_count=target_lut_count,
                    target_ff_count=target_ff_count,
                    bound_resources=bound_resources,
                )
                local_plan = _build_execution_plan(
                    candidate_id="local_bound_cells",
                    plan_mode=PBLOCK_LOCAL_MODE,
                    pblock_ranges=local_result["pblock_ranges"],
                    resource_multiplier=adaptive_multiplier,
                    target_lut_count=local_required_lut,
                    target_ff_count=local_required_ff,
                    target_dsp_count=local_required_dsp,
                    target_bram_count=local_required_bram,
                    bind_cells_to_pblock=True,
                    unplace_mode=PBLOCK_UNPLACE_LOCAL,
                    is_soft=local_density > 0.8,
                    place_directive="Explore",
                    route_directive="Explore",
                    reference_col=local_result["reference_col"],
                    reference_row=local_result["reference_row"],
                    selection_reason=f"local_bound_cells:{local_result['region_selection_reason']}",
                    fallback_reason=local_fallback_reason,
                    critical_path_cells_snapshot=critical_snapshot,
                    capacity_ok=local_result["capacity_ok"],
                    estimated_resources=local_result["estimated"],
                    region=local_result["region"],
                    utilization_density=local_density,
                    bound_resources=bound_resources,
                )

        whole_required_lut = int(target_lut_count * adaptive_multiplier)
        whole_required_ff = int(target_ff_count * adaptive_multiplier)
        whole_required_dsp = int(target_dsp_count * resource_multiplier)
        whole_required_bram = int(target_bram_count * resource_multiplier)
        reference_points = _get_reference_points(design, critical_snapshot)
        global_candidates: list[PblockExecutionPlan] = []
        for candidate_id, (reference_col, reference_row) in reference_points.items():
            bounded_result = _search_global_replacement_region(
                design,
                target_lut_count=target_lut_count,
                target_ff_count=target_ff_count,
                target_dsp_count=target_dsp_count,
                target_bram_count=target_bram_count,
                resource_multiplier=adaptive_multiplier,
                reference_col=reference_col,
                reference_row=reference_row,
            )
            candidate_results: list[tuple[str, dict]] = []
            if bounded_result.get("status") == "success":
                candidate_results.append((candidate_id, bounded_result))
            if bounded_result.get("status") != "success" or not bounded_result.get("capacity_ok"):
                capacity_result = _search_candidate_region(
                    design,
                    required_lut=whole_required_lut,
                    required_ff=whole_required_ff,
                    required_dsp=whole_required_dsp,
                    required_bram=whole_required_bram,
                    reference_col=reference_col,
                    reference_row=reference_row,
                    distance_weight_factor=distance_weight_factor,
                )
                if capacity_result.get("status") == "success":
                    candidate_results.append((f"{candidate_id}_capacity_fallback", capacity_result))

            for result_candidate_id, result in candidate_results:
                global_density = float(
                    result.get(
                        "utilization_density",
                        max(
                            target_lut_count / max(result["estimated"].get("luts", 1), 1),
                            target_ff_count / max(result["estimated"].get("ffs", 1), 1),
                        ),
                    )
                )
                global_candidates.append(
                    _build_execution_plan(
                        candidate_id=result_candidate_id,
                        plan_mode=PBLOCK_GLOBAL_MODE,
                        pblock_ranges=result["pblock_ranges"],
                        resource_multiplier=adaptive_multiplier,
                        target_lut_count=target_lut_count,
                        target_ff_count=target_ff_count,
                        target_dsp_count=target_dsp_count,
                        target_bram_count=target_bram_count,
                        bind_cells_to_pblock=False,
                        unplace_mode=PBLOCK_UNPLACE_GLOBAL,
                        is_soft=True,
                        place_directive="Explore",
                        route_directive="Explore",
                        reference_col=reference_col,
                        reference_row=reference_row,
                        selection_reason=f"{result_candidate_id}:{result['region_selection_reason']}",
                        fallback_reason=local_fallback_reason,
                        critical_path_cells_snapshot=critical_snapshot,
                        capacity_ok=result["capacity_ok"],
                        estimated_resources=result["estimated"],
                        region=result["region"],
                        utilization_density=global_density,
                    )
                )

        selected_plan, ordered_candidates = recommend_pblock_plan(
            [plan for plan in [local_plan, *global_candidates] if plan is not None],
            critical_path_reference=reference_points.get("global_cp_center"),
        )
        if selected_plan is None:
            return {
                "status": "error",
                "message": "PBLOCK planning produced no candidate plans",
                "target_resources": {
                    "luts": whole_required_lut,
                    "ffs": whole_required_ff,
                    "dsps": whole_required_dsp,
                    "brams": whole_required_bram,
                },
                "resource_multiplier": adaptive_multiplier,
            }

    estimated = dict(selected_plan.estimated_resources or {})
    region = dict(selected_plan.region or {})
    required = {
        "luts": selected_plan.target_lut_count,
        "ffs": selected_plan.target_ff_count,
        "dsps": selected_plan.target_dsp_count,
        "brams": selected_plan.target_bram_count,
    }
    capacity_ok = bool(selected_plan.capacity_ok)
    utilization_density = (
        float(selected_plan.utilization_density)
        if selected_plan.utilization_density is not None else 0.0
    )
    density_warning = utilization_density > max_utilization_density
    deficit = (
        _build_deficit(
            estimated,
            selected_plan.target_lut_count,
            selected_plan.target_ff_count,
            selected_plan.target_dsp_count,
            selected_plan.target_bram_count,
        )
        if not capacity_ok else None
    )

    if capacity_ok:
        advice = _build_advice_sufficient(utilization_density)
        advice.append(
            f"IS_SOFT={'1' if selected_plan.is_soft else '0'} recommended "
            f"(utilization density: {utilization_density:.1%})."
        )
        if not selected_plan.bind_cells_to_pblock:
            advice.append(
                "Global replacement mode selected: do not bind cells to the pblock; "
                "use a wide soft region plus global unplace."
            )
        if density_warning:
            advice.append(
                f"WARNING: utilization density {utilization_density:.1%} exceeds "
                f"max_utilization_density {max_utilization_density:.0%}."
            )
    else:
        advice = _build_advice_insufficient(
            deficit or {},
            full_device,
            selected_plan.target_lut_count,
            selected_plan.target_ff_count,
            selected_plan.resource_multiplier,
            [],
        )

    next_steps = None
    if capacity_ok:
        soft_str = "true" if selected_plan.is_soft else "false"
        if selected_plan.unplace_mode == PBLOCK_UNPLACE_GLOBAL:
            next_steps = [
                "vivado: place_design -unplace",
                f"vivado: create_and_apply_pblock with pblock_ranges above, pblock_name={selected_plan.pblock_name}, is_soft={soft_str}",
                "vivado: place_design",
                "vivado: route_design",
                "vivado: report_timing_summary",
            ]
        else:
            next_steps = [
                "vivado: unplace_cells(cells=critical_path_cells)",
                f"vivado: create_and_apply_pblock with pblock_ranges above, pblock_name={selected_plan.pblock_name}, is_soft={soft_str}, cells=critical_path_cells",
                "vivado: place_design",
                "vivado: route_design",
                "vivado: report_timing_summary",
            ]

    msg = (
        f"PBLOCK plan selected: {selected_plan.candidate_id} ({selected_plan.plan_mode}). "
        f"Region cols {region.get('col_min')}-{region.get('col_max')}, "
        f"rows {region.get('row_min')}-{region.get('row_max')}. "
        f"Estimated {estimated.get('luts', 0):,} LUTs / {estimated.get('ffs', 0):,} FFs. "
        f"Target {selected_plan.target_lut_count:,} LUTs / {selected_plan.target_ff_count:,} FFs. "
        f"Capacity {'OK' if capacity_ok else 'INSUFFICIENT'}. "
        f"Multiplier: {input_multiplier}→{adaptive_multiplier} ({multiplier_transform})."
    )
    if selected_plan.fallback_reason:
        msg += f" Fallback note: {selected_plan.fallback_reason}."

    return {
        "status": "success",
        "message": msg,
        "region": region,
        "pblock_ranges": selected_plan.pblock_ranges,
        "pblock_name": selected_plan.pblock_name,
        "estimated_resources": estimated,
        "target_resources": required,
        "resource_multiplier": selected_plan.resource_multiplier,
        "input_multiplier": input_multiplier,
        "final_multiplier": adaptive_multiplier,
        "multiplier_transform": multiplier_transform,
        "capacity_ok": capacity_ok,
        "capacity_basis": selected_plan.selection_reason,
        "region_selection_reason": selected_plan.selection_reason,
        "utilization_density": utilization_density,
        "density_warning": density_warning,
        "max_utilization_density": max_utilization_density,
        "deficit": deficit,
        "advice": advice,
        "multi_region_suggestions": [],
        "next_steps": next_steps,
        "is_soft_recommended": selected_plan.is_soft,
        "critical_path_cells": list(selected_plan.critical_path_cells_snapshot),
        "bind_critical_path_cells_to_pblock": selected_plan.bind_cells_to_pblock,
        "sizing_basis": (
            "bound_cells" if selected_plan.plan_mode == PBLOCK_LOCAL_MODE else "whole_design"
        ),
        "bound_resources": bound_resources,
        "bound_cell_count": bound_resources["matched"] if bound_resources else 0,
        "pblock_fallback_mode": selected_plan.plan_mode,
        "pblock_fallback_reason": selected_plan.fallback_reason,
        "candidate_plans": [plan.to_dict() for plan in ordered_candidates],
        "recommended_candidate_id": selected_plan.candidate_id,
        "selected_pblock_plan": selected_plan.to_dict(),
        "reference_col": selected_plan.reference_col,
        "reference_row": selected_plan.reference_row,
    }


def _validate_pblock_inputs(**kwargs) -> tuple[bool, str]:
    """Validate pblock skill inputs: target_lut_count and target_ff_count must be positive ints.

    Shared between PblockStrategySkill and ExecutePblockStrategySkill
    to avoid code duplication.
    """
    if "target_lut_count" not in kwargs:
        return False, "target_lut_count is required"
    if "target_ff_count" not in kwargs:
        return False, "target_ff_count is required"
    lut = kwargs["target_lut_count"]
    ff = kwargs["target_ff_count"]
    if not isinstance(lut, int) or lut <= 0:
        return False, "target_lut_count must be a positive integer"
    if not isinstance(ff, int) or ff <= 0:
        return False, "target_ff_count must be a positive integer"
    return True, ""


@skill(
    name="pblock_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="PBLOCK Region Analysis",
    description="Analyze FPGA fabric to find optimal PBLOCK region for re-placement. "
                "READ-ONLY analysis. Returns region coordinates, pblock_ranges string, "
                "estimated resources, capacity validation (capacity_ok), deficit, advice, "
                "and next_steps (ONLY when capacity is sufficient). "
                "Trigger: recommendation == 'PBLOCK' or avg spread > 70 tiles. "
                "NOTE: If resource_multiplier is too high (default 1.5x), the returned region "
                "may be larger than necessary. Reduce to 1.0x-1.2x for congested designs. "
                "PREFERRED: Use execute_pblock_strategy instead for automatic Vivado tool chaining.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=[],
    timeout_ms=60000,
    parameters=[
        ParameterSpec("target_lut_count", int,
                      "Current LUT usage from Vivado report_utilization_for_pblock"),
        ParameterSpec("target_ff_count", int,
                      "Current FF usage from Vivado report_utilization_for_pblock"),
        ParameterSpec("target_dsp_count", int,
                      "Current DSP usage", default=0),
        ParameterSpec("target_bram_count", int,
                      "Current BRAM usage", default=0),
        ParameterSpec("resource_multiplier", float,
                      "Buffer multiplier for resource targets", default=1.5),
        ParameterSpec("critical_path_cells", list,
                      "Cells to BIND to the pblock (constraint targets, from Dashboard "
                      "critical_paths). When provided and resolved in the design, the "
                      "region is sized around these cells (local pblock), not the whole "
                      "design; is_soft follows the bound cells' true density.",
                      default=None),
        ParameterSpec("distance_weight_factor", float,
                      "Distance weight in region scoring (0.3 default, higher = more centering on critical paths)",
                      default=0.3),
        ParameterSpec("max_utilization_density", float,
                      "Max allowed region utilization density (0.0-1.0). When the selected region's "
                      "density exceeds this, density_warning=true is returned in the result. Lower this "
                      "(e.g. 0.80) for high-utilization designs to avoid congested pblocks. Default 0.90.",
                      default=0.90),
        ParameterSpec("frozen_pblock_plan", dict,
                      "Optional frozen PBLOCK plan from ANALYZE/SELECT. When provided, EXECUTE "
                      "must not re-plan the region and should only execute this plan.",
                      default=None),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class PblockStrategySkill(Skill):
    """READ-ONLY pblock region analysis skill.

    Analyzes FPGA fabric to find the optimal pblock region for re-placement.
    Returns region coordinates, pblock_ranges, estimated resources,
    capacity validation, deficit (LUT/FF/DSP/BRAM), IS_SOFT recommendation,
    and next_steps (only when capacity_ok).

    Does NOT modify the design. Use ExecutePblockStrategySkill for the full
    workflow (analysis + auto-chained Vivado tools).
    """

    def execute(self, context: SkillContext,
                target_lut_count: int, target_ff_count: int,
                target_dsp_count: int = 0, target_bram_count: int = 0,
                resource_multiplier: float = 1.5,
                critical_path_cells: list[str] | None = None,
                distance_weight_factor: float = 0.3,
                max_utilization_density: float = 0.90) -> SkillResult:
        try:
            result = generate_pblock_plan(
                context.design,
                target_lut_count, target_ff_count,
                target_dsp_count, target_bram_count,
                resource_multiplier,
                critical_path_cells=critical_path_cells,
                distance_weight_factor=distance_weight_factor,
                max_utilization_density=max_utilization_density,
            )
            is_error = result.get("status") == "error"
            error_msg = result.get("message") if is_error else None
            return SkillResult(success=not is_error, data=result, error=error_msg)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        return _validate_pblock_inputs(**kwargs)


@skill(
    name="execute_pblock_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="Execute PBLOCK Full Strategy",
    description="Complete PBLOCK workflow: analyze FPGA fabric, compute optimal region, "
                "and return pblock_ranges for automatic Vivado tool chaining. "
                "MUTATING (via chained Vivado tools). "
                "Side effects: cell placement changes, routing changes (via chain). "
                "Trigger: avg_distance > 70 tiles (distributed design), or recommendation == 'PBLOCK'. "
                "ORDERING: For distributed designs (avg_distance > 70), run this BEFORE fanout optimization. "
                "The system will automatically chain unplace_cells(critical_path_cells), "
                "create_and_apply_pblock(cells=critical_path_cells, is_soft=...), "
                "place_design, route_design, and report_timing_summary after this skill returns. "
                "When critical_path_cells resolve, the region is sized for those bound cells "
                "(local pblock), not the whole design. "
                "Prerequisite: vivado_report_utilization_for_pblock to get LUT/FF counts. "
                "NOTE: resource_multiplier defaults to 2.0x for execute mode; "
                "this is the validated logicnets baseline, while analysis mode "
                "can remain tighter/adaptive. "
                "CONSTRAINTS: Only proceed if capacity_ok is true in the result.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["cell_placement", "checkpoint_file"],
    timeout_ms=120000,
    parameters=[
        ParameterSpec("target_lut_count", int,
                      "Current LUT usage from vivado_report_utilization_for_pblock"),
        ParameterSpec("target_ff_count", int,
                      "Current FF usage from vivado_report_utilization_for_pblock"),
        ParameterSpec("target_dsp_count", int,
                      "Current DSP usage", default=0),
        ParameterSpec("target_bram_count", int,
                      "Current BRAM usage", default=0),
        ParameterSpec("resource_multiplier", float,
                      "Buffer multiplier for resource targets. Default 2.0x for execute mode (validated on logicnets).", default=2.0),
        ParameterSpec("critical_path_cells", list,
                      "Cells to BIND to the pblock (constraint targets, from Dashboard "
                      "critical_paths). When provided and resolved in the design, the "
                      "region is sized around these cells (local pblock), not the whole "
                      "design; is_soft follows the bound cells' true density.",
                      default=None),
        ParameterSpec("distance_weight_factor", float,
                      "Distance weight in region scoring (0.3 default, higher = more centering on critical paths)",
                      default=0.3),
        ParameterSpec("max_utilization_density", float,
                      "Max allowed region utilization density (0.0-1.0). When the selected region's "
                      "density exceeds this, density_warning=true is returned in the result. Lower this "
                      "(e.g. 0.80) for high-utilization designs to avoid congested pblocks. Default 0.90.",
                      default=0.90),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class ExecutePblockStrategySkill(Skill):
    """Full PBLOCK execution workflow: analyze + auto-chained Vivado tools.

    Same analysis as PblockStrategySkill, but designed for automatic Vivado
    tool chaining. The optimizer's SKILL_CHAIN_ACTIONS will auto-execute:
        vivado_unplace_cells(cells=critical_path_cells) →
        vivado_create_and_apply_pblock(cells=critical_path_cells, is_soft=...) →
        vivado_place_design →
        vivado_route_design

    When critical_path_cells resolve, the region is sized for those bound
    cells (local pblock), not the whole design. If any chain step fails, the
    design is restored from a pre-chain checkpoint. Critical paths are
    auto-refreshed after placement-affecting chain tools.
    """

    def execute(self, context: SkillContext,
                target_lut_count: int, target_ff_count: int,
                target_dsp_count: int = 0, target_bram_count: int = 0,
                resource_multiplier: float = 2.0,
                critical_path_cells: list[str] | None = None,
                distance_weight_factor: float = 0.3,
                max_utilization_density: float = 0.90,
                frozen_pblock_plan: dict | None = None) -> SkillResult:
        try:
            result = generate_pblock_plan(
                context.design,
                target_lut_count, target_ff_count,
                target_dsp_count, target_bram_count,
                resource_multiplier,
                critical_path_cells=critical_path_cells,
                distance_weight_factor=distance_weight_factor,
                max_utilization_density=max_utilization_density,
                frozen_pblock_plan=frozen_pblock_plan,
            )
            if result.get("status") == "error":
                return SkillResult(success=False, data=result,
                                   error=result.get("message", "PBLOCK analysis failed"))
            if not result.get("capacity_ok"):
                return SkillResult(success=False, data=result,
                                   error=f"PBLOCK capacity insufficient: {result.get('deficit', {})}")
            return SkillResult(success=True, data=result)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        return _validate_pblock_inputs(**kwargs)
