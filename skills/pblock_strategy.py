# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
PBLOCK-Based Re-placement Strategy Skill.

Analyzes FPGA fabric using RapidWright to find optimal pblock region,
generates pblock ranges, and returns a structured execution plan
with both completed (RapidWright) and pending (Vivado) steps.
"""

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.strategy_plan import StrategyPlan, StrategyStep


def generate_pblock_plan(
    design,
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    resource_multiplier: float = 1.5,
) -> StrategyPlan:
    """Generate a PBLOCK re-placement plan using RapidWright fabric analysis.

    Args:
        design: RapidWright Design object
        target_lut_count: Current LUT usage (from Vivado report)
        target_ff_count: Current FF usage
        target_dsp_count: Current DSP usage
        target_bram_count: Current BRAM usage
        resource_multiplier: Buffer multiplier for resource targets (default 1.5x)

    Returns:
        StrategyPlan with executed RapidWright steps and pending Vivado steps
    """
    if design is None:
        return StrategyPlan(
            strategy_name="PBLOCK",
            status="error",
            message="Design not loaded",
            preconditions_satisfied=False,
            error_details="context.design is None",
        )

    if target_lut_count <= 0 or target_ff_count <= 0:
        return StrategyPlan(
            strategy_name="PBLOCK",
            status="skipped",
            message=f"Invalid resource targets: LUT={target_lut_count}, FF={target_ff_count} (must be positive). "
                    f"Run report_utilization_for_pblock first to get actual resource counts.",
            preconditions_satisfied=False,
            analysis_summary={
                "target_lut_count": target_lut_count,
                "target_ff_count": target_ff_count,
            },
        )

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
        return StrategyPlan(
            strategy_name="PBLOCK",
            status="error",
            message=f"Smart region search failed: {region_error or 'unknown error'}",
            preconditions_satisfied=False,
            error_details=region_error,
            analysis_summary={
                "target_lut_count": target_lut_count,
                "target_ff_count": target_ff_count,
                "resource_multiplier": resource_multiplier,
            },
        )

    if region_result.status != "success":
        return StrategyPlan(
            strategy_name="PBLOCK",
            status="error",
            message=f"Region search did not succeed: {region_result.message}",
            preconditions_satisfied=False,
            error_details=region_result.message,
            analysis_summary={
                "region_status": region_result.status,
                "target_resources": {
                    "luts": adjusted_lut, "ffs": adjusted_ff,
                    "dsps": adjusted_dsp, "brams": adjusted_bram,
                },
            },
        )

    # Build pblock name
    pblock_name = "pblock_tight"

    # Build the full PBLOCK execution plan
    steps = [
        StrategyStep(
            step_name="report_utilization_for_pblock",
            platform="Vivado",
            params={"timeout": 300.0},
            description="Get current resource utilization from Vivado",
            executed=False,
            expected_duration_seconds=30,
        ),
        StrategyStep(
            step_name="analyze_fabric_for_pblock",
            platform="RapidWright",
            params={
                "target_lut_count": adjusted_lut,
                "target_ff_count": adjusted_ff,
                "target_dsp_count": adjusted_dsp,
                "target_bram_count": adjusted_bram,
            },
            description="Analyze FPGA fabric for optimal pblock region",
            executed=True,
            expected_duration_seconds=60,
        ),
        StrategyStep(
            step_name="convert_fabric_region_to_pblock",
            platform="RapidWright",
            params={"use_clock_regions": False},
            description="Convert fabric region to Vivado pblock ranges",
            executed=True,
            expected_duration_seconds=30,
        ),
        StrategyStep(
            step_name="place_design",
            platform="Vivado",
            params={"directive": "unplace"},
            description="Unplace all cells before applying pblock constraint",
            executed=False,
            expected_duration_seconds=60,
        ),
        StrategyStep(
            step_name="create_and_apply_pblock",
            platform="Vivado",
            params={
                "pblock_name": pblock_name,
                "ranges": region_result.pblock_ranges,
                "is_soft": False,
                "validate_resources": True,
            },
            description="Create and apply pblock constraint",
            executed=False,
            expected_duration_seconds=30,
        ),
        StrategyStep(
            step_name="place_design",
            platform="Vivado",
            params={},
            description="Re-place cells within pblock",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="route_design",
            platform="Vivado",
            params={},
            description="Route design after re-placement",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="report_timing_summary",
            platform="Vivado",
            params={},
            description="Verify timing after PBLOCK re-placement",
            executed=False,
            expected_duration_seconds=60,
        ),
    ]

    return StrategyPlan(
        strategy_name="PBLOCK",
        status="ready",
        message=f"PBLOCK plan ready. Region: cols {region_result.col_min}-{region_result.col_max}, "
                f"rows {region_result.row_min}-{region_result.row_max}",
        preconditions_satisfied=True,
        analysis_summary={
            "region": {
                "col_min": region_result.col_min,
                "col_max": region_result.col_max,
                "row_min": region_result.row_min,
                "row_max": region_result.row_max,
                "center_col": region_result.center_col,
                "center_row": region_result.center_row,
            },
            "estimated_resources": {
                "luts": region_result.estimated_luts,
                "ffs": region_result.estimated_ffs,
                "dsps": region_result.estimated_dsps,
                "brams": region_result.estimated_brams,
            },
            "pblock_ranges": region_result.pblock_ranges,
            "pblock_name": pblock_name,
            "resource_multiplier": resource_multiplier,
        },
        steps=steps,
    )


@skill(
    name="pblock_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="PBLOCK-Based Re-placement Strategy",
    description="Analyze design and generate PBLOCK re-placement plan. READ-ONLY. "
                "Uses smart_region_search to find optimal fabric region, "
                "then returns structured execution plan with Vivado TCL steps. "
                "Trigger: recommendation == 'PBLOCK' or avg spread > 70 tiles.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=[],
    timeout_ms=120000,
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
    """Skill for PBLOCK-Based Re-placement strategy planning."""

    def execute(self, context: SkillContext,
                target_lut_count: int, target_ff_count: int,
                target_dsp_count: int = 0, target_bram_count: int = 0,
                resource_multiplier: float = 1.5) -> SkillResult:
        try:
            plan = generate_pblock_plan(
                context.design,
                target_lut_count, target_ff_count,
                target_dsp_count, target_bram_count,
                resource_multiplier,
            )
            error_msg = plan.message if plan.status == "error" else None
            return SkillResult(success=(plan.status != "error"), data=plan, error=error_msg)
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
