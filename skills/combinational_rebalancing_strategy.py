# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Combinational Logic Rebalancing Strategy Skill.

Validation-safe "retiming" that does NOT insert or move flip-flops.
Instead, it identifies deep combinational cones (LUT6/LUT5/MUXF7/MUXF8
cascades between registers) on critical paths and delegates to Vivado
opt_design -remap for logic-equivalent resynthesis to rebalance logic
depth across stages. Design latency (clock cycles to output) is preserved,
so cycle-exact validation (validate_dcps.py) passes.

Rationale: true register retiming inserts/moves FFs -> changes latency ->
fails cycle-exact validation. This strategy achieves the same goal
(shortening critical-path logic depth) via combinational resynthesis only.
"""

import logging

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.strategy_plan import StrategyPlan, StrategyStep

logger = logging.getLogger(__name__)

# Combinational cell types that contribute to logic depth between registers.
# FFs (FDRE/FDCE/...) and DSP/BRAM break combinational chains.
_COMB_TYPES = {"LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6",
               "MUXF7", "MUXF8", "MUXF9", "CARRY4", "CARRY8"}

ALLOWED_DIRECTIVES = ["Default", "Explore", "ExploreArea", "AddRemap"]


def _is_comb_cell(cell) -> bool:
    """Check if a cell is a combinational logic type (not a register)."""
    try:
        return str(cell.getType()) in _COMB_TYPES
    except Exception:
        return False


def _find_deep_combinational_segments(
    design,
    critical_paths: list[list[str]],
    min_depth: int = 3,
) -> list[dict]:
    """Find deep combinational segments on critical paths.

    Walks each critical path and splits it at register boundaries (FDRE,
    FDCE, ...), keeping consecutive combinational cell runs whose depth
    exceeds min_depth. These are the segments where logic-equivalent
    resynthesis can rebalance logic depth.

    Args:
        design: RapidWright Design object
        critical_paths: List of paths, each a list of cell names
        min_depth: Minimum combinational depth to report

    Returns:
        List of segment dicts: {path_index, depth, cell_names, type_counts}
    """
    if design is None:
        return []

    segments = []

    for path_idx, path in enumerate(critical_paths):
        current_chain = []
        for cell_name in path:
            try:
                cell = design.getCell(cell_name)
            except Exception:
                cell = None

            if cell is not None and _is_comb_cell(cell):
                current_chain.append((cell_name, str(cell.getType())))
            else:
                # Register or unknown cell breaks the combinational chain
                if len(current_chain) >= min_depth:
                    segments.append(_build_segment(path_idx, current_chain))
                current_chain = []

        # Handle chain ending at path boundary
        if len(current_chain) >= min_depth:
            segments.append(_build_segment(path_idx, current_chain))

    return segments


def _build_segment(path_idx: int, chain: list[tuple[str, str]]) -> dict:
    """Build a segment dict from a combinational chain."""
    type_counts: dict[str, int] = {}
    for _, ctype in chain:
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
    return {
        "path_index": path_idx,
        "depth": len(chain),
        "cell_names": [name for name, _ in chain],
        "type_counts": type_counts,
    }


def generate_combinational_rebalance_plan(
    design,
    critical_paths: list[list[str]],
    min_depth: int = 3,
    directive: str = "Explore",
    retarget: bool = True,
) -> StrategyPlan:
    """Generate a combinational rebalancing execution plan.

    Args:
        design: RapidWright Design object (for cell-type lookups)
        critical_paths: List of paths, each a list of cell names
        min_depth: Minimum combinational depth to target
        directive: opt_design directive
        retarget: Retarget logic to equivalent primitives

    Returns:
        StrategyPlan with Vivado opt_design + place + route + report steps
    """
    if design is None:
        return StrategyPlan(
            strategy_name="CombinationalRebalance",
            status="error",
            message="Design not loaded",
            preconditions_satisfied=False,
            error_details="context.design is None",
        )

    if not critical_paths:
        return StrategyPlan(
            strategy_name="CombinationalRebalance",
            status="skipped",
            message="No critical paths provided",
            preconditions_satisfied=False,
        )

    resolved_directive = directive if directive in ALLOWED_DIRECTIVES else "Explore"

    segments = _find_deep_combinational_segments(design, critical_paths, min_depth)

    if not segments:
        return StrategyPlan(
            strategy_name="CombinationalRebalance",
            status="skipped",
            message=(f"No combinational segments with depth >= {min_depth} found. "
                     "Logic depth is already balanced or paths are register-bound."),
            preconditions_satisfied=False,
            analysis_summary={
                "segments_found": 0,
                "min_depth": min_depth,
                "note": "No deep combinational cones to rebalance.",
            },
        )

    max_depth = max(s["depth"] for s in segments)
    avg_depth = sum(s["depth"] for s in segments) / len(segments)

    steps = [
        StrategyStep(
            step_name="opt_design",
            platform="Vivado",
            params={"directive": resolved_directive, "retarget": retarget},
            description="Logic-equivalent resynthesis to rebalance combinational depth "
                        "(remap + retarget, NO retiming — preserves latency)",
            executed=False,
            expected_duration_seconds=600,
        ),
        StrategyStep(
            step_name="place_design",
            platform="Vivado",
            params={"directive": "Explore"},
            description="Re-place design after netlist resynthesis",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="route_design",
            platform="Vivado",
            params={"directive": "Explore", "reuse": True},
            description="Re-route design",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="report_timing_summary",
            platform="Vivado",
            params={},
            description="Evaluate timing after rebalance + place + route",
            executed=False,
            expected_duration_seconds=30,
        ),
    ]

    return StrategyPlan(
        strategy_name="CombinationalRebalance",
        status="ready",
        message=(f"Rebalance plan: {len(segments)} deep combinational segments "
                 f"(max depth {max_depth}, avg {avg_depth:.1f}). "
                 f"Vivado opt_design {resolved_directive} will resynthesize logic "
                 f"equivalently without inserting FFs."),
        preconditions_satisfied=True,
        steps=steps,
        analysis_summary={
            "strategy_type": "combinational_rebalancing",
            "target": "reduce logic depth via logic-equivalent resynthesis (no FF insert)",
            "directive": resolved_directive,
            "retarget": retarget,
            "segments_found": len(segments),
            "max_depth": max_depth,
            "avg_depth": round(avg_depth, 2),
            "min_depth": min_depth,
            "validation_safe": True,
            "latency_preserved": True,
            "note": "Unlike register retiming, this inserts NO new FFs. "
                    "opt_design -remap is logic-equivalent resynthesis only.",
        },
    )


@skill(
    name="combinational_rebalancing_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="Combinational Logic Rebalancing (validation-safe retiming)",
    description="Validation-safe alternative to register retiming: identify deep "
                "combinational cones (LUT6/MUXF7/MUXF8 cascades) on critical paths "
                "and delegate to Vivado opt_design -remap for logic-equivalent "
                "resynthesis to rebalance logic depth. Inserts NO flip-flops — "
                "design latency is preserved, so cycle-exact validation passes. "
                "MUTATING. Side effects: netlist remapping (logic-equivalent), "
                "checkpoint via Vivado chain. Trigger: WNS stuck, deep combinational "
                "chains between registers on critical paths, FF insertion unsafe. "
                "After this, chain auto-executes opt_design -> place -> route -> report.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=["netlist_modification", "cell_remapping"],
    timeout_ms=600000,
    parameters=[
        ParameterSpec("critical_paths", list,
                      "List of paths from Vivado extract_critical_path_cells: "
                      "[[cell1, cell2, ...], ...]. Provide at most 10 paths."),
        ParameterSpec("min_depth", int,
                      "Minimum combinational depth (LUT/MUXF levels between registers) "
                      "to target. Default 3.",
                      default=3),
        ParameterSpec("directive", str,
                      "opt_design directive. 'Explore' is balanced; 'AddRemap' is more "
                      "aggressive at LUT equation remapping.",
                      default="Explore"),
        ParameterSpec("retarget", bool,
                      "Retarget logic to equivalent primitives (e.g., LUT5->LUT6 merge). "
                      "Safe — does not change function.",
                      default=True),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class CombinationalRebalancingSkill(Skill):
    """Skill for combinational logic rebalancing (validation-safe retiming)."""

    def execute(self, context: SkillContext,
                critical_paths: list[list[str]],
                min_depth: int = 3,
                directive: str = "Explore",
                retarget: bool = True) -> SkillResult:
        try:
            plan = generate_combinational_rebalance_plan(
                context.design, critical_paths, min_depth, directive, retarget,
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
        return True, ""
