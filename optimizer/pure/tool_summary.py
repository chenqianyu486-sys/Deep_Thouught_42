"""Tool result summarization pure functions.

Extracted from dcp_optimizer.py: _summarize_tool_result (L1938-2349),
_filter_tool_result (L1896-1936).
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .timing import parse_timing_summary
from .constants import (
    TOOL_RESULT_TRUNCATE,
    SMALL_OUTPUT_THRESHOLD,
    RAW_OUTPUT_DIRECT_THRESHOLD,
    RAW_OUTPUT_SMART_TRUNCATE,
)


def filter_tool_result(tool_name: str, result: str, truncate_limit: int = TOOL_RESULT_TRUNCATE) -> str:
    """Retain key information based on tool type, truncate redundant content."""
    if len(result) <= truncate_limit:
        return result

    # Timing reports + critical path extraction: retain WNS/TNS/key path summary.
    # extract_critical_path_cells returns JSON that must not be mid-stream truncated
    # (would produce invalid JSON for the LLM), so route it through the timing branch.
    tool_lower = tool_name.lower()
    if 'timing' in tool_lower or 'critical_path' in tool_lower:
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
    prev_best_tns: Optional[float] = None,
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
        prev_best_tns: Previous best TNS for delta calculation.
    """
    lines = raw_result.split('\n')
    line_count = len(lines)
    char_count = len(raw_result)

    # Special case: vivado_get_raw_tool_output — LLM explicitly requested full detail.
    # Bypass summarization for moderate-sized outputs; smart-truncate for huge ones.
    if tool_name == "vivado_get_raw_tool_output":
        if char_count <= RAW_OUTPUT_DIRECT_THRESHOLD:
            indent = '\n'.join('    ' + line for line in lines)
            return (
                f"tool_result:\n"
                f"  tool: {tool_name}\n"
                f'  summary: "Raw output from {lines[0] if lines else ""}"\n'
                f"  status: completed\n"
                f"  raw_output_truncated: false\n"
                f"  raw_output_chars: {char_count}\n"
                f"  raw_output: |\n{indent}"
            )
        else:
            head_len = RAW_OUTPUT_SMART_TRUNCATE // 2
            tail_len = RAW_OUTPUT_SMART_TRUNCATE // 2
            truncated = (
                raw_result[:head_len]
                + f"\n...[{char_count - RAW_OUTPUT_SMART_TRUNCATE} chars truncated]...\n"
                + raw_result[-tail_len:]
            )
            indent = '\n'.join('    ' + line for line in truncated.split('\n'))
            return (
                f"tool_result:\n"
                f"  tool: {tool_name}\n"
                f'  summary: "Raw output from {lines[0] if lines else ""} ({char_count} chars, truncated)"\n'
                f"  status: completed\n"
                f"  raw_output_truncated: true\n"
                f"  raw_output_chars: {char_count}\n"
                f"  raw_output: |\n{indent}"
            )

    # Bypass summarization for compact outputs.
    # extract_critical_path_cells excluded from bypass: even small JSON should
    # go through the dedicated summary branch to extract D1/D2 diagnostics.
    if char_count < SMALL_OUTPUT_THRESHOLD and 'timing' not in tool_name \
            and tool_name != 'vivado_extract_critical_path_cells':
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
            if prev_best_tns is not None and tns is not None:
                key_details["tns_delta"] = round(tns - prev_best_tns, 3)
            key_details["failing_endpoints"] = fe

            # New: clock domain breakdown
            clock_domains = timing.get("clock_domains", [])
            if clock_domains:
                domain_strs = []
                for d in clock_domains:
                    w = d.get("wns")
                    if w is not None:
                        domain_strs.append(f"{d['clock']}({w:.3f})")
                    else:
                        domain_strs.append(d["clock"])
                if len(domain_strs) > 1:
                    summary_parts.append(f"Clocks: {'; '.join(domain_strs)}")
                key_details["clock_domains"] = [
                    {"clock": d["clock"], "wns": d.get("wns"), "tns": d.get("tns"),
                     "failing_endpoints": d.get("failing_endpoints")}
                    for d in clock_domains
                ]

            # New: hold timing
            hold_wns = timing.get("hold_wns")
            if hold_wns is not None:
                key_details["hold_wns"] = hold_wns
                key_details["hold_tns"] = timing.get("hold_tns")
                key_details["hold_failing"] = timing.get("hold_failing")

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

    elif tool_name == "vivado_physopt_and_route":
        try:
            data = json.loads(raw_result)
            post = data.get("post_optimization", {})
            if isinstance(post, dict) and post.get("wns") is not None:
                wns = float(post["wns"])
                tns = float(post.get("tns")) if post.get("tns") is not None else None
                fe = int(post.get("failing_endpoints")) if post.get("failing_endpoints") is not None else None
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
            pre = data.get("pre_optimization", {})
            if isinstance(pre, dict) and pre.get("wns") is not None:
                key_details["pre_wns"] = round(pre["wns"], 3)
                key_details["pre_tns"] = round(pre["tns"], 3) if pre.get("tns") is not None else None
            if data.get("physopt_directive"):
                key_details["directive"] = data["physopt_directive"]
            if data.get("errors"):
                status = "partial"
                summary_parts.append(f"Errors: {len(data['errors'])}")
        except (json.JSONDecodeError, TypeError, ValueError):
            # Not valid JSON, treat as text
            pass

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

    elif tool_name == "vivado_extract_critical_path_cells":
        # D1/D2: extract diagnostic summary from per-node breakdown + clock context
        try:
            data = json.loads(raw_result)
            if isinstance(data, dict) and "error" in data:
                summary_parts.append(f"Error: {data['error'][:200]}")
                status = "error"
            elif isinstance(data, list):
                path_count = len(data)
                summary_parts.append(f"Extracted {path_count} critical paths")
                key_details["path_count"] = path_count
                if data:
                    worst = data[0]  # paths sorted by slack (worst first)
                    if worst.get("slack") is not None:
                        key_details["worst_slack"] = round(worst["slack"], 3)
                        summary_parts.append(f"WNS: {worst['slack']:.3f}ns")
                    if worst.get("startpoint"):
                        key_details["worst_startpoint"] = worst["startpoint"]
                    if worst.get("endpoint_pin"):
                        key_details["worst_endpoint"] = worst["endpoint_pin"]
                    clk = worst.get("clock", {})
                    if clk.get("clock_skew") is not None:
                        key_details["worst_clock_skew"] = round(clk["clock_skew"], 3)
                    if clk.get("clock_uncertainty") is not None:
                        key_details["worst_clock_uncertainty"] = round(clk["clock_uncertainty"], 3)
                    if clk.get("is_cross_clock"):
                        key_details["worst_is_cross_clock"] = True
                    # Top delay hotspots from worst path
                    top_nodes = worst.get("top_delay_nodes", [])
                    if top_nodes:
                        key_details["total_delay_nodes_worst"] = len(top_nodes)
                        hotspots = []
                        for n in top_nodes[:5]:
                            hotspots.append({
                                "name": n.get("name", ""),
                                "type": n.get("cell_type") or n.get("kind", ""),
                                "incr": round(n.get("incr_delay", 0), 3) if n.get("incr_delay") is not None else None,
                            })
                        key_details["top_delay_hotspots"] = hotspots
                        hs_str = ", ".join(f"{h['name']}={h['incr']:.3f}ns" for h in hotspots if h.get('incr') is not None)
                        if hs_str:
                            summary_parts.append(f"Top hotspots: {hs_str}")
                        if len(top_nodes) > 5:
                            summary_parts.append(f"({len(top_nodes)} total delay nodes on worst path)")
                    # Delay hotspots from additional paths (up to 2 more)
                    for pi, path in enumerate(data[1:3], 1):
                        p_nodes = path.get("top_delay_nodes", [])
                        if p_nodes:
                            p_hot = [n for n in p_nodes[:2] if n.get("incr_delay") is not None]
                            for n in p_hot:
                                incr = round(n["incr_delay"], 3)
                                summary_parts.append(f"Path{pi}: {n.get('name','')}={incr:.3f}ns")
        except (json.JSONDecodeError, Exception):
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
                       "vivado_route_design", "vivado_place_design", "vivado_physopt_and_route"):
        was_truncated = True
    elif tool_name.startswith("rapidwright_"):
        was_truncated = False
    elif tool_name in ("vivado_extract_critical_path_pins", "vivado_create_and_apply_pblock",
                       "vivado_extract_critical_path_cells"):
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


# ── Compact one-line summary for dashboard / handoff ───────────────────
# Used by both phase_analyze._extract_recent_tool_results and
# state_space._append_recent_analysis_results to avoid duplicate regex logic.

_ANALYSIS_TOOL_NAMES: frozenset[str] = frozenset({
    "vivado_report_timing_summary", "vivado_extract_critical_path_cells",
    "vivado_get_cached_high_fanout_nets", "vivado_check_design_status",
    "rapidwright_analyze_critical_path_spread", "rapidwright_analyze_congestion",
    "rapidwright_analyze_net_detour", "rapidwright_get_design_info",
    "rapidwright_get_device_topology", "rapidwright_report_timing",
    "rapidwright_search_cells", "rapidwright_analyze_pblock_region",
    "rapidwright_flatten_lut_cascade",
})


def compact_tool_summary(tool_name: str, raw_result: str) -> str:
    """Return a compact one-line summary of an analysis tool's raw output.

    Prefers JSON parsing for structured tools, falls back to regex for text
    tools. Returns "" if no meaningful summary could be extracted.

    This is the single source of truth for dashboard/handoff tool-result
    summaries — both phase_analyze.py and state_space.py call this function
    instead of maintaining separate regex implementations.
    """
    if not raw_result or not raw_result.strip():
        return ""

    name = tool_name
    raw = raw_result

    if name == "vivado_report_timing_summary":
        wns = _regex_first(raw, r'"?wns"?[\s:=]+([-\d.]+)', r'wns[\s:=]+([-\d.]+)')
        tns = _regex_first(raw, r'"?tns"?[\s:=]+([-\d.]+)', r'tns[\s:=]+([-\d.]+)')
        fe = _regex_first(raw, r'"?failing_endpoints"?[\s:=]+([-\d.]+)',
                          r'failing_endpoints[\s:=]+([-\d.]+)')
        return f"WNS={wns}, TNS={tns}, FE={fe}"

    if name == "rapidwright_analyze_critical_path_spread":
        data = _try_json(raw)
        if isinstance(data, dict):
            avg = data.get("avg_distance", data.get("avg_max_distance", "?"))
            mx = data.get("max_distance", "?")
            cnt = data.get("paths_analyzed", "?")
            return f"avg={avg}, max={mx}, paths={cnt}"
        return raw.strip().split("\n")[0][:80]

    if name == "rapidwright_analyze_congestion":
        data = _try_json(raw)
        if isinstance(data, dict):
            score = data.get("global_score", data.get("congested_ratio", "?"))
            sev = data.get("severity", "?")
            return f"global_score={score}, severity={sev}"
        return raw.strip().split("\n")[0][:80]

    if name == "vivado_get_cached_high_fanout_nets":
        matches = re.findall(r"fanout[=:]\s*(\d+)", raw)
        if matches:
            max_fo = max(int(m) for m in matches)
            return f"{len(matches)} nets, max_fanout={max_fo}"
        return raw.strip().split("\n")[0][:80]

    if name == "rapidwright_analyze_net_detour":
        data = _try_json(raw)
        if isinstance(data, list):
            return f"{len(data)} cells with detour > threshold"
        if isinstance(data, dict) and data.get("cells"):
            return f"{len(data['cells'])} cells"
        return raw.strip().split("\n")[0][:80]

    if name == "vivado_check_design_status":
        m = re.search(r'"status["\s:]+"([^"]+)"', raw)
        st = m.group(1) if m else "?"
        return f"status={st}"

    if name == "rapidwright_search_cells":
        data = _try_json(raw)
        if isinstance(data, dict):
            cnt = data.get("cell_count", "?")
            return f"{cnt} cells"
        m = re.search(r'"cell_count["\s:]+(\d+)', raw)
        cnt = m.group(1) if m else "?"
        return f"{cnt} cells"

    if name == "vivado_extract_critical_path_cells":
        data = _try_json(raw)
        if isinstance(data, list):
            return f"{len(data)} critical paths extracted"
        return "critical paths extracted"

    if name == "rapidwright_get_design_info":
        data = _try_json(raw)
        if isinstance(data, dict):
            cells = data.get("total_cells", data.get("cell_count", "?"))
            return f"cells={cells}"
        return raw.strip().split("\n")[0][:80]

    if name == "rapidwright_report_timing":
        wns = _regex_first(raw, r'"wns"[\s:]+([-\d.]+)', r'wns[\s:=]+([-\d.]+)')
        return f"WNS={wns}" if wns != "?" else raw.strip().split("\n")[0][:80]

    # Generic fallback: first non-empty line
    first = raw.strip().split("\n")[0][:80] if raw.strip() else ""
    return first if first else "completed"


def _try_json(raw: str):
    """Try to parse raw as JSON, return None on failure."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _regex_first(raw: str, pattern_json: str, pattern_text: str) -> str:
    """Try JSON-key regex first, then plain-text regex. Return '?' on miss."""
    m = re.search(pattern_json, raw, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(pattern_text, raw, re.IGNORECASE)
    if m:
        return m.group(1)
    return "?"
