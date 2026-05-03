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

logger = logging.getLogger(__name__)


def _build_deficit(estimated: dict, required_lut: int, required_ff: int) -> dict:
    """Compute resource deficit (positive = shortfall)."""
    return {
        "luts": max(0, required_lut - estimated.get("luts", 0)),
        "ffs": max(0, required_ff - estimated.get("ffs", 0)),
        "dsps": max(0, 0),  # DSP/BRAM not primary gating
        "brams": max(0, 0),
    }


def _build_advice_insufficient(deficit: dict, full_device: dict,
                                required_lut: int, required_ff: int,
                                resource_multiplier: float,
                                multi_region: list | None = None) -> list[str]:
    """Build advice array for insufficient capacity scenario."""
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
        target_resources, capacity_ok, deficit, advice, multi_region_suggestions,
        next_steps. next_steps is non-null ONLY when capacity_ok == true.
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

    # Apply resource multiplier
    required_lut = int(target_lut_count * resource_multiplier)
    required_ff = int(target_ff_count * resource_multiplier)
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
            "resource_multiplier": resource_multiplier,
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
    deficit = _build_deficit(estimated, required_lut, required_ff) if not capacity_ok else None

    full_device = {}
    try:
        from skills.smart_region_search import estimate_full_device_resources
        full_device = estimate_full_device_resources(design.getDevice())
    except Exception:
        pass

    # Step 5: Build advice (use smart_region_search's advice as base, augment with our own)
    if capacity_ok:
        advice = _build_advice_sufficient()
    else:
        advice = _build_advice_insufficient(
            deficit or {}, full_device, required_lut, required_ff,
            resource_multiplier, multi_region,
        )

    # Step 6: Build next_steps — ONLY if capacity is sufficient
    next_steps = None
    if capacity_ok:
        next_steps = [
            "vivado: place_design -unplace",
            "vivado: create_and_apply_pblock with pblock_ranges above, "
            "pblock_name=pblock_tight, is_soft=false",
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
        "resource_multiplier": resource_multiplier,
        "capacity_ok": capacity_ok,
        "deficit": deficit,
        "advice": advice,
        "multi_region_suggestions": multi_region,
        "next_steps": next_steps,
    }


@skill(
    name="pblock_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="PBLOCK Region Analysis",
    description="Analyze FPGA fabric to find optimal PBLOCK region for re-placement. "
                "READ-ONLY analysis. Returns region coordinates, pblock_ranges string, "
                "estimated resources, capacity validation (capacity_ok), deficit, advice, "
                "and next_steps (ONLY when capacity is sufficient). "
                "Trigger: recommendation == 'PBLOCK' or avg spread > 70 tiles.",
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
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class PblockStrategySkill(Skill):
    """Skill for PBLOCK region analysis. Returns region data, not a StrategyPlan."""

    def execute(self, context: SkillContext,
                target_lut_count: int, target_ff_count: int,
                target_dsp_count: int = 0, target_bram_count: int = 0,
                resource_multiplier: float = 1.5) -> SkillResult:
        try:
            result = generate_pblock_plan(
                context.design,
                target_lut_count, target_ff_count,
                target_dsp_count, target_bram_count,
                resource_multiplier,
            )
            is_error = result.get("status") == "error"
            error_msg = result.get("message") if is_error else None
            return SkillResult(success=not is_error, data=result, error=error_msg)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
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
