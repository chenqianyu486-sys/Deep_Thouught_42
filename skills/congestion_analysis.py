# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Routing Congestion Analysis Skill.

Analyzes FPGA fabric tile utilization to detect routing congestion hotspots.
Identifies columns/regions with high resource density that may cause routing
congestion and degrade timing. READ-ONLY — no design modification.
"""

import logging
from typing import Optional

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill

logger = logging.getLogger(__name__)


def analyze_congestion(
    design,
    utilization_threshold: float = 0.8,
    top_n: int = 10,
) -> dict:
    """Analyze routing congestion by examining tile utilization density.

    Args:
        design: RapidWright Design object
        utilization_threshold: Threshold (0-1) for flagging high-utilization columns
        top_n: Number of top congested columns to return

    Returns:
        dict with congestion analysis results
    """
    if design is None:
        return {"error": "Design not loaded"}

    try:
        from com.xilinx.rapidwright.device import Device, TileTypeEnum

        device = design.getDevice()
        device_name = str(device.getName())

        # Count placed cells per column
        column_cell_count: dict[int, int] = {}
        total_cells = 0
        placed_cells = 0

        for cell in design.getCells():
            total_cells += 1
            if cell.isPlaced():
                placed_cells += 1
                site = cell.getSite()
                if site is not None:
                    tile = site.getTile()
                    if tile is not None:
                        col = int(tile.getColumn())
                        column_cell_count[col] = column_cell_count.get(col, 0) + 1

        if placed_cells == 0:
            return {
                "device": device_name,
                "total_cells": total_cells,
                "placed_cells": 0,
                "error": "No placed cells found — cannot analyze congestion",
            }

        # Compute column-level statistics
        columns = sorted(column_cell_count.keys())
        if not columns:
            return {
                "device": device_name,
                "total_cells": total_cells,
                "placed_cells": placed_cells,
                "error": "No column data available",
            }

        counts = [column_cell_count[c] for c in columns]
        avg_cells = sum(counts) / len(counts)
        max_cells = max(counts)
        min_cells = min(counts)

        # Identify congested columns (above threshold * max)
        congestion_threshold = max_cells * utilization_threshold
        congested_columns = []
        for col in columns:
            count = column_cell_count[col]
            if count >= congestion_threshold:
                congested_columns.append({
                    "column": col,
                    "cell_count": count,
                    "utilization_ratio": round(count / max_cells, 3) if max_cells > 0 else 0,
                })

        # Sort by cell count descending, take top_n
        congested_columns.sort(key=lambda x: x["cell_count"], reverse=True)
        congested_columns = congested_columns[:top_n]

        # Identify potential congestion clusters (adjacent congested columns)
        clusters = []
        if congested_columns:
            sorted_cols = sorted([c["column"] for c in congested_columns])
            current_cluster = [sorted_cols[0]]
            for i in range(1, len(sorted_cols)):
                if sorted_cols[i] - sorted_cols[i - 1] <= 2:  # Adjacent or near-adjacent
                    current_cluster.append(sorted_cols[i])
                else:
                    if len(current_cluster) >= 2:
                        clusters.append(current_cluster)
                    current_cluster = [sorted_cols[i]]
            if len(current_cluster) >= 2:
                clusters.append(current_cluster)

        # Determine congestion severity
        congested_count = len(congested_columns)
        total_columns = len(columns)
        congested_ratio = congested_count / total_columns if total_columns > 0 else 0

        if congested_ratio > 0.3:
            severity = "HIGH"
            recommendation = "Consider PBLOCK strategy to constrain placement and reduce congestion"
        elif congested_ratio > 0.15:
            severity = "MODERATE"
            recommendation = "Monitor congestion; consider PBLOCK if timing degrades"
        else:
            severity = "LOW"
            recommendation = "Congestion is not a primary concern"

        return {
            "device": device_name,
            "total_cells": total_cells,
            "placed_cells": placed_cells,
            "total_columns": total_columns,
            "congested_columns_count": congested_count,
            "congested_ratio": round(congested_ratio, 3),
            "severity": severity,
            "recommendation": recommendation,
            "column_statistics": {
                "avg_cells_per_column": round(avg_cells, 1),
                "max_cells": max_cells,
                "min_cells": min_cells,
                "utilization_threshold": utilization_threshold,
            },
            "congested_columns": congested_columns,
            "congestion_clusters": clusters,
            "has_congestion_issues": congested_count > 0,
        }

    except Exception as e:
        logger.error(f"Congestion analysis failed: {e}")
        return {"error": f"Congestion analysis failed: {e}"}


@skill(
    name="analyze_congestion",
    namespace="analysis",
    version="1.0.0",
    display_name="Routing Congestion Analysis",
    description="Analyze FPGA fabric tile utilization to detect routing congestion hotspots (READ-ONLY)",
    category=SkillCategory.ANALYSIS,
    idempotency="safe",
    side_effects=[],
    timeout_ms=30000,
    parameters=[
        ParameterSpec(
            name="utilization_threshold",
            type=float,
            description="Threshold (0-1) for flagging high-utilization columns. Default: 0.8 (80% of max).",
            default=0.8,
        ),
        ParameterSpec(
            name="top_n",
            type=int,
            description="Number of top congested columns to return. Default: 10.",
            default=10,
        ),
    ],
    required_context=["design"],
)
class CongestionAnalysisSkill(Skill):
    """Routing Congestion Analysis Skill."""

    def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """Execute congestion analysis."""
        design = context.design
        if design is None:
            return SkillResult(
                success=False,
                error="Design not loaded in context",
                error_code="SKILL_NOT_INITIALIZED",
            )

        utilization_threshold = kwargs.get("utilization_threshold", 0.8)
        top_n = kwargs.get("top_n", 10)

        result = analyze_congestion(
            design,
            utilization_threshold=utilization_threshold,
            top_n=top_n,
        )

        if "error" in result:
            return SkillResult(
                success=False,
                error=result["error"],
                error_code="ANALYSIS_FAILED",
            )

        return SkillResult(success=True, data=result)
