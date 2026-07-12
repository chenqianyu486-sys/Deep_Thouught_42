#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache 2.0

"""
MCP Server for Vivado - manages Vivado via pexpect for stdin/stdout control.

Usage:
    python vivado_mcp_server.py [--vivado-path /path/to/vivado]
"""

import argparse
import atexit
import json
import logging
import os
import re
import signal
import shutil
import sys
import time
from typing import Optional, Dict, Any

import pexpect
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Import sanitization utilities
try:
    from context_manager.logging_config import sanitize_payload, get_trace_id
except ImportError:
    # Fallback if context_manager not available
    def sanitize_payload(payload, max_length=1024):
        return payload
    def get_trace_id():
        return ""

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Vivado Tcl prompt pattern
# Pattern requires newline before prompt to avoid matching prompt in command echoes.
# This prevents the issue where pexpect matches stale prompts in the buffer.
VIVADO_PROMPT = r"\r?\nVivado% "

# Global state
_vivado_process: Optional[pexpect.spawn] = None
_vivado_pid: Optional[int] = None
_vivado_path: Optional[str] = None
_vivado_log_file: Optional[str] = None
_vivado_journal_file: Optional[str] = None
_design_open: bool = False
# Last successfully opened DCP path — used for auto-reopen after restart.
# Vivado Tcl timeout poisons the session; we kill and restart, then reopen.
_last_dcp_path: Optional[str] = None

# PhysOpt safety guard: block retiming directives that cause functional errors
PHYSOPT_BLOCKED_DIRECTIVES: frozenset[str] = frozenset({"AlternateFlowWithRetiming", "AddRetime"})
PHYSOPT_BLOCKED_BOOL_OPTIONS: frozenset[str] = frozenset({"retime", "interconnect_retime", "insert_negative_edge_ffs", "restruct_opt"})
PHYSOPT_SAFE_DIRECTIVES: frozenset[str] = frozenset({
    "Default", "Explore", "AggressiveExplore", "RuntimeOptimized",
    "ExploreWithHoldFix", "ExploreWithAggressiveHoldFix",
    "AlternateReplication", "AggressiveFanoutOpt", "RQS",
})

# opt_design directive that performs retiming - blocked (breaks functional equivalence)
OPT_BLOCKED_DIRECTIVES: frozenset[str] = frozenset({"AddRetime"})
# opt_design safe directives whitelist (consistent with PHYSOPT_SAFE_DIRECTIVES).
# Any directive not in this set is rejected - defense in depth.
OPT_SAFE_DIRECTIVES: frozenset[str] = frozenset({
    "Default", "Explore", "ExploreWithAreaDuplication",
    "ExploreSequentialArea", "NoBramOptimization",
    "NoDspOptimization", "RuntimeOptimized", "DataSpreadMem",
    "AddRemap",
})

# place_design safe directives whitelist (consistent with OPT_SAFE_DIRECTIVES).
# Any directive not in this set is rejected for defense-in-depth.
# Explicitly excludes retiming-related directives (AddRetime, Performance_Retiming, etc.)
#
# Audited against the Vivado 2025.1 place_design man page, which enumerates 23
# supported -directive values; only those are whitelisted. All Vivado implementation
# STRATEGY preset names (Performance_*, Area_*, Flow*, Congestion_SpreadLogic_*,
# SpreadLogic_*, LateBlockPlacement, and the WLBlockPlacement typo of
# WLDrivenBlockPlacement) were removed: they are `set_property strategy` values,
# NOT valid place_design -directive values, and are rejected by Vivado 2025.1 with
# Constraints 18-641 (run-20260711_015650: NetDelay_high; run-20260711_164134:
# Congestion_Explore). The handler returns a hard error (not a silent
# default-placement fallback) when Vivado rejects a whitelisted directive, so
# future whitelist drift fails the strategy cleanly instead of a silent regression.
PLACE_SAFE_DIRECTIVES: frozenset[str] = frozenset({
    "Default", "Explore", "ExtraTimingOpt", "ExtraPostPlacementOpt",
    "AltSpreadLogic_high", "AltSpreadLogic_medium", "AltSpreadLogic_low",
    "EarlyBlockPlacement", "SSI_SpreadLogic_high", "SSI_SpreadLogic_low",
    "Quick", "RuntimeOptimized",
})

# route_design safe directives whitelist. Audited against the Vivado 2025.1
# route_design man page, which enumerates 10 supported -directive values; only
# those are whitelisted. All Vivado implementation STRATEGY preset names
# (Performance_*, Area_*, Flow*, SSI_*, AlternateRoutability, LowerDelayCost) were
# removed: they are `set_property strategy` values, NOT valid route_design
# -directive values, rejected by Vivado 2025.1 with Constraints 18-641
# (run-20260711_015650: Congestion_Explore). The man page also lists
# MoreGlobalIterations, AdvancedSkewModeling, and AlternateCLBRouting as valid;
# not whitelisted only because no strategy uses them yet - add when needed.
ROUTE_SAFE_DIRECTIVES: frozenset[str] = frozenset({
    "Default", "Explore", "AggressiveExplore", "HigherDelayCost",
    "NoTimingRelaxation", "RuntimeOptimized", "Quick",
})


def _is_unrecognized_directive_error(output: str) -> bool:
    """Return True if Vivado rejected a -directive as unrecognized (Constraints 18-641).

    Used by the place_design/route_design handlers to auto-fall back to the
    default directive when a whitelisted directive is rejected by the running
    Vivado version. This keeps the toolchain robust to whitelist/version drift:
    a rejected directive retries once with the plain command instead of failing
    the whole strategy (run-20260711_015650: NetDelay_high / Congestion_Explore
    caused instant strategy failure before this guard existed).
    """
    if not output or not isinstance(output, str):
        return False
    return ("18-641" in output) or ("not a recognized directive" in output)


# TCL security primitives (blocked-command detection, safe quoting,
# line completeness check) live in tcl_security.py for independent unit testing.
from tcl_security import (
    BLOCKED_TCL_COMMANDS,
    contains_blocked_tcl_command,
    tcl_quote,
    tcl_line_is_complete,
)


def _is_truthy(val) -> bool:
    """Check if a value represents a truthy/affirmative setting.
    
    Handles both boolean True and string representations like
    "true", "1", "yes" that LLMs may send instead of proper booleans.
    """
    if val is True:
        return True
    if isinstance(val, str) and val.lower() in ("true", "1", "yes"):
        return True
    return False


def _safe_float(value: str) -> float | None:
    """Safely parse float, returning None on failure."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value: str) -> int | None:
    """Safely parse int, returning None on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_timing_summary(report: str) -> dict:
    """Extract WNS, TNS, WHS, THS, TPWS, and failing endpoints from report_timing_summary output.

    Uses column-index based parsing: locates the header line by 'WNS(ns)',
    determines column positions from header tokens, then reads the first data
    line using those positions. This is robust against different Vivado output
    formats and column ordering.

    Returns dict with all fields optional (None if parsing fails):
    - wns / tns / whs / ths / tpws: float or None
    - failing_endpoints / hold_failing_endpoints: int or None
    """
    result: dict = {
        "wns": None, "tns": None, "failing_endpoints": None,
        "whs": None, "ths": None, "hold_failing_endpoints": None,
        "tpws": None,
    }
    lines = report.split('\n')

    # Locate header line containing column names
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

    # Parse header tokens to build column index map
    header_tokens = lines[header_idx].split()
    col_indices: dict[str, int] = {}
    for field in ("WNS", "TNS", "WHS", "THS", "TPWS"):
        for idx, tok in enumerate(header_tokens):
            # Match field prefix (e.g. "WNS(ns)" or "Worst...WNS")
            if field in tok.upper():
                # For failing_endpoints count, it's always the column after TNS
                if field == "TNS":
                    # The "Failing Endpoints" column used to be named differently
                    pass
                col_indices[field] = idx
                break

    # Find first data line (non-empty, non-separator after header)
    data_line = None
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith('---') or stripped.startswith('==='):
            continue
        if any(x in stripped for x in ['Command:', 'INFO:', 'WARNING:', 'ERROR:', 'Attempting', 'Got license', 'Common 17-']):
            continue
        data_line = stripped
        break

    if data_line is None:
        return result

    parts = data_line.split()
    if len(parts) < 2:
        return result

    # Assign values by column index (if index exists and data is available)
    wns_idx = col_indices.get("WNS")
    tns_idx = col_indices.get("TNS")
    whs_idx = col_indices.get("WHS")
    ths_idx = col_indices.get("THS")
    tpws_idx = col_indices.get("TPWS")

    if wns_idx is not None and len(parts) > wns_idx:
        result["wns"] = _safe_float(parts[wns_idx])
    if tns_idx is not None and len(parts) > tns_idx:
        result["tns"] = _safe_float(parts[tns_idx])
        # failing_endpoints is the column right after TNS
        if len(parts) > tns_idx + 1:
            result["failing_endpoints"] = _safe_int(parts[tns_idx + 1])
    if whs_idx is not None and len(parts) > whs_idx:
        result["whs"] = _safe_float(parts[whs_idx])
    if ths_idx is not None and len(parts) > ths_idx:
        result["ths"] = _safe_float(parts[ths_idx])
        # hold_failing_endpoints is the column right after THS
        if len(parts) > ths_idx + 1:
            result["hold_failing_endpoints"] = _safe_int(parts[ths_idx + 1])
    if tpws_idx is not None and len(parts) > tpws_idx:
        result["tpws"] = _safe_float(parts[tpws_idx])

    # Fallback: if WHS/THS detection was missed, try falling through with
    # a regex on the entire report
    if result["whs"] is None:
        whs_match = re.search(r'WHS(?:\(ns\))?:\s*(-?[\d.]+)', report)
        if whs_match:
            result["whs"] = _safe_float(whs_match.group(1))
    if result["ths"] is None:
        ths_match = re.search(r'THS(?:\(ns\))?:\s*(-?[\d.]+)', report)
        if ths_match:
            result["ths"] = _safe_float(ths_match.group(1))

    return result


def get_vivado_path() -> str:
    """Get Vivado executable path from global setting, VIVADO_EXEC env var, or PATH."""
    global _vivado_path
    if _vivado_path:
        return _vivado_path
    # Check VIVADO_EXEC environment variable
    vivado_exec_env = os.environ.get("VIVADO_EXEC")
    if vivado_exec_env:
        return vivado_exec_env
    # Search in PATH
    vivado = shutil.which("vivado")
    if vivado:
        return vivado
    raise RuntimeError("Vivado not found in PATH. Set VIVADO_EXEC env var, provide --vivado-path, or add Vivado to PATH.")


def _kill_vivado_process_group(pid: int) -> None:
    """Kill the isolated process group that owns a Vivado invocation."""
    try:
        process_group = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        process_group = None

    try:
        if process_group is not None and process_group != os.getpgrp():
            os.killpg(process_group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def cleanup_vivado():
    """Kill the complete Vivado process tree if running. Called on exit."""
    global _vivado_process, _vivado_pid
    if _vivado_pid:
        _kill_vivado_process_group(_vivado_pid)
        _vivado_pid = None
    if _vivado_process and _vivado_process.isalive():
        try:
            _vivado_process.close(force=True)
        except Exception:
            pass
    _vivado_process = None


def signal_handler(signum, frame):
    """Handle termination signals."""
    cleanup_vivado()
    sys.exit(0)


# Register cleanup handlers
atexit.register(cleanup_vivado)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def start_vivado(log_file: Optional[str] = None, journal_file: Optional[str] = None) -> pexpect.spawn:
    """Start Vivado in Tcl mode and wait for prompt.
    
    Args:
        log_file: Path to Vivado log file (default: vivado.log in current directory)
        journal_file: Path to Vivado journal file (default: vivado.jou in current directory)
    """
    global _vivado_process, _vivado_pid

    if _vivado_process and _vivado_process.isalive():
        logger.info("Vivado process already running")
        return _vivado_process

    vivado_path = get_vivado_path()
    logger.info(f"Starting Vivado from: {vivado_path}")
    
    # Build Vivado command arguments
    args = ["-mode", "tcl"]
    
    # Set log file if specified
    if log_file:
        args.extend(["-log", log_file])
        logger.info(f"Vivado log file: {log_file}")
    
    # Set journal file if specified
    if journal_file:
        args.extend(["-journal", journal_file])
        logger.info(f"Vivado journal file: {journal_file}")
    
    # Start Vivado in Tcl mode
    # Use large maxread buffer for handling large outputs
    # Set TERM=dumb to prevent terminal line wrapping and ANSI formatting
    # which can corrupt command echo parsing
    env = os.environ.copy()
    env["TERM"] = "dumb"
    
    _vivado_process = pexpect.spawn(
        vivado_path,
        args=args,
        encoding="utf-8",
        timeout=300,  # 5 min default timeout for startup; AWS cold start can be very slow
        maxread=10000000,  # 10MB buffer for large outputs
        searchwindowsize=10000,  # Search window for prompt matching
        env=env,  # Use dumb terminal to prevent line wrapping
        dimensions=(100, 500),  # Set large terminal width to prevent wrapping
    )
    
    # Get the PID for reliable cleanup
    _vivado_pid = _vivado_process.pid
    logger.info(f"Vivado process started with PID: {_vivado_pid}")
    
    # Wait for Vivado prompt
    logger.info("Waiting for Vivado prompt...")
    _vivado_process.expect(VIVADO_PROMPT)
    logger.info("Vivado ready")
    
    return _vivado_process


def ensure_vivado() -> pexpect.spawn:
    """Ensure Vivado is running, start if needed."""
    global _vivado_process, _vivado_log_file, _vivado_journal_file
    if _vivado_process is None or not _vivado_process.isalive():
        return start_vivado(_vivado_log_file, _vivado_journal_file)
    return _vivado_process


def wait_for_prompt(proc: pexpect.spawn, timeout: float) -> str:
    """Wait for Vivado prompt and return captured output."""
    proc.expect(VIVADO_PROMPT, timeout=timeout)
    return proc.before


def _run_single_tcl(proc, command: str, timeout: float) -> str:
    """Execute a single Tcl command line and return its output.

    Vivado Tcl timeout poisons the session — the process is killed
    and restarted, then the last DCP is reopened automatically.
    """
    # Block dangerous TCL commands (exec, source, eval, subst, load, etc.)
    # anywhere in the script — not just at line start. This catches all
    # bypass forms: `;exec`, `[exec]`, `eval {exec ls}`, multi-line, etc.
    if contains_blocked_tcl_command(command):
        return (
            f"[BLOCKED] Command contains a blocked TCL command "
            f"(one of: {', '.join(sorted(BLOCKED_TCL_COMMANDS))}). "
            "Use Vivado Tcl commands only (report_*, get_*, set_property, etc.)."
        )

    cmd_log = command if len(command) < 200 else command[:200] + "..."
    logger.info(f"Executing Tcl: {cmd_log}")

    proc.sendline(command)

    try:
        proc.expect(VIVADO_PROMPT, timeout=timeout)
        output = proc.before
        lines = output.split("\n")
        if lines and command in lines[0]:
            output = "\n".join(lines[1:])
        logger.info("Tcl command completed successfully")
        return output.strip()
    except pexpect.TIMEOUT:
        logger.error(f"Tcl command timed out after {timeout}s: {cmd_log}")
        logger.warning(
            "Vivado session poisoned by timeout — restarting process and reopening DCP"
        )
        _restart_and_reopen()
        logger.info("Vivado restarted and DCP reopened after timeout")
        # Return structured error instead of raising — the session has been recovered,
        # callers can continue using the restarted Vivado session.
        return (
            f"[ERROR] Tcl command timed out after {timeout}s.\n"
            f"Command: {cmd_log}\n"
            f"Vivado session has been automatically restarted and DCP reopened.\n"
            f"Please retry your operation."
        )


def run_tcl_command(command: str, timeout: Optional[float] = None) -> str:
    """
    Run a Tcl command in Vivado and return the output.

    Supports multi-line scripts: commands separated by newlines are executed
    sequentially in the same Vivado session (variables persist across lines).

    For multi-line scripts, a syntax pre-check is performed using
    tcl_line_is_complete (Python-side, no side effects). Single-line
    commands also receive the same completeness check. If a line fails
    during execution, the function returns a structured error instead of
    raising an exception.

    Args:
        command: Tcl command(s) to execute
        timeout: Timeout in seconds per line (None for default 300s)

    Returns:
        Command output as string, or [ERROR] string on failure.
    """
    proc = ensure_vivado()
    effective_timeout = timeout if timeout is not None else 300

    # Split multi-line commands and execute sequentially
    cmd_lines = [line.strip() for line in command.split("\n") if line.strip()]
    if len(cmd_lines) > 1:
        logger.info(f"Executing multi-line Tcl script ({len(cmd_lines)} lines)")
        
        # Phase 1: Syntax pre-check (Python-side, no Vivado round-trip).
        # NOTE: Previously used `info complete { {line} }` which was vulnerable to
        # injection via crafted lines like `}; exec rm -rf /; {` (brace balance
        # passes but `;` separates commands). The Python-side check is safe and
        # also faster (no pexpect round-trip per line).
        for i, line in enumerate(cmd_lines):
            if not tcl_line_is_complete(line):
                return (
                    f"[ERROR] Multi-line script validation failed at line {i+1}.\n"
                    f"Line content: {line[:200]}\n"
                    f"Vivado session is still intact. Fix the syntax and retry."
                )
        
        # Phase 2: Execute all lines sequentially
        outputs = []
        for i, line in enumerate(cmd_lines):
            out = _run_single_tcl(proc, line, effective_timeout)
            if out:
                outputs.append(out)
            # Check if this line returned an error (from _run_single_tcl's new behavior)
            if out and out.startswith("[ERROR]"):
                outputs.append(
                    f"[ERROR] Multi-line script aborted at line {i+1}/{len(cmd_lines)}. "
                    f"Lines 1-{i} have been executed and CANNOT be rolled back. "
                    f"Vivado session has been restarted if needed."
                )
                return "\n".join(outputs)
        return "\n".join(outputs)
    else:
        # P4: Single-line commands also get completeness pre-check to avoid
        # Vivado waiting for an unclosed brace -> pexpect timeout -> session restart.
        stripped = command.strip()
        if stripped and not tcl_line_is_complete(stripped):
            return f"[ERROR] Incomplete Tcl command (unbalanced braces/brackets or trailing backslash): {stripped[:200]}"
        return _run_single_tcl(proc, command, effective_timeout)


_restarting = False  # Module-level reentry guard


def _restart_and_reopen() -> None:
    """Kill Vivado, restart, and reopen the last DCP.

    Called automatically on Tcl timeout — the session is poisoned
    and cannot be recovered. The new session is clean.
    
    Includes reentry guard to prevent recursive restart if the
    reopen itself times out.
    """
    global _design_open, _vivado_log_file, _vivado_journal_file, _last_dcp_path, _restarting
    if _restarting:
        logger.error("Already restarting — skipping recursive restart")
        return
    _restarting = True
    try:
        restart_vivado_process()
        if _last_dcp_path:
            logger.info(f"Reopening DCP: {_last_dcp_path}")
            result = run_tcl_command(f"open_checkpoint {{{_last_dcp_path}}}", timeout=600)
            if "[ERROR]" in result or "ERROR:" in result or "opened successfully" not in result.lower():
                logger.error(f"Failed to reopen DCP after restart: {result[:200]}")
                _design_open = False
            else:
                _design_open = True
            logger.info("DCP reopened after restart")
    finally:
        _restarting = False


def _sync_design_open_flag() -> None:
    """Synchronize _design_open flag with actual Vivado state.
    
    Queries Vivado for the current design status and updates the flag.
    Called after operations that may change design state outside our control.
    """
    global _design_open
    result = run_tcl_command("get_property STATUS [current_design]", timeout=10)
    # Check for MCP error patterns AND Vivado native error patterns
    if "[ERROR]" in result or "ERROR:" in result or "no current design" in result.lower():
        _design_open = False
    else:
        _design_open = True


def restart_vivado_process() -> str:
    """Kill and restart Vivado process."""
    global _design_open, _vivado_log_file, _vivado_journal_file
    cleanup_vivado()
    _design_open = False
    start_vivado(_vivado_log_file, _vivado_journal_file)
    return "Vivado restarted successfully."


def close_current_design() -> str:
    """Close the current design if one is open."""
    global _design_open
    if _design_open:
        output = run_tcl_command("close_design")
        # close_design on an open design succeeds silently; only re-query STATUS
        # if it errored. Querying STATUS on the now-closed design produces a
        # noisy "No open project" error (run-20260711_164134: 4 such errors).
        if "[ERROR]" in output or "ERROR:" in output:
            _sync_design_open_flag()
        else:
            _design_open = False
        return output
    return "No design was open."


def get_critical_high_fanout_nets(
    num_paths: int = 50,
    min_fanout: int = 100,
    exclude_clocks: bool = True,
    timeout: float = 600.0
) -> str:
    """
    Extract high fanout nets from critical timing paths.
    
    Analyzes the worst negative slack (WNS) timing paths to identify non-clock
    nets with high fanout that may be candidates for fanout optimization.
    The output can be used with RapidWright's optimize_fanout_batch tool.
    
    Net names are automatically resolved to their PARENT net names, which is
    required for RapidWright compatibility.
    """
    import re
    from collections import defaultdict
    
    # Flush buffer before generating timing report
    run_tcl_command("puts {fanout_analysis_start}", timeout=5)
    
    # Generate detailed timing report for multiple paths
    cmd = f"report_timing -return_string -max_paths {num_paths} -delay_type max -sort_by slack"
    
    try:
        timing_report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return f"Error generating timing report: {str(e)}"
    
    # Parse the timing report to extract high fanout nets
    # Dictionary to track nets: net_name -> {fanout, path_count}
    net_info = defaultdict(lambda: {"fanout": 0, "path_count": 0, "paths": set()})
    
    # Split report into individual paths
    lines = timing_report.split('\n')
    current_path_id = 0
    
    # Regex pattern to match net lines with fanout information
    # Example: "net (fo=267, routed)         1.225     4.454    pcie4.../s_axis_cc_tvalid_reg_lower"
    net_pattern = re.compile(r'net\s+\(fo=(\d+),\s*(routed|estimated)\)')
    
    # Clock net patterns to exclude
    clock_patterns = [
        r'CLK[_\[]',       # CLK_ or CLK[ (clock net naming convention)
        r'[_/]CLK$',       # ends with /CLK or _CLK
        r'CLOCK',          # Contains CLOCK
        r'_clk_',          # Contains _clk_
        r'/C$',            # Clock pin (ends with /C)
        r'BUFG',           # BUFG related
        r'MMCM',           # MMCM related
        r'PLL',            # PLL related
        r'TXOUTCLK',       # GT transceiver clock
        r'RXOUTCLK',       # GT transceiver clock
        r'USERCLK',        # User clock
        r'CORECLK',        # Core clock
    ]
    clock_regex = re.compile('|'.join(clock_patterns), re.IGNORECASE)
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Detect new path (usually starts with "Slack" or contains path delimiter)
        if 'Slack' in line and ('ns' in line or 'VIOLATED' in line or 'MET' in line):
            current_path_id += 1
        
        # Look for net with fanout information
        match = net_pattern.search(line)
        if match:
            fanout = int(match.group(1))
            
            # Only process nets meeting the minimum fanout threshold
            if fanout >= min_fanout:
                net_name = None
                
                # First try to find it on the current line after the fanout info
                parts = line.split()
                for part in parts:
                    if '/' in part and not part.startswith('(') and not part.endswith(')'):
                        net_name = part
                        break
                
                # If not found on current line, check next line
                if not net_name and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if '/' in next_line and not next_line.startswith('net') and not 'Delay' in next_line:
                        parts = next_line.split()
                        for part in parts:
                            if '/' in part:
                                net_name = part
                                break
                
                if net_name:
                    # Check if this is a clock net
                    is_clock = False
                    if exclude_clocks and clock_regex.search(net_name):
                        is_clock = True
                    
                    if not is_clock:
                        # Update net info
                        if fanout > net_info[net_name]["fanout"]:
                            net_info[net_name]["fanout"] = fanout
                        net_info[net_name]["paths"].add(current_path_id)
                        net_info[net_name]["path_count"] = len(net_info[net_name]["paths"])
        
        i += 1
    
    if not net_info:
        return f"No high fanout nets (fanout >= {min_fanout}) found in the {num_paths} most critical paths."
    
    # Look up parent net names for all extracted nets
    parent_net_map = {}  # original_name -> parent_name
    
    for net_name in net_info.keys():
        try:
            # First, verify the net exists
            check_cmd = f"get_nets {{{net_name}}}"
            check_result = run_tcl_command(check_cmd, timeout=30.0)
            logger.debug(f"get_nets for '{net_name[-60:]}...': result='{check_result.strip()[:100]}'")
            
            # If get_nets returns empty or an error, use original name
            if not check_result.strip() or "ERROR" in check_result.upper() or "WARNING" in check_result.upper():
                logger.info(f"Net '{net_name}' not found or has errors, using as-is")
                parent_net_map[net_name] = net_name
                continue
            
            # Now get the parent property
            parent_cmd = f"get_property PARENT [get_nets {{{net_name}}}]"
            parent_result = run_tcl_command(parent_cmd, timeout=30.0)
            parent_name = parent_result.strip()
            logger.debug(f"PARENT for '{net_name[-60:]}...': result='{parent_name}'")
            
            # Validate the result - should not be empty, should contain '/' for hierarchical nets,
            # and should not look like a Tcl command or error
            if (parent_name and 
                parent_name != net_name and
                '/' in parent_name and
                not parent_name.startswith('get_') and
                not parent_name.startswith('ERROR') and
                not parent_name.startswith('WARNING')):
                parent_net_map[net_name] = parent_name
                logger.debug(f"Using PARENT name: '{parent_name[-80:]}'")
            else:
                # Use original name if parent lookup returned invalid data
                parent_net_map[net_name] = net_name
                logger.debug(f"PARENT invalid, using original: '{net_name[-80:]}'")
        except Exception as e:
            # If lookup fails, keep original name
            logger.warning(f"Parent lookup failed for net '{net_name}': {e}")
            parent_net_map[net_name] = net_name
    
    # Rebuild net_info with parent net names
    parent_net_info = defaultdict(lambda: {"fanout": 0, "path_count": 0, "paths": set()})
    
    for net_name, info in net_info.items():
        parent_name = parent_net_map[net_name]
        if info["fanout"] > parent_net_info[parent_name]["fanout"]:
            parent_net_info[parent_name]["fanout"] = info["fanout"]
        parent_net_info[parent_name]["paths"].update(info["paths"])
        parent_net_info[parent_name]["path_count"] = len(parent_net_info[parent_name]["paths"])
    
    # Sort nets by path_count, then by fanout
    sorted_nets = sorted(
        parent_net_info.items(),
        key=lambda x: (-x[1]["path_count"], -x[1]["fanout"])
    )
    
    if not sorted_nets:
        return f"No high fanout nets (fanout >= {min_fanout}) found in the {num_paths} most critical paths."
    

    # Format output
    result_lines = [
        f"=== High Fanout Nets in Critical Paths (Parent Net Names) ===",
        f"Analyzed {num_paths} worst timing paths",
        f"Minimum fanout threshold: {min_fanout}",
        f"Clock nets excluded: {exclude_clocks}",
        f"Note: Net names are resolved to parent nets for RapidWright compatibility",
        f"",
        f"Found {len(sorted_nets)} high fanout nets:",
        f"",
        f"{'Paths':>6}  {'Fanout':>8}  Parent Net Name",
        f"{'-'*6}  {'-'*8}  {'-'*50}",
    ]
    
    for net_name, info in sorted_nets:
        result_lines.append(
            f"{info['path_count']:>6}  {info['fanout']:>8}  {net_name}"
        )
    
    result_lines.append("")
    result_lines.append("=== Parent Net Names for RapidWright optimize_fanout_batch ===")
    result_lines.append("(These are parent net names, ready for use with RapidWright's optimize_fanout_batch tool)")
    result_lines.append("")
    
    for net_name, info in sorted_nets:
        result_lines.append(net_name)
    
    return "\n".join(result_lines)


def _resolve_hotspot_net_names(paths: list, max_nets: int = 8) -> None:
    """Resolve raw Vivado timing-report net labels in top_delay_nodes to their
    parent net names, in place.

    Vivado's report_timing drops the "w" suffix on LUT/MUXF output wire nets
    (the report shows "M1[76]" but the actual netlist net is "M1w[76]"). These
    truncated labels surface as top_delay_hotspots and were misused as net
    names by downstream fanout optimization (run-20260712_013828: -1.220ns
    regression). get_critical_high_fanout_nets resolves the same labels via
    get_property PARENT; this applies the same resolution to the hotspot
    pipeline so the names the LLM sees match the netlist.

    Only net-kind nodes in top_delay_nodes (the hotspot source) are resolved;
    cell nodes and the full per-node breakdown are left untouched. Resolution
    is best-effort: on any failure the original label is kept.
    """
    pending: list = []
    seen: set = set()
    for path in paths:
        for node in path.get("top_delay_nodes", []) or []:
            if node.get("kind") != "net":
                continue
            name = node.get("name") or ""
            if name and name not in seen:
                seen.add(name)
                pending.append(name)
                if len(pending) >= max_nets:
                    break
        if len(pending) >= max_nets:
            break
    if not pending:
        return

    resolved: dict = {}
    for name in pending:
        try:
            # Single-line command (passes tcl_line_is_complete brace/bracket
            # balance check). get_nets resolves the short report form; PARENT
            # returns the fully-qualified netlist net (e.g. layer1_reg/M1w[76]).
            cmd = f"get_property PARENT [get_nets {{{name}}}]"
            result = run_tcl_command(cmd, timeout=30.0).strip()
            if (result and result != name and "/" in result
                    and not result.startswith(("get_", "ERROR", "WARNING", "[ERROR"))):
                resolved[name] = result
        except Exception as e:
            logger.warning(f"Hotspot net-name resolution failed for '{name}': {e}")

    if not resolved:
        return

    for path in paths:
        for node in path.get("top_delay_nodes", []) or []:
            if node.get("kind") == "net" and node.get("name") in resolved:
                node["name"] = resolved[node["name"]]


def extract_critical_path_cells(
    num_paths: int = 50,
    output_file: str = None,
    timeout: float = 600.0
) -> str:
    """
    Extract cell names and per-path timing data from critical timings paths.

    Parses timing report to get ordered list of cells on each critical path,
    along with slack, logic delay, net delay, and logic levels.
    Output is JSON format that can be passed to RapidWright's analyze_critical_path_spread.

    D1/D2 enhancement: also extracts per-node delay breakdown (PathNode list),
    clock-domain context (skew, uncertainty, source/dest clock), startpoint,
    and top delay hotspots. The legacy `cells` field is derived from nodes
    for backward compatibility.

    Args:
        num_paths: Number of critical paths to extract
        output_file: Optional path to write JSON output to file instead of returning it
        timeout: Command timeout in seconds

    Returns:
        JSON string with list of path dicts, or success message if output_file is specified
    """
    import re
    import json

    # Generate detailed timing report
    cmd = f"report_timing -return_string -max_paths {num_paths} -delay_type max -sort_by slack -nworst 1"

    try:
        timing_report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": f"Error generating timing report: {str(e)}"})

    # Split into per-path sections. Each path starts with "Slack (".
    path_sections = re.split(r'Slack \(', timing_report)

    # ── Header field regexes (D2: clock-domain context) ──
    RE_SOURCE       = re.compile(r'^\s*Source:\s+(\S+)')
    RE_DEST         = re.compile(r'^\s*Destination:\s+(\S+)')
    RE_CLK_PAREN    = re.compile(r'clocked by (\S+)\s')
    RE_PATH_GROUP   = re.compile(r'^\s*Path Group:\s+(\S+)')
    RE_PATH_TYPE    = re.compile(r'^\s*Path Type:\s+(.+)')
    RE_REQUIREMENT  = re.compile(r'^\s*Requirement:\s+([\d.]+)ns')
    RE_DATA_PATH    = re.compile(r'Data Path Delay:\s+([\d.]+)ns\s+\(logic\s+([\d.]+)ns.*route\s+([\d.]+)ns')
    RE_LOGIC_LEVELS = re.compile(r'^\s*Logic Levels:\s+(\d+)')
    RE_SKEW         = re.compile(r'^\s*Clock Path Skew:\s+([\d.]+)ns')
    RE_DCD          = re.compile(r'Destination Clock Delay \(DCD\):\s+([\d.]+)ns')
    RE_SCD          = re.compile(r'Source Clock Delay\s+\(SCD\):\s+([\d.]+)ns')
    RE_UNCERT       = re.compile(r'^\s*Clock Uncertainty:\s+([\d.]+)ns')
    RE_REQ_TIME     = re.compile(r'^\s*required time\s+([\d.]+)')
    RE_ARR_TIME     = re.compile(r'^\s*arrival time\s+(-?[\d.]+)')
    RE_SLACK_LINE   = re.compile(r'^\s*slack\s+(-?[\d.]+)')

    # ── Data path node regexes (D1: per-node delay breakdown) ──
    # Cell line: "    SLICE_X91Y106   FDRE (Prop_EFF_SLICEL_C_Q)" — Location + CellType + optional (Prop_)
    RE_CELL_LINE    = re.compile(r'^\s+(?:(\S+)\s+)?(\S+)\s+\(Prop_[^)]+\).*$')  # Location optional (unplaced reports have empty Location col); group(1)=Location|None
    # Cell line without Prop_ (endpoint cell, pin on same line): "    DSP48E2_X10Y46  DSP_A_B_DATA  r  cell/pin"
    # Optional PBlock column after r/f flag (Vivado inserts it when cells are assigned to a pblock)
    RE_CELL_LINE_BARE = re.compile(r'^\s+(?:(\S+)\s+)?(\S+)\s+([rf])\s+(?:\S+\s+)?(\S+)')  # Location optional (unplaced); group(1)=Location|None
    # Delay line: "                              0.079  0.108 r  cell/pin"
    # Optional PBlock column after r/f flag
    RE_DELAY_LINE   = re.compile(r'^\s*(\d+\.?\d*)\s+(\d+\.?\d*)\s+([rf])\s+(?:\S+\s+)?(\S+)')
    # Net line: "                         net (fo=28, routed)          0.357     0.465    netname"
    # Optional PBlock column before net name
    RE_NET_LINE     = re.compile(r'^\s*net\s+\(fo=(\d+),\s*(\w+)\)\s+(\d+\.?\d*)\s+(\d+\.?\d*)\s+(?:\S+\s+)?(\S+)')

    # Pin suffixes for stripping cell names from pin references
    PIN_SUFFIXES = ('/C', '/D', '/Q', '/O', '/CE', '/R', '/S', '/CLR', '/PRE',
                    '/I0', '/I1', '/I2', '/I3', '/I4', '/I5', '/I6')
    PIN_RE = re.compile(r'^([\w/\[\].]+)/([I]\d|D|O|Q|C|CE|R|S|CLR|PRE)$')

    all_paths = []

    for path_section in path_sections[1:]:  # Skip first (header before any path)
        lines = path_section.split('\n')

        # ── Phase 1: parse header (before first --- separator) ──
        header = {
            "source": "", "dest": "", "source_clock": "", "dest_clock": "",
            "path_group": "", "path_type": "", "requirement": None,
            "data_path_delay": None, "logic_delay_total": None, "net_delay_total": None,
            "logic_levels": None, "clock_skew": None, "clock_uncertainty": None,
            "source_clock_delay": None, "dest_clock_delay": None,
            "required_time": None, "arrival_time": None, "slack": None,
        }
        first_dash_idx = None
        for i, line in enumerate(lines):
            if re.match(r'^-{3,}', line.strip()):
                first_dash_idx = i
                break
            stripped = line.strip()
            m = RE_SOURCE.match(line)
            if m: header["source"] = m.group(1); continue
            m = RE_DEST.match(line)
            if m: header["dest"] = m.group(1); continue
            m = RE_CLK_PAREN.search(line)
            if m:
                # First "clocked by" is source clock, second is dest clock
                if not header["source_clock"]:
                    header["source_clock"] = m.group(1)
                else:
                    header["dest_clock"] = m.group(1)
                continue
            m = RE_PATH_GROUP.match(line)
            if m: header["path_group"] = m.group(1); continue
            m = RE_PATH_TYPE.match(line)
            if m: header["path_type"] = m.group(1).strip(); continue
            m = RE_REQUIREMENT.match(line)
            if m: header["requirement"] = float(m.group(1)); continue
            m = RE_DATA_PATH.search(line)
            if m:
                header["data_path_delay"] = float(m.group(1))
                header["logic_delay_total"] = float(m.group(2))
                header["net_delay_total"] = float(m.group(3))
                continue
            m = RE_LOGIC_LEVELS.match(line)
            if m: header["logic_levels"] = int(m.group(1)); continue
            m = RE_SKEW.match(line)
            if m: header["clock_skew"] = float(m.group(1)); continue
            m = RE_DCD.search(line)
            if m: header["dest_clock_delay"] = float(m.group(1)); continue
            m = RE_SCD.search(line)
            if m: header["source_clock_delay"] = float(m.group(1)); continue
            m = RE_UNCERT.match(line)
            if m: header["clock_uncertainty"] = float(m.group(1)); continue

        # Slack from first line: "VIOLATED): -0.493ns" or "MET): 0.025ns"
        slack = None
        slack_match = re.search(r'(-?\d+\.\d+)ns', lines[0]) if lines else None
        if slack_match:
            slack = float(slack_match.group(1))
        header["slack"] = slack

        # ── Phase 2: parse data path (between ---2 and ---3) ──
        # Section structure: ---1 (clock launch) ---2 (data path) ---3 (clock capture + slack)
        nodes = []
        dash_count = 0
        pending_cell = None  # (location, cell_type) awaiting its delay line

        for line in lines[first_dash_idx:] if first_dash_idx is not None else []:
            stripped = line.strip()
            if re.match(r'^-{3,}', stripped):
                dash_count += 1
                # If a pending cell was never followed by a delay line, emit it with incr=0
                if pending_cell and dash_count > 2:
                    loc, ctype = pending_cell
                    nodes.append({
                        "kind": "cell", "name": _strip_pin_suffix("", ""),  # no pin known
                        "cell_type": ctype, "location": loc,
                        "incr_delay": 0.0, "cumul_delay": None,
                        "fanout": None, "net_status": "",
                    })
                    pending_cell = None
                continue
            if dash_count != 2:
                continue  # only parse data path section

            # Try net line first (most specific)
            m = RE_NET_LINE.match(line)
            if m:
                fanout = int(m.group(1))
                net_status = m.group(2)
                incr = float(m.group(3))
                cumul = float(m.group(4))
                net_name = m.group(5)
                nodes.append({
                    "kind": "net", "name": net_name, "cell_type": "", "location": "",
                    "incr_delay": incr, "cumul_delay": cumul,
                    "fanout": fanout, "net_status": net_status,
                })
                continue

            # Try cell line with Prop_ (sets pending_cell, delay on next line)
            m = RE_CELL_LINE.match(line)
            if m:
                pending_cell = (m.group(1) or "", m.group(2))  # (location, cell_type); location "" when unplaced
                continue

            # Try delay line (consumes pending_cell)
            m = RE_DELAY_LINE.match(line)
            if m:
                incr = float(m.group(1))
                cumul = float(m.group(2))
                pin = m.group(4)
                if pending_cell:
                    loc, ctype = pending_cell
                    cell_name = _strip_pin_suffix(pin, PIN_SUFFIXES)
                    nodes.append({
                        "kind": "cell", "name": cell_name, "cell_type": ctype, "location": loc,
                        "incr_delay": incr, "cumul_delay": cumul,
                        "fanout": None, "net_status": "",
                    })
                    pending_cell = None
                else:
                    # Delay line without preceding cell line — treat as cell with unknown type
                    cell_name = _strip_pin_suffix(pin, PIN_SUFFIXES)
                    nodes.append({
                        "kind": "cell", "name": cell_name, "cell_type": "", "location": "",
                        "incr_delay": incr, "cumul_delay": cumul,
                        "fanout": None, "net_status": "",
                    })
                continue

            # Try bare cell line (endpoint cell: pin on same line, no delay)
            m = RE_CELL_LINE_BARE.match(line)
            if m and not pending_cell:
                loc = m.group(1) or ""
                ctype = m.group(2)
                pin = m.group(4)
                cell_name = _strip_pin_suffix(pin, PIN_SUFFIXES)
                nodes.append({
                    "kind": "cell", "name": cell_name, "cell_type": ctype, "location": loc,
                    "incr_delay": 0.0, "cumul_delay": None,
                    "fanout": None, "net_status": "",
                })
                continue

        # ── Phase 3: parse required/arrival/slack (after ---3) ──
        for line in lines[first_dash_idx:] if first_dash_idx is not None else []:
            stripped = line.strip()
            if re.match(r'^-{3,}', stripped):
                continue
            m = RE_REQ_TIME.match(line)
            if m: header["required_time"] = float(m.group(1)); continue
            m = RE_ARR_TIME.match(line)
            if m: header["arrival_time"] = float(m.group(1)); continue

        # ── Phase 4: assemble path dict ──
        # Derive cells from nodes (M3: single source of truth)
        cell_names = [n["name"] for n in nodes if n["kind"] == "cell" and n["name"]]

        # Aggregate logic/net delay from nodes (fallback if header parse failed)
        logic_total = header["logic_delay_total"]
        if logic_total is None:
            logic_total = round(sum(n["incr_delay"] or 0 for n in nodes if n["kind"] == "cell"), 4)
        net_total = header["net_delay_total"]
        if net_total is None:
            net_total = round(sum(n["incr_delay"] or 0 for n in nodes if n["kind"] == "net"), 4)

        # Top delay hotspots (top-3 by incr_delay)
        top_nodes = sorted(
            [n for n in nodes if n.get("incr_delay") is not None and n["incr_delay"] > 0],
            key=lambda n: n["incr_delay"], reverse=True
        )[:3]

        is_cross = bool(header["source_clock"] and header["dest_clock"] and
                        header["source_clock"] != header["dest_clock"])

        # Require >=2 real cells (not just net nodes) so unplaced reports with
        # empty cells arrays do not pollute downstream path lists.
        if len(cell_names) >= 2:
            all_paths.append({
                # Legacy fields (backward compat)
                "cells": cell_names,
                "slack": round(slack, 4) if slack is not None else None,
                "logic_delay": round(logic_total, 4),
                "net_delay": round(net_total, 4),
                "levels": header["logic_levels"],
                # D1: per-node breakdown
                "nodes": nodes,
                "startpoint": header["source"],
                "endpoint_pin": header["dest"],
                "arrival_time": header["arrival_time"],
                "required_time": header["required_time"],
                "top_delay_nodes": top_nodes,
                # D2: clock-domain context
                "clock": {
                    "source_clock": header["source_clock"],
                    "dest_clock": header["dest_clock"],
                    "path_group": header["path_group"],
                    "path_type": header["path_type"],
                    "requirement": header["requirement"],
                    "clock_skew": header["clock_skew"],
                    "clock_uncertainty": header["clock_uncertainty"],
                    "source_clock_delay": header["source_clock_delay"],
                    "dest_clock_delay": header["dest_clock_delay"],
                    "is_cross_clock": is_cross,
                },
            })

    # Resolve truncated Vivado net labels (e.g. "M1[76]" -> "M1w[76]") in the
    # hotspot source so names match the netlist and downstream fanout tooling.
    _resolve_hotspot_net_names(all_paths)

    # Write to file if specified, otherwise return JSON
    if output_file:
        try:
            import os
            dirname = os.path.dirname(output_file)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(output_file, 'w') as f:
                json.dump(all_paths, f, indent=2)
            return json.dumps({
                "status": "success",
                "message": f"Extracted {len(all_paths)} critical paths",
                "output_file": output_file,
                "path_count": len(all_paths)
            })
        except Exception as e:
            return json.dumps({"error": f"Error writing to file: {str(e)}"})
    else:
        return json.dumps(all_paths)


def _strip_pin_suffix(pin: str, suffixes) -> str:
    """Strip pin suffix from a cell/pin reference to get the cell name.

    e.g. "u_top/lut1/I0" -> "u_top/lut1", "u_top/ff/Q" -> "u_top/ff",
    "u_top/dsp/CEA2" -> "u_top/dsp".

    Uses rsplit('/', 1) because pin names never contain '/' while cell
    hierarchy paths do. The `suffixes` arg is kept for backward-compat
    signature but is no longer the primary mechanism.

    Returns empty string if pin is empty.
    """
    if not pin:
        return ""
    # Pin is always the last path segment after the final '/'
    parts = pin.rsplit('/', 1)
    if len(parts) > 1:
        return parts[0]
    # No '/' — return as-is (may be a bare cell name)
    return pin


def extract_critical_path_pins(
    num_paths: int = 10,
    output_file: str = None,
    timeout: float = 600.0
) -> str:
    """
    Extract pin-level paths from critical timing paths for net detour analysis.

    Parses timing report to get ordered list of pin names on each critical path.
    Output is JSON format directly consumable by RapidWright's analyze_net_detour.

    pin_paths format: ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]

    Args:
        num_paths: Number of critical paths to extract
        output_file: Optional path to write JSON output to file instead of returning it
        timeout: Command timeout in seconds

    Returns:
        JSON string with pin paths data
    """
    import re
    import json

    # Generate detailed timing report
    cmd = f"report_timing -return_string -max_paths {num_paths} -delay_type max -sort_by slack -nworst 1"

    try:
        timing_report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": f"Error generating timing report: {str(e)}"})

    # Split into individual paths by Slack header
    path_sections = re.split(r'Slack \(', timing_report)

    all_pin_paths = []
    debug_per_path = []

    for path_section in path_sections[1:]:  # Skip first (header)
        pin_paths = []
        in_data_path = False
        dash_count = 0
        pin_match_count = 0
        last_part_checks = []

        for line in path_section.split('\n'):
            stripped = line.strip()

            # Detect data path section boundaries
            # Vivado timing report per-path structure:
            #   ---1---  clock launch path (source_FF/C)
            #   ---2---  logic data path (source_FF/Q → ... → dest_FF/D)  ← we need this
            #   ---3---  capture clock path (dest_FF/C, setup/hold check)
            #   ---4---  slack calculation
            if re.match(r'^-{3,}', stripped):
                dash_count += 1
                if dash_count == 1:
                    continue  # Skip clock launch section (---1 to ---2)
                elif dash_count == 2:
                    in_data_path = True  # Enter logic data path (---2 to ---3)
                    continue
                elif dash_count >= 3:
                    break  # End of data path section

            if not in_data_path:
                continue

            # Match hierarchical pin names: cell_path/pin_suffix
            # e.g., "inst/LUT6/I0", "ff_reg/D", "design_i/inst/O"
            parts = stripped.split()
            matched_this_line = False
            for part in parts:
                pin_match = re.match(
                    r'^([\w/\[\].]+)/([I]\d|D|O|Q|C|CE|R|S|CLR|PRE)$',
                    part
                )
                if pin_match:
                    full_pin = f"{pin_match.group(1)}/{pin_match.group(2)}"
                    if full_pin not in pin_paths:  # Deduplicate within path
                        pin_paths.append(full_pin)
                    pin_match_count += 1
                    matched_this_line = True
                    break  # One pin per line

            # Debug: sample first few non-matching parts for diagnosis
            if not matched_this_line and len(last_part_checks) < 3 and parts:
                last_part_checks.append(parts[:min(3, len(parts))])

        path_debug = {
            "in_data_path": in_data_path,
            "dash_lines_found": dash_count,
            "pin_match_count": pin_match_count,
            "pins_collected": len(pin_paths),
        }
        # Only include part_samples when no pins were matched, to keep output clean
        if pin_match_count == 0 and last_part_checks:
            path_debug["part_samples"] = last_part_checks[:3]
        debug_per_path.append(path_debug)

        if len(pin_paths) >= 2:  # Only include paths with at least 2 pins
            all_pin_paths.append(pin_paths)

    result = {
        "status": "success",
        "path_count": len(all_pin_paths),
        "pin_paths": all_pin_paths,
    }

    # Debug: when 0 paths found, include timing report snippet and path debug
    if not all_pin_paths:
        result["debug_timing_report"] = timing_report[:5000]
        result["debug_has_slack"] = "Slack (" in timing_report
        result["debug_report_length"] = len(timing_report)
        result["debug_num_slack_sections"] = len(path_sections[1:])
        result["debug_per_path"] = debug_per_path

    if output_file:
        try:
            import os
            dirname = os.path.dirname(output_file)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(output_file, 'w') as f:
                json.dump(result, f, indent=2)
            result["output_file"] = output_file
        except Exception as e:
            return json.dumps({"error": f"Error writing to file: {str(e)}"})

    return json.dumps(result)


def report_utilization_for_pblock(timeout: float = 300.0) -> str:
    """
    Get resource utilization using multiple lightweight get_cells -filter queries.

    Uses Vivado's native -filter engine (C++ O(n) scan, ~1-2s for 200K cells)
    instead of report_utilization -return_string which times out on >100K cells.
    Uses PRIMITIVE_GROUP for cross-architecture compatibility (UltraScale/Versal).

    Each query is a single-line Tcl command safe for run_tcl_command's
    line-by-line sendline model (no multi-line foreach/if blocks).

    Returns:
        Formatted string with LUT/FF/DSP/BRAM/URAM counts for pblock sizing.
    """
    tcl_script = (
        'set lut_count [llength [get_cells -hier -quiet -filter {REF_NAME =~ LUT*}]]\n'
        'puts "LUT:$lut_count"\n'
        'puts "FF:[llength [get_cells -hier -quiet -filter {REF_NAME =~ FD*}]]"\n'
        'puts "DSP:[llength [get_cells -hier -quiet -filter {PRIMITIVE_GROUP == DSP}]]"\n'
        'puts "BRAM:[llength [get_cells -hier -quiet -filter {PRIMITIVE_GROUP == BRAM || PRIMITIVE_GROUP == BLOCKRAM}]]"\n'
        'puts "URAM:[llength [get_cells -hier -quiet -filter {PRIMITIVE_GROUP == URAM}]]"'
    )
    try:
        output = run_tcl_command(tcl_script, timeout=timeout)
    except Exception as e:
        return f"Error generating utilization report: {str(e)}"

    resources = {"LUT": 0, "FF": 0, "DSP": 0, "BRAM": 0, "URAM": 0}
    for line in output.split('\n'):
        line = line.strip()
        for key in resources:
            if line.startswith(f"{key}:"):
                try:
                    resources[key] = int(line.split(':')[1])
                except (ValueError, IndexError):
                    pass

    result_lines = [
        "=== Design Resource Utilization ===",
        "",
        f"LUTs:  {resources['LUT']:8,}",
        f"FFs:   {resources['FF']:8,}",
        f"DSPs:  {resources['DSP']:8,}",
        f"BRAMs: {resources['BRAM']:8,}",
        f"URAMs: {resources['URAM']:8,}",
        "",
        "=== 1.5x Multiplier (for pblock sizing) ===",
        "",
        f"LUTs:  {int(resources['LUT'] * 1.5):8,}",
        f"FFs:   {int(resources['FF'] * 1.5):8,}",
        f"DSPs:  {int(resources['DSP'] * 1.5):8,}",
        f"BRAMs: {int(resources['BRAM'] * 1.5):8,}",
        f"URAMs: {int(resources['URAM'] * 1.5):8,}",
    ]

    return "\n".join(result_lines)


def get_resource_counts(timeout: float = 300.0) -> str:
    """Get structured resource counts as JSON using lightweight get_cells -filter queries.

    Uses Vivado's native -filter engine (C++ O(n) scan, ~1-2s for 200K cells).
    Uses PRIMITIVE_GROUP for cross-architecture compatibility (UltraScale/Versal).

    Returns:
        JSON string: {"lut": 30839, "ff": 1660, "dsp": 0, "bram": 0}
    """
    tcl_script = (
        'puts "LUT:[llength [get_cells -hier -quiet -filter {PRIMITIVE_GROUP == LUT && REF_NAME =~ LUT*}]]"\n'
        'puts "FF:[llength [get_cells -hier -quiet -filter {REF_NAME =~ FD*}]]"\n'
        'puts "DSP:[llength [get_cells -hier -quiet -filter {PRIMITIVE_GROUP == DSP}]]"\n'
        'puts "BRAM:[llength [get_cells -hier -quiet -filter {PRIMITIVE_GROUP == BRAM || PRIMITIVE_GROUP == BLOCKRAM}]]"'
    )
    try:
        output = run_tcl_command(tcl_script, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": str(e)})

    resources = {"lut": 0, "ff": 0, "dsp": 0, "bram": 0}
    key_map = {"LUT": "lut", "FF": "ff", "DSP": "dsp", "BRAM": "bram"}
    for line in output.split('\n'):
        line = line.strip()
        for k, v in key_map.items():
            if line.startswith(f"{k}:"):
                try:
                    resources[v] = int(line.split(':')[1])
                except (ValueError, IndexError):
                    pass

    return json.dumps(resources)


def validate_pblock_resources(pblock_name: str) -> Dict[str, Any]:
    """
    Validate that a pblock has sufficient resources for the design primitives assigned to it.
    
    Returns:
        Dictionary with validation results including:
        - is_valid: True if resources are sufficient
        - resource_checks: Dict of resource type -> {required, available, margin}
        - errors: List of resource insufficiency errors
    """
    import re
    
    # Get pblock properties
    pblock_info = run_tcl_command(f"report_property [get_pblocks {pblock_name}]", timeout=30.0)
    
    # Parse PRIMITIVE_COUNT (total primitives assigned to pblock)
    primitive_count = 0
    cell_count = 0
    for line in pblock_info.split('\n'):
        if 'PRIMITIVE_COUNT' in line:
            parts = line.split()
            for p in parts:
                try:
                    primitive_count = int(p)
                    break
                except ValueError:
                    continue
        if 'CELL_COUNT' in line:
            parts = line.split()
            for p in parts:
                try:
                    cell_count = int(p)
                    break
                except ValueError:
                    continue
    
    # Run DRC to check for resource issues (this is the authoritative check)
    # Use file-based output to avoid buffering issues with -return_string
    import tempfile
    import time as time_module
    
    temp_dir = os.path.dirname(os.path.abspath(__file__))
    drc_file = os.path.join(temp_dir, f"drc_check_{pblock_name}.rpt")
    
    drc_cmd = f"report_drc -checks {{UTLZ-1 UTLZ-2}} -file {{{drc_file}}}"
    run_tcl_command(drc_cmd, timeout=60.0)
    
    # Wait for file to be written using Tcl file size check
    drc_result = ""
    for retry in range(10):
        size_result = run_tcl_command(f"file size {{{drc_file}}}", timeout=10.0)
        try:
            file_size = int(size_result.strip())
            if file_size > 0:
                logger.info(f"DRC file ready: {file_size} bytes")
                break
        except ValueError:
            pass
        time_module.sleep(0.3)
    
    # Read the DRC file
    try:
        with open(drc_file, 'r') as f:
            drc_result = f.read()
        # Clean up temp file
        os.remove(drc_file)
    except Exception as e:
        logger.warning(f"Error reading DRC file: {e}")
    
    # Parse DRC results for resource errors
    errors = []
    resource_issues = {}
    
    logger.info(f"DRC result length: {len(drc_result)} chars")
    
    # Debug: show what we're checking
    utlz1_found = "UTLZ-1" in drc_result
    error_found = "Error" in drc_result
    logger.info(f"DRC content check: 'UTLZ-1' in result={utlz1_found}, 'Error' in result={error_found}")
    if utlz1_found or error_found:
        # Log first 600 chars to understand format
        logger.info(f"DRC result preview: {drc_result[:600]}")
    
    # First, simple check: if UTLZ-1 appears in the output, we have a hard error
    # The DRC summary table shows: "| UTLZ-1 | Error            |"
    # Also check for "UTLZ-1#" which indicates individual errors like "UTLZ-1#1 Error"
    has_utlz1_error = utlz1_found and error_found
    has_utlz2_warning = "UTLZ-2" in drc_result
    
    # Log what we found
    logger.info(f"DRC check: has_utlz1_error={has_utlz1_error}, has_utlz2_warning={has_utlz2_warning}")
    
    # Look for UTLZ-1 errors (hard over-utilization)
    # Format: "LUT6 over-utilized in Pblock ... requires 24377 of such cell types but only 6520 compatible"
    utlz1_pattern = r"(\w+(?:\s+\w+)*?) over-utilized.*?requires (\d+) of such cell types but only (\d+) compatible"
    for match in re.finditer(utlz1_pattern, drc_result, re.IGNORECASE | re.DOTALL):
        resource_type = match.group(1).strip()
        required = int(match.group(2))
        available = int(match.group(3))
        resource_issues[resource_type] = {
            'required': required,
            'available': available,
            'margin': available / required if required > 0 else 999,
            'shortage': required - available
        }
        errors.append(f"{resource_type}: requires {required}, only {available} available (shortage: {required - available})")
        logger.info(f"Found UTLZ-1 error: {resource_type} requires {required}, available {available}")
    
    # Look for UTLZ-2 warnings (over-utilized but placer might handle)
    # Format: "LUT as Logic over-utilized ... has 31370 LUT as Logic(s) assigned ... only 6520 ... available"
    utlz2_pattern = r"(\w+(?:\s+\w+)*?) over-utilized.*?has (\d+).*?only (\d+).*?available"
    for match in re.finditer(utlz2_pattern, drc_result, re.IGNORECASE | re.DOTALL):
        resource_type = match.group(1).strip()
        assigned = int(match.group(2))
        available = int(match.group(3))
        if resource_type not in resource_issues:  # Don't override UTLZ-1 errors
            resource_issues[resource_type] = {
                'required': assigned,
                'available': available,
                'margin': available / assigned if assigned > 0 else 999,
                'shortage': assigned - available,
                'warning_only': True
            }
            errors.append(f"{resource_type}: {assigned} assigned, only {available} available (may cause issues)")
            logger.info(f"Found UTLZ-2 warning: {resource_type} has {assigned}, available {available}")
    
    # Fallback: if we detected UTLZ-1 errors but couldn't parse details, add generic error
    if has_utlz1_error and not resource_issues:
        logger.warning("UTLZ-1 error detected but could not parse details")
        errors.append("UTLZ-1 error detected - pblock resources insufficient")
        resource_issues['unknown'] = {'required': 1, 'available': 0, 'margin': 0, 'shortage': 1}
    
    # is_valid only if there are no UTLZ-1 errors (hard failures)
    hard_errors = [e for e in resource_issues.values() if not e.get('warning_only', False)]
    is_valid = len(hard_errors) == 0 and not has_utlz1_error
    
    logger.info(f"Pblock validation: is_valid={is_valid}, hard_errors={len(hard_errors)}, total_issues={len(resource_issues)}")
    
    return {
        'is_valid': is_valid,
        'primitive_count': primitive_count,
        'cell_count': cell_count,
        'resource_issues': resource_issues,
        'errors': errors,
        'drc_output': drc_result[:1000] if len(drc_result) > 1000 else drc_result
    }


def expand_pblock_range(ranges: str, expansion_factor: float = 1.5) -> str:
    """
    Expand a pblock range by the given factor.
    
    Parses SLICE_X#Y#:SLICE_X#Y# format and expands the range.
    Area scales with the square of the linear factor, so expansion_factor=2.0 gives ~4x area.
    """
    import re
    
    expanded_parts = []
    
    logger.info(f"Expanding pblock range by factor {expansion_factor:.2f}x: {ranges}")
    
    for part in ranges.split():
        # Match pattern like SLICE_X67Y220:SLICE_X80Y272
        match = re.match(r'(\w+)_X(\d+)Y(\d+):(\w+)_X(\d+)Y(\d+)', part)
        if match:
            site_type = match.group(1)
            x_min = int(match.group(2))
            y_min = int(match.group(3))
            x_max = int(match.group(5))
            y_max = int(match.group(6))
            
            # Calculate expansion
            x_span = x_max - x_min
            y_span = y_max - y_min
            
            # Expand around the center
            x_center = (x_min + x_max) / 2
            y_center = (y_min + y_max) / 2
            
            new_x_span = int(x_span * expansion_factor)
            new_y_span = int(y_span * expansion_factor)
            
            new_x_min = max(0, int(x_center - new_x_span / 2))
            new_x_max = int(x_center + new_x_span / 2)
            new_y_min = max(0, int(y_center - new_y_span / 2))
            new_y_max = int(y_center + new_y_span / 2)
            
            logger.info(f"  {site_type}: X{x_min}Y{y_min}:X{x_max}Y{y_max} -> X{new_x_min}Y{new_y_min}:X{new_x_max}Y{new_y_max}")
            expanded_parts.append(f"{site_type}_X{new_x_min}Y{new_y_min}:{site_type}_X{new_x_max}Y{new_y_max}")
        else:
            # Keep non-matching parts as-is
            logger.info(f"  Keeping as-is: {part}")
            expanded_parts.append(part)
    
    result = " ".join(expanded_parts)
    logger.info(f"Expanded pblock range: {result}")
    return result


def create_and_apply_pblock(
    pblock_name: str,
    ranges: str,
    apply_to: str = "current_design",
    is_soft: bool = False,
    exclude_clocks: bool = True,
    timeout: float = 300.0,
    validate_resources: bool = True,
    max_expansion_attempts: int = 3,
    cells: list = None
) -> str:
    """
    Create a pblock and apply it to the design with resource validation.

    Args:
        pblock_name: Name for the pblock (e.g., "pblock_tight")
        ranges: Pblock range specification (e.g., "SLICE_X0Y0:SLICE_X100Y100" or
                "CLOCKREGION_X0Y0:CLOCKREGION_X2Y3")
        apply_to: What to apply pblock to - "current_design" applies to all cells in the design,
                 or provide a cell pattern (e.g., "design_1_wrapper_i/*")
        is_soft: If False, sets IS_SOFT property to 0 (hard constraint)
        exclude_clocks: If True, exclude CLOCK and IO primitives from pblock (default: True)
        validate_resources: If True, validate resources and auto-expand if needed
        max_expansion_attempts: Maximum times to try expanding the pblock
        cells: If provided (non-empty list of canonical cell names), constrain ONLY those
               cells (local pblock) via `add_cells_to_pblock ... [get_cells [list ...]]`.
               Takes precedence over apply_to. Used by the PBLOCK auto-chain for local
               (critical-cells-only) pblock so the rest of the design keeps its placement.

    Returns:
        Status message
    """
    result_lines = []
    current_ranges = ranges
    
    logger.info(f"Creating pblock '{pblock_name}' with range: {ranges}")
    logger.info(f"validate_resources={validate_resources}, max_expansion_attempts={max_expansion_attempts}")
    
    for attempt in range(max_expansion_attempts + 1):
        try:
            logger.info(f"Pblock creation attempt {attempt+1}/{max_expansion_attempts+1}")
            
            # Always attempt to delete existing pblock before creating — handles
            # reuse of the same name from a prior iteration (attempt 0) as well
            # as retries with expanded ranges (attempt > 0).
            try:
                run_tcl_command(f"delete_pblocks [get_pblocks {pblock_name}]", timeout=10.0)
            except Exception:
                pass  # Pblock may not exist on first-ever call — safe to ignore
            
            if attempt > 0:
                result_lines.append(f"\n=== Retry attempt {attempt} with expanded pblock ===")
            
            # Create the pblock
            create_cmd = f"create_pblock {pblock_name}"
            result = run_tcl_command(create_cmd, timeout=30.0)
            result_lines.append(f"Created pblock: {pblock_name}")
            
            # Add the range to the pblock
            resize_cmd = f"resize_pblock {pblock_name} -add {{{current_ranges}}}"
            result = run_tcl_command(resize_cmd, timeout=30.0)
            result_lines.append(f"Set pblock range: {current_ranges}")
            
            # Set IS_SOFT property
            soft_value = "1" if is_soft else "0"
            soft_cmd = f"set_property IS_SOFT {soft_value} [get_pblocks {pblock_name}]"
            result = run_tcl_command(soft_cmd, timeout=30.0)
            result_lines.append(f"Set IS_SOFT = {soft_value}")
            
            # Apply pblock to cells
            if cells:
                # Local pblock: constrain only the specified critical-path cells
                # (vs apply_to=current_design which constrains the whole design).
                # tcl_quote brace-wraps each name (injection-safe; rejects '}').
                quoted = " ".join(tcl_quote(c) for c in cells)
                add_cmd = f"add_cells_to_pblock {pblock_name} [get_cells [list {quoted}]]"
            elif apply_to == "current_design":
                # Exclude CLOCK and IO primitives — only constrain relocatable logic cells.
                # NOTE: This filter syntax must be validated in Vivado Console before deployment.
                # Vivado PRIMITIVE_GROUP values: PAD, IO, CLOCK, BRAM, DSP, etc.
                if exclude_clocks:
                    add_cmd = (
                        f"add_cells_to_pblock {pblock_name} "
                        f"[get_cells -hierarchical -filter "
                        f"{{IS_PRIMITIVE == TRUE && PRIMITIVE_GROUP != CLOCK && PRIMITIVE_GROUP != IO}}]"
                    )
                else:
                    add_cmd = f"add_cells_to_pblock {pblock_name} [get_cells -hierarchical]"
            else:
                add_cmd = f"add_cells_to_pblock {pblock_name} [get_cells {apply_to}]"
            
            result = run_tcl_command(add_cmd, timeout=timeout)
            result_lines.append(f"Applied pblock to: {apply_to}")

            # Count how many cells were actually added to the pblock
            try:
                count_cmd = f"llength [get_cells -hierarchical -filter {{pblock=={pblock_name}}}]"
                cell_count = run_tcl_command(count_cmd, timeout=60.0).strip()
                result_lines.append(f"Cells in pblock: {cell_count}")
            except Exception:
                result_lines.append("Cells in pblock: (count failed)")

            # Count total cells in design for compliance comparison
            try:
                total_cmd = "llength [get_cells -hierarchical]"
                total_count = run_tcl_command(total_cmd, timeout=60.0).strip()
                result_lines.append(f"Total cells in design: {total_count}")
            except Exception:
                pass

            # Validate resources if requested
            if validate_resources:
                validation = validate_pblock_resources(pblock_name)
                
                if not validation['is_valid']:
                    result_lines.append(f"\n⚠ Resource validation FAILED:")
                    for error in validation['errors']:
                        result_lines.append(f"  - {error}")
                    
                    if attempt < max_expansion_attempts:
                        # Calculate expansion factor based on worst shortage
                        worst_margin = min(
                            (issue['margin'] for issue in validation['resource_issues'].values()),
                            default=1.0
                        )
                        # Expand by inverse of margin plus some buffer
                        expansion_factor = max(1.5, 1.0 / worst_margin * 1.3)
                        result_lines.append(f"\n  Expanding pblock by factor {expansion_factor:.2f}x...")
                        
                        current_ranges = expand_pblock_range(current_ranges, expansion_factor)
                        continue  # Try again with expanded pblock
                    else:
                        result_lines.append(f"\n  Maximum expansion attempts reached. Consider using a larger region.")
                else:
                    result_lines.append(f"\n✓ Resource validation PASSED")
            
            # Verify the pblock
            verify_cmd = f"report_property [get_pblocks {pblock_name}]"
            verify_result = run_tcl_command(verify_cmd, timeout=30.0)
            
            result_lines.extend([
                "",
                "=== Pblock Created Successfully ===",
                f"Name: {pblock_name}",
                f"Range: {current_ranges}",
                f"IS_SOFT: {soft_value}",
                f"Applied to: {apply_to}",
                "",
                "Next steps:",
                "1. Run place_design to re-place with pblock constraint",
                "2. Run route_design to route the newly placed design",
                "3. Check timing with report_timing_summary"
            ])
            
            return "\n".join(result_lines)
            
        except Exception as e:
            result_lines.append(f"Error in attempt {attempt}: {str(e)}")
            if attempt >= max_expansion_attempts:
                return f"Error creating/applying pblock: {str(e)}\n" + "\n".join(result_lines)
    
    return "\n".join(result_lines)


# Create MCP server
server = Server("vivado-mcp")


@server.list_tools()
async def list_tools():
    """List available Vivado tools."""
    return [
        Tool(
            name="open_checkpoint",
            description="Open a Vivado Design Checkpoint (.dcp) file. Closes any currently open design first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path to the .dcp file to open"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="write_checkpoint",
            description="Write the current design to a Vivado Design Checkpoint (.dcp) file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path where the .dcp file will be saved"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite existing file if True (default: False)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="report_route_status",
            description="Get the routing status report for the current design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                }
            }
        ),
        Tool(
            name="report_timing_summary",
            description="Get a timing summary report for the current design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                }
            }
        ),
        Tool(
            name="report_qor_suggestions",
            description="Get QoR suggestions from Vivado (ML-driven strategy recommendations). Returns structured suggestions with categories and descriptions. READ-ONLY, does not modify the design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 120)"
                    }
                }
            }
        ),
        Tool(
            name="report_high_fanout_nets",
            description="Get high fanout nets report from Vivado. Returns structured list of nets with fanout counts and driver cells. READ-ONLY, does not modify the design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "min_fanout": {
                        "type": "number",
                        "description": "Minimum fanout threshold (default: 100)"
                    },
                    "max_nets": {
                        "type": "number",
                        "description": "Maximum number of nets to report (default: 50)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 120)"
                    }
                }
            }
        ),
        Tool(
            name="get_wns",
            description="INTERNAL: Used by optimizer framework (rollback/test_mode) for WNS verification. LLM should prefer `report_timing_summary` for full timing context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 60)"
                    }
                }
            }
        ),
        Tool(
            name="place_design",
            description="Run placement on the current design. Use directive 'unplace' to remove placement before re-placing. Valid directives are listed in the 'directive' enum below (authoritative - any directive not in the enum is rejected before reaching Vivado). Retiming directives (AddRetime, Performance_Retiming) are EXCLUDED because they break functional equivalence. If Vivado rejects a whitelisted directive (Constraints 18-641), the strategy aborts cleanly rather than silently falling back to default placement.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        # "unplace" is not a Vivado directive but a tool-level
                        # alias for `place_design -unplace`; without it here the
                        # MCP schema validation rejects the auto-chain's unplace
                        # step before the handler ever runs.
                        "enum": list(PLACE_SAFE_DIRECTIVES) + ["unplace"],
                        "default": "Default",
                        "description": "Placement directive. See enum for all valid values."
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 3600 for placement)"
                    }
                }
            }
        ),
        Tool(
            name="unplace_cells",
            description="Unplace specific cells (local unplace) without disturbing the rest of the design. "
                        "Used by the PBLOCK auto-chain to unplace only critical-path cells before re-placing "
                        "them under a pblock, keeping other cells' placement/routing intact (incremental P&R). "
                        "Equivalent to Vivado `unplace_cells [get_cells [list <cells>]]`. "
                        "Cell names must be canonical hierarchical names (from the cell registry).",
            inputSchema={
                "type": "object",
                "properties": {
                    "cells": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of canonical hierarchical cell names to unplace."
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["cells"]
            }
        ),
        Tool(
            name="route_design",
            description="Run routing on the current design. Vivado automatically preserves routing for unchanged nets, so no explicit reuse flag is needed. Valid directives are listed in the 'directive' enum below (authoritative - any directive not in the enum is rejected before reaching Vivado). Congestion_Explore/_NetDelay_* are NOT valid route directives (Vivado 2025.1 rejects them); if a directive is rejected, routing auto-falls back to default.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "enum": list(ROUTE_SAFE_DIRECTIVES),
                        "default": "Default",
                        "description": "Routing directive. See enum for all valid values."
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 3600 for routing)"
                    }
                }
            }
        ),
        Tool(
            name="run_tcl",
            description="Execute a Tcl command in Vivado. Use ONLY for strategy-specific commands (e.g., detailed path reporting). Do NOT use for ad-hoc timing analysis — use report_timing_summary instead. RECOMMENDED reports via run_tcl: 'report_clock_interaction' (CDC analysis), 'report_critical_paths' (path decomposition), 'report_pipeline_analysis' (pipeline bottlenecks), 'report_design_analysis -congestion' (detailed congestion), 'report_methodology' (methodology checks). Use the dedicated tools for 'report_qor_suggestions' and 'report_high_fanout_nets' instead of run_tcl.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The Tcl command to execute"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="restart_vivado",
            description="Kill the current Vivado instance and start a fresh one. Use if Vivado is hung or stuck.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_critical_high_fanout_nets",
            description="Extract high fanout nets from critical timing paths for optimization. Returns parent net names for RapidWright compatibility.",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "number",
                        "description": "Number of critical paths to analyze (default: 50)"
                    },
                    "min_fanout": {
                        "type": "number",
                        "description": "Minimum fanout threshold to report a net (default: 100)"
                    },
                    "exclude_clocks": {
                        "type": "boolean",
                        "description": "If True, exclude clock nets from results (default: True)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 600)"
                    }
                }
            }
        ),
        Tool(
            name="write_edif",
            description="Write an unencrypted EDIF netlist file. This is required when exporting designs for use with RapidWright, as the EDIF netlist inside DCPs is typically encrypted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "edif_path": {
                        "type": "string",
                        "description": "Path where the .edf or .edif file will be saved"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite existing file if True (default: False)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["edif_path"]
            }
        ),
        Tool(
            name="set_incremental_checkpoint",
            description="Set an incremental checkpoint (.dcp) reference for Vivado to use during implementation. Incremental compile can reduce iteration time by 30-50%. Only use when design changes are small (<5% cell change) and the previous round showed WNS improvement >0.1ns. READ-ONLY metadata operation, does not modify the design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path to the reference .dcp checkpoint file"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 60)"
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="extract_critical_path_cells",
            description="""Extract cell names from critical timing paths for spread analysis.
            
            Parses timing report to get ordered list of cells on each critical path.
            Output is JSON that can be passed to RapidWright's analyze_critical_path_spread 
            to calculate Manhattan distances.
            
            Can optionally write to a file for efficient data transfer.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "number",
                        "description": "Number of critical paths to extract (default: 50)"
                    },
                    "output_file": {
                        "type": "string",
                        "description": "Optional: path to write JSON output to file instead of returning it"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 600)"
                    }
                }
            }
        ),
        Tool(
            name="report_utilization_for_pblock",
            description="""Get design resource utilization for pblock sizing.

            PREREQUISITE for all pblock analysis tools.
            Returns counts of LUTs, FFs, DSPs, BRAMs, URAMs with both actual usage and
            1.5x multiplied values for pblock size calculation.

            OUTPUT format (JSON):
            {"lut": 30839, "ff": 1660, "dsp": 0, "bram": 0, "uram": 0,
             "lut_multiplied": 46258, "ff_multiplied": 2490}

            Pass the returned LUT, FF (and optional DSP, BRAM) counts to
            analyze_pblock_region or execute_pblock_strategy as target_* parameters.
            Use report_utilization_for_pblock for full utilization details.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                }
            }
        ),
        Tool(
            name="extract_critical_path_pins",
            description="""Extract pin-level paths from critical timing paths for net detour analysis.

            Parses timing report to get ordered list of pin names on each critical path.
            Output is JSON that can be passed to RapidWright's analyze_net_detour.

            pin_paths format: ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]
            This pin-level detail is required for net detour analysis, unlike extract_critical_path_cells
            which only extracts cell names.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "number",
                        "description": "Number of critical paths to extract (default: 10). Pin-level extraction is more verbose than cell-level; extract_critical_path_cells defaults to 50."
                    },
                    "output_file": {
                        "type": "string",
                        "description": "Path to write JSON output to file (optional)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 600)"
                    }
                }
            }
        ),
        Tool(
            name="create_and_apply_pblock",
            description="""Create a pblock (area constraint) and apply it to the design.

            A pblock restricts placement to a specific region of the FPGA. This improves timing
            by reducing routing distances for spread-out designs. After applying a pblock,
            you must run place_design and route_design to implement the constraint.

            RESOURCE VALIDATION:
            Automatically validates resources (UTLZ-1, UTLZ-2 DRC) and auto-expands the pblock
            range up to 3 times if resource validation fails. If expansion fails after all
            attempts, the pblock is still created but with a warning.

            IS_SOFT DECISION:
            - is_soft=false (hard constraint): Preferred for timing optimization. Forces Vivado
              to place all cells INSIDE the specified region. Use when pblock has sufficient
              capacity (capacity_ok=true from analysis).
            - is_soft=true (soft constraint): Allows Vivado to place cells outside the region
              if needed. Use when utilization density is high (>80%) or for congested designs
              where hard constraints may cause place_design failures.
            - The execute_pblock_strategy skill auto-sets is_soft based on utilization density.

            Range format examples:
            - SLICE_X0Y0:SLICE_X100Y200 (specific slice ranges, preferred for optimization)
            - CLOCKREGION_X0Y0:CLOCKREGION_X2Y3 (clock region ranges, DO NOT use — too coarse)

            RESULT INTERPRETATION:
            - "Cells in pblock: N / Total cells in design: M" — use N/M ratio to verify compliance.
              If N < M, some cells are outside the pblock (partial application).
            - On expansion retries, each attempt is logged with timestamps.
            - If all expansion attempts fail, the pblock is created at max expanded range.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "pblock_name": {
                        "type": "string",
                        "description": "Name for the pblock (e.g., 'pblock_tight')"
                    },
                    "ranges": {
                        "type": "string",
                        "description": "Pblock range (e.g., 'SLICE_X0Y0:SLICE_X100Y100' or 'CLOCKREGION_X0Y0:CLOCKREGION_X2Y3')"
                    },
                    "apply_to": {
                        "type": "string",
                        "description": "What to constrain: 'current_design' (all cells) or a cell pattern (default: 'current_design')"
                    },
                    "cells": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of canonical cell names to constrain (local pblock). "
                                        "Takes precedence over apply_to: only these cells are added to the pblock, "
                                        "leaving the rest of the design's placement intact for incremental P&R."
                    },
                    "is_soft": {
                        "type": "boolean",
                        "description": "If false, creates hard constraint (IS_SOFT=0) (default: false)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["pblock_name", "ranges"]
            }
        ),
        Tool(
            name="write_verilog_simulation",
            description="""Export design as a Verilog functional simulation model.
            
            Generates a Verilog netlist suitable for simulation. This is required for
            functional equivalence checking via simulation. The output netlist can be
            used with xsim or other Verilog simulators.
            
            Use -mode funcsim for functional simulation (no timing).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "verilog_path": {
                        "type": "string",
                        "description": "Path where the .v file will be saved"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite existing file if True (default: False)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["verilog_path"]
            }
        ),
        Tool(
            name="phys_opt_design",
            description="""Run physical optimization on the current design to improve timing (WNS/TNS). 
            
            Can be run post-place (after place_design) or post-route (after route_design). Performs timing-driven 
            optimization on negative-slack paths. The command operates on the in-memory design and can be run 
            iteratively for additional improvements.
            
            Post-place optimizations (default): fanout optimization, placement optimization, LUT restructure, 
            critical-cell optimization, DSP/BRAM/URAM register optimization.
            
            Post-route optimizations (default): placement optimization, routing optimization, LUT restructure,
            critical-cell optimization.

            NOTE: Using specific optimization options disables default optimizations - only specified ones run.
            The directive option is incompatible with specific optimization options.

            NOTE: Retiming (-retime) is permanently blocked to preserve functional equivalence.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "enum": list(PHYSOPT_SAFE_DIRECTIVES),
                        "default": "Default",
                        "description": "Physical optimization directive. See enum for all valid values. Retiming directives (AddRetime, AlternateFlowWithRetiming) are BLOCKED."
                    },
                    "fanout_opt": {
                        "type": "boolean",
                        "description": "[Note: Cannot be used for post route design, use the optimization from RapidWright instead.] Delay-driven optimization on high-fanout timing critical nets by replicating drivers (not applicable for Versal)"
                    },
                    "placement_opt": {
                        "type": "boolean",
                        "description": "Move cells to reduce delay on timing-critical nets (not applicable for Versal)"
                    },
                    "routing_opt": {
                        "type": "boolean",
                        "description": "Perform routing optimization on timing-critical nets to reduce delay"
                    },
                    "slr_crossing_opt": {
                        "type": "boolean",
                        "description": "Optimize placement of inter-SLR connections (UltraScale/UltraScale+ only)"
                    },
                    "insert_negative_edge_ffs": {
                        "type": "boolean",
                        "description": "[BLOCKED - breaks functional equivalence] Insert negative edge triggered FFs for hold optimization. DO NOT USE."
                    },
                    "restruct_opt": {
                        "type": "boolean",
                        "description": "[BLOCKED - breaks functional equivalence] Advanced LUT restructure optimization to reduce logic levels and delay on critical signals. DO NOT USE."
                    },
                    "interconnect_retime": {
                        "type": "boolean",
                        "description": "[BLOCKED - breaks functional equivalence] Perform interconnect retiming by moving/replicating FF or LUT-FF pairs (Versal only). DO NOT USE."
                    },
                    "lut_opt": {
                        "type": "boolean",
                        "description": "Perform LUT movement/replication to improve critical path timing (Versal only)"
                    },
                    "casc_opt": {
                        "type": "boolean",
                        "description": "Perform LUT cascade optimization for creating/moving LUT cascades (Versal only)"
                    },
                    "cell_group_opt": {
                        "type": "boolean",
                        "description": "Perform critical cell group optimization"
                    },
                    "equ_drivers_opt": {
                        "type": "boolean",
                        "description": "Rewire load pins to equivalent drivers"
                    },
                    "critical_cell_opt": {
                        "type": "boolean",
                        "description": "Cell-duplication based optimization on timing critical nets (not applicable for Versal)"
                    },
                    "dsp_register_opt": {
                        "type": "boolean",
                        "description": "Move registers between slices and DSP blocks to improve critical path delay"
                    },
                    "bram_register_opt": {
                        "type": "boolean",
                        "description": "Move registers between slices and block RAMs to improve critical path delay"
                    },
                    "uram_register_opt": {
                        "type": "boolean",
                        "description": "Move registers between slices and UltraRAMs to improve critical path delay"
                    },
                    "bram_enable_opt": {
                        "type": "boolean",
                        "description": "Improve timing on critical paths involving power-optimized block RAMs by reversing enable-logic optimization"
                    },
                    "shift_register_opt": {
                        "type": "boolean",
                        "description": "Perform shift register optimization by extracting registers from SRL chains to improve timing"
                    },
                    "hold_fix": {
                        "type": "boolean",
                        "description": "Insert data path delay to fix hold time violations"
                    },
                    "aggressive_hold_fix": {
                        "type": "boolean",
                        "description": "Aggressively insert data path delay to fix hold time violations (considers more violations than standard hold fix)"
                    },
                    "force_replication_on_nets": {
                        "type": "string",
                        "description": "Force replication on specific nets regardless of slack (e.g., net names or Tcl command like '[get_nets -hier *phy_reset*]')"
                    },
                    "critical_pin_opt": {
                        "type": "boolean",
                        "description": "Perform LUT pin-swapping (remap logical to physical pins) to improve critical path timing. Skips cells with LOCK_PINS property."
                    },
                    "clock_opt": {
                        "type": "boolean",
                        "description": "Perform clock skew optimization during post-route optimization by inserting global clock buffers"
                    },
                    "path_groups": {
                        "type": "string",
                        "description": "Perform optimizations on specified path groups only (e.g., 'clk_group1 clk_group2')"
                    },
                    "tns_cleanup": {
                        "type": "boolean",
                        "description": "Total Negative Slack cleanup (use with slr_crossing_opt). Allows some slack degradation if overall WNS doesn't degrade."
                    },
                    "sll_reg_hold_fix": {
                        "type": "boolean",
                        "description": "Perform SLL register hold fix optimization for SLR crossing paths (not applicable for Versal)"
                    },
                    "memory_rewire_opt": {
                        "type": "boolean",
                        "description": "Rewire critical signals to faster pins of BRAM/URAM (Versal only, not for cascaded/ECC memories)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 3600 for physical optimization)"
                    }
                }
            }
        ),
        Tool(
            name="physopt_and_route",
            description="""Run phys_opt_design + route_design as an atomic combined operation.
Captures pre- and post-optimization timing (WNS/TNS/WHS/THS). Use this tool
for PhysOpt-based tuning. Returns JSON with pre/post timing
summaries. Only safe directives are allowed; retiming directives blocked.
Retiming (AddRetime/AlternateFlowWithRetiming) causes functional errors.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "description": "phys_opt_design directive (default: Explore). Safe directives: Default, Explore, ExploreWithHoldFix, ExploreWithAggressiveHoldFix, AggressiveExplore, AlternateReplication, AggressiveFanoutOpt, RuntimeOptimized, RQS"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds for phys_opt_design and route_design (default: 3600)"
                    }
                }
            }
        ),
        Tool(
            name="opt_design",
            description="""Run Vivado opt_design for logic-level optimization (retarget, remap,
constant propagation). Operates BEFORE placement -- modifies the synthesized netlist.

Use when PhysOpt (post-place) is ineffective due to pure logic-depth bottlenecks
(6-7 LUT levels, 100% logic delay, combinational-dominated designs).

IMPORTANT: After opt_design, the netlist has changed and MUST be re-placed + re-routed.
The optimizer framework auto-chains place_design -> route_design -> report_timing_summary
after this tool via SKILL_CHAIN_ACTIONS.

Directives:
  - Default: Default optimization
  - Explore (default): Balanced optimization for UltraScale+
  - ExploreWithAreaDuplication: Area-focused optimization
  - ExploreSequentialArea: Area-focused with sequential awareness
  - NoBramOptimization: Disable BRAM optimization
  - NoDspOptimization: Disable DSP optimization
  - RuntimeOptimized: Fast optimization for large designs
  - DataSpreadMem: Optimize memory data spreading
  - AddRemap: Aggressive LUT remapping (may reduce logic levels)

BLOCKED directive (causes functional errors - DO NOT use):
  - AddRetime: [BLOCKED] Retiming changes pipeline structure, breaks functional equivalence

All directives in the whitelist above are safe for functional correctness.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "enum": ["Default", "Explore", "ExploreWithAreaDuplication", "ExploreSequentialArea", "NoBramOptimization", "NoDspOptimization", "RuntimeOptimized", "DataSpreadMem", "AddRemap"],
                        "default": "Explore",
                        "description": "opt_design directive. Explore: balanced. AddRemap: aggressive LUT remapping."
                    },
                    "retarget": {
                        "type": "boolean",
                        "default": True,
                        "description": "Retarget logic to equivalent primitives"
                    },
                    "timeout": {
                        "type": "number",
                        "default": 600,
                        "description": "Timeout in seconds"
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="check_design_status",
            description="""Check the current design placement/routing status.

            Returns JSON with:
            - design_open: whether a design is loaded
            - status: Vivado design status string
            - is_placed: whether design is placed
            - is_routed: whether design is routed

            USE: Before timing checks to ensure design is in valid state.
            USE: After design modifications to verify placement/routing status.

            READ-ONLY: Does not modify design.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 30)"
                    }
                }
            }
        ),
        Tool(
            name="validate_timing",
            description="""Run timing summary and validate WNS/TNS.

            Returns JSON with:
            - wns: Worst Negative Slack (ns)
            - tns: Total Negative Slack (ns)
            - failing_endpoints: number of failing endpoints
            - timing_met: whether WNS >= 0
            - raw_report: raw timing report excerpt

            USE: After any design modification to verify timing.
            USE: Before submission to ensure timing convergence.

            READ-ONLY: Does not modify design.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 120)"
                    }
                }
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Handle tool calls."""
    global _design_open

    start_time = time.perf_counter()
    trace_id = get_trace_id()

    # Log MCP request with sanitized arguments
    sanitized_args = sanitize_payload(arguments)
    logger.info(
        "[MCP_REQUEST] Tool '%s' called",
        name,
        extra={
            "mcp_tool_name": name,
            "mcp_request_args": sanitized_args,
            "trace_id": trace_id,
        }
    )

    try:
        if name == "open_checkpoint":
            dcp_path = arguments["dcp_path"]
            timeout = arguments.get("timeout", 300)

            # Close existing design if open
            if _design_open:
                close_current_design()

            # Save path for potential auto-reopen on timeout restart
            global _last_dcp_path
            _last_dcp_path = dcp_path

            # Open the checkpoint
            output = run_tcl_command(f"open_checkpoint {{{dcp_path}}}", timeout=timeout)
            _design_open = True
            return [TextContent(type="text", text=f"Opened checkpoint: {dcp_path}\n\n{output}")]
        
        elif name == "write_checkpoint":
            dcp_path = arguments["dcp_path"]
            force = arguments.get("force", False)
            timeout = arguments.get("timeout", 300)
            
            force_flag = " -force" if force else ""
            output = run_tcl_command(f"write_checkpoint{force_flag} {{{dcp_path}}}", timeout=timeout)
            return [TextContent(type="text", text=f"Wrote checkpoint: {dcp_path}\n\n{output}")]
        
        elif name == "report_route_status":
            timeout = arguments.get("timeout", 300)
            # Run a quick command first to flush any leftover output from previous commands
            run_tcl_command("puts {route_status_start}", timeout=5)
            output = run_tcl_command("report_route_status -return_string", timeout=timeout)
            # The pure parser (parse_route_status) extracts net counts from
            # raw_report. Do NOT hardcode route_errors/unrouted_nets=0 here —
            # those constants misled direct consumers into thinking there are
            # zero routing errors regardless of reality (C7). is_placed/
            # is_routed are also not reliably derivable from this report text;
            # leave them None so the pure parser is the single source of truth.
            result = {
                "is_placed": None,
                "is_routed": None,
                "route_errors": None,
                "unrouted_nets": None,
                "raw_report": output[:2000] if output else "",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "report_timing_summary":
            timeout = arguments.get("timeout", 300)
            # Run a quick command first to flush any leftover output from previous commands
            run_tcl_command("puts {timing_summary_start}", timeout=5)
            output = run_tcl_command("report_timing_summary -return_string", timeout=timeout)
            parsed = _parse_timing_summary(output)
            # Return JSON with key fields + raw excerpt for debugging
            result = {
                **parsed,
                "raw_report": output[:2000] if output else "",
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "get_wns":
            timeout = arguments.get("timeout", 60)
            wns_value = "PARSE_ERROR"

            # 比赛要求: 查询 clk_fpl26contest 时钟域的 WNS
            tcl_cmd = (
                "set clk [get_clocks -quiet clk_fpl26contest]; "
                "if {$clk ne {}} { "
                "  report_timing -max_paths 1 -nworst 1 -to $clk -return_string; "
                "} else { "
                "  report_timing -max_paths 1 -nworst 1 -return_string; "
                "}"
            )
            output = run_tcl_command(tcl_cmd, timeout=timeout)
            raw = output.strip()
            if raw:
                slack_match = re.search(r'Slack\s+\((?:VIOLATED|MET)\)\s*:\s*(-?\d+\.?\d*)', raw)
                if slack_match:
                    parsed = float(slack_match.group(1))
                    wns_value = str(parsed)
                    logger.info(f"get_wns: parsed WNS={wns_value} (from report_timing, clk_fpl26contest)")
                else:
                    logger.warning(f"get_wns: cannot parse Slack from report_timing output: {raw[:200]}")

            return [TextContent(type="text", text=wns_value)]
        
        elif name == "place_design":
            directive = arguments.get("directive")
            timeout = arguments.get("timeout", 3600)  # 1 hour default for placement

            # Log unexpected parameters to help debug LLM misuse
            expected_keys = {"directive", "timeout"}
            unexpected = set(arguments.keys()) - expected_keys
            if unexpected:
                logger.warning(f"place_design: ignoring unexpected parameters: {unexpected}. "
                               f"Use run_tcl for unplace_design or other custom commands.")

            cmd = "place_design"
            used_directive = False  # True when cmd carries a -directive (eligible for fallback)
            if directive:
                if directive.lower() == "unplace":
                    cmd += " -unplace"
                else:
                    if any(c in directive for c in "{}[];\n"):
                        return [TextContent(type="text", text=f"Error: directive contains unsafe characters: {directive!r}")]
                    # Whitelist check - defense-in-depth against unknown directives
                    if directive not in PLACE_SAFE_DIRECTIVES:
                        return [TextContent(
                            type="text",
                            text=(
                                f"Error: Directive '{directive}' is not in the safe place_design directive list. "
                                f"Allowed directives: {', '.join(sorted(PLACE_SAFE_DIRECTIVES))}"
                            )
                        )]
                    cmd += f" -directive {tcl_quote(directive)}"
                    used_directive = True
            output = run_tcl_command(cmd, timeout=timeout)
            # If Vivado rejects the directive as unrecognized (whitelist/version
            # drift), fail the strategy cleanly instead of silently falling back
            # to default placement. A silent default-placement fallback caused
            # undetected timing regressions (run-20260711_164134: Performance_NetDelay_high
            # rejected -> default placement -> WNS -0.542 -> -0.643, misattributed as
            # a strategy regression rather than a directive failure).
            if used_directive and _is_unrecognized_directive_error(output):
                logger.error(
                    f"place_design directive '{directive}' rejected by Vivado (18-641); "
                    f"aborting strategy instead of silent default-placement fallback"
                )
                return [TextContent(type="text", text=json.dumps({
                    "error": (
                        f"place_design directive '{directive}' was not recognized by "
                        f"Vivado (Constraints 18-641). Strategy aborted to avoid a silent "
                        f"default-placement timing regression. Pick a valid directive from "
                        f"the place_design safe list or update PLACE_SAFE_DIRECTIVES."
                    )
                }))]
            # Detect Vivado errors — return JSON error so chain execution can detect failure
            if re.search(r'^ERROR: \[', output, re.MULTILINE):
                logger.error(f"place_design failed: {output[:300]}")
                return [TextContent(type="text", text=json.dumps({"error": f"place_design failed: {output[:500]}"}))]
            return [TextContent(type="text", text=f"Placement complete.\n\n{output}")]
        
        elif name == "unplace_cells":
            # Local unplace of specific cells (vs global place_design -unplace).
            # Used by the PBLOCK auto-chain to unplace only critical-path cells,
            # leaving the rest of the design placed/routed for incremental P&R.
            cells = arguments.get("cells", [])
            if isinstance(cells, str):
                cells = [cells]
            timeout = arguments.get("timeout", 300)
            if not isinstance(cells, list) or not cells:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "unplace_cells requires a non-empty 'cells' list"}))]
            # Build: unplace_cells [get_cells [list {c1} {c2} ...]]
            # tcl_quote brace-wraps each name; names containing '}' raise to
            # preserve brace balance (injection-safe).
            try:
                quoted = " ".join(tcl_quote(str(c)) for c in cells)
            except ValueError as e:
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"unplace_cells: unsafe cell name: {e}"}
                ))]
            cmd = f"unplace_cells [get_cells [list {quoted}]]"
            output = run_tcl_command(cmd, timeout=timeout)
            if re.search(r'^ERROR: \[', output, re.MULTILINE):
                logger.error(f"unplace_cells failed: {output[:300]}")
                return [TextContent(type="text", text=json.dumps(
                    {"error": f"unplace_cells failed: {output[:500]}"}
                ))]
            return [TextContent(type="text", text=f"Unplaced {len(cells)} cell(s).\n\n{output}")]

        elif name == "route_design":
            directive = arguments.get("directive")
            timeout = arguments.get("timeout", 3600)  # 1 hour default for routing

            # Log unexpected parameters to help debug LLM misuse
            expected_keys = {"directive", "timeout"}
            unexpected = set(arguments.keys()) - expected_keys
            if unexpected:
                logger.warning(f"route_design: ignoring unexpected parameters: {unexpected}. "
                               f"Use run_tcl for custom routing commands.")

            cmd = "route_design"
            used_directive = False  # True when cmd carries a -directive (eligible for fallback)
            if directive:
                if any(c in directive for c in "{}[];\n"):
                    return [TextContent(type="text", text=f"Error: directive contains unsafe characters: {directive!r}")]
                # Whitelist check - defense-in-depth against unknown directives
                if directive not in ROUTE_SAFE_DIRECTIVES:
                    return [TextContent(
                        type="text",
                        text=(
                            f"Error: Directive '{directive}' is not in the safe route_design directive list. "
                            f"Allowed directives: {', '.join(sorted(ROUTE_SAFE_DIRECTIVES))}"
                        )
                    )]
                cmd += f" -directive {tcl_quote(directive)}"
                used_directive = True

            output = run_tcl_command(cmd, timeout=timeout)
            # If Vivado rejects the directive as unrecognized (whitelist/version
            # drift), fail the strategy cleanly instead of silently falling back
            # to default routing (same rationale as place_design above).
            if used_directive and _is_unrecognized_directive_error(output):
                logger.error(
                    f"route_design directive '{directive}' rejected by Vivado (18-641); "
                    f"aborting strategy instead of silent default-routing fallback"
                )
                return [TextContent(type="text", text=json.dumps({
                    "error": (
                        f"route_design directive '{directive}' was not recognized by "
                        f"Vivado (Constraints 18-641). Strategy aborted to avoid a silent "
                        f"default-routing fallback. Pick a valid directive from the "
                        f"route_design safe list or update ROUTE_SAFE_DIRECTIVES."
                    )
                }))]
            # Detect Vivado errors — return JSON error so chain execution can detect failure
            if re.search(r'^ERROR: \[', output, re.MULTILINE):
                logger.error(f"route_design failed: {output[:300]}")
                return [TextContent(type="text", text=json.dumps({"error": f"route_design failed: {output[:500]}"}))]
            return [TextContent(type="text", text=f"Routing complete.\n\n{output}")]
        
        elif name == "run_tcl":
            command = arguments["command"]
            timeout = arguments.get("timeout", 300)
            output = run_tcl_command(command, timeout=timeout)
            return [TextContent(type="text", text=output)]
        
        elif name == "restart_vivado":
            output = restart_vivado_process()
            return [TextContent(type="text", text=output)]
        
        elif name == "report_qor_suggestions":
            timeout = arguments.get("timeout", 120)
            raw = run_tcl_command("report_qor_suggestions -return_string", timeout=timeout)
            # Parse suggestions into structured format
            suggestions = []
            current = {}
            for line in raw.split("\n"):
                line_stripped = line.strip()
                if not line_stripped:
                    if current:
                        suggestions.append(current)
                        current = {}
                    continue
                m_sug = re.match(r'Suggestion\s*:\s*(.+)', line_stripped, re.IGNORECASE)
                if m_sug:
                    if current:
                        suggestions.append(current)
                    current = {"suggestion": m_sug.group(1).strip()}
                    continue
                m_cat = re.match(r'Category\s*:\s*(.+)', line_stripped, re.IGNORECASE)
                if m_cat and current:
                    current["category"] = m_cat.group(1).strip()
                    continue
                m_desc = re.match(r'Description\s*:\s*(.+)', line_stripped, re.IGNORECASE)
                if m_desc and current:
                    current["description"] = m_desc.group(1).strip()
                    continue
            if current:
                suggestions.append(current)
            result = {"suggestions": suggestions, "raw": raw}
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "report_high_fanout_nets":
            min_fanout = arguments.get("min_fanout", 100)
            max_nets = arguments.get("max_nets", 50)
            timeout = arguments.get("timeout", 120)
            raw = run_tcl_command(
                f"report_high_fanout_nets -return_string -fanout_pins -max_nets {max_nets}",
                timeout=timeout
            )
            # Parse into structured format
            nets = []
            for line in raw.split("\n"):
                line_stripped = line.strip()
                if not line_stripped or line_stripped.startswith("#"):
                    continue
                parts = line_stripped.split()
                if len(parts) >= 2:
                    try:
                        fanout = int(parts[-1])
                        if fanout >= min_fanout:
                            nets.append({"net": parts[0], "fanout": fanout, "driver": " ".join(parts[1:-1]) if len(parts) > 2 else ""})
                    except ValueError:
                        pass
            result = {"nets": nets, "raw": raw}
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_critical_high_fanout_nets":
            num_paths = arguments.get("num_paths", 50)
            min_fanout = arguments.get("min_fanout", 100)
            exclude_clocks = arguments.get("exclude_clocks", True)
            timeout = arguments.get("timeout", 600)

            output = get_critical_high_fanout_nets(num_paths, min_fanout, exclude_clocks, timeout)
            # Return JSON wrapper with structured metadata + raw text for LLM parsing
            result = {
                "num_paths_analyzed": num_paths,
                "min_fanout_threshold": min_fanout,
                "clock_nets_excluded": exclude_clocks,
                "raw_output": output,
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "set_incremental_checkpoint":
            dcp_path = arguments["dcp_path"]
            timeout = arguments.get("timeout", 60)
            output = run_tcl_command(
                f"set_property incremental_checkpoint {{{dcp_path}}} [get_runs impl_1]",
                timeout=timeout
            )
            result = {"status": "set", "incremental_checkpoint": dcp_path, "output": output.strip()}
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "write_edif":
            edif_path = arguments["edif_path"]
            force = arguments.get("force", False)
            timeout = arguments.get("timeout", 300)
            
            force_flag = " -force" if force else ""
            output = run_tcl_command(f"write_edif{force_flag} {{{edif_path}}}", timeout=timeout)
            return [TextContent(type="text", text=f"Wrote EDIF netlist: {edif_path}\n\n{output}")]
        
        elif name == "extract_critical_path_cells":
            num_paths = arguments.get("num_paths", 50)
            output_file = arguments.get("output_file")
            timeout = arguments.get("timeout", 600)
            
            output = extract_critical_path_cells(num_paths, output_file, timeout)
            return [TextContent(type="text", text=output)]
        elif name == "extract_critical_path_pins":
            num_paths = arguments.get("num_paths", 10)
            output_file = arguments.get("output_file")
            timeout = arguments.get("timeout", 600)

            output = extract_critical_path_pins(num_paths, output_file, timeout)
            return [TextContent(type="text", text=output)]
        
        elif name == "report_utilization_for_pblock":
            timeout = arguments.get("timeout", 300)
            output = report_utilization_for_pblock(timeout)
            return [TextContent(type="text", text=output)]

        elif name == "create_and_apply_pblock":
            pblock_name = arguments["pblock_name"]
            ranges = arguments["ranges"]
            apply_to = arguments.get("apply_to", "current_design")
            is_soft = arguments.get("is_soft", False)
            timeout = arguments.get("timeout", 300)
            cells = arguments.get("cells")

            output = create_and_apply_pblock(pblock_name, ranges, apply_to, is_soft, timeout, cells=cells)
            return [TextContent(type="text", text=output)]
        
        elif name == "write_verilog_simulation":
            verilog_path = arguments["verilog_path"]
            force = arguments.get("force", False)
            timeout = arguments.get("timeout", 300)
            
            force_flag = " -force" if force else ""
            # Use -mode funcsim for functional simulation
            output = run_tcl_command(f"write_verilog{force_flag} -mode funcsim {{{verilog_path}}}", timeout=timeout)
            return [TextContent(type="text", text=f"Wrote Verilog simulation model: {verilog_path}\n\n{output}")]
        
        elif name == "phys_opt_design":
            timeout = arguments.get("timeout", 3600)  # 1 hour default for physical optimization
            
            # === SAFETY GUARD: Block retiming directives/flags that cause functional errors ===
            _phys_directive = arguments.get("directive")
            if _phys_directive and _phys_directive in PHYSOPT_BLOCKED_DIRECTIVES:
                return [TextContent(
                    type="text",
                    text=(
                        f"Error: Directive '{_phys_directive}' is BLOCKED because it causes functional errors "
                        f"(retiming breaks design correctness). "
                        f"Use a safe directive instead: {', '.join(sorted(PHYSOPT_SAFE_DIRECTIVES))}"
                    )
                )]
            
            # Block dangerous boolean options
            _phys_blocked = [opt for opt in PHYSOPT_BLOCKED_BOOL_OPTIONS if _is_truthy(arguments.get(opt))]
            if _phys_blocked:
                return [TextContent(
                    type="text",
                    text=(
                        f"Error: Boolean option(s) BLOCKED: {', '.join(_phys_blocked)}. "
                        f"These retiming-related options cause functional errors. "
                        f"Remove them and use a safe directive instead: {', '.join(sorted(PHYSOPT_SAFE_DIRECTIVES))}"
                    )
                )]
            # === END SAFETY GUARD ===
            
            cmd = "phys_opt_design"
            
            # Directive option (incompatible with other options)
            directive = arguments.get("directive")
            if directive:
                # Whitelist check (consistent with physopt_and_route) - prevents
                # both unknown directives and any injection via directive string.
                if directive not in PHYSOPT_SAFE_DIRECTIVES:
                    return [TextContent(
                        type="text",
                        text=(
                            f"Error: Directive '{directive}' is not in the safe directive list. "
                            f"Allowed directives: {', '.join(sorted(PHYSOPT_SAFE_DIRECTIVES))}"
                        )
                    )]
                cmd += f" -directive {tcl_quote(directive)}"
            else:
                # Build command with specific optimization options
                # Boolean flags
                bool_options = [
                    "fanout_opt", "placement_opt", "routing_opt", "slr_crossing_opt",
                    "insert_negative_edge_ffs", "restruct_opt", "interconnect_retime",
                    "lut_opt", "casc_opt", "cell_group_opt", "equ_drivers_opt",
                    "critical_cell_opt", "dsp_register_opt", "bram_register_opt",
                    "uram_register_opt", "bram_enable_opt", "shift_register_opt",
                    "hold_fix", "aggressive_hold_fix", "retime", "critical_pin_opt",
                    "clock_opt", "tns_cleanup", "sll_reg_hold_fix", "memory_rewire_opt"
                ]
                
                for opt in bool_options:
                    if _is_truthy(arguments.get(opt)):
                        cmd += f" -{opt}"
                
                # String options
                force_replication = arguments.get("force_replication_on_nets")
                if force_replication:
                    cmd += f" -force_replication_on_nets {force_replication}"
                
                path_groups = arguments.get("path_groups")
                if path_groups:
                    cmd += f" -path_groups {{{path_groups}}}"
            
            output = run_tcl_command(cmd, timeout=timeout)
            # Detect Vivado errors — return JSON error so chain execution can detect failure
            if re.search(r'^ERROR: \[', output, re.MULTILINE):
                logger.error(f"phys_opt_design failed: {output[:300]}")
                return [TextContent(type="text", text=json.dumps({"error": f"phys_opt_design failed: {output[:500]}"}))]
            return [TextContent(type="text", text=f"Physical optimization complete.\n\n{output}")]

        elif name == "physopt_and_route":
            timeout = arguments.get("timeout", 3600)
            directive = arguments.get("directive", "Explore")

            # === SAFETY GUARD: same as phys_opt_design ===
            if directive in PHYSOPT_BLOCKED_DIRECTIVES:
                return [TextContent(
                    type="text",
                    text=(
                        f"Error: Directive '{directive}' is BLOCKED because it causes functional errors "
                        f"(retiming breaks design correctness). "
                        f"Use a safe directive instead: {', '.join(sorted(PHYSOPT_SAFE_DIRECTIVES))}"
                    )
                )]
            if directive not in PHYSOPT_SAFE_DIRECTIVES:
                return [TextContent(
                    type="text",
                    text=(
                        f"Error: Directive '{directive}' is not in the safe directive list. "
                        f"Allowed directives: {', '.join(sorted(PHYSOPT_SAFE_DIRECTIVES))}"
                    )
                )]
            # === END SAFETY GUARD ===

            result = {
                "status": "success",
                "physopt_directive": directive,
                "pre_optimization": {},
                "post_optimization": {},
                "physopt_output": "",
                "route_output": "",
            }
            error_messages = []

            # Step 1: Pre-optimization timing
            try:
                pre_timing = run_tcl_command("report_timing_summary -return_string", timeout=120)
                result["pre_optimization"] = _parse_timing_summary(pre_timing)
            except Exception as e:
                error_messages.append(f"pre_timing_failed: {e}")
                result["pre_optimization"] = {"error": str(e)}

            # Step 2: phys_opt_design
            try:
                cmd = f"phys_opt_design -directive {tcl_quote(directive)}"
                physopt_output = run_tcl_command(cmd, timeout=timeout)
                if re.search(r'^ERROR: \[', physopt_output, re.MULTILINE):
                    error_messages.append(f"physopt_vivado_error: {physopt_output[:200]}")
                    result["status"] = "partial"
                result["physopt_output"] = physopt_output[:2000]
            except Exception as e:
                error_messages.append(f"physopt_failed: {e}")
                result["physopt_output"] = f"Error: {e}"
                result["status"] = "partial"

            # Step 3: route_design
            try:
                route_output = run_tcl_command("route_design", timeout=timeout)
                if re.search(r'^ERROR: \[', route_output, re.MULTILINE):
                    error_messages.append(f"route_vivado_error: {route_output[:200]}")
                    result["status"] = "partial"
                result["route_output"] = route_output[:2000]
            except Exception as e:
                error_messages.append(f"route_failed: {e}")
                result["route_output"] = f"Error: {e}"
                result["status"] = "partial"

            # Step 4: Post-optimization timing
            try:
                post_timing = run_tcl_command("report_timing_summary -return_string", timeout=120)
                result["post_optimization"] = _parse_timing_summary(post_timing)
            except Exception as e:
                error_messages.append(f"post_timing_failed: {e}")
                result["post_optimization"] = {"error": str(e)}

            # If post-optimization timing data is missing/unavailable, mark as partial
            if "post_optimization" in result and (
                isinstance(result["post_optimization"], dict)
                and result["post_optimization"].get("wns") is None
            ):
                if result["status"] == "success":
                    result["status"] = "partial"
                    error_messages.append("post_timing_no_wns")

            if error_messages:
                result["errors"] = error_messages
                result["error"] = error_messages[0]  # Singular key for chain error detection

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "opt_design":
            timeout = arguments.get("timeout", 600)
            directive = arguments.get("directive", "Explore")
            retarget = arguments.get("retarget", True)

            # Safety guards below: (1) AddRetime directive blocked (retiming breaks
            # functional equivalence), (2) directive whitelist (OPT_SAFE_DIRECTIVES),
            # (3) -retarget suppressed when -directive is active ([Vivado_Tcl 4-167]).

            # Get WNS before opt_design for delta reporting
            wns_before = None
            try:
                timing_result = run_tcl_command("report_timing_summary -max_paths 1 -return_string", timeout=60)
                _m = re.search(r'WNS(?:\(ns\))?:\s*(-?[\d.]+)', timing_result)
                if _m:
                    wns_before = float(_m.group(1))
            except Exception:
                pass

            # Build and run opt_design command
            cmd = "opt_design"
            if directive:
                # Block retiming directive (AddRetime changes pipeline structure,
                # breaks functional equivalence - same risk as phys_opt_design retiming)
                if directive in OPT_BLOCKED_DIRECTIVES:
                    return [TextContent(
                        type="text",
                        text=(
                            f"Error: Directive '{directive}' is BLOCKED because it performs "
                            f"retiming (breaks design correctness)."
                        )
                    )]
                if directive not in OPT_SAFE_DIRECTIVES:
                    return [TextContent(
                        type="text",
                        text=(
                            f"Error: Directive {directive!r} is not in the safe directive list. "
                            f"Allowed directives: {', '.join(sorted(OPT_SAFE_DIRECTIVES))}"
                        )
                    )]
                cmd += f" -directive {tcl_quote(directive)}"
                # Vivado rejects -directive + -retarget together ([Vivado_Tcl 4-167]).
                # The directive implies equivalent retargeting, so suppress -retarget.
                retarget = False
            if retarget:
                cmd += " -retarget"

            result_text = run_tcl_command(cmd, timeout=timeout)

            # Detect Vivado errors — return JSON error so chain execution can detect failure
            if re.search(r'^ERROR: \[', result_text, re.MULTILINE):
                logger.error(f"opt_design failed: {result_text[:300]}")
                return [TextContent(type="text", text=json.dumps({"error": f"opt_design failed: {result_text[:500]}"}))]

            # Check for "no optimization" patterns
            no_opt_patterns = [
                "No optimization performed",
                "0 cells optimized",
                "INFO: [Opt 31-138]",
            ]
            no_opt_detected = any(p.lower() in result_text.lower() for p in no_opt_patterns)

            # Get WNS after
            wns_after = None
            try:
                timing_result2 = run_tcl_command("report_timing_summary -max_paths 1 -return_string", timeout=60)
                _m2 = re.search(r'WNS(?:\(ns\))?:\s*(-?[\d.]+)', timing_result2)
                if _m2:
                    wns_after = float(_m2.group(1))
            except Exception:
                pass

            response_data = {
                "status": "no_optimization" if no_opt_detected else "completed",
                "directive": directive,
                "wns_before": wns_before,
                "wns_after": wns_after,
                "wns_delta": round(wns_after - wns_before, 4) if (wns_before is not None and wns_after is not None) else None,
                "vivado_output": result_text[:5000],
            }
            return [TextContent(type="text", text=json.dumps(response_data))]

        elif name == "check_design_status":
            timeout = arguments.get("timeout", 30)
            if not _design_open:
                # No design is open - return early without querying (avoids the
                # noisy "No open project" error from get_property on a closed
                # design - run-20260711_164134).
                return [TextContent(type="text", text=json.dumps({
                    "design_open": False,
                    "status": "no_design",
                    "is_placed": False,
                    "is_routed": False,
                }, indent=2))]
            # Check if design is placed/routed.
            # Vivado's STATUS property reports completion messages like
            # "route_design Complete!" / "place_design Complete!" (lowercase
            # verb), so match case-insensitively. When STATUS is empty (some
            # Vivado versions after open_checkpoint), report Unknown rather
            # than fabricating a placed/routed state — the Dashboard's
            # design_state (parsed from report_timing_summary) is the
            # authoritative source (C1).
            status_result = run_tcl_command("get_property STATUS [current_design]", timeout=timeout)
            design_open = _design_open
            status_lower = status_result.lower() if status_result else ""
            # IS_PLACED / IS_ROUTED are NOT reliable on Vivado 2025.1 after
            # open_checkpoint - they return empty strings just like STATUS, so
            # the STATUS-string fallback below also yields false for a routed
            # design. When all three get_property calls return empty, fall back
            # to parsing report_route_status -return_string, which inspects
            # actual net routing state and is reliable (see report_route_status
            # tool). The Dashboard's design_state remains authoritative for
            # timing decisions, but is_placed/is_routed here must be correct so
            # callers do not re-place/re-route an already-routed design.
            is_placed_raw = run_tcl_command("get_property IS_PLACED [current_design]", timeout=5)
            is_routed_raw = run_tcl_command("get_property IS_ROUTED [current_design]", timeout=5)
            is_placed = is_placed_raw.strip() == "1"
            is_routed = is_routed_raw.strip() == "1"
            # Fallback 1: if IS_PLACED/IS_ROUTED returned non-boolean (old Vivado
            # with a non-empty STATUS), use the STATUS string heuristic.
            if is_placed_raw.strip() not in ("1", "0"):
                is_placed = ("place_design" in status_lower) or ("route_design" in status_lower)
            if is_routed_raw.strip() not in ("1", "0"):
                is_routed = "route_design" in status_lower
            # Fallback 2: on Vivado 2025.1 after open_checkpoint, STATUS /
            # IS_PLACED / IS_ROUTED all return empty, so the heuristics above
            # yield false even for a routed design. Recover the true state by
            # parsing report_route_status -return_string (inspects actual net
            # routing, not metadata properties).
            if (is_placed_raw.strip() not in ("1", "0")
                    and is_routed_raw.strip() not in ("1", "0")
                    and not is_placed and not is_routed):
                run_tcl_command("puts {status_check_start}", timeout=5)
                route_report = run_tcl_command("report_route_status -return_string", timeout=30)
                fully_routed = 0
                routing_errors = 0
                routable = 0
                for line in (route_report or "").split("\n"):
                    s = line.strip().lower()
                    m = re.search(r"(\d[\d,]*)", line)
                    if "# of fully routed nets" in s:
                        fully_routed = int(m.group(1).replace(",", "")) if m else 0
                    elif "# of nets with routing errors" in s:
                        routing_errors = int(m.group(1).replace(",", "")) if m else 0
                    elif "# of routable nets" in s:
                        routable = int(m.group(1).replace(",", "")) if m else 0
                if fully_routed > 0 and routing_errors == 0:
                    is_routed = True
                    is_placed = True
                elif routable > 0:
                    # Placed but not fully routed (routable nets exist).
                    is_placed = True

            response = {
                "design_open": design_open,
                "status": status_result.strip() if status_result else "Unknown",
                "is_placed": is_placed,
                "is_routed": is_routed,
            }
            return [TextContent(type="text", text=json.dumps(response, indent=2))]

        elif name == "validate_timing":
            timeout = arguments.get("timeout", 120)
            # Run timing summary and validate
            run_tcl_command("puts {timing_validation_start}", timeout=5)
            timing_report = run_tcl_command("report_timing_summary -return_string", timeout=timeout)
            parsed = _parse_timing_summary(timing_report)

            response = {
                "wns": parsed.get("wns"),
                "tns": parsed.get("tns"),
                "whs": parsed.get("whs"),
                "ths": parsed.get("ths"),
                "tpws": parsed.get("tpws"),
                "failing_endpoints": parsed.get("failing_endpoints"),
                "hold_failing_endpoints": parsed.get("hold_failing_endpoints"),
                "timing_met": parsed.get("wns") is not None and parsed.get("wns") >= 0,
                "hold_met": parsed.get("whs") is None or parsed.get("whs", 0) >= 0,
                "raw_report": timing_report[:2000] if timing_report else "",
            }
            return [TextContent(type="text", text=json.dumps(response, indent=2))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except pexpect.TIMEOUT:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.error(
            "[MCP_RESPONSE] Tool '%s' timed out (%dms)",
            name,
            duration_ms,
            extra={
                "mcp_tool_name": name,
                "mcp_response_duration_ms": duration_ms,
                "mcp_response_status": "timeout",
                "trace_id": trace_id,
            }
        )
        return [TextContent(
            type="text",
            text=f"Error: Command timed out. Vivado may be stuck. Use restart_vivado to recover."
        )]
    except pexpect.EOF:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.error(
            "[MCP_RESPONSE] Tool '%s' failed: Vivado process terminated (%dms)",
            name,
            duration_ms,
            extra={
                "mcp_tool_name": name,
                "mcp_response_duration_ms": duration_ms,
                "mcp_response_status": "error",
                "mcp_error_type": "EOF",
                "trace_id": trace_id,
            }
        )
        _design_open = False  # Mark design as closed — Vivado is dead

        if not _restarting:  # Reentry guard
            try:
                _restart_and_reopen()
                if _design_open:
                    return [TextContent(
                        type="text",
                        text="[ERROR] Vivado crashed (EOF) and was auto-restarted. DCP reopened. Please retry."
                    )]
                else:
                    return [TextContent(
                        type="text",
                        text="[ERROR] Vivado crashed (EOF) and was restarted, but DCP could not be reopened."
                    )]
            except Exception as restart_err:
                logger.error("Auto-restart after EOF failed: %s", restart_err)
                return [TextContent(
                    type="text",
                    text=f"[ERROR] Vivado crashed (EOF) and auto-restart failed: {restart_err}"
                )]
        else:
            return [TextContent(
                type="text",
                text="[ERROR] Vivado crash during restart — aborting"
            )]
    except Exception as e:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.error(
            "[MCP_RESPONSE] Tool '%s' failed: %s (%dms)",
            name,
            str(e),
            duration_ms,
            exc_info=True,
            extra={
                "mcp_tool_name": name,
                "mcp_response_duration_ms": duration_ms,
                "mcp_response_status": "error",
                "mcp_error_message": str(e),
                "mcp_error_type": type(e).__name__,
                "trace_id": trace_id,
            }
        )
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    """Main entry point."""
    global _vivado_path, _vivado_log_file, _vivado_journal_file
    
    parser = argparse.ArgumentParser(description="Vivado MCP Server")
    parser.add_argument(
        "--vivado-path",
        type=str,
        help="Path to Vivado executable (default: search in PATH)"
    )
    parser.add_argument(
        "--vivado-log",
        type=str,
        help="Path to Vivado log file (default: vivado.log)"
    )
    parser.add_argument(
        "--vivado-journal",
        type=str,
        help="Path to Vivado journal file (default: vivado.jou)"
    )
    
    args = parser.parse_args()
    
    if args.vivado_path:
        _vivado_path = args.vivado_path
    
    if args.vivado_log:
        _vivado_log_file = args.vivado_log
    
    if args.vivado_journal:
        _vivado_journal_file = args.vivado_journal
    
    logger.info("Starting Vivado MCP Server...")
    
    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Server running on stdio transport")
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
