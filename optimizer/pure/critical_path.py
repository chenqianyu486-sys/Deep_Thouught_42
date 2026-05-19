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
DISPLAY_LIMIT_SNAPSHOT = 5
DISPLAY_LIMIT_HANDOFF_PLANNER = 5
DISPLAY_LIMIT_HANDOFF_WORKER = 3

# Maximum cell names to show per path in display
DISPLAY_CELLS_PER_PATH = 6


def parse_critical_path_cells(result: str) -> list[list[str]]:
    """Parse vivado_extract_critical_path_cells JSON result.

    The tool returns:
    - Success (no output_file): JSON array of arrays, e.g. [["cell1","cell2"], ...]
    - Success (with output_file): {"status":"success","path_count":N,...}
    - Error: {"error":"..."}

    Returns list of paths, each path is a list of cell names.
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

    # Direct cell list: [[cell_name, ...], ...]
    if isinstance(data, list):
        paths = []
        for path in data:
            if isinstance(path, list) and len(path) >= 2:
                paths.append([str(c) for c in path])
        logger.info(f"[critical_path] Parsed {len(paths)} paths from tool result")
        return paths

    return []


def update_critical_paths(
    state: OptimizerState,
    cell_paths: list[list[str]],
    iteration: int = 0,
) -> None:
    """Update state.timing.critical_paths from parsed cell lists.

    Keeps top MAX_CRITICAL_PATHS paths sorted by length (longest first).
    """
    if not cell_paths:
        return

    # Sort by path length descending, keep top N
    sorted_paths = sorted(cell_paths, key=len, reverse=True)[:MAX_CRITICAL_PATHS]

    state.timing.critical_paths = [
        CriticalPathEntry(
            cells=path,
            path_length=len(path),
            iteration=iteration,
        )
        for path in sorted_paths
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
        cells_preview = " -> ".join(entry.cells[:DISPLAY_CELLS_PER_PATH])
        if len(entry.cells) > DISPLAY_CELLS_PER_PATH:
            cells_preview += " -> ..."
        lines.append(
            f"path{i+1}: {cells_preview} ({entry.path_length} cells, iter {entry.iteration})"
        )
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
        lines.append(f"- Path {i+1} ({entry.path_length} cells): {cells_preview}")

    return "\n".join(lines)
