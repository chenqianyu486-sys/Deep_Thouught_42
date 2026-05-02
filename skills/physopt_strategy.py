# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Physical Optimization Strategy Skill.

Generates a structured execution plan for Vivado phys_opt_design.
This is a planning-only skill — all steps are marked for Vivado execution.
"""

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.strategy_plan import StrategyPlan, StrategyStep


def _count_placed_cells(design) -> int:
    """Count placed cells in the design for precondition validation."""
    try:
        count = 0
        for cell in design.getCells():
            if cell.isPlaced():
                count += 1
        return count
    except Exception:
        return 0


def generate_physopt_plan(
    design,
    directive: str = "Default",
    design_is_routed: bool = True
) -> StrategyPlan:
    """Generate a PhysOpt execution plan.

    Args:
        design: RapidWright Design object (for precondition checks)
        directive: phys_opt_design directive
        design_is_routed: Whether the design is currently routed

    Returns:
        StrategyPlan with Vivado execution steps
    """
    if design is None:
        return StrategyPlan(
            strategy_name="PhysOpt",
            status="error",
            message="Design not loaded",
            preconditions_satisfied=False,
            error_details="context.design is None",
        )

    placed_count = _count_placed_cells(design)
    if placed_count == 0:
        return StrategyPlan(
            strategy_name="PhysOpt",
            status="skipped",
            message="No placed cells found — phys_opt_design requires a placed design",
            preconditions_satisfied=False,
            analysis_summary={"placed_cells": 0},
        )

    valid_directives = [
        "Default", "Explore", "ExploreWithHoldFix", "ExploreWithAggressiveHoldFix",
        "AggressiveExplore", "AlternateReplication", "AggressiveFanoutOpt",
        "AlternateFlowWithRetiming", "AddRetime", "RuntimeOptimized", "RQS",
    ]
    resolved_directive = directive if directive in valid_directives else "Default"

    steps = [
        StrategyStep(
            step_name="phys_opt_design",
            platform="Vivado",
            params={"directive": resolved_directive},
            description="Run physical optimization to improve timing (WNS/TNS)",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="route_design",
            platform="Vivado",
            params={},
            description="Re-route after phys_opt changes",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="report_timing_summary",
            platform="Vivado",
            params={},
            description="Verify timing improvement after phys_opt",
            executed=False,
            expected_duration_seconds=60,
        ),
    ]

    return StrategyPlan(
        strategy_name="PhysOpt",
        status="ready",
        message=f"PhysOpt plan ready: {resolved_directive} on {placed_count} placed cells",
        preconditions_satisfied=True,
        analysis_summary={
            "placed_cells": placed_count,
            "directive": resolved_directive,
            "design_is_routed": design_is_routed,
        },
        steps=steps,
    )


@skill(
    name="physopt_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="Physical Optimization Strategy",
    description="Generate PhysOpt execution plan for Vivado. READ-ONLY. "
                "Physical optimization improves timing by replicating cells, "
                "retiming, and swapping LUT pins. "
                "Trigger: 1-2 critical paths with spread but no high fanout.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=[],
    timeout_ms=30000,
    parameters=[
        ParameterSpec("directive", str,
                      "phys_opt_design directive: Default, Explore, AggressiveExplore, or AddRetime",
                      default="Default"),
        ParameterSpec("design_is_routed", bool,
                      "Whether the design is currently routed", default=True),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class PhysOptStrategySkill(Skill):
    """Skill for generating Physical Optimization execution plans."""

    def execute(self, context: SkillContext,
                directive: str = "Default",
                design_is_routed: bool = True) -> SkillResult:
        try:
            plan = generate_physopt_plan(context.design, directive, design_is_routed)
            return SkillResult(success=(plan.status != "error"), data=plan)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        directive = kwargs.get("directive", "Default")
        valid = ["Default", "Explore", "ExploreWithHoldFix", "ExploreWithAggressiveHoldFix",
                 "AggressiveExplore", "AlternateReplication", "AggressiveFanoutOpt",
                 "AlternateFlowWithRetiming", "AddRetime", "RuntimeOptimized", "RQS"]
        if directive not in valid:
            return False, f"Invalid directive '{directive}'. Valid: {', '.join(valid)}"
        return True, ""
