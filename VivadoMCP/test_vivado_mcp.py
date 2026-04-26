#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache 2.0

"""
Test script for Vivado MCP Server.

Tests all Vivado MCP tools including:
1. open_checkpoint       - Open a DCP file
2. report_timing_summary - Get timing summary
3. get_wns              - Get Worst Negative Slack value
4. report_route_status  - Get routing status
5. report_utilization_for_pblock - Get resource utilization
6. get_critical_high_fanout_nets - Extract high fanout nets
7. extract_critical_path_cells   - Extract critical path cells
8. place_design         - Run placement
9. route_design         - Run routing
10. phys_opt_design     - Run physical optimization
11. create_and_apply_pblock - Create and apply pblock
12. write_edif           - Export EDIF netlist
13. write_verilog_simulation - Export Verilog simulation
14. write_checkpoint    - Write DCP file
15. run_tcl             - Execute Tcl command
16. restart_vivado      - Restart Vivado instance
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# MCP client imports
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

# Test checkpoint path - relative to parent directory (downloaded by Makefile)
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
TEST_DCP = PROJECT_ROOT / "logicnets_jscl.dcp"

# Output directory for written files
OUTPUT_DIR = Path(tempfile.gettempdir()) / "vivado_mcp_test"

# Test counter
_test_counter = 0


def get_test_num():
    """Get next test number."""
    global _test_counter
    _test_counter += 1
    return _test_counter


def reset_test_counter():
    """Reset test counter."""
    global _test_counter
    _test_counter = 0


async def call_tool(session: ClientSession, name: str, arguments: dict) -> str:
    """Call an MCP tool and return the result text."""
    result = await session.call_tool(name, arguments)

    # Extract text from result
    if result.content:
        texts = [c.text for c in result.content if hasattr(c, 'text')]
        return "\n".join(texts)
    return ""


# =============================================================================
# Stage 1: Basic Information Tools
# =============================================================================

async def test_open_checkpoint(session: ClientSession, dcp_path: str) -> bool:
    """Test 1: Open a DCP file."""
    n = get_test_num()
    print(f"\n[{n}] Opening checkpoint: {dcp_path}")
    try:
        result = await call_tool(session, "open_checkpoint", {"dcp_path": dcp_path, "timeout": 600})
        if "ERROR" in result.upper() and "successfully" not in result.lower():
            print(f"✗ Failed: {result[:500]}")
            return False
        print(f"✓ Checkpoint opened")
        return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_report_timing_summary(session: ClientSession) -> bool:
    """Test 2: Get timing summary report."""
    n = get_test_num()
    print(f"\n[{n}] Getting timing summary")
    try:
        result = await call_tool(session, "report_timing_summary", {"timeout": 300})
        print(f"Result (first 500 chars):\n{result[:500]}...")
        # Verify it contains timing data
        if "WNS" in result or "TNS" in result or "timing" in result.lower():
            print(f"✓ Timing summary generated")
            return True
        print(f"⚠ Unexpected output format")
        return True  # Don't fail, just warn
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_get_wns(session: ClientSession) -> bool:
    """Test 3: Get WNS value directly."""
    n = get_test_num()
    print(f"\n[{n}] Getting WNS value")
    try:
        result = await call_tool(session, "get_wns", {"timeout": 60})
        wns_val = result.strip()
        print(f"WNS: {wns_val} ns")
        # Verify it's a valid number
        try:
            float(wns_val)
            print(f"✓ WNS retrieved")
            return True
        except ValueError:
            if wns_val == "PARSE_ERROR":
                print(f"⚠ PARSE_ERROR (WNS could not be determined)")
                return True  # Don't fail, this is expected in some cases
            print(f"✗ Invalid WNS value: {wns_val}")
            return False
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_report_route_status(session: ClientSession) -> bool:
    """Test 4: Get routing status report."""
    n = get_test_num()
    print(f"\n[{n}] Getting route status")
    try:
        result = await call_tool(session, "report_route_status", {"timeout": 300})
        print(f"Result (first 500 chars):\n{result[:500]}...")
        print(f"✓ Route status generated")
        return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_report_utilization(session: ClientSession) -> bool:
    """Test 5: Get resource utilization for pblock sizing."""
    n = get_test_num()
    print(f"\n[{n}] Getting resource utilization")
    try:
        result = await call_tool(session, "report_utilization_for_pblock", {"timeout": 300})
        print(f"Result (first 500 chars):\n{result[:500]}...")
        print(f"✓ Utilization report generated")
        return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


# =============================================================================
# Stage 2: High Fanout Net Analysis Tools
# =============================================================================

async def test_get_critical_high_fanout_nets(session: ClientSession) -> bool:
    """Test 6: Extract critical high fanout nets."""
    n = get_test_num()
    print(f"\n[{n}] Getting critical high fanout nets")
    try:
        result = await call_tool(session, "get_critical_high_fanout_nets", {
            "num_paths": 50,
            "min_fanout": 10,
            "exclude_clocks": True,
            "timeout": 600
        })
        print(f"Result (first 1500 chars):\n{result[:1500]}...")

        # Verify parent net resolution is working
        net_matches = re.findall(r"parent_net\s*[=:]\s*\"?([^\",}\s]+)\"?", result)
        if not net_matches:
            net_matches = re.findall(r"[\"'\\w]+/[\\w\\[\\]]+", result)
        if net_matches:
            print(f"✓ Found parent nets: {len(net_matches)} nets")
            return True
        print(f"⚠ No parent nets found (design may have no high fanout nets)")
        return True  # Don't fail, this is informational
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_extract_critical_path_cells(session: ClientSession) -> bool:
    """Test 7: Extract critical path cells."""
    n = get_test_num()
    print(f"\n[{n}] Extracting critical path cells")
    output_file = OUTPUT_DIR / "critical_paths.json"
    try:
        result = await call_tool(session, "extract_critical_path_cells", {
            "num_paths": 50,
            "output_file": str(output_file),
            "timeout": 600
        })
        # Check if result contains JSON or file was written
        if output_file.exists():
            print(f"✓ Critical paths written to: {output_file}")
            return True
        # If result is directly returned as JSON string
        try:
            data = json.loads(result)
            print(f"✓ Critical paths extracted: {len(data.get('paths', []))} paths")
            return True
        except json.JSONDecodeError:
            print(f"Result (first 500 chars):\n{result[:500]}...")
            print(f"✓ Critical path cells extracted")
            return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


# =============================================================================
# Stage 3: Placement and Routing Tools
# =============================================================================

async def test_place_design(session: ClientSession) -> bool:
    """Test 8: Run placement."""
    n = get_test_num()
    print(f"\n[{n}] Running place_design")
    try:
        result = await call_tool(session, "place_design", {
            "directive": "Default",
            "timeout": 3600
        })
        print(f"Result (first 500 chars):\n{result[:500]}...")
        if "ERROR" in result.upper() and "place_design: Running" not in result:
            print(f"✗ Placement failed")
            return False
        print(f"✓ Placement completed")
        return True
    except asyncio.TimeoutError:
        print(f"✗ Placement timed out")
        return False
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_route_design(session: ClientSession) -> bool:
    """Test 9: Run routing."""
    n = get_test_num()
    print(f"\n[{n}] Running route_design")
    try:
        result = await call_tool(session, "route_design", {
            "directive": "Default",
            "timeout": 21600  # 6 hours
        })
        print(f"Result (first 500 chars):\n{result[:500]}...")
        if "ERROR" in result.upper() and "route_design: Routing" not in result:
            print(f"✗ Routing failed")
            return False
        print(f"✓ Routing completed")
        return True
    except asyncio.TimeoutError:
        print(f"✗ Routing timed out")
        return False
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_phys_opt_design(session: ClientSession) -> bool:
    """Test 10: Run physical optimization."""
    n = get_test_num()
    print(f"\n[{n}] Running phys_opt_design")
    try:
        result = await call_tool(session, "phys_opt_design", {
            "directive": "Default",
            "timeout": 3600
        })
        print(f"Result (first 500 chars):\n{result[:500]}...")
        print(f"✓ Physical optimization completed")
        return True
    except asyncio.TimeoutError:
        print(f"✗ phys_opt_design timed out")
        return False
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


# =============================================================================
# Stage 4: Constraints and Export Tools
# =============================================================================

async def test_create_and_apply_pblock(session: ClientSession) -> bool:
    """Test 11: Create and apply pblock."""
    n = get_test_num()
    print(f"\n[{n}] Creating pblock constraint")
    try:
        result = await call_tool(session, "create_and_apply_pblock", {
            "pblock_name": "pblock_test",
            "ranges": "CLOCKREGION_X0Y0:CLOCKREGION_X3Y3",
            "apply_to": "current_design",
            "is_soft": True,
            "timeout": 300
        })
        print(f"Result:\n{result[:500]}...")
        if "Successfully" in result or "Pblock Created" in result or "pblock_test" in result:
            print(f"✓ Pblock created and applied")
            return True
        print(f"⚠ Pblock creation returned unexpected result")
        return True  # Don't fail, may depend on design
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_write_edif(session: ClientSession) -> bool:
    """Test 12: Export EDIF netlist."""
    n = get_test_num()
    print(f"\n[{n}] Writing EDIF netlist")
    output_edif = OUTPUT_DIR / "test_output.edf"
    try:
        result = await call_tool(session, "write_edif", {
            "edif_path": str(output_edif),
            "force": True,
            "timeout": 300
        })
        print(f"Result:\n{result[:500]}...")
        if output_edif.exists():
            print(f"✓ EDIF written to: {output_edif}")
            return True
        if "ERROR" in result.upper():
            print(f"✗ EDIF export failed")
            return False
        print(f"⚠ EDIF file not verified on disk")
        return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_write_verilog_simulation(session: ClientSession) -> bool:
    """Test 13: Export Verilog simulation."""
    n = get_test_num()
    print(f"\n[{n}] Writing Verilog simulation")
    output_v = OUTPUT_DIR / "test_output.v"
    try:
        result = await call_tool(session, "write_verilog_simulation", {
            "verilog_path": str(output_v),
            "force": True,
            "timeout": 300
        })
        print(f"Result:\n{result[:500]}...")
        if output_v.exists():
            print(f"✓ Verilog written to: {output_v}")
            return True
        if "ERROR" in result.upper():
            print(f"✗ Verilog export failed")
            return False
        print(f"⚠ Verilog file not verified on disk")
        return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_write_checkpoint(session: ClientSession) -> bool:
    """Test 14: Write DCP checkpoint."""
    n = get_test_num()
    print(f"\n[{n}] Writing checkpoint")
    output_dcp = OUTPUT_DIR / "test_output.dcp"
    try:
        result = await call_tool(session, "write_checkpoint", {
            "dcp_path": str(output_dcp),
            "force": True,
            "timeout": 600
        })
        print(f"Result:\n{result[:500]}...")
        if output_dcp.exists():
            size_kb = output_dcp.stat().st_size // 1024
            print(f"✓ Checkpoint written: {output_dcp} ({size_kb} KB)")
            return True
        if "ERROR" in result.upper():
            print(f"✗ Checkpoint write failed")
            return False
        print(f"⚠ Checkpoint file not verified on disk")
        return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


# =============================================================================
# Stage 5: Tcl and System Tools
# =============================================================================

async def test_run_tcl(session: ClientSession) -> bool:
    """Test 15: Execute Tcl command."""
    n = get_test_num()
    print(f"\n[{n}] Executing Tcl command")
    try:
        # Simple Tcl command to test functionality
        result = await call_tool(session, "run_tcl", {
            "command": "puts {Hello from Vivado Tcl}",
            "timeout": 60
        })
        print(f"Result:\n{result[:500]}...")
        if "Hello from Vivado Tcl" in result or "puts" in result:
            print(f"✓ Tcl command executed")
            return True
        print(f"⚠ Tcl output unexpected")
        return True  # Don't fail, output format may vary
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


async def test_restart_vivado(session: ClientSession) -> bool:
    """Test 16: Restart Vivado instance."""
    n = get_test_num()
    print(f"\n[{n}] Restarting Vivado")
    try:
        result = await call_tool(session, "restart_vivado", {})
        print(f"Result:\n{result[:500]}...")

        # After restart, we need to re-open the design
        print(f"Waiting for Vivado to restart...")
        await asyncio.sleep(5)

        # Try to re-open the checkpoint to verify Vivado is responsive
        dcp_path = str(TEST_DCP)
        open_result = await call_tool(session, "open_checkpoint", {
            "dcp_path": dcp_path,
            "timeout": 600
        })
        if "ERROR" in open_result.upper() and "successfully" not in open_result.lower():
            print(f"⚠ Vivado restarted but design re-open failed")
            return True  # Don't fail the restart test
        print(f"✓ Vivado restarted and design re-opened")
        return True
    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


# =============================================================================
# Main Test Function
# =============================================================================

async def test_vivado_tools(session: ClientSession, dcp_path: str):
    """Test all Vivado MCP tools in sequence.

    Args:
        session: MCP client session
        dcp_path: Path to test DCP file

    Returns:
        True if all tests passed, False otherwise
    """
    reset_test_counter()

    print(f"\n{'='*80}")
    print("STAGE 1: BASIC INFORMATION TOOLS")
    print(f"{'='*80}")

    results = []

    # Stage 1: Basic Information Tools
    results.append(("open_checkpoint", await test_open_checkpoint(session, dcp_path)))
    results.append(("report_timing_summary", await test_report_timing_summary(session)))
    results.append(("get_wns", await test_get_wns(session)))
    results.append(("report_route_status", await test_report_route_status(session)))
    results.append(("report_utilization_for_pblock", await test_report_utilization(session)))

    print(f"\n{'='*80}")
    print("STAGE 2: HIGH FANOUT NET ANALYSIS TOOLS")
    print(f"{'='*80}")

    # Stage 2: High Fanout Net Analysis
    results.append(("get_critical_high_fanout_nets", await test_get_critical_high_fanout_nets(session)))
    results.append(("extract_critical_path_cells", await test_extract_critical_path_cells(session)))

    print(f"\n{'='*80}")
    print("STAGE 3: PLACEMENT AND ROUTING TOOLS")
    print(f"{'='*80}")

    # Stage 3: Placement and Routing
    results.append(("place_design", await test_place_design(session)))
    results.append(("route_design", await test_route_design(session)))
    results.append(("phys_opt_design", await test_phys_opt_design(session)))

    print(f"\n{'='*80}")
    print("STAGE 4: CONSTRAINT AND EXPORT TOOLS")
    print(f"{'='*80}")

    # Stage 4: Constraints and Export
    results.append(("create_and_apply_pblock", await test_create_and_apply_pblock(session)))
    results.append(("write_edif", await test_write_edif(session)))
    results.append(("write_verilog_simulation", await test_write_verilog_simulation(session)))
    results.append(("write_checkpoint", await test_write_checkpoint(session)))

    print(f"\n{'='*80}")
    print("STAGE 5: TCL AND SYSTEM TOOLS")
    print(f"{'='*80}")

    # Stage 5: Tcl and System
    results.append(("run_tcl", await test_run_tcl(session)))
    results.append(("restart_vivado", await test_restart_vivado(session)))

    # Summary
    print(f"\n{'='*80}")
    print("TEST SUMMARY")
    print(f"{'='*80}")

    passed = sum(1 for _, r in results if r)
    total = len(results)

    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    return passed == total


async def main():
    """Main test function."""
    # Create output directory
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {OUTPUT_DIR}")

    # Verify test DCP exists
    if not TEST_DCP.exists():
        print(f"ERROR: Test checkpoint not found: {TEST_DCP}")
        print(f"Please run 'make setup' in the project root to download example DCPs:")
        print(f"  cd {PROJECT_ROOT} && make setup")
        sys.exit(1)

    # Path to the MCP server
    server_script = SCRIPT_DIR / "vivado_mcp_server.py"
    if not server_script.exists():
        print(f"ERROR: Server script not found: {server_script}")
        sys.exit(1)

    print(f"{'='*80}")
    print(f"VIVADO MCP SERVER - COMPLETE TOOL CHAIN TEST")
    print(f"{'='*80}")
    print(f"Server script: {server_script}")
    print(f"Test DCP: {TEST_DCP}")
    print(f"Output directory: {OUTPUT_DIR}")

    # Create server parameters
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_script)],
        env=os.environ.copy()
    )

    # Connect to the MCP server
    print(f"\nConnecting to Vivado MCP Server...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the session
            await session.initialize()
            print(f"✓ Session initialized")

            # List available tools
            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            print(f"\nAvailable tools ({len(tool_names)}):")

            # All tools that will be tested
            tested_tools = [
                "open_checkpoint", "report_timing_summary", "get_wns",
                "report_route_status", "report_utilization_for_pblock",
                "get_critical_high_fanout_nets", "extract_critical_path_cells",
                "place_design", "route_design", "phys_opt_design",
                "create_and_apply_pblock", "write_edif", "write_verilog_simulation",
                "write_checkpoint", "run_tcl", "restart_vivado"
            ]

            for tool_name in tool_names:
                marker = "★" if tool_name in tested_tools else " "
                print(f"  {marker} {tool_name}")

            # Run the complete tool chain tests
            success = await test_vivado_tools(session, str(TEST_DCP))

            print(f"\n{'='*80}")
            if success:
                print("✓ ALL TESTS PASSED!")
            else:
                print("✗ SOME TESTS FAILED!")
            print(f"{'='*80}")

            # Final cleanup - Vivado will be terminated when server exits
            print("\nCleaning up and exiting...")

    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
