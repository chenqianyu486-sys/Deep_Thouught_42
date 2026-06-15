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


logger = logging.getLogger(__name__)


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


def _build_advice_sufficient() -> list[str]:
    """Build advice array for sufficient capacity scenario."""
    return [
        "Region capacity is sufficient for the target resources. "
        "You can safely proceed with pblock creation and placement."
    ]


def generate_pblock_plan(
    design,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    resource_multiplier: float = 1.5,
    critical_path_cells: list[str] | None = None,
    distance_weight_factor: float = 0.3,
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
    adaptive_multiplier = compute_adaptive_resource_multiplier(
        target_lut_count, target_ff_count, base_multiplier=resource_multiplier
    )
    logger.info(
        "Adaptive resource multiplier: base=%.1f, adaptive=%.1f (LUT=%d, FF=%d)",
        resource_multiplier, adaptive_multiplier, target_lut_count, target_ff_count
    )
    required_lut = int(target_lut_count * adaptive_multiplier)
    required_ff = int(target_ff_count * adaptive_multiplier)
    required_dsp = int(target_dsp_count * resource_multiplier)
    required_bram = int(target_bram_count * resource_multiplier)

    logger.info(
        "analyze_pblock_region: target LUT=%d FF=%d DSP=%d BRAM=%d | "
        "multiplier=%.1fx | required LUT=%d FF=%d DSP=%d BRAM=%d",
        target_lut_count, target_ff_count, target_dsp_count, target_bram_count,
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

    # Step 5: Build advice (use smart_region_search's advice as base, augment with our own)
    is_soft_recommended = False
    if capacity_ok:
        est_luts = estimated.get("luts", 1)
        utilization_density = required_lut / max(est_luts, 1)
        is_soft_recommended = utilization_density > 0.8
        advice = _build_advice_sufficient()
        advice.append(
            f"IS_SOFT={'1' if is_soft_recommended else '0'} recommended "
            f"(utilization density: {utilization_density:.1%})."
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
            "vivado: place_design -unplace",
            f"vivado: create_and_apply_pblock with pblock_ranges above, "
            f"pblock_name=pblock_tight, is_soft={soft_str}",
            "vivado: place_design (re-place cells within pblock constraint)",
            "vivado: route_design",
            "vivado: report_timing_summary (verify WNS improvement after PBLOCK re-placement)",
        ]

    # Build message
    if capacity_ok:
        qualifier = " (expanded via fallback)" if expanded else ""
        msg = (
            f"PBLOCK region found{qualifier}: cols {region['col_min']}-{region['col_max']}, "
            f"rows {region['row_min']}-{region['row_max']}. "
            f"Estimated: {estimated['luts']:,} LUTs, {estimated['ffs']:,} FFs, "
            f"{estimated['dsps']} DSPs, {estimated['brams']} BRAMs. "
            f"Capacity OK (target: {required_lut:,} LUTs x{resource_multiplier}, "
            f"{required_ff:,} FFs x{resource_multiplier})."
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
        "resource_multiplier": adaptive_multiplier,
        "capacity_ok": capacity_ok,
        "deficit": deficit,
        "advice": advice,
        "multi_region_suggestions": multi_region,
        "next_steps": next_steps,
        "is_soft_recommended": is_soft_recommended,
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
                      "Critical path cell names for region centering (from Dashboard critical_paths)",
                      default=None),
        ParameterSpec("distance_weight_factor", float,
                      "Distance weight in region scoring (0.3 default, higher = more centering on critical paths)",
                      default=0.3),
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
                distance_weight_factor: float = 0.3) -> SkillResult:
        try:
            result = generate_pblock_plan(
                context.design,
                target_lut_count, target_ff_count,
                target_dsp_count, target_bram_count,
                resource_multiplier,
                critical_path_cells=critical_path_cells,
                distance_weight_factor=distance_weight_factor,
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
                "The system will automatically chain place_design -unplace, create_and_apply_pblock, "
                "place_design, route_design, and report_timing_summary after this skill returns. "
                "Prerequisite: vivado_report_utilization_for_pblock to get LUT/FF counts. "
                "NOTE: resource_multiplier defaults to 1.2x for tighter regions. "
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
                      "Buffer multiplier for resource targets. Default 1.2x for tighter regions.", default=1.2),
        ParameterSpec("critical_path_cells", list,
                      "Critical path cell names for region centering (from Dashboard critical_paths)",
                      default=None),
        ParameterSpec("distance_weight_factor", float,
                      "Distance weight in region scoring (0.3 default, higher = more centering on critical paths)",
                      default=0.3),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class ExecutePblockStrategySkill(Skill):
    """Full PBLOCK execution workflow: analyze + auto-chained Vivado tools.

    Same analysis as PblockStrategySkill, but designed for automatic Vivado
    tool chaining. The optimizer's SKILL_CHAIN_ACTIONS will auto-execute:
        vivado_place_design(-unplace) →
        vivado_create_and_apply_pblock(is_soft from recommendation) →
        vivado_place_design →
        vivado_route_design

    If any chain step fails, the design is restored from a pre-chain checkpoint.
    Critical paths are auto-refreshed after placement-affecting chain tools.
    """

    def execute(self, context: SkillContext,
                target_lut_count: int, target_ff_count: int,
                target_dsp_count: int = 0, target_bram_count: int = 0,
                resource_multiplier: float = 1.2,
                critical_path_cells: list[str] | None = None,
                distance_weight_factor: float = 0.3) -> SkillResult:
        try:
            result = generate_pblock_plan(
                context.design,
                target_lut_count, target_ff_count,
                target_dsp_count, target_bram_count,
                resource_multiplier,
                critical_path_cells=critical_path_cells,
                distance_weight_factor=distance_weight_factor,
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
