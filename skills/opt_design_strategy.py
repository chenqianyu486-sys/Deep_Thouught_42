# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Logic Optimization (opt_design) Strategy Skill.

Generates a structured execution plan for Vivado opt_design.
This is a planning-only skill — Vivado execution steps are handled
by the skill chain actions (SKILL_CHAIN_ACTIONS in constants.py).
"""

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.strategy_plan import StrategyPlan, StrategyStep


# Allowed opt_design directives (safe: no retiming options exist for opt_design)
ALLOWED_DIRECTIVES = [
    "Default", "Explore", "ExploreArea",
    "ExploreSequentialArea", "RuntimeOptimized", "AddRemap",
]


def generate_opt_design_plan(
    design,
    directive: str = "Explore",
    retarget: bool = True,
) -> StrategyPlan:
    """Generate an opt_design execution plan.

    Args:
        design: RapidWright Design object (for precondition checks)
        directive: opt_design directive (default: Explore for UltraScale+ compatibility)
        retarget: Whether to retarget logic to equivalent primitives

    Returns:
        StrategyPlan with Vivado execution parameters
    """
    if design is None:
        return StrategyPlan(
            strategy_name="OptDesign",
            status="error",
            message="Design not loaded",
            preconditions_satisfied=False,
            error_details="context.design is None",
        )

    # Validate directive
    resolved_directive = directive if directive in ALLOWED_DIRECTIVES else "Explore"
    if directive not in ALLOWED_DIRECTIVES:
        return StrategyPlan(
            strategy_name="OptDesign",
            status="error",
            message=f"Invalid directive '{directive}'. Allowed: {ALLOWED_DIRECTIVES}",
            preconditions_satisfied=False,
            error_details=f"directive '{directive}' not in allowed list",
        )

    steps = [
        StrategyStep(
            step_name="opt_design",
            platform="Vivado",
            params={"directive": resolved_directive, "retarget": retarget},
            description="Run logic-level optimization to reduce LUT depth via remapping and constant propagation",
            executed=False,
            expected_duration_seconds=600,
        ),
        StrategyStep(
            step_name="place_design",
            platform="Vivado",
            params={},
            description="Re-place design after netlist modification",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="route_design",
            platform="Vivado",
            params={},
            description="Re-route design",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="report_timing_summary",
            platform="Vivado",
            params={},
            description="Evaluate timing after opt_design + place + route",
            executed=False,
            expected_duration_seconds=30,
        ),
    ]

    return StrategyPlan(
        strategy_name="OptDesign",
        status="ready",
        message=f"opt_design plan generated with directive={resolved_directive}, retarget={retarget}",
        preconditions_satisfied=True,
        steps=steps,
        execution_params={
            "directive": resolved_directive,
            "retarget": retarget,
        },
        analysis_summary={
            "strategy_type": "logic_optimization",
            "target": "reduce LUT depth via logic remapping",
            "note": "opt_design runs BEFORE placement — safe for all design types. "
                    "No retiming risk (unlike phys_opt_design).",
        },
    )


@skill(
    name="opt_design_strategy",
    display_name="Logic Optimization (opt_design)",
    description="Generate execution plan for Vivado opt_design: logic-level optimization "
                "(retarget, remap, constant propagation). Targets LUT-depth bottlenecks "
                "in combinational-dominated designs. Runs BEFORE placement — no retiming risk. "
                "Use when PhysOpt (post-place) is ineffective due to pure logic depth.",
    category=SkillCategory.OPTIMIZATION,
    parameters={
        "directive": ParameterSpec(
            type="string",
            description="opt_design directive. 'Explore' is safest for UltraScale+ (xcvu3p). "
                        "'AddRemap' is more aggressive (remaps LUT equations) but may not benefit UltraScale+.",
            default="Explore",
            allowed_values=ALLOWED_DIRECTIVES,
        ),
        "retarget": ParameterSpec(
            type="boolean",
            description="Retarget logic to equivalent primitives (e.g., LUT5→LUT6 merge). Safe — does not change function.",
            default=True,
        ),
    },
    side_effects=["netlist_modification", "cell_remapping"],
    timeout_default_ms=600000,
    timeout_max_ms=1800000,
)
async def opt_design_strategy(context: SkillContext) -> SkillResult:
    """Generate opt_design execution plan."""
    directive = context.parameters.get("directive", "Explore")
    retarget = context.parameters.get("retarget", True)

    plan = generate_opt_design_plan(
        design=context.design,
        directive=directive,
        retarget=retarget,
    )

    return SkillResult(
        success=plan.status != "error",
        data=plan.to_dict(),
        message=plan.to_dict().get("message", ""),
    )
