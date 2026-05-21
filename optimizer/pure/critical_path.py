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

from ..state import CriticalPathEntry

logger = logging.getLogger(__name__)

# Maximum paths to keep in state
MAX_CRITICAL_PATHS = 10

# Maximum paths to show in context/handoff
DISPLAY_LIMIT_SNAPSHOT = 8
DISPLAY_LIMIT_HANDOFF_PLANNER = 5
DISPLAY_LIMIT_HANDOFF_WORKER = 3

# Maximum cell names to show per path in display
DISPLAY_CELLS_PER_PATH = 6


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

    logger.info(
        f"[critical_path] Updated: {len(state.timing.critical_paths)} paths, "
        f"longest={sorted_paths[0] if sorted_paths else 0} cells, "
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
