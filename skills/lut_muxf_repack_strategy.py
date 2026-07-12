# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
LUT6 + MUXF Co-Repack Strategy Skill.

Targets neural-network / wide-datapath designs where critical paths are
dominated by LUT6 -> MUXF7 -> MUXF8 -> LUT6 cascades (8:1/16:1 mux trees
that exceed the 6-input LUT physical limit). Unlike flatten_lut_cascade
(which is ineffective on such wide cones), this strategy delegates to
Vivado opt_design -directive AddRemap for aggressive LUT-equation
repacking that merges LUT5/LUT6 pairs and restructures MUXF+LUT6
adjacencies — without changing function or inserting FFs.

Validation-safe: opt_design -remap is logic-equivalent resynthesis only.
Design latency is preserved, so cycle-exact validation (validate_dcps.py)
passes.
"""

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.strategy_plan import StrategyPlan, StrategyStep

_LUT_TYPES = {"LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6"}
_MUXF_TYPES = {"MUXF7", "MUXF8", "MUXF9"}

ALLOWED_DIRECTIVES = ["Default", "Explore", "ExploreArea", "ExploreWithRemap", "AddRemap"]


def _cell_type(design, cell_name: str) -> str:
    """Look up a cell's type string, or "" on failure."""
    try:
        cell = design.getCell(cell_name)
        if cell is not None:
            return str(cell.getType())
    except Exception:
        pass
    return ""


def _find_lut_muxf_pairs(
    design,
    critical_paths: list[list[str]],
) -> list[dict]:
    """Find LUT6 <-> MUXF7/MUXF8 adjacency pairs on critical paths.

    Scans each path for adjacent cells where one is a LUT and the next is
    a MUXF (or vice-versa). These are the structural units that
    opt_design -AddRemap can restructure into fewer logic levels.

    Args:
        design: RapidWright Design object
        critical_paths: List of paths, each a list of cell names

    Returns:
        List of pair dicts: {path_index, lut_cell, lut_type, muxf_cell,
        muxf_type, direction}
    """
    if design is None:
        return []

    pairs = []

    for path_idx, path in enumerate(critical_paths):
        for i in range(len(path) - 1):
            t1 = _cell_type(design, path[i])
            t2 = _cell_type(design, path[i + 1])
            if not t1 or not t2:
                continue

            if t1 in _LUT_TYPES and t2 in _MUXF_TYPES:
                pairs.append({
                    "path_index": path_idx,
                    "lut_cell": path[i], "lut_type": t1,
                    "muxf_cell": path[i + 1], "muxf_type": t2,
                    "direction": "LUT->MUXF",
                })
            elif t1 in _MUXF_TYPES and t2 in _LUT_TYPES:
                pairs.append({
                    "path_index": path_idx,
                    "lut_cell": path[i + 1], "lut_type": t2,
                    "muxf_cell": path[i], "muxf_type": t1,
                    "direction": "MUXF->LUT",
                })

    return pairs


def _find_lut5_lut6_merge_candidates(
    design,
    critical_paths: list[list[str]],
) -> list[dict]:
    """Find LUT5 cells that could merge into LUT6 (retarget candidates).

    Identifies LUT5 cells on critical paths that opt_design -retarget can
    merge into adjacent LUT6 sites, reducing cell count and routing.
    """
    if design is None:
        return []

    candidates = []
    seen = set()

    for path in critical_paths:
        for cell_name in path:
            if cell_name in seen:
                continue
            seen.add(cell_name)
            if _cell_type(design, cell_name) == "LUT5":
                candidates.append({"cell": cell_name, "type": "LUT5"})

    return candidates


def generate_lut_muxf_repack_plan(
    design,
    critical_paths: list[list[str]],
    directive: str = "AddRemap",
    retarget: bool = True,
) -> StrategyPlan:
    """Generate a LUT6+MUXF co-repack execution plan.

    Args:
        design: RapidWright Design object
        critical_paths: List of paths, each a list of cell names
        directive: opt_design directive (AddRemap recommended for aggressive
                   LUT-equation repacking)
        retarget: Retarget LUT5 -> LUT6 merge candidates

    Returns:
        StrategyPlan with Vivado opt_design + place + route + report steps
    """
    if design is None:
        return StrategyPlan(
            strategy_name="LUTMUXFRepack",
            status="error",
            message="Design not loaded",
            preconditions_satisfied=False,
            error_details="context.design is None",
        )

    if not critical_paths:
        return StrategyPlan(
            strategy_name="LUTMUXFRepack",
            status="skipped",
            message="No critical paths provided",
            preconditions_satisfied=False,
        )

    resolved_directive = directive if directive in ALLOWED_DIRECTIVES else "AddRemap"

    pairs = _find_lut_muxf_pairs(design, critical_paths)
    lut5_candidates = _find_lut5_lut6_merge_candidates(design, critical_paths)

    if not pairs and not lut5_candidates:
        # Analyze input cell types for diagnostics — helps EVALUATE distinguish
        # "strategy is wrong fit" from "input data quality issue".
        cell_types: dict[str, int] = {}
        total_cells = 0
        for path in critical_paths:
            for cell_name in path:
                total_cells += 1
                ctype = _cell_type(design, cell_name)
                cell_types[ctype] = cell_types.get(ctype, 0) + 1
        sorted_types = dict(sorted(cell_types.items(), key=lambda x: -x[1])[:10])
        lut_muxf_count = sum(
            v for k, v in cell_types.items()
            if k in _LUT_TYPES or k in _MUXF_TYPES
        )
        if lut_muxf_count == 0:
            quality_note = (
                "INPUT DATA QUALITY ISSUE: No LUT or MUXF cells found in provided paths "
                f"(cell types: {sorted_types}). Ensure critical_paths are extracted "
                "via vivado_extract_critical_path_cells, not raw TCL."
            )
        else:
            quality_note = (
                f"LUT/MUXF cells found ({lut_muxf_count}) but none form adjacent "
                f"LUT<->MUXF pairs or LUT5 merge candidates. Cell types: {sorted_types}."
            )

        return StrategyPlan(
            strategy_name="LUTMUXFRepack",
            status="skipped",
            message=("No LUT<->MUXF adjacency pairs or LUT5 merge candidates found "
                     f"on critical paths. Cell type distribution: {sorted_types} "
                     f"({total_cells} total cells). {quality_note}"),
            preconditions_satisfied=False,
            analysis_summary={
                "lut_muxf_pairs": 0,
                "lut5_candidates": 0,
                "cell_type_distribution": dict(sorted(cell_types.items(), key=lambda x: -x[1])),
                "total_cells_analyzed": total_cells,
                "lut_muxf_cells_found": lut_muxf_count,
                "diagnosis": "input_data_quality" if lut_muxf_count == 0 else "no_adjacent_pairs",
                "note": quality_note,
            },
        )

    # Direction breakdown
    dir_counts: dict[str, int] = {}
    for p in pairs:
        dir_counts[p["direction"]] = dir_counts.get(p["direction"], 0) + 1

    steps = [
        StrategyStep(
            step_name="opt_design",
            platform="Vivado",
            params={"directive": resolved_directive, "retarget": retarget},
            description="Aggressive LUT-equation repacking (AddRemap) + LUT5->LUT6 "
                        "retarget. Logic-equivalent — no FF insert, latency preserved.",
            executed=False,
            expected_duration_seconds=600,
        ),
        StrategyStep(
            step_name="place_design",
            platform="Vivado",
            params={"directive": "Explore"},
            description="Re-place design after LUT/MUXF repacking",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="route_design",
            platform="Vivado",
            params={"directive": "Explore"},
            description="Re-route design",
            executed=False,
            expected_duration_seconds=300,
        ),
        StrategyStep(
            step_name="report_timing_summary",
            platform="Vivado",
            params={},
            description="Evaluate timing after repack + place + route",
            executed=False,
            expected_duration_seconds=30,
        ),
    ]

    return StrategyPlan(
        strategy_name="LUTMUXFRepack",
        status="ready",
        message=(f"Repack plan: {len(pairs)} LUT<->MUXF pairs "
                 f"({dir_counts}), {len(lut5_candidates)} LUT5 merge candidates. "
                 f"Vivado opt_design {resolved_directive} will attempt equivalent "
                 f"LUT6/MUXF restructuring (diagnostic plan; opt_design re-derives "
                 f"remap opportunities itself, so per-pair application is not guaranteed)."),
        preconditions_satisfied=True,
        steps=steps,
        analysis_summary={
            "strategy_type": "lut_muxf_repack",
            "target": "merge LUT5->LUT6 + restructure LUT6/MUXF adjacencies",
            "directive": resolved_directive,
            "retarget": retarget,
            "lut_muxf_pairs": len(pairs),
            "lut5_candidates": len(lut5_candidates),
            "direction_breakdown": dir_counts,
            "validation_safe": True,
            "latency_preserved": True,
            "note": "AddRemap aggressively remaps LUT equations but is still "
                    "logic-equivalent. No FFs inserted.",
        },
    )


@skill(
    name="lut_muxf_repack_strategy",
    namespace="optimization",
    version="1.0.0",
    display_name="LUT6+MUXF Co-Repack (validation-safe LUT merge)",
    description="Validation-safe LUT merge strategy for NN/wide-datapath designs: "
                "identify LUT6<->MUXF7/MUXF8 adjacency pairs and LUT5 merge "
                "candidates on critical paths, then delegate to Vivado "
                "opt_design -directive AddRemap for aggressive logic-equivalent "
                "LUT-equation repacking. Targets the 8:1/16:1 mux tree cascades "
                "that flatten_lut_cascade cannot handle (cones >6 inputs). "
                "Inserts NO flip-flops — latency preserved, cycle-exact "
                "validation passes. MUTATING. Side effects: netlist remapping "
                "(logic-equivalent), checkpoint via Vivado chain. "
                "Trigger: NN/datapath design, MUXF7/MUXF8 + LUT6 cascade on "
                "critical paths, flatten_lut_cascade returned optimized_count=0. "
                "After this, chain auto-executes opt_design -> place -> route -> report.",
    category=SkillCategory.OPTIMIZATION,
    idempotency="safe",
    side_effects=["netlist_modification", "cell_remapping"],
    timeout_ms=600000,
    parameters=[
        ParameterSpec("critical_paths", list,
                      "List of paths from Vivado extract_critical_path_cells: "
                      "[[cell1, cell2, ...], ...]. Provide at most 10 paths."),
        ParameterSpec("directive", str,
                      "opt_design directive. 'AddRemap' is recommended for aggressive "
                      "LUT-equation repacking; 'Explore' is milder.",
                      default="AddRemap"),
        ParameterSpec("retarget", bool,
                      "Retarget LUT5 -> LUT6 merge candidates. Safe — does not "
                      "change function.",
                      default=True),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class LUTMUXFRepackSkill(Skill):
    """Skill for LUT6+MUXF co-repacking (validation-safe LUT merge)."""

    def execute(self, context: SkillContext,
                critical_paths: list[list[str]],
                directive: str = "AddRemap",
                retarget: bool = True) -> SkillResult:
        try:
            plan = generate_lut_muxf_repack_plan(
                context.design, critical_paths, directive, retarget,
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
        directive = kwargs.get("directive", "AddRemap")
        if directive not in ALLOWED_DIRECTIVES:
            return False, f"Invalid directive '{directive}'. Valid: {', '.join(ALLOWED_DIRECTIVES)}"
        return True, ""
