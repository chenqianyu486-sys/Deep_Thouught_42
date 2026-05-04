# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
High Fanout Net Optimization Skill.

Executes RapidWright fanout optimization and writes a checkpoint directly.
Returns optimization results as a plain dict — no intermediate plan.
"""

import os

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill


def execute_fanout_optimization(
    design,
    nets: list[dict],
    temp_dir: str = "temp",
    checkpoint_prefix: str = "fanout_opt"
) -> dict:
    """Execute fanout optimization in RapidWright and return results.

    Args:
        design: RapidWright Design object (mutated in-place)
        nets: List of {"net_name": str, "fanout": int}
        temp_dir: Directory for intermediate checkpoint
        checkpoint_prefix: Checkpoint filename prefix

    Returns:
        dict with nets_processed, successful_count, failed_count,
        checkpoint_path, and per-net results
    """
    if design is None:
        return {"error": "Design not loaded"}

    if not nets:
        return {
            "nets_processed": 0,
            "skipped": True,
            "message": "No nets provided for fanout optimization",
        }

    # Step 1: Batch optimize high fanout nets
    fanout_results = None
    fanout_error = None
    try:
        from rapidwright_tools import optimize_fanout_batch
        fanout_results = optimize_fanout_batch(nets)
        if isinstance(fanout_results, dict) and "error" in fanout_results:
            fanout_error = fanout_results["error"]
    except Exception as e:
        fanout_error = str(e)

    if fanout_error:
        return {"error": f"Fanout optimization failed: {fanout_error}"}

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
        return {
            "error": f"Checkpoint write failed: {checkpoint_error}",
            "checkpoint_path": checkpoint_path,
            "fanout_results": fanout_results,
        }

    successful_count = fanout_results.get("successful_count", 0)
    failed_count = fanout_results.get("failed_count", 0)

    return {
        "nets_processed": len(nets),
        "successful_count": successful_count,
        "failed_count": failed_count,
        "checkpoint_path": checkpoint_path,
        "results": fanout_results.get("results", []),
    }


@skill(
    name="fanout_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="High Fanout Net Optimization",
    description="Split high fanout nets using RapidWright and write checkpoint. "
                "MUTATING. Side effects: net topology changes, checkpoint file written. "
                "Trigger: High fanout nets present (fanout > 100), no path spread. "
                "WARNING: Running after PBLOCK placement may worsen WNS by disrupting dense layout.",
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
    """Skill for High Fanout Net Optimization execution."""

    def execute(self, context: SkillContext,
                nets: list[dict],
                temp_dir: str = "temp",
                checkpoint_prefix: str = "fanout_opt") -> SkillResult:
        try:
            result = execute_fanout_optimization(context.design, nets, temp_dir, checkpoint_prefix)
            if "error" in result:
                return SkillResult(success=False, data=result, error=result["error"])
            return SkillResult(success=True, data=result)
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
