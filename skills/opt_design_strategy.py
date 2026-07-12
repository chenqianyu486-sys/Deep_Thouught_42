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


ALLOWED_DIRECTIVES = [
    "Default", "Explore", "ExploreWithAreaDuplication",
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
        analysis_summary={
            "strategy_type": "logic_optimization",
            "target": "reduce LUT depth via logic remapping",
            "directive": resolved_directive,
            "retarget": retarget,
            "note": "opt_design runs BEFORE placement — safe for all design types. "
                    "No retiming risk (unlike phys_opt_design).",
        },
    )


@skill(
    name="opt_design_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="Logic Optimization (opt_design)",
    description="Generate execution plan for Vivado opt_design: logic-level optimization "
                "(retarget, remap, constant propagation). Targets LUT-depth bottlenecks "
                "in combinational-dominated designs. Runs BEFORE placement — no retiming risk. "
                "Use when PhysOpt (post-place) is ineffective due to pure logic depth.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=["netlist_modification", "cell_remapping"],
    timeout_ms=600000,
    parameters=[
        ParameterSpec("directive", str,
                      "opt_design directive. 'Explore' is safest for UltraScale+ (xcvu3p). "
                      "'AddRemap' is more aggressive (remaps LUT equations) but may not benefit UltraScale+.",
                      default="Explore"),
        ParameterSpec("retarget", bool,
                      "Retarget logic to equivalent primitives (e.g., LUT5->LUT6 merge). Safe — does not change function.",
                      default=True),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class OptDesignStrategySkill(Skill):
    """Skill for generating opt_design execution plans."""

    def execute(self, context: SkillContext,
                directive: str = "Explore",
                retarget: bool = True) -> SkillResult:
        try:
            plan = generate_opt_design_plan(context.design, directive, retarget)
            return SkillResult(success=(plan.status != "error"), data=plan)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        directive = kwargs.get("directive", "Explore")
        if directive not in ALLOWED_DIRECTIVES:
            return False, f"Invalid directive '{directive}'. Valid: {', '.join(ALLOWED_DIRECTIVES)}"
        return True, ""
