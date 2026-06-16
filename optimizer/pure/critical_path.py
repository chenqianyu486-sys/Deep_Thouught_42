"""Critical path parsing and state update pure functions.

Handles:
- Parsing vivado_extract_critical_path_cells JSON output
- Updating OptimizerState.timing.critical_paths
- Formatting critical paths for context snapshot and handoff prompts
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState

from ..state import CriticalPathEntry, ViolationSummary, PathCluster

logger = logging.getLogger(__name__)

# Maximum paths to keep in state
MAX_CRITICAL_PATHS = 10

# Maximum paths to show in context/handoff
DISPLAY_LIMIT_SNAPSHOT = 8
DISPLAY_LIMIT_HANDOFF_PLANNER = 5
DISPLAY_LIMIT_HANDOFF_WORKER = 3

# Maximum cell names to show per path in display
DISPLAY_CELLS_PER_PATH = 6


def refresh_violation_summary(state: OptimizerState) -> None:
    """Refresh state.timing.violation_summary from current critical paths.

    Called when failing_endpoints changes without a critical path extraction,
    or whenever state timing data is updated. Safe to call repeatedly.
    Also refreshes failing_endpoint_names from critical path cells.
    """
    from .timing import compute_violation_summary
    vs_data = compute_violation_summary(
        state.timing.critical_paths,
        failing_endpoints=state.timing.latest_failing_endpoints,
    )
    if vs_data is not None:
        from ..state import ViolationSummary, PathCluster
        path_clusters = [
            PathCluster(
                cluster_id=c["cluster_id"],
                cluster_type=c["cluster_type"],
                module=c["module"],
                path_count=c["path_count"],
                worst_slack=c["worst_slack"],
                best_slack=c["best_slack"],
                avg_logic_delay_pct=c["avg_logic_delay_pct"],
                avg_logic_levels=c["avg_logic_levels"],
                representative_cells=c["representative_cells"],
            )
            for c in vs_data.get("path_clusters", [])
        ]
        state.timing.violation_summary = ViolationSummary(
            total_failing_endpoints=vs_data["total_failing_endpoints"],
            severity_distribution=vs_data["severity_distribution"],
            delay_profile_breakdown=vs_data["delay_profile_breakdown"],
            logic_level_distribution=vs_data["logic_level_distribution"],
            top_violating_modules=vs_data["top_violating_modules"],
            path_clusters=path_clusters,
        )
    else:
        state.timing.violation_summary = None

    # Refresh failing_endpoint_names from critical paths (last cell = endpoint)
    if state.timing.critical_paths:
        state.timing.failing_endpoint_names = [
            path.cells[-1]
            for path in state.timing.critical_paths
            if path.cells
        ]
    else:
        state.timing.failing_endpoint_names = []


def parse_critical_path_cells(result: str) -> list[dict]:
    """Parse vivado_extract_critical_path_cells JSON result.

    The tool returns two formats:
    - New format: [{"cells":[...], "slack":float, "logic_delay":float, "net_delay":float, "levels":int}, ...]
    - Legacy format: [["cell1","cell2"], ...]
    - Success (with output_file): {"status":"success","path_count":N,...}
    - Error: {"error":"..."}

    Returns list of dicts, each with at least "cells" key plus optional timing fields.
    Returns empty list on error.
    """
    if not result or not result.strip():
        return []

    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        logger.warning("[critical_path] Failed to parse JSON result")
        return []

    # Error response
    if isinstance(data, dict):
        if "error" in data:
            logger.warning(f"[critical_path] Tool error: {data['error']}")
            return []
        # output_file mode — no cell data in result
        if data.get("status") == "success" and "path_count" in data:
            logger.info(f"[critical_path] Tool wrote {data['path_count']} paths to file (no inline data)")
            return []
        return []

    if isinstance(data, list):
        paths = []
        for item in data:
            if isinstance(item, dict) and "cells" in item:
                # New format: dict with cells + timing fields
                cells = [str(c) for c in item["cells"]]
                if len(cells) >= 2:
                    paths.append({
                        "cells": cells,
                        "slack": item.get("slack"),
                        "logic_delay": item.get("logic_delay"),
                        "net_delay": item.get("net_delay"),
                        "levels": item.get("levels"),
                    })
            elif isinstance(item, list) and len(item) >= 2:
                # Legacy format: plain list of cell names
                paths.append({"cells": [str(c) for c in item]})
        logger.info(f"[critical_path] Parsed {len(paths)} paths from tool result")
        return paths

    return []


def update_critical_paths(
    state: OptimizerState,
    cell_paths: list[dict],
    iteration: int = 0,
) -> None:
    """Update state.timing.critical_paths from parsed path data.

    Each element should be a dict with "cells" key plus optional timing fields.
    Keeps top MAX_CRITICAL_PATHS paths sorted by length (longest first).
    Also updates state.timing.violation_summary from the new path data.
    """
    if not cell_paths:
        return

    # Sort by path length descending, keep top N
    sorted_paths = sorted(cell_paths, key=lambda p: len(p.get("cells", [])), reverse=True)[:MAX_CRITICAL_PATHS]

    state.timing.critical_paths = [
        CriticalPathEntry(
            cells=p["cells"],
            path_length=len(p["cells"]),
            iteration=iteration,
            slack=p.get("slack"),
            logic_delay=p.get("logic_delay"),
            net_delay=p.get("net_delay"),
            levels=p.get("levels"),
        )
        for p in sorted_paths
    ]
    state.timing.critical_paths_iteration = iteration
    state.timing.critical_paths_stale = False

    # Recompute violation summary from updated critical paths
    from .timing import compute_violation_summary
    vs_data = compute_violation_summary(
        state.timing.critical_paths,
        failing_endpoints=state.timing.latest_failing_endpoints,
    )
    if vs_data is not None:
        from ..state import ViolationSummary, PathCluster
        path_clusters = [
            PathCluster(
                cluster_id=c["cluster_id"],
                cluster_type=c["cluster_type"],
                module=c["module"],
                path_count=c["path_count"],
                worst_slack=c["worst_slack"],
                best_slack=c["best_slack"],
                avg_logic_delay_pct=c["avg_logic_delay_pct"],
                avg_logic_levels=c["avg_logic_levels"],
                representative_cells=c["representative_cells"],
            )
            for c in vs_data.get("path_clusters", [])
        ]
        state.timing.violation_summary = ViolationSummary(
            total_failing_endpoints=vs_data["total_failing_endpoints"],
            severity_distribution=vs_data["severity_distribution"],
            delay_profile_breakdown=vs_data["delay_profile_breakdown"],
            logic_level_distribution=vs_data["logic_level_distribution"],
            top_violating_modules=vs_data["top_violating_modules"],
            path_clusters=path_clusters,
        )

    # Refresh failing_endpoint_names from critical path cells (last cell = endpoint)
    state.timing.failing_endpoint_names = [
        path.cells[-1]
        for path in state.timing.critical_paths
        if path.cells
    ]

    logger.info(
        f"[critical_path] Updated: {len(state.timing.critical_paths)} paths, "
        f"longest={len(sorted_paths[0]['cells']) if sorted_paths else 0} cells, "
        f"iteration={iteration}"
    )


def format_critical_paths_snapshot(
    critical_paths: list[CriticalPathEntry],
    limit: int = DISPLAY_LIMIT_SNAPSHOT,
) -> list[str]:
    """Format critical paths for context snapshot (YAML list of strings)."""
    if not critical_paths:
        return []

    lines = []
    for i, entry in enumerate(critical_paths[:limit]):
        cells_preview = "->".join(entry.cells[:DISPLAY_CELLS_PER_PATH])
        if len(entry.cells) > DISPLAY_CELLS_PER_PATH:
            cells_preview += "->..."

        # Build timing detail string
        detail_parts = [f"{entry.path_length} cells"]
        if entry.slack is not None:
            detail_parts.append(f"slack={entry.slack:.3f}ns")
        if entry.logic_delay is not None:
            detail_parts.append(f"logic={entry.logic_delay:.3f}ns")
        if entry.net_delay is not None:
            detail_parts.append(f"net={entry.net_delay:.3f}ns")
        if entry.levels is not None:
            detail_parts.append(f"L={entry.levels}")
        detail_parts.append(f"iter {entry.iteration}")

        lines.append(f"path{i+1}: {cells_preview} ({', '.join(detail_parts)})")
    return lines


def format_critical_paths_handoff(
    critical_paths: list[CriticalPathEntry],
    limit: int = DISPLAY_LIMIT_HANDOFF_PLANNER,
) -> str:
    """Format critical paths for handoff prompt (plain text)."""
    if not critical_paths:
        return "(no critical path data available)"

    lines = []
    for i, entry in enumerate(critical_paths[:limit]):
        cells_preview = " -> ".join(entry.cells[:DISPLAY_CELLS_PER_PATH])
        if len(entry.cells) > DISPLAY_CELLS_PER_PATH:
            cells_preview += " -> ..."
        # Include slack if available
        if entry.slack is not None:
            lines.append(f"- Path {i+1} ({entry.path_length} cells, slack={entry.slack:.3f}ns): {cells_preview}")
        else:
            lines.append(f"- Path {i+1} ({entry.path_length} cells): {cells_preview}")

    return "\n".join(lines)
