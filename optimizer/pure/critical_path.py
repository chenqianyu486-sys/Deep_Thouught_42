"""Critical path parsing and state update pure functions.

Handles:
- Parsing vivado_extract_critical_path_cells JSON output
- Updating OptimizerState.timing.critical_paths
- Formatting critical paths for context snapshot and handoff prompts
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState

from ..state import CriticalPathEntry, ViolationSummary, PathCluster, PathNode, ClockDomainInfo

logger = logging.getLogger(__name__)

# Maximum paths to keep in state
MAX_CRITICAL_PATHS = 15

# Maximum paths to show in context/handoff
DISPLAY_LIMIT_SNAPSHOT = 10
DISPLAY_LIMIT_HANDOFF_PLANNER = 5
DISPLAY_LIMIT_HANDOFF_WORKER = 3

# Maximum cell names to show per path in display
DISPLAY_CELLS_PER_PATH = 10

# Maximum delay hotspots to show per path (D1)
MAX_DELAY_HOTSPOTS = 5

# Cell types expected on well-formed sequential timing paths
# (LUTs, flip-flops, MUXFs, carry chains — NOT fanout-inflated intermediates)
_VALID_PATH_CELL_PREFIXES = (
    "LUT", "FDRE", "FDCE", "FDPE", "FDSE", "FDRSE",
    "MUXF7", "MUXF8", "MUXF9", "MUXFX",
    "CARRY4", "CARRY8",
    "DSP", "DSP48",
    "RAM", "RAMB", "URAM",
    "SRL", "SRLC", "SHIFT",
)

# Patterns that look like device site/coordinate names (NOT valid cell instances)
_DEVICE_SITE_PATTERNS = re.compile(
    r'^(SLICE_X\d+Y\d+'
    r'|DSP\d*_X\d+Y\d+'
    r'|RAMB\d*_X\d+Y\d+'
    r'|URAM\d*_X\d+Y\d+'
    r'|BUFGCE_X\d+Y\d+'
    r'|MMCM\d*_X\d+Y\d+'
    r'|PLL\d*_X\d+Y\d+'
    r'|X\d+Y\d+'
    r')$', re.IGNORECASE,
)


def _is_valid_cell_name(name: str) -> bool:
    """Check if a string looks like a valid hierarchical cell instance name.

    Valid cell names are hierarchical paths with at least one '/' separator
    and alphanumeric leaf names. Rejects pblock labels, device site
    coordinates, and other non-cell strings that may appear in Vivado
    output under certain conditions (e.g. PBLOCK-active extraction).

    Examples of valid names:
      - layer0_reg/data_out_reg[41]
      - layer0_inst/layer0_N25_inst/data_out[76]_i_19
      - u_core/u_alu/reg_0

    Examples of invalid names:
      - pblock_tight          (PBLOCK constraint name)
      - pblock_io             (PBLOCK constraint name)
      - SLICE_X56Y0           (device site coordinate)
      - DSP48E2_X8Y0          (device site)
    """
    if not name or not isinstance(name, str):
        return False

    # Reject pblock labels entirely
    name_lower = name.lower()
    if name_lower.startswith("pblock") or "pblock" in name_lower:
        return False

    # Reject device site coordinates
    leaf = name.split("/")[-1] if "/" in name else name
    if _DEVICE_SITE_PATTERNS.match(leaf):
        return False

    # Must have hierarchical structure (at least one '/')
    if "/" not in name:
        return False

    # Each path segment must be non-empty and contain valid characters
    for segment in name.split("/"):
        if not segment:
            return False
        # Segments should contain at least one alphanumeric character
        if not re.search(r'[a-zA-Z0-9]', segment):
            return False

    return True


def validate_critical_path_data(
    paths: list[list[str]],
    reference_paths: list[list[str]] | None = None,
) -> dict:
    """Validate and diagnose critical path data quality.

    Checks whether the provided paths look like well-formed sequential
    timing paths. Detects common data quality issues such as:
    - Paths containing mostly LUT leaf cells (fanout-contaminated extraction)
    - Missing expected cell types (MUXF, FF, etc.)
    - Paths that are too short (< 2 cells)
    - Empty or malformed input

    Args:
        paths: List of paths, each a list of cell names
        reference_paths: Optional reference paths (e.g. from state) to compare against.
            When provided, computes overlap and structural similarity metrics.

    Returns:
        dict with keys:
            - is_valid: bool — whether the data passes basic quality checks
            - issues: list[str] — human-readable issue descriptions
            - cell_type_stats: dict[str, int] — cell type distribution across all paths
            - overlap_with_reference: float (0-1) — fraction of cells matching reference
              (only if reference_paths is provided)
            - diagnosis: str — shorthand diagnosis tag
    """
    issues: list[str] = []
    cell_types: dict[str, int] = {}
    total_cells = 0
    reference_cell_set: set[str] | None = None

    if reference_paths is not None:
        reference_cell_set = {c for p in reference_paths if p for c in p}

    if not paths:
        return {
            "is_valid": False,
            "issues": ["No critical path data provided"],
            "cell_type_stats": {},
            "overlap_with_reference": 0.0 if reference_cell_set else None,
            "diagnosis": "empty",
        }

    for path_idx, path in enumerate(paths):
        if not path or len(path) < 2:
            issues.append(f"Path {path_idx}: too short ({len(path) if path else 0} cells, need >= 2)")

    # Skip cell type analysis if design context is unavailable
    # (cell type strings are extracted from cell names heuristically when
    # RapidWright design is not accessible)
    for path in paths:
        for cell_name in path:
            total_cells += 1
            # Extract cell type from name heuristically:
            #   "data_out[76]_i_19" / "data_out_reg[76]_i_1" -> type from prefix
            #   Full cell names like "layer0_inst/layer0_N25_inst/LUT6_inst"
            #   typically have the type embedded or end with _i_N (LUT) / _reg (FF)
            parts = cell_name.split("/")
            short_name = parts[-1] if parts else cell_name
            ctype = _heuristic_cell_type(short_name)
            cell_types[ctype] = cell_types.get(ctype, 0) + 1

    # Data quality heuristics
    total_types = sum(cell_types.values())
    if total_types > 0:
        # If >70% of cells are generic LUT-like (no clear type marker),
        # the data may be fanout-contaminated
        unknown_ratio = cell_types.get("unknown", 0) / total_types
        if unknown_ratio > 0.7:
            issues.append(
                f"High ratio of untyped cells ({unknown_ratio:.0%}): "
                f"paths may contain fanout-contaminated intermediate cells "
                f"from incorrect TCL extraction"
            )

        # Check for expected sequential elements
        ff_count = sum(v for k, v in cell_types.items() if k.startswith("FD") or k in ("FDPE", "FDCE"))
        if ff_count == 0 and total_types > 10:
            issues.append(
                "No flip-flop cells found in any path — paths may not be "
                "valid sequential timing paths"
            )

    # Overlap analysis with reference data
    overlap = None
    if reference_cell_set and total_cells > 0:
        path_cell_set = {c for p in paths if p for c in p}
        if reference_cell_set:
            intersection = path_cell_set & reference_cell_set
            overlap = len(intersection) / max(len(reference_cell_set), 1)
            if overlap < 0.3:
                issues.append(
                    f"Low overlap ({overlap:.0%}) with reference critical path data — "
                    f"LLM-extracted paths differ significantly from verified state data"
                )

    # Determine diagnosis
    if not issues:
        diagnosis = "ok"
    elif any("fanout" in i.lower() or "contaminated" in i.lower() for i in issues):
        diagnosis = "probable_bad_extraction"
    elif any("Overlap" in i or "overlap" in i for i in issues):
        diagnosis = "low_reference_overlap"
    elif any("flip-flop" in i or "FF" in i for i in issues):
        diagnosis = "missing_sequential_cells"
    else:
        diagnosis = "quality_warning"

    return {
        "is_valid": len(issues) == 0,
        "issues": issues,
        "cell_type_stats": dict(sorted(cell_types.items(), key=lambda x: -x[1])),
        "total_cells_analyzed": total_cells,
        "overlap_with_reference": overlap,
        "diagnosis": diagnosis,
    }


def heuristic_cell_type(short_name: str) -> str:
    """Public wrapper around _heuristic_cell_type.

    Determines cell type (LUT, FF, MUXF, CARRY, DSP, RAM, SRL, etc.)
    from a short cell name using Vivado naming conventions.
    """
    return _heuristic_cell_type(short_name)


def build_cell_type_chain(cells: list[str]) -> tuple[str, dict[str, int]]:
    """Build a cell type chain string and type counts from a list of cell names.

    Each cell name is mapped to a type via heuristic_cell_type() on the leaf name.
    Unknown types are grouped as "?".

    Returns:
        (chain_string, counts_dict)
        chain_string: e.g. "LUT6→MUXF7→LUT6→FDRE"
        counts_dict: e.g. {"LUT": 3, "MUXF": 1, "FF": 1}
    """
    if not cells:
        return "", {}

    type_names = []
    counts: dict[str, int] = {}
    for cell in cells:
        leaf = cell.split("/")[-1] if "/" in cell else cell
        ctype = _heuristic_cell_type(leaf)
        # Normalize: use the known type label when available
        label = ctype if ctype != "unknown" else "?"
        counts[label] = counts.get(label, 0) + 1
        type_names.append(ctype if ctype != "unknown" else leaf[:20])

    chain = "→".join(type_names)
    return chain, counts


def _heuristic_cell_type(short_name: str) -> str:
    """Heuristically determine cell type from a short cell name.

    Uses naming conventions from Vivado synthesis:
    - "*_i_*" or "*_i*" -> LUT cell
    - "*_reg*" or "*_rep*" -> FF/register cell
    - "MUXF*" -> MUXF cell
    - "CARRY*" -> carry chain cell
    - "DSP*" -> DSP cell
    - "RAM*", "URAM*" -> memory cell
    - "SRL*" -> shift register

    Returns a type label or "unknown".
    """
    name = short_name.upper()
    if any(name.startswith(p) for p in ("MUXF7", "MUXF8", "MUXF9", "MUXFX")):
        return "MUXF"
    if name.startswith("CARRY"):
        return "CARRY"
    if name.startswith("DSP"):
        return "DSP"
    if name.startswith("RAM") or name.startswith("URAM"):
        return "RAM"
    if name.startswith("SRL"):
        return "SRL"
    # _i_ prefix is Vivado's naming for LUT cells
    if "_I_" in name:
        return "LUT"
    # Common cell type markers in names
    if "_REG" in name:
        return "FF"
    if "_REP" in name:
        return "FF_REPLICA"
    return "unknown"


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
        # Unexpected dict format — log structure for diagnosis
        logger.warning(
            f"[critical_path] Received unexpected dict (not error, not output_file mode). "
            f"Keys: {list(data.keys())[:10]}. Full data preview: {str(data)[:500]}"
        )
        return []

    if isinstance(data, list):
        paths = []
        total_invalid_cells = 0
        total_cells = 0
        total_empty_cells = 0  # diagnose "Parsed 0 paths" issue
        for item in data:
            if isinstance(item, dict) and "cells" in item:
                # New format: dict with cells + timing fields + D1/D2 diagnostic fields
                raw_cells = [str(c) for c in item["cells"]]
                total_cells += len(raw_cells)
                if not raw_cells:
                    total_empty_cells += 1
                # Filter out invalid cell names (pblock labels, device sites, etc.)
                valid_cells = [c for c in raw_cells if _is_valid_cell_name(c)]
                invalid_count = len(raw_cells) - len(valid_cells)
                total_invalid_cells += invalid_count
                if invalid_count > 0:
                    logger.debug(
                        f"[critical_path] Filtered {invalid_count}/{len(raw_cells)} invalid cell names "
                        f"from path (first invalid: {next((c for c in raw_cells if not _is_valid_cell_name(c)), '?')})"
                    )
                # Skip path if >50% of cells were filtered (likely entirely contaminated)
                if len(valid_cells) < 2 or invalid_count >= len(raw_cells) / 2:
                    if invalid_count > len(raw_cells) / 2:
                        logger.warning(
                            f"[critical_path] Skipping path: {invalid_count}/{len(raw_cells)} "
                            f"cells were invalid (likely pblock-contaminated extraction)"
                        )
                    continue
                paths.append({
                    # Legacy fields
                    "cells": valid_cells,
                    "slack": item.get("slack"),
                    "logic_delay": item.get("logic_delay"),
                    "net_delay": item.get("net_delay"),
                    "levels": item.get("levels"),
                    # D1: per-node breakdown
                    "nodes": item.get("nodes", []),
                    "startpoint": item.get("startpoint", ""),
                    "endpoint_pin": item.get("endpoint_pin", ""),
                    "arrival_time": item.get("arrival_time"),
                    "required_time": item.get("required_time"),
                    "top_delay_nodes": item.get("top_delay_nodes", []),
                    # D2: clock-domain context
                    "clock": item.get("clock", {}),
                })
            elif isinstance(item, list) and len(item) >= 2:
                # Legacy format: plain list of cell names
                raw_cells = [str(c) for c in item]
                total_cells += len(raw_cells)
                valid_cells = [c for c in raw_cells if _is_valid_cell_name(c)]
                invalid_count = len(raw_cells) - len(valid_cells)
                total_invalid_cells += invalid_count
                if len(valid_cells) >= 2 and invalid_count < len(raw_cells) / 2:
                    paths.append({"cells": valid_cells})
        if total_invalid_cells > 0:
            logger.info(
                f"[critical_path] Parsed {len(paths)} paths, "
                f"filtered {total_invalid_cells}/{total_cells} invalid cell names"
            )
        elif total_empty_cells > 0 and len(paths) == 0:
            logger.warning(
                f"[critical_path] Parsed 0 paths: received {len(data)} path dicts, "
                f"but {total_empty_cells}/{len(data)} have empty 'cells' arrays. "
                f"This indicates the Vivado timing report regexes did not match cell lines. "
                f"First item keys: {list(data[0].keys()) if data else 'N/A'}. "
                f"First item cells: {data[0].get('cells', 'KEY_MISSING') if data else 'N/A'}. "
                f"First item nodes count: {len(data[0].get('nodes', [])) if data else 'N/A'}."
            )
        else:
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
            # D1: per-node breakdown
            nodes=[PathNode(**n) for n in p.get("nodes", []) if isinstance(n, dict)],
            startpoint=p.get("startpoint", ""),
            endpoint_pin=p.get("endpoint_pin", ""),
            arrival_time=p.get("arrival_time"),
            required_time=p.get("required_time"),
            top_delay_nodes=[PathNode(**n) for n in p.get("top_delay_nodes", []) if isinstance(n, dict)],
            # D2: clock-domain context
            clock=ClockDomainInfo(**(p.get("clock") or {})),
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


def derive_cells_rich(entry: CriticalPathEntry) -> list[dict]:
    """Derive rich cell descriptors from a CriticalPathEntry's nodes.

    Returns cells in the format expected by critical_path_cell_replication
    skill: [{"name": str, "delay": float, "type": str, "fanout": int}, ...].
    Only cell-kind nodes with a non-None incr_delay are included.

    This fixes a pre-existing mismatch: the replication skill expected
    cells:[{name,delay,type,fanout}] but extract_critical_path_cells
    returned cells:list[str]. With D1's per-node breakdown, we can now
    provide the rich format the skill was designed for.
    """
    rich = []
    for node in entry.nodes:
        if node.kind != "cell":
            continue
        rich.append({
            "name": node.name,
            "delay": node.incr_delay if node.incr_delay is not None else 0.0,
            "type": node.cell_type or "unknown",
            "fanout": node.fanout if node.fanout is not None else 0,
        })
    return rich


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
        # D1/D2: diagnostic summary
        if entry.startpoint:
            # startpoint is cell/pin; show cell name (second-to-last path segment)
            sp_parts = entry.startpoint.rsplit('/', 1)
            detail_parts.append(f"src={sp_parts[0].rsplit('/', 1)[-1] if len(sp_parts) > 1 else entry.startpoint}")
        if entry.clock.clock_skew is not None:
            detail_parts.append(f"skew={entry.clock.clock_skew:.3f}ns")
        if entry.clock.is_cross_clock:
            detail_parts.append("CROSS-CLOCK")
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
        # Build detail suffix with D1/D2 diagnostics
        detail_parts = [f"{entry.path_length} cells"]
        if entry.slack is not None:
            detail_parts.append(f"slack={entry.slack:.3f}ns")
        if entry.clock.clock_skew is not None:
            detail_parts.append(f"skew={entry.clock.clock_skew:.3f}ns")
        if entry.clock.is_cross_clock:
            detail_parts.append("CROSS-CLOCK")
        lines.append(f"- Path {i+1} ({', '.join(detail_parts)}): {cells_preview}")

    return "\n".join(lines)
