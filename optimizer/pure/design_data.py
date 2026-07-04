"""Design data persistence and aggregation pure functions.

Stores full (untruncated) design analysis data to structured JSON files
in the run directory, and provides tools for the LLM to access them.

Directory layout::

    {run_dir}/design_data/
        index.json                         # Global index of available iterations
        iteration_3/
            snapshot.json                  # Snapshot metadata
            critical_paths.json            # Full list of all critical paths
            high_fanout_nets.json          # Full list of high-fanout nets
            congestion.json                # Full congestion analysis data
            route_status.json              # Full route status report
            design_info.json               # Full design info
            tool_output_vivado_report_timing_summary_5.json   # Raw tool output
            tool_output_rapidwright_analyze_congestion_3.json
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .constants import DESIGN_DATA_DIR, DESIGN_DATA_MAX_FILES

if __name__ == "__main__":
    # Allow running as script for testing
    pass

logger = logging.getLogger(__name__)

# ── Dataclass-aware JSON encoder ────────────────────────────────────


class _DesignDataEncoder(json.JSONEncoder):
    """JSON encoder that handles dataclass objects and edge cases.

    Converts dataclass instances via asdict(), handles Path objects,
    and provides fallback stringification for non-serializable values.
    """
    def default(self, o: Any) -> Any:
        # Path objects: convert to string
        if isinstance(o, Path):
            return str(o)
        # Dataclass instances: recursively convert
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        # Fallback: string representation
        try:
            return super().default(o)
        except TypeError:
            return str(o)


def _encode_design_data(data: Any) -> str:
    """Serialize design data to JSON string.

    Handles nested dataclasses, Path objects, and non-serializable
    values gracefully.
    """
    return json.dumps(data, cls=_DesignDataEncoder, indent=2, ensure_ascii=False)


# ── Truncation statistics (pure functions) ──────────────────────────


def compute_unshown_path_stats(
    full_paths: list,
    max_shown: int,
) -> dict[str, Any]:
    """Compute aggregate statistics for paths beyond the display limit.

    Args:
        full_paths: Full list of CriticalPathEntry objects (or compatible dicts).
        max_shown: How many were shown in the dashboard (the cutoff point).

    Returns:
        dict with unshown_path_count, slack_range, mean_slack,
        severity_distribution, clock_domain_breakdown, and common_cell_types.
        Empty dict if full_paths is empty or no paths beyond max_shown.
    """
    if not full_paths or len(full_paths) <= max_shown:
        return {}

    unshown = full_paths[max_shown:]
    slacks = []
    severity = {"critical": 0, "moderate": 0, "marginal": 0}
    clocks: dict[str, int] = {}
    logic_pcts: list[float] = []
    levels_list: list[int] = []

    for entry in unshown:
        # Slack
        slack = _get_path_slack(entry)
        if slack is not None:
            slacks.append(slack)
            if slack < -1.0:
                severity["critical"] += 1
            elif slack < -0.3:
                severity["moderate"] += 1
            else:
                severity["marginal"] += 1

        # Clock domain
        src_clk = _get_path_source_clock(entry)
        dst_clk = _get_path_dest_clock(entry)
        cd = f"{src_clk}->{dst_clk}" if src_clk or dst_clk else "unknown"
        clocks[cd] = clocks.get(cd, 0) + 1

        # Logic delay percentage
        lp = _get_path_logic_delay_pct(entry)
        if lp is not None:
            logic_pcts.append(lp)

        # Logic levels
        lv = _get_path_levels(entry)
        if lv is not None:
            levels_list.append(lv)

    if not slacks:
        return {"total_unshown": len(unshown)}

    # Cell type accumulation across all unshown paths
    cell_type_counts: dict[str, int] = {}
    for entry in unshown:
        cells = _get_path_cells(entry)
        for cell in cells:
            ctype = _heuristic_type(cell)
            cell_type_counts[ctype] = cell_type_counts.get(ctype, 0) + 1

    total_typed = sum(cell_type_counts.values())
    top_types = sorted(cell_type_counts.items(), key=lambda x: -x[1])[:5]
    type_strs = [
        f"{t}: {c // max(1, total_typed // 100)}%"
        for t, c in top_types
    ] if total_typed > 0 else []

    result: dict[str, Any] = {
        "total_unshown": len(unshown),
        "slack_range": f"{min(slacks):.3f}ns to {max(slacks):.3f}ns",
        "mean_slack": f"{sum(slacks) / len(slacks):.3f}ns",
        "severity_distribution": severity,
    }

    if len(clocks) > 0:
        result["clock_domain_breakdown"] = dict(
            sorted(clocks.items(), key=lambda x: -x[1])
        )

    if logic_pcts:
        result["mean_logic_delay_pct"] = round(
            sum(logic_pcts) / len(logic_pcts), 2
        )

    if levels_list:
        result["logic_levels"] = {
            "min": min(levels_list),
            "max": max(levels_list),
            "mean": round(sum(levels_list) / len(levels_list), 1),
        }

    if type_strs:
        result["common_cell_types"] = ", ".join(type_strs)

    return result


def compute_unshown_hotspot_stats(
    hotspots: list[dict],
    max_shown: int,
) -> dict[str, Any]:
    """Compute aggregate stats for congestion hotspots beyond display limit."""
    if not hotspots or len(hotspots) <= max_shown:
        return {}
    unshown = hotspots[max_shown:]
    severities = [h.get("severity", 0) for h in unshown if h.get("severity") is not None]
    if not severities:
        return {"total_unshown": len(unshown)}
    return {
        "total_unshown": len(unshown),
        "severity_range": f"{min(severities):.2f} to {max(severities):.2f}",
    }


# ── Helper accessors (work with both CriticalPathEntry objects and dicts) ──


def _get_path_slack(entry) -> Optional[float]:
    if hasattr(entry, "slack"):
        return entry.slack
    return entry.get("slack") if isinstance(entry, dict) else None


def _get_path_source_clock(entry) -> str:
    if hasattr(entry, "clock") and hasattr(entry.clock, "source_clock"):
        return entry.clock.source_clock
    clock = entry.get("clock", {}) if isinstance(entry, dict) else {}
    return clock.get("source_clock", "") if isinstance(clock, dict) else ""


def _get_path_dest_clock(entry) -> str:
    if hasattr(entry, "clock") and hasattr(entry.clock, "dest_clock"):
        return entry.clock.dest_clock
    clock = entry.get("clock", {}) if isinstance(entry, dict) else {}
    return clock.get("dest_clock", "") if isinstance(clock, dict) else ""


def _get_path_logic_delay_pct(entry) -> Optional[float]:
    if hasattr(entry, "logic_delay") and hasattr(entry, "net_delay"):
        ld = entry.logic_delay
        nd = entry.net_delay
        if ld is not None and nd is not None and (ld + nd) > 0:
            return ld / (ld + nd)
        return None
    if isinstance(entry, dict):
        ld = entry.get("logic_delay")
        nd = entry.get("net_delay")
        if ld is not None and nd is not None and (ld + nd) > 0:
            return ld / (ld + nd)
    return None


def _get_path_levels(entry) -> Optional[int]:
    if hasattr(entry, "levels"):
        return entry.levels
    return entry.get("levels") if isinstance(entry, dict) else None


def _get_path_cells(entry) -> list[str]:
    if hasattr(entry, "cells"):
        return entry.cells or []
    return entry.get("cells", []) if isinstance(entry, dict) else []


def _heuristic_type(short_name: str) -> str:
    """Minimal heuristic to classify a cell from its short name."""
    name = short_name.upper().split("/")[-1] if "/" in short_name else short_name.upper()
    if any(name.startswith(p) for p in ("MUXF7", "MUXF8", "MUXF9")):
        return "MUXF"
    if name.startswith("CARRY"):
        return "CARRY"
    if name.startswith("DSP"):
        return "DSP"
    if name.startswith("RAM") or name.startswith("URAM"):
        return "RAM"
    if name.startswith("SRL"):
        return "SRL"
    if "_I_" in name:
        return "LUT"
    if "_REG" in name or "_REP" in name:
        return "FF"
    return "?"


# ── DesignDataManager ───────────────────────────────────────────────


class DesignDataManager:
    """Manages persistence and retrieval of full design analysis data.

    All data is stored as structured JSON under ``{run_dir}/design_data/``,
    organized by iteration. The stored data supplements the LLM's dashboard
    context, providing access to untruncated datasets without re-running
    Vivado or RapidWright tools.
    """

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self._base_dir = run_dir / DESIGN_DATA_DIR

    # ── Path helpers ──────────────────────────────────────────────

    def _iter_dir(self, iteration: int) -> Path:
        return self._base_dir / f"iteration_{iteration}"

    def _ensure_iter_dir(self, iteration: int) -> Path:
        d = self._iter_dir(iteration)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Store raw tool output ─────────────────────────────────────

    def store_raw_output(
        self,
        tool_name: str,
        iteration: int,
        phase: str,
        round_index: int,
        raw_text: str,
    ) -> str:
        """Persist a raw tool output to disk.

        Returns:
            The absolute file path of the stored data.
        """
        iter_dir = self._ensure_iter_dir(iteration)
        safe_name = tool_name.replace("vivado_", "").replace("rapidwright_", "").replace(" ", "_").lower()
        # Include phase in the filename so cross-phase calls of the same tool
        # at the same round do not overwrite each other (M6: the in-memory
        # raw_tool_outputs buffer uses (iteration, phase, round) keys; the
        # on-disk filename now matches that scoping).
        safe_phase = (phase or "unknown").replace(" ", "_").lower()
        file_path = iter_dir / f"tool_output_{safe_name}_{safe_phase}_{round_index}.json"

        data = {
            "_meta": {
                "stored_at": time.time(),
                "stored_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "iteration": iteration,
                "tool_round": round_index,
                "phase": phase,
                "tool_name": tool_name,
                "original_chars": len(raw_text),
            },
            "data": raw_text,
        }

        file_path.write_text(_encode_design_data(data), encoding="utf-8")
        self._rebuild_index(iteration)

        # Enforce max file limit
        self._enforce_file_limit(iteration)

        logger.debug(
            "[DESIGN_DATA] Stored raw output: %s (%d chars)",
            file_path, len(raw_text),
        )
        return str(file_path)

    # ── Store snapshot ────────────────────────────────────────────

    def store_snapshot(
        self,
        *,
        critical_paths: Optional[list] = None,
        high_fanout_nets: Optional[list] = None,
        congestion_data: Optional[dict] = None,
        route_status: Optional[dict] = None,
        design_info: Optional[dict] = None,
        failing_endpoint_names: Optional[list[str]] = None,
        field_freshness: Optional[dict] = None,
        iteration: int = 0,
        phase: str = "",
    ) -> str:
        """Store a full snapshot of design analysis data to disk.

        Each data type is written as a separate JSON file for easy access.
        Only non-None data types are stored.

        Returns:
            The absolute path to the iteration directory (parent of all files).
        """
        iter_dir = self._ensure_iter_dir(iteration)

        snapshots: dict[str, Any] = {
            "critical_paths": critical_paths,
            "high_fanout_nets": high_fanout_nets,
            "congestion": congestion_data,
            "route_status": route_status,
            "design_info": design_info,
            "failing_endpoint_names": failing_endpoint_names,
        }

        stored: list[str] = []
        for data_type, data in snapshots.items():
            if data is None:
                continue
            file_path = iter_dir / f"{data_type}.json"
            _file_ffs = {}
            if field_freshness:
                # Map data_type to the relevant field_freshness key
                _ff_map = {
                    "critical_paths": "critical_path_cells",
                    "high_fanout_nets": "high_fanout_nets",
                    "congestion": "congestion_data",
                    "route_status": "route_status",
                    "design_info": "design_info",
                }
                _ff_key = _ff_map.get(data_type)
                if _ff_key and _ff_key in field_freshness:
                    _file_ffs[_ff_key] = field_freshness[_ff_key]
            payload = {
                "_meta": {
                    "stored_at": time.time(),
                    "stored_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "iteration": iteration,
                    "data_type": data_type,
                    "phase": phase,
                    "field_freshness": _file_ffs or None,
                },
                "data": data,
            }
            file_path.write_text(_encode_design_data(payload), encoding="utf-8")
            stored.append(data_type)

        # Write snapshot index
        _ff = field_freshness or {}
        index_data = {
            "_meta": {
                "stored_at": time.time(),
                "stored_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "iteration": iteration,
                "phase": phase,
                "stored_types": stored,
                "field_freshness": _ff,
            },
        }
        # Check if critical_paths_stale is available via the critical_paths data
        if field_freshness:
            for fname, fstatus in field_freshness.items():
                if fstatus == "stale":
                    index_data["_meta"]["has_stale_fields"] = True
                    break
        (iter_dir / "snapshot.json").write_text(
            _encode_design_data(index_data), encoding="utf-8",
        )

        self._rebuild_index(iteration)
        logger.debug("[DESIGN_DATA] Stored snapshot iteration=%d: %s", iteration, stored)
        return str(iter_dir)

    # ── Read design data ──────────────────────────────────────────

    def read_design_data(
        self,
        iteration: int,
        data_type: str,
    ) -> str:
        """Read a stored design data file.

        Args:
            iteration: Iteration number to read from.
            data_type: One of ``critical_paths``, ``high_fanout_nets``,
                       ``congestion``, ``route_status``, ``design_info``,
                       ``failing_endpoint_names``, or ``tool_output:<name>``.

        Returns:
            JSON string with the stored data, or an error JSON if not found.
        """
        iter_dir = self._iter_dir(iteration)
        if not iter_dir.is_dir():
            return json.dumps({
                "error": f"No design data for iteration {iteration}",
                "available_iterations": self._list_available_iterations(),
            })

        # Tool output: data_type = "tool_output:<name>"
        if data_type.startswith("tool_output:"):
            tool_part = data_type[len("tool_output:"):]
            return self._read_tool_output(iter_dir, tool_part)

        # Structured data types
        file_path = iter_dir / f"{data_type}.json"
        if not file_path.is_file():
            available = [p.stem for p in iter_dir.glob("*.json")
                         if p.stem not in ("snapshot", "index")]
            return json.dumps({
                "error": f"Data type '{data_type}' not found for iteration {iteration}",
                "available_types": available,
            })

        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            inner = payload.get("data")
            total_records = None
            if isinstance(inner, list):
                total_records = len(inner)
            elif isinstance(inner, dict):
                total_records = len(inner)

            return json.dumps({
                "data_type": data_type,
                "iteration": iteration,
                "size": file_path.stat().st_size,
                "total_records": total_records,
                "data": inner,
                "meta": payload.get("_meta", {}),
                # M9: field_freshness in meta reflects persist-time state only.
                # If the design was modified after this snapshot was written,
                # the data may be stale regardless of the recorded freshness.
                "freshness_caveat": (
                    "meta.field_freshness reflects state at persist time. "
                    "If the design was modified after this snapshot, re-run "
                    "the analysis tool for current values."
                ),
            }, cls=_DesignDataEncoder, indent=2, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "error": f"Failed to read {data_type}: {e}",
                "file": str(file_path),
            })

    def _read_tool_output(self, iter_dir: Path, tool_name: str) -> str:
        """Find and read a stored raw tool output by tool name."""
        # List matching files
        safe_name = tool_name.replace("vivado_", "").replace("rapidwright_", "").replace(" ", "_").lower()
        candidates = sorted(iter_dir.glob(f"tool_output_{safe_name}_*.json"))

        if not candidates:
            # Fallback: search all tool_output files for partial name match
            all_tool_files = sorted(iter_dir.glob("tool_output_*.json"))
            candidates = [
                f for f in all_tool_files
                if safe_name in f.stem.lower()
            ]

        if not candidates:
            # List available tool outputs
            available = sorted(
                f.stem.replace("tool_output_", "", 1)
                for f in iter_dir.glob("tool_output_*.json")
            )
            return json.dumps({
                "error": f"Tool output '{tool_name}' not found",
                "available_tool_outputs": available if available else None,
            })

        # Return the most recent by mtime (phase is now part of the filename,
        # so multiple phases of the same tool no longer collide — M6).
        file_path = sorted(candidates, key=lambda f: f.stat().st_mtime)[-1]
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            inner = payload.get("data", "")
            meta = payload.get("_meta", {})
            return json.dumps({
                "data_type": f"tool_output:{tool_name}",
                "iteration": meta.get("iteration"),
                "size": file_path.stat().st_size,
                "total_records": len(inner) if isinstance(inner, str) else None,
                "data": inner,
                "meta": meta,
            }, cls=_DesignDataEncoder, indent=2, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "error": f"Failed to read tool output: {e}",
                "file": str(file_path),
            })

    # ── List available data ───────────────────────────────────────

    def list_available_data(self, iteration: int) -> str:
        """List all available design data types for a given iteration."""
        iter_dir = self._iter_dir(iteration)
        if not iter_dir.is_dir():
            return json.dumps({
                "error": f"No design data for iteration {iteration}",
                "available_iterations": self._list_available_iterations(),
            })

        files = []
        for f in sorted(iter_dir.glob("*.json")):
            if f.stem in ("index",):
                continue
            is_tool = f.stem.startswith("tool_output_")
            data_type = f.stem.replace("tool_output_", "tool_output:", 1) if is_tool else f.stem
            files.append({
                "data_type": data_type,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })

        return json.dumps({
            "iteration": iteration,
            "file_count": len(files),
            "files": files,
        }, cls=_DesignDataEncoder, indent=2, ensure_ascii=False)

    def list_all_iterations(self) -> str:
        """List all iterations that have design data stored."""
        iterations = self._list_available_iterations()
        return json.dumps({
            "total_iterations": len(iterations),
            "iterations": iterations,
        }, indent=2)

    # ── Internal helpers ──────────────────────────────────────────

    def _rebuild_index(self, iteration: int) -> None:
        """Write or update the global index."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        index_path = self._base_dir / "index.json"

        all_iterations = self._list_available_iterations()
        index_data = {
            "_meta": {
                "updated_at": time.time(),
                "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "iterations": all_iterations,
        }
        try:
            index_path.write_text(
                _encode_design_data(index_data), encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[DESIGN_DATA] Failed to write index: %s", e)

    def _list_available_iterations(self) -> list[int]:
        """Return sorted list of iteration numbers with stored data."""
        if not self._base_dir.is_dir():
            return []
        result = []
        for d in self._base_dir.iterdir():
            if d.is_dir() and d.name.startswith("iteration_"):
                try:
                    result.append(int(d.name[len("iteration_"):]))
                except ValueError:
                    continue
        return sorted(result)

    def _enforce_file_limit(self, iteration: int) -> None:
        """Remove oldest tool-output files if the iteration directory exceeds the limit.

        Only ``tool_output_*`` files are eligible for removal — snapshot data
        files (critical_paths.json, congestion.json, ...) must be preserved so
        ``design_data_read`` keeps working (M7).
        """
        iter_dir = self._iter_dir(iteration)
        if not iter_dir.is_dir():
            return
        files = sorted(
            (f for f in iter_dir.glob("*.json") if f.name.startswith("tool_output_")),
            key=lambda f: f.stat().st_mtime,
        )
        if len(files) > DESIGN_DATA_MAX_FILES:
            for f in files[:len(files) - DESIGN_DATA_MAX_FILES]:
                try:
                    f.unlink()
                except OSError:
                    pass
