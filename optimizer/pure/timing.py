"""Timing parsing pure functions.

Extracted from dcp_optimizer.py: parse_timing_summary_static (L194),
parse_high_fanout_nets (L639), _parse_resource_utilization (L372),
_is_valid_wns (L957), _compute_timing_hash (L979).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)


def parse_timing_summary(timing_report: str) -> dict:
    """Parse timing summary report to extract WNS, TNS, and failing endpoints.

    Automatically skips separator lines, empty lines, and non-timing lines
    (license messages, command echoes, info/warning messages) to locate the
    timing header and data.

    Extended return dict (beyond the original 3 fields):
        clock_domains: list of per-clock {clock, wns, tns, failing_endpoints, total_endpoints}
        hold_wns / hold_tns / hold_failing: hold timing from parse_hold_timing()
    """
    # The Vivado MCP tool wraps report_timing_summary as JSON with pre-parsed
    # fields (wns/tns/whs/ths/tpws) plus a truncated raw_report excerpt. Use
    # the MCP's pre-parsed values when available — they come from the full
    # (untruncated) report and are more reliable than re-parsing the excerpt.
    try:
        wrapped = json.loads(timing_report)
        if isinstance(wrapped, dict) and "wns" in wrapped:
            wns = wrapped.get("wns")
            if wns is not None:
                return {
                    "wns": wns,
                    "tns": wrapped.get("tns"),
                    "failing_endpoints": wrapped.get("failing_endpoints"),
                    "hold_wns": wrapped.get("whs"),
                    "hold_tns": wrapped.get("ths"),
                    "hold_failing": wrapped.get("hold_failing_endpoints"),
                    "wpws": wrapped.get("tpws"),
                    "wpws_tns": None,
                    "wpws_failing": None,
                    "clock_domains": _parse_clock_domains(wrapped.get("raw_report") or ""),
                }
            # MCP parser returned wns=None — fall back to text parsing on raw_report
            timing_report = wrapped.get("raw_report") or timing_report
    except (json.JSONDecodeError, TypeError):
        pass

    result = {"wns": None, "tns": None, "failing_endpoints": None}
    lines = timing_report.split('\n')

    # --- Existing table-format parser (backward compatible) ---
    header_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('Command:'):
            continue
        if 'Attempting to get a license' in stripped or 'Got license' in stripped:
            continue
        if any(x in stripped for x in ['INFO:', 'WARNING:', 'ERROR:', 'Common 17-']):
            continue
        if any(stripped.startswith(x) for x in ['phys_opt_design', 'place_design', 'route_design', 'report_']):
            continue
        if 'WNS(ns)' in line and 'TNS(ns)' in line:
            header_idx = i
            break
    if header_idx == -1:
        # Return with clock_domains + hold even if table not found
        result["clock_domains"] = _parse_clock_domains(timing_report)
        hold = parse_hold_timing(timing_report)
        result["hold_wns"] = hold.get("hold_wns")
        result["hold_tns"] = hold.get("hold_tns")
        result["hold_failing"] = hold.get("hold_failing")
        pulse = parse_pulse_width(timing_report)
        result["wpws"] = pulse.get("wpws")
        result["wpws_tns"] = pulse.get("wpws_tns")
        result["wpws_failing"] = pulse.get("wpws_failing")
        return result

    for data_line in lines[header_idx + 1:]:
        stripped = data_line.strip()
        if not stripped or stripped.startswith('---') or stripped.startswith('==='):
            continue
        if any(x in stripped for x in ['Command:', 'INFO:', 'WARNING:', 'ERROR:', 'Attempting', 'Got license', 'Common 17-']):
            continue
        parts = stripped.split()
        if len(parts) >= 3:
            try:
                result["wns"] = float(parts[0])
                result["tns"] = float(parts[1])
                result["failing_endpoints"] = int(parts[2])
                break
            except (ValueError, IndexError):
                continue

    # --- New: clock domain breakdown ---
    result["clock_domains"] = _parse_clock_domains(timing_report)

    # --- New: hold timing ---
    hold = parse_hold_timing(timing_report)
    result["hold_wns"] = hold.get("hold_wns")
    result["hold_tns"] = hold.get("hold_tns")
    result["hold_failing"] = hold.get("hold_failing")
    pulse = parse_pulse_width(timing_report)
    result["wpws"] = pulse.get("wpws")
    result["wpws_tns"] = pulse.get("wpws_tns")
    result["wpws_failing"] = pulse.get("wpws_failing")

    return result


def _parse_clock_domains(timing_report: str) -> list[dict]:
    """Parse per-clock-domain timing tables from report_timing_summary output.

    Vivado's report_timing_summary includes per-clock sections like::

        Clock Domain: clk_name
        WNS(ns)    TNS(ns)    Failing Endpoints    Total Endpoints
        -0.234    -123.456    45                    2345

    Returns:
        List of {clock, wns, tns, failing_endpoints, total_endpoints} dicts,
        one per clock domain found, or empty list if none found.
    """
    domains: list[dict] = []
    lines = timing_report.split('\n')

    for i, line in enumerate(lines):
        stripped = line.strip()
        if 'Clock Domain:' not in stripped:
            continue

        # Extract clock name (first token after "Clock Domain:")
        clock_name = stripped.split('Clock Domain:')[1].strip()
        # Take only the first word (some variants include extra text)
        clock_name = clock_name.split()[0] if clock_name else ""
        if not clock_name:
            continue

        # Find the WNS table header within the next 10 lines
        for j in range(i + 1, min(i + 10, len(lines))):
            j_line = lines[j].strip()
            if 'WNS(ns)' in j_line and 'TNS(ns)' in j_line:
                # Read first data row (skip separator/blank lines)
                for k in range(j + 1, min(j + 5, len(lines))):
                    data = lines[k].strip()
                    if not data or data.startswith('---') or data.startswith('==='):
                        continue
                    parts = data.split()
                    if len(parts) >= 3:
                        try:
                            domain = {
                                "clock": clock_name,
                                "wns": float(parts[0]),
                                "tns": float(parts[1]),
                                "failing_endpoints": int(parts[2]),
                                "total_endpoints": int(parts[3]) if len(parts) >= 4 else None,
                            }
                            domains.append(domain)
                        except (ValueError, IndexError):
                            pass
                    break  # Only try first non-separator data row
                break  # Found the table for this clock domain
    return domains


def parse_high_fanout_nets(report: str) -> list[tuple[str, int, int]]:
    """Parse high fanout nets report.

    Returns:
        List of (net_name, fanout, path_count) tuples.
    """
    # The vivado get_critical_high_fanout_nets tool returns a JSON wrapper
    # (json.dumps({"raw_output": <report>, ...}, indent=2)). The report's
    # newlines are JSON-escaped inside raw_output, so split('\n') on the
    # wrapper only sees the inter-key newlines and never the data rows -
    # the parser returned [] and state.high_fanout_nets stayed empty, forcing
    # the Fanout strategy to fall back to LLM-hallucinated net names
    # (run-20260713_073429). Unwrap raw_output first; fall back to treating
    # the input as plain text for direct callers (e.g. unit tests).
    if report and report.lstrip().startswith("{"):
        try:
            wrapper = json.loads(report)
            if isinstance(wrapper, dict):
                raw = wrapper.get("raw_output")
                if isinstance(raw, str) and raw:
                    report = raw
        except (ValueError, TypeError):
            pass

    nets = []
    lines = report.split('\n')
    in_net_section = False

    for line in lines:
        if 'Paths' in line and 'Fanout' in line and 'Parent Net Name' in line:
            in_net_section = True
            continue

        if in_net_section:
            if line.startswith('---') or not line.strip():
                continue
            if line.startswith('==='):
                break

            parts = line.split()
            if len(parts) >= 3:
                try:
                    path_count = int(parts[0])
                    fanout = int(parts[1])
                    # Join all remaining tokens: net names with spaces (escaped
                    # identifiers) would be truncated if only parts[2] is taken.
                    net_name = " ".join(parts[2:])

                    if (net_name and
                            '/' in net_name and
                            not net_name.startswith('get_') and
                            not net_name.startswith('ERROR') and
                            not net_name.startswith('WARNING')):
                        nets.append((net_name, fanout, path_count))
                except ValueError:
                    continue

    return nets


def parse_resource_utilization(report: str) -> dict | None:
    """Parse LUT/FF/DSP/BRAM/URAM counts from report_utilization_for_pblock output.

    Expected format::

        LUTs:    12,345
        FFs:     24,567
        DSPs:    45
        BRAMs:   120
        URAMs:   0

    Returns:
        dict with keys LUT/FF/DSP/BRAM/URAM, or None if parse fails.
    """
    resources = {"LUT": 0, "FF": 0, "DSP": 0, "BRAM": 0, "URAM": 0}
    patterns = {
        "LUT": r'LUTs:\s+([0-9,]+)',
        "FF": r'FFs:\s+([0-9,]+)',
        "DSP": r'DSPs:\s+([0-9,]+)',
        "BRAM": r'BRAMs:\s+([0-9,]+)',
        "URAM": r'URAMs:\s+([0-9,]+)',
    }
    for key, pat in patterns.items():
        m = re.search(pat, report)
        if m:
            try:
                resources[key] = int(m.group(1).replace(",", ""))
            except ValueError:
                return None
        else:
            return None  # Missing key means parse failure
    return resources


def is_valid_wns(
    wns: float | None,
    clock_period: float | None,
    best_wns: float,
) -> bool:
    """Validate WNS value to reject parsing errors and false positives.

    Args:
        wns: WNS value to validate.
        clock_period: Design clock period (for sanity check).
        best_wns: Current best WNS (for jump detection).
    """
    if wns is None:
        return False
    # WNS should not exceed 10x the clock period
    if clock_period and abs(wns) > clock_period * 10:
        logger.warning(f"WNS sanity check failed: {wns:.3f} ns > {clock_period * 10:.1f} ns (10x clock period)")
        return False
    # Extreme negative values are usually parsing errors
    if wns < -999:
        logger.warning(f"WNS sanity check failed: {wns:.3f} ns < -999")
        return False
    # Jump from negative to exactly 0.0 without optimization is suspicious
    if wns == 0.0 and best_wns > float('-inf') and best_wns < -0.1:
        logger.warning(f"WNS suspicious: 0.000 ns from {best_wns:.3f} ns without visible optimization")
    return True


def compute_timing_hash(raw_text: str) -> str:
    """Compute SHA256[:16] consistency hash from timing summary raw text."""
    if not raw_text:
        return ""
    return hashlib.sha256(raw_text.encode()).hexdigest()[:16]


def parse_route_status(report: str) -> dict:
    """Parse Vivado report_route_status output.

    Handles both raw Vivado text and the JSON envelope returned by the
    vivado_report_route_status MCP tool (which embeds the real report under
    "raw_report"); the envelope's own is_placed/is_routed fields are
    unreliable (see architecture.md §15.1) so we parse the raw text only.

    Vivado's ``report_route_status -return_string`` format::

        Design Route Status
           # of logical nets.......................... : 37081 :
           # of nets not needing routing.......... : 9120 :
           # of routable nets..................... : 27961 :
           # of fully routed nets............. : 27961 :
           # of nets with routing errors.......... : 0 :

    Field semantics (net counts):
      - total_nets: # of logical nets (all nets; exists regardless of routing)
      - routed_nets: # of fully routed nets (0 when design is unrouted)
      - unresolved_nets: # of nets with routing errors
      - routable_nets / not_needing_routing_nets: informational sub-totals

    Extended metrics (avg_wirelength, congestion_level, total_wirelength,
    max_wirelength, timing_violated_nets, long_route_nets_count) are extracted
    when present in extended report variants; the basic report omits them.

    Returns:
        dict with the keys above; counts default to 0, floats to None.
    """
    result: dict = {
        "total_nets": 0, "routed_nets": 0, "unresolved_nets": 0,
        "routable_nets": 0, "not_needing_routing_nets": 0,
        "long_route_nets_count": 0, "avg_wirelength": None,
        "congestion_level": None, "total_wirelength": None,
        "max_wirelength": None, "timing_violated_nets": None,
    }
    if not report or not report.strip():
        return result

    # Unwrap the MCP JSON envelope if present, so we parse the real report
    # instead of a single escaped JSON string line.
    text = report
    stripped = report.strip()
    if stripped.startswith("{"):
        try:
            envelope = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            envelope = None
        if isinstance(envelope, dict) and envelope.get("raw_report"):
            text = envelope["raw_report"]

    def _first_int(line: str):
        m = re.search(r"(\d[\d,]*)", line)
        return int(m.group(1).replace(",", "")) if m else None

    for line in text.split('\n'):
        s = line.strip().lower()
        if not s:
            continue
        # Net counts — match the full Vivado label so "fully routed" is not
        # caught by a generic "routed" substring (the prior bug).
        if "# of logical nets" in s:
            v = _first_int(line)
            if v is not None:
                result["total_nets"] = v
        elif "# of fully routed nets" in s:
            v = _first_int(line)
            if v is not None:
                result["routed_nets"] = v
        elif "# of nets with routing errors" in s:
            v = _first_int(line)
            if v is not None:
                result["unresolved_nets"] = v
        elif "# of routable nets" in s:
            v = _first_int(line)
            if v is not None:
                result["routable_nets"] = v
        elif "# of nets not needing routing" in s:
            v = _first_int(line)
            if v is not None:
                result["not_needing_routing_nets"] = v
        elif 'long route' in s or 'longest route' in s:
            v = _first_int(line)
            if v is not None:
                result["long_route_nets_count"] = v
        elif 'average wirelength' in s or 'avg wirelength' in s:
            m = re.search(r'([\d.]+)', line)
            if m:
                result["avg_wirelength"] = float(m.group(1))
        elif 'congestion level' in s:
            m = re.search(r'(LOW|MEDIUM|HIGH|CRITICAL)', line, re.IGNORECASE)
            if m:
                result["congestion_level"] = m.group(1).upper()
        elif 'total wirelength' in s and 'average' not in s:
            m = re.search(r'([\d,.]+)', line)
            if m:
                try:
                    result["total_wirelength"] = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        elif 'max wirelength' in s or 'maximum wirelength' in s:
            m = re.search(r'([\d.]+)', line)
            if m:
                result["max_wirelength"] = float(m.group(1))
        elif 'timing violated' in s or 'violated nets' in s:
            v = _first_int(line)
            if v is not None:
                result["timing_violated_nets"] = v
    return result


def parse_control_sets(report: str) -> dict:
    """Parse Vivado report_control_sets output.

    Fallback: if report_control_sets is unavailable, estimate from
    unique control set count via fanout analysis.

    Returns:
        dict with keys: total_control_sets, avg_per_slice (None if SLICE count unknown).
    """
    result: dict = {"total_control_sets": 0, "avg_per_slice": None}
    if not report or not report.strip():
        return result
    lines = report.split('\n')
    for line in lines:
        s = line.strip()
        if 'control set' in s.lower() and re.search(r'\d', s):
            m = re.search(r'(\d[\d,]*)', s)
            if m and 'unique' in s.lower():
                result["total_control_sets"] = int(m.group(1).replace(",", ""))
            elif m and 'total' in s.lower():
                result["total_control_sets"] = max(result["total_control_sets"], int(m.group(1).replace(",", "")))
    if result["total_control_sets"] == 0:
        for line in lines:
            m = re.search(r'(\d[\d,]*)\s*control', line, re.IGNORECASE)
            if m:
                result["total_control_sets"] = int(m.group(1).replace(",", ""))
                break
    return result


def parse_cdc_paths(report: str) -> int:
    """Parse report_timing -cross_clock output to count cross-domain paths.

    Returns:
        Number of cross-clock-domain timing paths found.
    """
    if not report or not report.strip():
        return 0
    count = 0
    lines = report.split('\n')
    for line in lines:
        if 'Slack' in line and 'Source' not in line and '---' not in line:
            count += 1
    if count == 0:
        for line in lines:
            if re.search(r'^\s*\d+\.', line):
                count += 1
    return count


def parse_design_info(result_text: str) -> dict | None:
    """Parse RapidWright get_design_info JSON output into standardized dict.

    Returns:
        dict with keys: design_name, cell_count, net_count, top_cell_types,
        or None if parse fails.
    """
    import json
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if data.get("status") != "success":
        return None
    return {
        "design_name": str(data.get("design_name", "")),
        "cell_count": int(data.get("cell_count", 0)),
        "net_count": int(data.get("net_count", 0)),
        "top_cell_types": dict(data.get("top_cell_types", {})),
    }


def parse_pvt_corner(timing_report: str) -> str | None:
    """Extract PVT corner from report_timing_summary header.

    Vivado timing reports include a header line like:
      'Speed Grade: -2  PVT: slow_0p95v_85c'
    or similar. Returns the PVT string, or None if not found.

    Returning None (rather than a fabricated default) prevents an unmeasured
    PVT corner from being presented to the LLM as a real value — the Dashboard
    renders None as ``N/A(not_extracted)``.
    """
    if not timing_report:
        return None
    for line in timing_report.split('\n'):
        if 'PVT' in line or 'pvt' in line:
            m = re.search(r'PVT[:\s]+(\S+)', line, re.IGNORECASE)
            if m:
                return m.group(1).rstrip(',.;')
        if 'Speed Grade' in line and ('slow' in line or 'fast' in line or 'typ' in line):
            for token in line.split():
                token_lower = token.lower().rstrip(',.;')
                if any(c in token_lower for c in ('slow', 'fast', 'typ', '0p', '_')):
                    if len(token) > 4:
                        return token.rstrip(',.;')
    return None


def compute_violation_summary(
    critical_paths: list,
    failing_endpoints: int | None = None,
) -> dict | None:
    """Compute aggregated violation distribution from critical paths.

    Returns a dict matching ViolationSummary fields, or None if no data.
    Pure function — can be tested independently.
    """
    if not critical_paths:
        if failing_endpoints is not None and failing_endpoints > 0:
            return {
                "total_failing_endpoints": failing_endpoints,
                "severity_distribution": {},
                "delay_profile_breakdown": {},
                "logic_level_distribution": {},
                "top_violating_modules": {},
                "path_clusters": [],
            }
        return None

    severity = {"critical": 0, "moderate": 0, "marginal": 0}
    delay_profile = {"logic_dominated": 0, "route_dominated": 0, "mixed": 0}
    logic_levels = {"levels_1_to_5": 0, "levels_6_to_10": 0, "levels_gt_10": 0}
    module_stats: dict[str, dict] = {}

    # Cluster accumulation: key = (module, delay_type)
    cluster_acc: dict[tuple[str, str], list] = {}

    for entry in critical_paths:
        slack = entry.slack if hasattr(entry, 'slack') else entry.get("slack")
        logic_pct = None
        route_pct = None
        levels = entry.levels if hasattr(entry, 'levels') else entry.get("levels")
        cells = entry.cells if hasattr(entry, 'cells') else entry.get("cells", [])

        if hasattr(entry, 'logic_delay') and entry.logic_delay is not None and hasattr(entry, 'net_delay') and entry.net_delay is not None:
            total = entry.logic_delay + entry.net_delay
            if total > 0:
                logic_pct = entry.logic_delay / total
                route_pct = entry.net_delay / total
        elif hasattr(entry, 'logic_delay_pct'):
            logic_pct = entry.logic_delay_pct

        # Severity classification
        if slack is not None:
            if slack < -1.0:
                severity["critical"] += 1
            elif slack < -0.3:
                severity["moderate"] += 1
            else:
                severity["marginal"] += 1

        # Delay profile classification
        delay_type = "mixed"
        if logic_pct is not None:
            if logic_pct > 0.6:
                delay_type = "logic_dominated"
                delay_profile["logic_dominated"] += 1
            elif logic_pct < 0.4:
                delay_type = "route_dominated"
                delay_profile["route_dominated"] += 1
            else:
                delay_profile["mixed"] += 1

        if levels is not None:
            if levels <= 5:
                logic_levels["levels_1_to_5"] += 1
            elif levels <= 10:
                logic_levels["levels_6_to_10"] += 1
            else:
                logic_levels["levels_gt_10"] += 1

        # Module-level accumulation for endpoint stats
        primary_module = ""
        for cell in cells:
            parts = cell.split("/")
            if len(parts) >= 2:
                module = parts[1]
                if module not in module_stats:
                    module_stats[module] = {
                        "endpoint_count": 0,
                        "min_slack": None,
                    }
                module_stats[module]["endpoint_count"] += 1
                if slack is not None:
                    if module_stats[module]["min_slack"] is None or slack < module_stats[module]["min_slack"]:
                        module_stats[module]["min_slack"] = round(slack, 3)
                if not primary_module:
                    primary_module = module

        # Cluster accumulation
        if primary_module:
            cluster_key = (primary_module, delay_type)
            if cluster_key not in cluster_acc:
                cluster_acc[cluster_key] = []
            cluster_acc[cluster_key].append(entry)

    top_modules = dict(
        sorted(module_stats.items(), key=lambda x: -x[1]["endpoint_count"])[:5]
    )

    # Build path clusters with representatives
    path_clusters = _build_path_clusters(cluster_acc, critical_paths)

    return {
        "total_failing_endpoints": failing_endpoints,
        "severity_distribution": severity,
        "delay_profile_breakdown": delay_profile,
        "logic_level_distribution": logic_levels,
        "top_violating_modules": top_modules,
        "path_clusters": path_clusters,
    }


def _build_path_clusters(
    cluster_acc: dict[tuple[str, str], list],
    critical_paths: list,
) -> list[dict]:
    """Build representative path clusters from accumulated data.

    Groups paths by (module, delay_type), picks worst-slack representative,
    and returns cluster descriptors with enough detail for LLM decision-making.
    """
    clusters = []
    # Sort clusters by worst slack (most severe first)
    sorted_keys = sorted(
        cluster_acc.keys(),
        key=lambda k: min(
            (e.slack if hasattr(e, 'slack') and e.slack is not None else 0.0)
            for e in cluster_acc[k]
        ),
    )

    for module, delay_type in sorted_keys[:5]:  # Max 5 clusters
        entries = cluster_acc[(module, delay_type)]
        slacks = [
            e.slack for e in entries
            if hasattr(e, 'slack') and e.slack is not None
        ]
        levels_list = [
            e.levels for e in entries
            if hasattr(e, 'levels') and e.levels is not None
        ]

        # Find worst-slack path as representative
        worst_entry = min(
            entries,
            key=lambda e: e.slack if hasattr(e, 'slack') and e.slack is not None else float('inf'),
        )

        # Get representative cells (up to 6)
        rep_cells = []
        if hasattr(worst_entry, 'cells') and worst_entry.cells:
            rep_cells = worst_entry.cells[:6]

        # Find the index of the representative path in the original critical_paths list
        rep_idx = 0
        for i, cp in enumerate(critical_paths):
            if (hasattr(cp, 'cells') and hasattr(worst_entry, 'cells')
                    and cp.cells == worst_entry.cells):
                rep_idx = i
                break

        # Compute delay pct for representative
        avg_logic_delay_pct = None
        if hasattr(worst_entry, 'logic_delay') and worst_entry.logic_delay is not None:
            total_delay = (worst_entry.logic_delay or 0) + (worst_entry.net_delay or 0)
            if total_delay > 0:
                avg_logic_delay_pct = round(worst_entry.logic_delay / total_delay, 4)

        cluster = {
            "cluster_id": f"{delay_type}_{module}",
            "cluster_type": delay_type,
            "module": module,
            "path_count": len(entries),
            "worst_slack": round(min(slacks), 3) if slacks else None,
            "best_slack": round(max(slacks), 3) if slacks else None,
            "avg_logic_delay_pct": avg_logic_delay_pct,
            "avg_logic_levels": round(sum(levels_list) / len(levels_list), 1) if levels_list else None,
            "representative_cells": rep_cells,
            "representative_path_idx": rep_idx,
        }
        clusters.append(cluster)

    return clusters


def parse_hold_timing(timing_text: str) -> dict:
    """Parse hold timing section from report_timing_summary output.

    Vivado's report_timing_summary includes both setup and hold sections.
    This extracts the hold WNS/WHS from the min delay path report.
    Competition requires hold WNS >= 0.

    Returns:
        dict with keys: hold_wns, hold_tns, hold_failing (None if parse fails).
    """
    result = {"hold_wns": None, "hold_tns": None, "hold_failing": None}
    m = re.search(
        r"Hold\s*:\s*(\d+)\s+Failing.*?Worst\s+Slack\s+(-?\d+\.?\d*)ns.*?Total\s+Violation\s+(-?\d+\.?\d*)ns",
        timing_text,
        re.DOTALL,
    )
    if m:
        result["hold_failing"] = int(m.group(1))
        result["hold_wns"] = float(m.group(2))
        result["hold_tns"] = float(m.group(3))
    return result

def parse_pulse_width(timing_text: str) -> dict:
    """Parse pulse width slack section from report_timing_summary output.

    Vivado's report_timing_summary includes a Pulse Width section that
    reports minimum pulse width violations (WPWS).

    Returns:
        dict with keys: wpws, wpws_tns, wpws_failing (None if parse fails).
    """
    result = {"wpws": None, "wpws_tns": None, "wpws_failing": None}
    m = re.search(r"Pulse\s*Width\s*:\s*(\d+)\s+Failing.*?Worst\s+Slack\s+(-?\d+\.?\d*)ns.*?Total\s+Violation\s+(-?\d+\.?\d*)ns", timing_text, re.DOTALL)
    if m:
        result["wpws_failing"] = int(m.group(1))
        result["wpws"] = float(m.group(2))
        result["wpws_tns"] = float(m.group(3))
    return result
