# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
High Fanout Net Optimization Strategy Skill.

Executes RapidWright fanout optimization and writes a checkpoint,
then returns a structured plan with remaining Vivado steps.
"""

import os

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.strategy_plan import StrategyPlan, StrategyStep


def execute_fanout_optimization(
    design,
    nets: list[dict],
    temp_dir: str = "temp",
    checkpoint_prefix: str = "fanout_opt"
) -> StrategyPlan:
    """Execute fanout optimization in RapidWright and return a Vivado execution plan.

    Args:
        design: RapidWright Design object (mutated in-place)
        nets: List of {"net_name": str, "fanout": int}
        temp_dir: Directory for intermediate checkpoint
        checkpoint_prefix: Checkpoint filename prefix

    Returns:
        StrategyPlan with executed RapidWright steps and pending Vivado steps
    """
    if design is None:
        return StrategyPlan(
            strategy_name="Fanout",
            status="error",
            message="Design not loaded",
            preconditions_satisfied=False,
            error_details="context.design is None",
        )

    if not nets:
        return StrategyPlan(
            strategy_name="Fanout",
            status="skipped",
            message="No nets provided for fanout optimization",
            preconditions_satisfied=False,
            analysis_summary={"nets_provided": 0},
        )

    # Step 1: Batch optimize high fanout nets
    fanout_results = None
    fanout_error = None
    try:
        # Import lazily — rapidwright_tools module path is configured at runtime
        from rapidwright_tools import optimize_fanout_batch
        fanout_results = optimize_fanout_batch(nets)
        if isinstance(fanout_results, dict) and "error" in fanout_results:
            fanout_error = fanout_results["error"]
    except Exception as e:
        fanout_error = str(e)

    if fanout_error:
        return StrategyPlan(
            strategy_name="Fanout",
            status="error",
            message=f"Fanout optimization failed: {fanout_error}",
            preconditions_satisfied=False,
            error_details=fanout_error,
        )

    # Step 2: Write checkpoint
    os.makedirs(temp_dir, exist_ok=True)
    checkpoint_path = os.path.join(temp_dir, f"{checkpoint_prefix}_post_fanout.dcp")

    checkpoint_error = None
    try:
        from rapidwright_tools import write_checkpoint
        ckpt_result = write_checkpoint(dcp_path=checkpoint_path, overwrite=True)
        if isinstance(ckpt_result, dict) and "error" in ckpt_result:
            checkpoint_error = ckpt_result["error"]
    except Exception as e:
        checkpoint_error = str(e)

    if checkpoint_error:
        return StrategyPlan(
            strategy_name="Fanout",
            status="error",
            message=f"Checkpoint write failed: {checkpoint_error}",
            preconditions_satisfied=True,
            error_details=checkpoint_error,
            analysis_summary={
                "fanout_results": fanout_results,
                "checkpoint_path": checkpoint_path,
            },
        )

    # Build plan
    successful_count = fanout_results.get("successful_count", 0)
    failed_count = fanout_results.get("failed_count", 0)

    steps = [
        StrategyStep(
            step_name="optimize_fanout_batch",
            platform="RapidWright",
            params={"nets": nets},
            description="Split high fanout nets in RapidWright",
            executed=True,
            expected_duration_seconds=120,
        ),
        StrategyStep(
            step_name="write_checkpoint",
            platform="RapidWright",
            params={"overwrite": True, "directory": temp_dir},
            description="Save modified design for Vivado",
            executed=True,
            expected_duration_seconds=30,
        ),
        StrategyStep(
            step_name="open_checkpoint",
            platform="Vivado",
            params={"dcp_path": checkpoint_path},
            description="Load RapidWright-modified design in Vivado",
            executed=False,
            expected_duration_seconds=30,
        ),
        StrategyStep(
            step_name="place_design",
            platform="Vivado",
            params={},
            description="Re-place design after fanout netlist changes",
            executed=False,
            expected_duration_seconds=120,
        ),
        StrategyStep(
            step_name="route_design",
            platform="Vivado",
            params={},
            description="Re-route after fanout optimization",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="report_timing_summary",
            platform="Vivado",
            params={},
            description="Verify timing improvement after fanout opt",
            executed=False,
            expected_duration_seconds=60,
        ),
    ]

    return StrategyPlan(
        strategy_name="Fanout",
        status="ready",
        message=f"Fanout optimization complete: {successful_count} succeeded, {failed_count} failed. "
                f"Checkpoint: {checkpoint_path}",
        preconditions_satisfied=True,
        analysis_summary={
            "nets_processed": len(nets),
            "successful_count": successful_count,
            "failed_count": failed_count,
            "results": fanout_results.get("results", []),
            "checkpoint_path": checkpoint_path,
        },
        steps=steps,
    )


@skill(
    name="fanout_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="High Fanout Net Optimization Strategy",
    description="Split high fanout nets using RapidWright and return Vivado execution plan. "
                "MUTATING. Side effects: net topology changes, checkpoint file written. "
                "Trigger: High fanout nets present (fanout > 100), no path spread.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["net_topology", "checkpoint_file"],
    timeout_ms=300000,
    parameters=[
        ParameterSpec("nets", list,
                      "List of net configs: [{\"net_name\": str, \"fanout\": int}, ...]"),
        ParameterSpec("temp_dir", str, "Directory for intermediate checkpoint", default="temp"),
        ParameterSpec("checkpoint_prefix", str, "Checkpoint filename prefix", default="fanout_opt"),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class FanoutStrategySkill(Skill):
    """Skill for High Fanout Net Optimization strategy execution."""

    def execute(self, context: SkillContext,
                nets: list[dict],
                temp_dir: str = "temp",
                checkpoint_prefix: str = "fanout_opt") -> SkillResult:
        try:
            plan = execute_fanout_optimization(context.design, nets, temp_dir, checkpoint_prefix)
            return SkillResult(success=(plan.status != "error"), data=plan)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "nets" not in kwargs:
            return False, "nets is required"
        nets = kwargs["nets"]
        if not isinstance(nets, list) or len(nets) == 0:
            return False, "nets must be a non-empty list"
        for i, net in enumerate(nets):
            if not isinstance(net, dict) or "net_name" not in net:
                return False, f"nets[{i}]: each entry must have a 'net_name' key"
        return True, ""
