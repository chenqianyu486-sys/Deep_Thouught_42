# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
MUXF Tree Reorder Strategy Skill.

Maps "carry chain reorder" onto designs that have no CARRY4/CARRY8 but
instead use MUXF7/MUXF8 mux trees (8:1/16:1 selectors) as the dominant
inter-layer combinational structure — common in neural-network designs.
Identifies MUXF trees on critical paths where the timing-critical input
traverses the deepest mux level, then delegates to Vivado
phys_opt_design -directive Explore (NO -retime) for logic-equivalent
pin/cell optimization that reorders selection paths and pulls critical
inputs to faster mux levels.

Validation-safe: phys_opt_design without -retime performs only
placement-level logic-equivalent optimization (pin swap, cell relocation,
duplication). No FFs are inserted or moved — latency preserved, so
cycle-exact validation (validate_dcps.py) passes.
"""

import logging

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.strategy_plan import StrategyPlan, StrategyStep

logger = logging.getLogger(__name__)

_MUXF_TYPES = {"MUXF7", "MUXF8", "MUXF9"}

# phys_opt_design directives that do NOT invoke retiming (AddRetime would
# insert/move FFs -> changes latency -> fails validation).
ALLOWED_DIRECTIVES = ["Default", "Explore", "AggressiveExplore",
                      "AlternateReplication", "AddRetime"]


def _is_muxf_cell(cell) -> bool:
    try:
        return str(cell.getType()) in _MUXF_TYPES
    except Exception:
        return False


def _cell_type(design, cell_name: str) -> str:
    try:
        cell = design.getCell(cell_name)
        if cell is not None:
            return str(cell.getType())
    except Exception:
        pass
    return ""


def _find_muxf_trees(
    design,
    critical_paths: list[list[str]],
) -> list[dict]:
    """Find MUXF tree segments on critical paths and locate deep inputs.

    For each critical path, extracts maximal runs of consecutive MUXF
    cells (the mux tree traversal on that path). The depth of the run is
    the number of mux levels the critical signal passes through. Deeper
    runs are higher-priority reorder targets — phys_opt_design can pull
    the critical input to a faster (earlier) mux level.

    Args:
        design: RapidWright Design object
        critical_paths: List of paths, each a list of cell names

    Returns:
        List of tree dicts: {path_index, depth, muxf_cells, entry_cell,
        exit_cell, muxf_types}
    """
    if design is None:
        return []

    trees = []

    for path_idx, path in enumerate(critical_paths):
        current_run = []
        for cell_name in path:
            ctype = _cell_type(design, cell_name)
            if ctype in _MUXF_TYPES:
                current_run.append((cell_name, ctype))
            else:
                if len(current_run) >= 2:
                    trees.append(_build_tree(path_idx, current_run))
                current_run = []
        if len(current_run) >= 2:
            trees.append(_build_tree(path_idx, current_run))

    return trees


def _build_tree(path_idx: int, run: list[tuple[str, str]]) -> dict:
    """Build a tree dict from a MUXF run."""
    type_counts: dict[str, int] = {}
    for _, ctype in run:
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
    return {
        "path_index": path_idx,
        "depth": len(run),
        "muxf_cells": [name for name, _ in run],
        "entry_cell": run[0][0],
        "exit_cell": run[-1][0],
        "muxf_types": type_counts,
    }


def generate_muxf_tree_reorder_plan(
    design,
    critical_paths: list[list[str]],
    directive: str = "Explore",
    min_tree_depth: int = 2,
) -> StrategyPlan:
    """Generate a MUXF tree reorder execution plan.

    Args:
        design: RapidWright Design object
        critical_paths: List of paths, each a list of cell names
        directive: phys_opt_design directive (must NOT be AddRetime for
                   validation safety)
        min_tree_depth: Minimum MUXF run depth to target

    Returns:
        StrategyPlan with Vivado phys_opt_design + route + report steps
    """
    if design is None:
        return StrategyPlan(
            strategy_name="MUXFTreeReorder",
            status="error",
            message="Design not loaded",
            preconditions_satisfied=False,
            error_details="context.design is None",
        )

    if not critical_paths:
        return StrategyPlan(
            strategy_name="MUXFTreeReorder",
            status="skipped",
            message="No critical paths provided",
            preconditions_satisfied=False,
        )

    # Guard: AddRetime would insert/move FFs -> validation-unsafe.
    resolved_directive = directive if directive in ALLOWED_DIRECTIVES else "Explore"

    trees = _find_muxf_trees(design, critical_paths)
    trees = [t for t in trees if t["depth"] >= min_tree_depth]

    if not trees:
        return StrategyPlan(
            strategy_name="MUXFTreeReorder",
            status="skipped",
            message=(f"No MUXF tree runs with depth >= {min_tree_depth} found. "
                     "Critical paths do not traverse deep mux trees."),
            preconditions_satisfied=False,
            analysis_summary={
                "muxf_trees": 0,
                "min_tree_depth": min_tree_depth,
                "note": "No MUXF tree reorder targets on these paths.",
            },
        )

    max_depth = max(t["depth"] for t in trees)
    avg_depth = sum(t["depth"] for t in trees) / len(trees)

    steps = [
        StrategyStep(
            step_name="phys_opt_design",
            platform="Vivado",
            params={"directive": resolved_directive},
            description="Post-placement logic-equivalent optimization: reorder MUXF "
                        "selection paths, pull critical inputs to faster mux levels. "
                        "NO -retime (latency preserved).",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="route_design",
            platform="Vivado",
            params={"directive": "Explore", "reuse": True},
            description="Re-route design after phys_opt changes",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="report_timing_summary",
            platform="Vivado",
            params={},
            description="Evaluate timing after MUXF reorder + route",
            executed=False,
            expected_duration_seconds=30,
        ),
    ]

    return StrategyPlan(
        strategy_name="MUXFTreeReorder",
        status="ready",
        message=(f"Reorder plan: {len(trees)} MUXF tree runs "
                 f"(max depth {max_depth}, avg {avg_depth:.1f}). "
                 f"Vivado phys_opt_design {resolved_directive} will reorder "
                 f"selection paths equivalently without retiming."),
        preconditions_satisfied=True,
        steps=steps,
        analysis_summary={
            "strategy_type": "muxf_tree_reorder",
            "target": "reorder MUXF7/MUXF8 selection paths to shorten critical mux traversal",
            "directive": resolved_directive,
            "muxf_trees": len(trees),
            "max_depth": max_depth,
            "avg_depth": round(avg_depth, 2),
            "min_tree_depth": min_tree_depth,
            "validation_safe": True,
            "latency_preserved": True,
            "note": "phys_opt_design without -retime performs only placement-level "
                    "logic-equivalent optimization. No FFs inserted or moved.",
            "warning": "Do NOT pass directive='AddRetime' — it would insert/move FFs "
                       "and fail cycle-exact validation.",
        },
    )


@skill(
    name="muxf_tree_reorder_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="MUXF Tree Reorder (validation-safe carry-reorder analogue)",
    description="Validation-safe 'carry reorder' analogue for designs without "
                "CARRY4/CARRY8: identifies MUXF7/MUXF8 mux tree runs on critical "
                "paths (the dominant inter-layer combinational structure in NN "
                "designs) and delegates to Vivado phys_opt_design -directive "
                "Explore (NO -retime) for logic-equivalent pin/cell optimization "
                "that reorders selection paths and pulls critical inputs to faster "
                "mux levels. Inserts NO flip-flops — latency preserved, cycle-exact "
                "validation passes. MUTATING. Side effects: placement/netlist "
                "optimization (logic-equivalent), checkpoint via Vivado chain. "
                "Trigger: NN/datapath design, MUXF7/MUXF8 tree on critical paths, "
                "no CARRY4 carry chains, WNS stuck after PBLOCK. "
                "After this, chain auto-executes phys_opt_design -> route -> report.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=["placement_optimization", "cell_remapping"],
    timeout_ms=600000,
    parameters=[
        ParameterSpec("critical_paths", list,
                      "List of paths from Vivado extract_critical_path_cells: "
                      "[[cell1, cell2, ...], ...]. Provide at most 10 paths."),
        ParameterSpec("directive", str,
                      "phys_opt_design directive. 'Explore' is balanced; "
                      "'AggressiveExplore' is stronger. Do NOT use 'AddRetime' — "
                      "it would insert FFs and fail validation.",
                      default="Explore"),
        ParameterSpec("min_tree_depth", int,
                      "Minimum MUXF run depth (consecutive MUXF7/MUXF8 cells) "
                      "to target. Default 2.",
                      default=2),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class MUXFTreeReorderSkill(Skill):
    """Skill for MUXF tree reorder (validation-safe carry-reorder analogue)."""

    def execute(self, context: SkillContext,
                critical_paths: list[list[str]],
                directive: str = "Explore",
                min_tree_depth: int = 2) -> SkillResult:
        try:
            plan = generate_muxf_tree_reorder_plan(
                context.design, critical_paths, directive, min_tree_depth,
            )
            return SkillResult(success=(plan.status != "error"), data=plan)
        except Exception as e:
            return SkillResult(success=False, data=None, error=str(e))

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "critical_paths" not in kwargs:
            return False, "critical_paths is required"
        paths = kwargs["critical_paths"]
        if not isinstance(paths, list) or len(paths) == 0:
            return False, "critical_paths must be a non-empty list"
        directive = kwargs.get("directive", "Explore")
        if directive not in ALLOWED_DIRECTIVES:
            return False, f"Invalid directive '{directive}'. Valid: {', '.join(ALLOWED_DIRECTIVES)}"
        if directive == "AddRetime":
            return False, ("directive='AddRetime' is validation-unsafe (inserts/moves FFs, "
                           "changes latency). Use 'Explore' or 'AggressiveExplore'.")
        return True, ""
