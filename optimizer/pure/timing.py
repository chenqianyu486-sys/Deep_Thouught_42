"""Timing parsing pure functions.

Extracted from dcp_optimizer.py: parse_timing_summary_static (L194),
parse_high_fanout_nets (L639), _parse_resource_utilization (L372),
_is_valid_wns (L957), _compute_timing_hash (L979).
"""

from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger(__name__)


def parse_timing_summary(timing_report: str) -> dict:
    """Parse timing summary report to extract WNS, TNS, and failing endpoints.

    Automatically skips separator lines, empty lines, and non-timing lines
    (license messages, command echoes, info/warning messages) to locate the
    timing header and data.

    Returns:
        dict with keys: wns, tns, failing_endpoints (None if parse fails).
    """
    result = {"wns": None, "tns": None, "failing_endpoints": None}
    lines = timing_report.split('\n')

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
    return result


def parse_high_fanout_nets(report: str) -> list[tuple[str, int, int]]:
    """Parse high fanout nets report.

    Returns:
        List of (net_name, fanout, path_count) tuples.
    """
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
                    net_name = parts[2]

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


def parse_hold_timing(timing_text: str) -> dict:
    """Parse hold timing section from report_timing_summary output.

    Vivado's report_timing_summary includes both setup and hold sections.
    This extracts the hold WNS/WHS from the min delay path report.
    Competition requires hold WNS >= 0.

    Returns:
        dict with keys: hold_wns, hold_tns, hold_failing (None if parse fails).
    """
    result = {"hold_wns": None, "hold_tns": None, "hold_failing": None}
    lines = timing_text.split('\n')

    in_hold = False
    for line in lines:
        s = line.strip()
        if 'Hold' in s and ('WNS' in s or 'WHS' in s):
            in_hold = True
            continue
        if in_hold and s and not s.startswith('---'):
            parts = s.split()
            try:
                if len(parts) >= 4:
                    result["hold_wns"] = float(parts[0])
                    result["hold_tns"] = float(parts[1])
                    result["hold_failing"] = int(parts[2])
                elif len(parts) >= 2:
                    result["hold_wns"] = float(parts[0])
                    result["hold_tns"] = float(parts[1])
                break
            except (ValueError, IndexError):
                continue
        if in_hold and 'Setup' in s:
            break

    return result
