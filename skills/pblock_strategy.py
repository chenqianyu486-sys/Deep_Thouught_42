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


def generate_pblock_plan(
    design,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    resource_multiplier: float = 1.5,
) -> dict:
    """Analyze FPGA fabric to find optimal PBLOCK region.

    Args:
        design: RapidWright Design object
        target_lut_count: Current LUT usage (from Vivado report)
        target_ff_count: Current FF usage
        target_dsp_count: Current DSP usage
        target_bram_count: Current BRAM usage
        resource_multiplier: Buffer multiplier for resource targets (default 1.5x)

    Returns:
        Dict with status, region, pblock_ranges, estimated_resources, next_steps
    """
    if design is None:
        logger.warning("generate_pblock_plan: design is None")
        return {
            "status": "error",
            "message": "Design not loaded",
            "error_details": "context.design is None",
        }

    if target_lut_count <= 0 or target_ff_count <= 0:
        return {
            "status": "skipped",
            "message": f"Invalid resource targets: LUT={target_lut_count}, FF={target_ff_count} (must be positive). "
                       f"Run report_utilization_for_pblock first to get actual resource counts.",
            "target_resources": {
                "luts": target_lut_count,
                "ffs": target_ff_count,
                "dsps": target_dsp_count,
                "brams": target_bram_count,
            },
        }

    # Apply resource multiplier
    adjusted_lut = int(target_lut_count * resource_multiplier)
    adjusted_ff = int(target_ff_count * resource_multiplier)
    adjusted_dsp = int(target_dsp_count * resource_multiplier)
    adjusted_bram = int(target_bram_count * resource_multiplier)

    # Step 1: Call smart_region_search for optimal pblock region
    region_result = None
    region_error = None
    try:
        from skills.smart_region_search import smart_region_search
        region_result = smart_region_search(
            design,
            target_lut_count=adjusted_lut,
            target_ff_count=adjusted_ff,
            target_dsp_count=adjusted_dsp,
            target_bram_count=adjusted_bram,
        )
    except Exception as e:
        region_error = str(e)

    if region_error or region_result is None:
        return {
            "status": "error",
            "message": f"Smart region search failed: {region_error or 'unknown error'}",
            "error_details": region_error,
            "target_resources": {
                "luts": target_lut_count, "ffs": target_ff_count,
                "dsps": target_dsp_count, "brams": target_bram_count,
            },
            "resource_multiplier": resource_multiplier,
        }

    if region_result.status != "success":
        return {
            "status": "error",
            "message": f"Region search did not succeed: {region_result.message}",
            "error_details": region_result.message,
            "target_resources": {
                "luts": adjusted_lut, "ffs": adjusted_ff,
                "dsps": adjusted_dsp, "brams": adjusted_bram,
            },
            "region_status": region_result.status,
        }

    pblock_name = "pblock_tight"

    return {
        "status": "success",
        "message": (f"PBLOCK region found: cols {region_result.col_min}-{region_result.col_max}, "
                     f"rows {region_result.row_min}-{region_result.row_max}. "
                     f"Estimated resources: {region_result.estimated_luts:,} LUTs, "
                     f"{region_result.estimated_ffs:,} FFs, "
                     f"{region_result.estimated_dsps} DSPs, "
                     f"{region_result.estimated_brams} BRAMs."),
        "region": {
            "col_min": region_result.col_min,
            "col_max": region_result.col_max,
            "row_min": region_result.row_min,
            "row_max": region_result.row_max,
            "center_col": region_result.center_col,
            "center_row": region_result.center_row,
            "columns_used": region_result.columns_used,
            "rows_used": region_result.rows_used,
        },
        "pblock_ranges": region_result.pblock_ranges,
        "pblock_name": pblock_name,
        "estimated_resources": {
            "luts": region_result.estimated_luts,
            "ffs": region_result.estimated_ffs,
            "dsps": region_result.estimated_dsps,
            "brams": region_result.estimated_brams,
        },
        "target_resources": {
            "luts": adjusted_lut,
            "ffs": adjusted_ff,
            "dsps": adjusted_dsp,
            "brams": adjusted_bram,
        },
        "resource_multiplier": resource_multiplier,
        "next_steps": [
            "vivado: place_design -unplace",
            "vivado: create_and_apply_pblock with pblock_ranges above, pblock_name=pblock_tight, is_soft=false",
            "vivado: place_design (re-place cells within pblock constraint)",
            "vivado: route_design",
            "vivado: report_timing_summary (verify WNS improvement after PBLOCK re-placement)",
        ],
    }


@skill(
    name="pblock_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="PBLOCK Region Analysis",
    description="Analyze FPGA fabric to find optimal PBLOCK region for re-placement. "
                "READ-ONLY analysis. Returns region coordinates, pblock_ranges string, "
                "and estimated resources. Use the returned pblock_ranges to call Vivado "
                "tools (create_and_apply_pblock, place_design, route_design) yourself. "
                "Trigger: recommendation == 'PBLOCK' or avg spread > 70 tiles.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=[],
    timeout_ms=600000,
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
