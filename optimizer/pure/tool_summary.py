"""Tool result summarization pure functions.

Extracted from dcp_optimizer.py: _summarize_tool_result (L1938-2349),
_filter_tool_result (L1896-1936).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .timing import parse_timing_summary
from .constants import SMALL_OUTPUT_THRESHOLD, TOOL_RESULT_TRUNCATE

logger = logging.getLogger(__name__)


def filter_tool_result(tool_name: str, result: str, truncate_limit: int = TOOL_RESULT_TRUNCATE) -> str:
    """Retain key information based on tool type, truncate redundant content."""
    if len(result) <= truncate_limit:
        return result

    # Timing reports: retain WNS/TNS/key path summary
    if 'timing' in tool_name.lower():
        lines = result.split('\n')
        kept_lines = []
        for line in lines:
            if any(kw in line.lower() for kw in ['wns', 'tns', 'failing', 'clock', 'target', 'level',
                                          'slack', 'delay', 'path', 'endpoint', 'start']):
                kept_lines.append(line)
            elif line.strip().startswith('---') or line.strip().startswith('==='):
                kept_lines.append(line)
        if kept_lines:
            filtered = '\n'.join(kept_lines[:200])
            if len(result) > len(filtered):
                return filtered + f"\n...[timing report truncated, original: {len(result)} chars]..."
        return result[:truncate_limit]

    # Route status: retain errors and congestion info
    if 'route' in tool_name.lower():
        lines = result.split('\n')
        kept_lines = []
        for line in lines:
            if any(kw in line.lower() for kw in ['error', 'fail', 'congestion', 'unrouted', 'nets', 'status']):
                kept_lines.append(line)
        if kept_lines:
            filtered = '\n'.join(kept_lines[:50])
            if len(result) > len(filtered):
                return filtered + f"\n...[route status truncated, original: {len(result)} chars]..."
        return result[:truncate_limit]

    # General truncation
    head_len = truncate_limit // 2
    tail_len = truncate_limit // 2
    return result[:head_len] + f"\n...[{len(result) - truncate_limit} chars truncated]...\n" + result[-tail_len:]


def summarize_tool_result(
    tool_name: str,
    raw_result: str,
    latest_wns: Optional[float] = None,
    latest_tns: Optional[float] = None,
    latest_failing_endpoints: Optional[int] = None,
    prev_best_wns: Optional[float] = None,
) -> str:
    """Convert raw tool output to structured YAML summary for LLM consumption.

    Preserves key metrics while discarding verbose output.
    Full raw output is stored separately for on-demand retrieval.

    Args:
        tool_name: Name of the tool that produced the result.
        raw_result: Raw tool output string.
        latest_wns: Current latest WNS for delta calculation.
        latest_tns: Current latest TNS.
        latest_failing_endpoints: Current failing endpoints count.
        prev_best_wns: Previous best WNS for delta calculation.
    """
    lines = raw_result.split('\n')
    line_count = len(lines)
    char_count = len(raw_result)

    # Bypass summarization for compact outputs
    if char_count < SMALL_OUTPUT_THRESHOLD and 'timing' not in tool_name:
        indent = '\n'.join('    ' + line for line in lines)
        return (
            f"tool_result:\n"
            f"  tool: {tool_name}\n"
            f'  summary: "{lines[0][:200] if lines else ""}"\n'
            f"  status: completed\n"
            f"  raw_output_truncated: false\n"
            f"  raw_output_chars: {char_count}\n"
            f"  raw_output: |\n{indent}"
        )

    summary_parts = []
    key_details = {}
    status = "completed"

    # Common: extract error/fail indicators
    has_error = any("error" in l.lower() for l in lines[:20])
    has_fail = any("fail" in l.lower() for l in lines[:20])
    if has_error and "success" not in raw_result.lower():
        status = "error" if has_error else "failed"

    # Tool-type specific extraction
    if tool_name in ("vivado_phys_opt_design", "vivado_report_timing_summary"):
        timing = parse_timing_summary(raw_result)
        wns = timing.get("wns")
        tns = timing.get("tns")
        fe = timing.get("failing_endpoints")

        if wns is None:
            wns = latest_wns
        if tns is None:
            tns = latest_tns
        if fe is None:
            fe = latest_failing_endpoints
        if wns is not None:
            delta_str = ""
            if prev_best_wns is not None and prev_best_wns > float('-inf'):
                diff = wns - prev_best_wns
                delta_str = f"{diff:+.3f}"
            summary_parts.append(f"WNS: {wns:.3f}")
            if tns is not None:
                summary_parts.append(f"TNS: {tns:.3f}")
            if fe is not None:
                summary_parts.append(f"Failing endpoints: {fe}")
            key_details["wns"] = round(wns, 3)
            if prev_best_wns is not None and prev_best_wns > float('-inf'):
                key_details["wns_delta"] = round(wns - prev_best_wns, 3)
            key_details["tns"] = round(tns, 3) if tns is not None else None
            key_details["failing_endpoints"] = fe

    elif tool_name == "vivado_route_design":
        route_status = ""
        for line in lines[:50]:
            if any(kw in line.lower() for kw in ["error", "fail", "congestion", "unrouted", "status"]):
                route_status += line.strip() + "; "
        if route_status:
            summary_parts.append(f"Route: {route_status[:200]}")
        timing = parse_timing_summary(raw_result)
        if timing.get("wns") is not None:
            summary_parts.append(f"WNS: {timing['wns']:.3f}")
            key_details["wns"] = timing["wns"]
            key_details["tns"] = timing["tns"]

    elif tool_name == "vivado_get_wns":
        try:
            wns_val = float(raw_result.strip())
            summary_parts.append(f"WNS: {wns_val:.3f}")
            key_details["wns"] = round(wns_val, 3)
        except ValueError:
            summary_parts.append(f"WNS: {raw_result.strip()[:50]}")

    elif tool_name == "vivado_place_design":
        for line in lines[:50]:
            if any(kw in line.lower() for kw in ["error", "warning", "placed", "utilization", "slack"]):
                stripped = line.strip()
                if stripped:
                    summary_parts.append(stripped[:200])

    elif tool_name == "vivado_run_tcl_info":
        summary_parts.append(f"Output: {line_count} lines, {char_count} chars")

    elif tool_name == "vivado_extract_critical_path_pins":
        try:
            data = json.loads(raw_result)
            if "error" in data:
                summary_parts.append(f"Error: {data['error'][:200]}")
                status = "error"
            else:
                path_count = data.get("path_count", 0)
                pin_paths = data.get("pin_paths", [])
                summary_parts.append(f"Extracted {path_count} critical pin paths")
                key_details["path_count"] = path_count
                if pin_paths:
                    preview_count = min(10, len(pin_paths))
                    for i, pp in enumerate(pin_paths[:preview_count]):
                        preview = " -> ".join(pp[:6])
                        if len(pp) > 6:
                            preview += " -> ..."
                        summary_parts.append(f"  Path {i+1}: {preview}")
                    key_details["pin_paths"] = pin_paths
        except Exception:
            pass

    elif tool_name == "vivado_create_and_apply_pblock":
        has_validation_failure = False
        for line in lines:
            if "Resource validation FAILED" in line:
                summary_parts.append(line.strip()[:300])
                has_validation_failure = True
            elif "shortage:" in line:
                summary_parts.append(line.strip()[:300])
                has_validation_failure = True
            elif "Resource validation PASSED" in line:
                summary_parts.append(line.strip())
            elif "Pblock Created Successfully" in line:
                key_details["pblock_created"] = True
            elif "Maximum expansion attempts reached" in line:
                summary_parts.append(line.strip()[:300])
                has_validation_failure = True
        cells_in_pblock = None
        cells_in_design = None
        for line in lines:
            if "Cells in pblock:" in line:
                try:
                    cells_in_pblock = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            elif "Total cells in design:" in line:
                try:
                    cells_in_design = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
        if cells_in_pblock is not None:
            key_details["cells_in_pblock"] = cells_in_pblock
        if cells_in_design is not None:
            key_details["cells_in_design"] = cells_in_design
        if cells_in_pblock is not None and cells_in_design is not None:
            if cells_in_pblock < cells_in_design:
                key_details["compliance"] = f"added {cells_in_pblock}/{cells_in_design} cells (PARTIAL)"
                if status == "success":
                    status = "partial"
            else:
                key_details["compliance"] = f"added {cells_in_pblock}/{cells_in_design} cells"
        if not summary_parts:
            for line in lines[:50]:
                if any(kw in line for kw in ["Created pblock", "Set pblock", "Applied pblock", "Error"]):
                    stripped = line.strip()
                    if stripped:
                        summary_parts.append(stripped[:200])
        if has_validation_failure:
            status = "validation_failed"
            key_details["validation_failed"] = True

    elif tool_name.startswith("rapidwright_"):
        # JSON-based tools: parse and extract key fields
        try:
            data = json.loads(raw_result)
            if "error" in data:
                summary_parts.append(f"Error: {data.get('error', 'unknown')[:200]}")
                status = "error"
            else:
                # Store all data in key_details for full access
                for k, v in data.items():
                    if v is not None and not isinstance(v, (list, dict)):
                        key_details[k] = v
                    elif isinstance(v, list) and len(v) <= 20:
                        key_details[k] = v
                    elif isinstance(v, dict) and len(v) <= 10:
                        key_details[k] = v
                # Generate summary from top-level fields
                if data.get("message"):
                    summary_parts.append(data["message"][:200])
                if data.get("status"):
                    summary_parts.append(f"Status: {data['status']}")
                if not summary_parts:
                    # Fallback: first few keys
                    top_keys = [k for k in data.keys() if k != "error"][:5]
                    summary_parts.append(f"Keys: {', '.join(top_keys)}")
        except (json.JSONDecodeError, Exception):
            # Not JSON, treat as text
            pass

    # Determine truncation status
    was_truncated = True
    if tool_name in ("vivado_get_wns",):
        was_truncated = False
    elif tool_name in ("vivado_phys_opt_design", "vivado_report_timing_summary",
                       "vivado_route_design", "vivado_place_design"):
        was_truncated = True
    elif tool_name.startswith("rapidwright_"):
        was_truncated = False
    elif tool_name in ("vivado_extract_critical_path_pins", "vivado_create_and_apply_pblock"):
        was_truncated = False

    # Fallback: generic truncation
    if not summary_parts:
        was_truncated = True
        meaningful = [l.strip() for l in lines[:30] if l.strip() and not l.strip().startswith(("INFO:", "WARNING:", "//", "#"))]
        if meaningful:
            summary_parts.extend(meaningful[:5])
        else:
            summary_parts.append(f"{line_count} lines, {char_count} chars")

    summary_line = "; ".join(summary_parts)

    # Build YAML output
    yaml_lines = ["tool_result:"]
    yaml_lines.append(f"  tool: {tool_name}")
    yaml_lines.append(f'  summary: "{summary_line}"')
    if key_details:
        yaml_lines.append("  key_details:")
        for k, v in key_details.items():
            if v is not None:
                yaml_lines.append(f"    {k}: {v}")
    yaml_lines.append(f"  status: {status}")
    yaml_lines.append(f"  raw_output_truncated: {str(was_truncated).lower()}")
    yaml_lines.append(f"  raw_output_chars: {char_count}")

    return '\n'.join(yaml_lines)
