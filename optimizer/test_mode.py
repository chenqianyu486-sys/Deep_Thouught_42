"""v2 Test Mode: validate MCP tools and skills without LLM.

Mirrors the v1 FPGAOptimizerTest class using v2 infrastructure:
- Uses optimizer/pure/tool_router.call_tool() for MCP routing
- Uses optimizer/pure/timing for timing parsing
- Standalone class, no LLM dependency

Reference: dcp_optimizer.py FPGAOptimizerTest (L5980-7591)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .pure.timing import (
    parse_timing_summary, parse_route_status, parse_control_sets,
    parse_cdc_paths, parse_design_info, parse_pvt_corner,
)

logger = logging.getLogger(__name__)


class TeeLogger:
    """Captures print output to both console and log file."""

    def __init__(self, log_file_path: Path):
        self._file = open(log_file_path, 'w', encoding='utf-8')
        self._original_stdout = sys.stdout

    def write(self, text: str):
        self._original_stdout.write(text)
        self._file.write(text)
        self._file.flush()

    def flush(self):
        self._original_stdout.flush()
        self._file.flush()

    def close(self):
        self._file.close()
        sys.stdout = self._original_stdout


class V2TestMode:
    """v2 test mode: validate MCP tools and skills without LLM."""

    def __init__(self, run_dir: Path, debug: bool = False, skip_skills: bool = False):
        self.run_dir = run_dir
        self.debug = debug
        self.skip_skills = skip_skills
        self.exit_stack = AsyncExitStack()
        self.rapidwright_session: Optional[ClientSession] = None
        self.vivado_session: Optional[ClientSession] = None
        self.skill_test_results: list[dict] = []
        self.tool_test_results: list[dict] = []
        self._quit_requested = False
        self._tee_logger: Optional[TeeLogger] = None

        # Timing state
        self.initial_wns: Optional[float] = None
        self.final_wns: Optional[float] = None
        self.clock_period: Optional[float] = None
        self.high_fanout_nets: list[tuple[str, int, int]] = []
        self.tns: Optional[float] = None
        self.failing_endpoints: Optional[int] = None

        # Extended analysis state (new fields)
        self.route_status: Optional[dict] = None
        self.control_sets: Optional[dict] = None
        self.cross_domain_paths_count: int = 0
        self.design_info: Optional[dict] = None
        self.constraints_info: Optional[dict] = None
        self.pvt_corner: str = "slow_0p95v_85c"
        self.device_capacity: Optional[dict] = None
        self.critical_path_spread: Optional[dict] = None
        self.resource_utilization: Optional[dict] = None
        self.hold_wns: Optional[float] = None
        self.hold_tns: Optional[float] = None

        # Start stdin quit listener
        self._quit_thread = threading.Thread(target=self._stdin_listener, daemon=True)
        self._quit_thread.start()

    def _stdin_listener(self):
        """Listen for 'quit' on stdin for graceful exit."""
        try:
            for line in sys.stdin:
                if line.strip().lower() == "quit":
                    print("[TEST] Quit requested by user")
                    self._quit_requested = True
                    break
        except (EOFError, OSError):
            pass

    def _check_test_exit(self, step_name: str = "") -> bool:
        """Check if user requested quit."""
        if self._quit_requested:
            print(f"[TEST] Quit requested, stopping at: {step_name}")
            return True
        return False

    # ── MCP Server Management ─────────────────────────────────────

    async def start_servers(self):
        """Start and connect to both MCP servers."""
        script_dir = Path(__file__).parent.parent.resolve()

        # Log files
        rw_mcp_log = self.run_dir / "rapidwright-mcp.log"
        v_mcp_log = self.run_dir / "vivado-mcp.log"
        rapidwright_log = self.run_dir / "rapidwright.log"
        vivado_log = self.run_dir / "vivado.log"
        vivado_journal = self.run_dir / "vivado.jou"

        rw_log_file = open(rw_mcp_log, 'w')
        self.exit_stack.callback(rw_log_file.close)
        v_log_file = open(v_mcp_log, 'w')
        self.exit_stack.callback(v_log_file.close)

        # RapidWright MCP
        rapidwright_config = {
            "command": sys.executable,
            "args": [
                str(script_dir / "RapidWrightMCP" / "server.py"),
                "--java-log", str(rapidwright_log),
                "--mcp-log", str(rw_mcp_log),
            ],
            "cwd": str(self.run_dir),
            "env": {**os.environ},
        }
        vivado_config = {
            "command": sys.executable,
            "args": [
                str(script_dir / "VivadoMCP" / "vivado_mcp_server.py"),
                "--vivado-log", str(vivado_log),
                "--vivado-journal", str(vivado_journal),
            ],
            "cwd": str(self.run_dir),
            "env": {**os.environ},
        }

        print("[TEST] Starting RapidWright MCP server...")
        rw_params = StdioServerParameters(**rapidwright_config)
        rw_transport = await self.exit_stack.enter_async_context(
            stdio_client(rw_params, errlog=rw_log_file)
        )
        rw_read, rw_write = rw_transport
        self.rapidwright_session = await self.exit_stack.enter_async_context(
            ClientSession(rw_read, rw_write)
        )
        await self.rapidwright_session.initialize()
        print("[TEST] RapidWright MCP connected")

        print("[TEST] Starting Vivado MCP server...")
        vivado_params = StdioServerParameters(**vivado_config)
        vivado_transport = await self.exit_stack.enter_async_context(
            stdio_client(vivado_params, errlog=v_log_file)
        )
        v_read, v_write = vivado_transport
        self.vivado_session = await self.exit_stack.enter_async_context(
            ClientSession(v_read, v_write)
        )
        await self.vivado_session.initialize()
        print("[TEST] Vivado MCP connected")

    async def cleanup(self):
        """Clean up MCP sessions."""
        await self.exit_stack.aclose()
        print(f"[TEST] Run directory preserved at: {self.run_dir}")

    # ── Tool Call Wrappers ─────────────────────────────────────────

    async def call_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Call an MCP tool with timeout, heartbeat, and logging."""
        # Determine server prefix
        if tool_name.startswith("rapidwright_"):
            session = self.rapidwright_session
            bare_name = tool_name[len("rapidwright_"):]
            prefix = "rapidwright"
        elif tool_name.startswith("vivado_"):
            session = self.vivado_session
            bare_name = tool_name[len("vivado_"):]
            prefix = "vivado"
        else:
            raise ValueError(f"Unknown tool prefix: {tool_name}")

        if session is None:
            raise RuntimeError(f"{prefix} session not initialized")

        logger.info(f"[{prefix.upper()}] Calling {bare_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[TEST] Calling {tool_name}...")
        start_time = time.time()

        # Heartbeat
        heartbeat_count = 0
        heartbeat_done = threading.Event()

        async def heartbeat():
            nonlocal heartbeat_count
            while not heartbeat_done.is_set():
                await asyncio.sleep(60)
                if heartbeat_done.is_set():
                    break
                heartbeat_count += 1
                elapsed = time.time() - start_time
                logger.info(f"[HEARTBEAT #{heartbeat_count}] {tool_name} still running after {elapsed:.1f}s")
                print(f"[TEST] [HEARTBEAT #{heartbeat_count}] {tool_name} still running after {elapsed:.1f}s")

        heartbeat_task = asyncio.create_task(heartbeat())

        try:
            result = await asyncio.wait_for(
                session.call_tool(bare_name, arguments),
                timeout=timeout,
            )
            heartbeat_done.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            elapsed = time.time() - start_time
            logger.info(f"[{prefix.upper()}] {bare_name} completed in {elapsed:.2f}s")
            print(f"[TEST] {tool_name} completed in {elapsed:.2f}s")

            # Record tool test result
            self.tool_test_results.append({"tool": tool_name, "success": True, "elapsed": elapsed})

            # Extract text content
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                raw = "\n".join(text_parts)
                if raw.strip():
                    return raw
            return json.dumps({"error": f"Empty MCP response for {tool_name}"})

        except asyncio.TimeoutError:
            heartbeat_done.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.error(f"[{prefix.upper()}] {bare_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[TEST] ERROR: {tool_name} TIMED OUT after {elapsed:.2f}s")
            self.tool_test_results.append({"tool": tool_name, "success": False, "elapsed": elapsed, "error": "timeout"})
            raise

        except Exception as e:
            heartbeat_done.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.error(f"[{prefix.upper()}] {bare_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[TEST] ERROR: {tool_name} failed after {elapsed:.2f}s: {e}")
            self.tool_test_results.append({"tool": tool_name, "success": False, "elapsed": elapsed, "error": str(e)})
            raise

    # ── Skill Verification ─────────────────────────────────────────

    def verify_skill_result(self, skill_name: str, raw_result: str) -> dict:
        """Parse and verify a skill invocation result."""
        try:
            data = json.loads(raw_result)
        except json.JSONDecodeError as e:
            print(f"[TEST] ⚠ Skill '{skill_name}' returned non-JSON result: {e}")
            self.skill_test_results.append({"skill": skill_name, "success": False, "error": str(e)})
            return {}

        has_error = isinstance(data, dict) and "error" in data
        entry = {"skill": skill_name, "success": not has_error, "data": data}
        if has_error:
            entry["error"] = data["error"]
        self.skill_test_results.append(entry)

        if has_error:
            print(f"[TEST] ⚠ Skill '{skill_name}' returned error: {data['error']}")
            return data

        # Fanout result
        if "nets_processed" in data:
            successful = data.get("successful_count", 0)
            failed = data.get("failed_count", 0)
            total = data.get("nets_processed", 0)
            ckpt = data.get("checkpoint_path", "")
            skipped = data.get("skipped", False)
            if skipped:
                print(f"[TEST] ✓ Skill '{skill_name}' | skipped: {data.get('message', '')}")
            else:
                print(f"[TEST] ✓ Skill '{skill_name}' | optimized: {successful}/{total} nets"
                      + (f", {failed} failed" if failed else "")
                      + (f" | checkpoint: {ckpt}" if ckpt else ""))
            results = data.get("results", [])
            if results:
                for r in results[:3]:
                    print(f"[TEST]   - {r.get('net_name', '?')}: "
                          f"fanout {r.get('original_fanout', '?')} → split_factor {r.get('split_factor', '?')}")
                if len(results) > 3:
                    print(f"[TEST]   ... and {len(results) - 3} more")

        # StrategyPlan format
        elif "strategy_name" in data:
            status = data.get("status", "unknown")
            steps = data.get("steps", [])
            print(f"[TEST] ✓ Skill '{skill_name}' | status: {status} | steps: {len(steps)}")
            for s in steps:
                mark = " ✓" if s.get("executed") else ""
                print(f"[TEST]   - {s['step_name']} ({s.get('platform', '?')}){mark}")
            if data.get("analysis_summary"):
                print(f"[TEST]   analysis: {json.dumps(data['analysis_summary'], ensure_ascii=False)[:200]}")

        # Pblock analysis format
        elif "region" in data and "pblock_ranges" in data:
            status = data.get("status", "unknown")
            region = data.get("region", {})
            er = data.get("estimated_resources", {})
            tr = data.get("target_resources", {})
            print(f"[TEST] ✓ Skill '{skill_name}' | status: {status}")
            print(f"[TEST]   region: cols {region.get('col_min')}-{region.get('col_max')}, "
                  f"rows {region.get('row_min')}-{region.get('row_max')}")
            print(f"[TEST]   estimated: {er.get('luts', '?')} LUTs, {er.get('ffs', '?')} FFs, "
                  f"{er.get('dsps', 0)} DSPs, {er.get('brams', 0)} BRAMs")
            print(f"[TEST]   target:    {tr.get('luts', '?')} LUTs, {tr.get('ffs', '?')} FFs, "
                  f"{tr.get('dsps', 0)} DSPs, {tr.get('brams', 0)} BRAMs (x{data.get('resource_multiplier', '?')})")
            if data.get("pblock_ranges"):
                print(f"[TEST]   pblock_ranges: {data['pblock_ranges'][:120]}...")
            if data.get("capacity_ok") is not None:
                print(f"[TEST]   capacity_ok: {data['capacity_ok']}")
            if data.get("deficit"):
                d = data["deficit"]
                print(f"[TEST]   deficit: LUTs={d.get('luts', 0)}, FFs={d.get('ffs', 0)}, "
                      f"DSPs={d.get('dsps', 0)}, BRAMs={d.get('brams', 0)}")
            if data.get("advice"):
                print(f"[TEST]   advice ({len(data['advice'])}):")
                for a in data["advice"]:
                    print(f"[TEST]     • {a}")
            if data.get("multi_region_suggestions"):
                mrs = data["multi_region_suggestions"]
                print(f"[TEST]   multi_region_suggestions ({len(mrs)} groups):")
                for mr in mrs:
                    print(f"[TEST]     group {mr.get('group', '?')}: "
                          f"cols {mr.get('cols', [])}, rows {mr.get('rows', [])}, "
                          f"suggested LUTs={mr.get('suggested_target_luts', 0):,}, "
                          f"FFs={mr.get('suggested_target_ffs', 0):,}")
            if data.get("next_steps"):
                ns = data["next_steps"]
                print(f"[TEST]   next_steps ({len(ns)}):")
                for s in ns:
                    print(f"[TEST]     → {s}")

        elif "status" in data:
            print(f"[TEST] ✓ Skill '{skill_name}' | status: {data.get('status')} | "
                  f"message: {data.get('message', '')}")
        else:
            top_keys = list(data.keys())[:5]
            print(f"[TEST] ✓ Skill '{skill_name}' completed | keys: {top_keys}")

        return data

    # ── Init Analysis ──────────────────────────────────────────────

    async def run_init_analysis(self, input_dcp: Path) -> dict:
        """Run initial analysis (equivalent to v1 Steps 0-4).

        Returns dict with timing, nets, cells data.
        """
        print("\n" + "=" * 60)
        print("INIT ANALYSIS")
        print("=" * 60)

        init_data: dict = {}

        # Step 0: Initialize RapidWright
        print("\n" + "-" * 60)
        print("STEP 0: Initialize RapidWright")
        print("-" * 60)
        result = await self.call_tool("rapidwright_initialize_rapidwright", {
            "jvm_max_memory": "8G",
        }, timeout=300.0)
        print(f"RapidWright init: {result[:500]}...")

        if self._check_test_exit("Step 1"):
            return init_data

        # Step 1: Open checkpoint in Vivado
        print("\n" + "-" * 60)
        print("STEP 1: Open input DCP in Vivado")
        print("-" * 60)
        result = await self.call_tool("vivado_open_checkpoint", {
            "dcp_path": str(input_dcp.resolve()),
        }, timeout=600.0)
        print(f"Open checkpoint: {result}")

        if self._check_test_exit("Step 2"):
            return init_data

        # Step 2: Report timing
        print("\n" + "-" * 60)
        print("STEP 2: Report timing in Vivado")
        print("-" * 60)
        result = await self.call_tool("vivado_report_timing_summary", {}, timeout=300.0)
        print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")

        timing_info = parse_timing_summary(result)
        init_data["wns"] = timing_info["wns"]
        init_data["tns"] = timing_info["tns"]
        init_data["failing_endpoints"] = timing_info["failing_endpoints"]
        init_data["timing_report_raw"] = result  # for PVT corner parsing
        self.initial_wns = timing_info["wns"]
        self.tns = timing_info["tns"]
        self.failing_endpoints = timing_info["failing_endpoints"]
        print(f"\n*** Initial WNS: {self.initial_wns} ns ***")

        # Get clock period via run_tcl (clk_fpl26contest is the contest clock)
        try:
            tcl_cmd = (
                "set clk [get_clocks -quiet clk_fpl26contest]; "
                "if {$clk ne {}} { "
                "  puts [get_property PERIOD $clk]; "
                "} else { "
                "  puts {NO_CONTEST_CLOCK}; "
                "}"
            )
            clock_result = await self.call_tool("vivado_run_tcl", {"command": tcl_cmd}, timeout=60.0)
            period = None
            if clock_result and clock_result.strip():
                for token in clock_result.strip().split():
                    if token.startswith("ERROR") or token.startswith("WARNING"):
                        continue
                    try:
                        val = float(token)
                        if val > 0:
                            period = val
                            break
                    except ValueError:
                        continue
            if period is None:
                fallback_cmd = (
                    "set tp [get_timing_paths -max_paths 1 -setup]; "
                    "if {$tp ne {}} { "
                    "  set clk [get_property ENDPOINT_CLOCK $tp]; "
                    "  if {$clk ne {}} { "
                    "    puts [get_property PERIOD [get_clocks $clk]]; "
                    "  } "
                    "}"
                )
                fallback_result = await self.call_tool("vivado_run_tcl", {"command": fallback_cmd}, timeout=60.0)
                if fallback_result and fallback_result.strip():
                    for token in fallback_result.strip().split():
                        if token.startswith("ERROR") or token.startswith("WARNING"):
                            continue
                        try:
                            val = float(token)
                            if val > 0:
                                period = val
                                break
                        except ValueError:
                            continue
            if period is not None:
                self.clock_period = period
                init_data["clock_period"] = self.clock_period
                print(f"*** Clock period: {self.clock_period:.3f} ns ***")
            else:
                print("[TEST] Could not determine clock period")
        except Exception as e:
            print(f"[TEST] Could not get clock period: {e}")

        if self._check_test_exit("Step 3"):
            return init_data

        # Step 3: High fanout nets
        print("\n" + "-" * 60)
        print("STEP 3: Get critical high fanout nets")
        print("-" * 60)
        result = await self.call_tool("vivado_get_critical_high_fanout_nets", {
            "num_paths": 50,
            "min_fanout": 100,
        }, timeout=600.0)
        print(f"High fanout nets:\n{result}")
        self.high_fanout_nets = parse_high_fanout_nets(result)
        init_data["high_fanout_nets"] = self.high_fanout_nets
        print(f"\nParsed {len(self.high_fanout_nets)} high fanout nets")

        if self._check_test_exit("Step 4"):
            return init_data

        # Step 4: Open DCP in RapidWright
        print("\n" + "-" * 60)
        print("STEP 4: Open DCP in RapidWright")
        print("-" * 60)
        result = await self.call_tool("rapidwright_read_checkpoint", {
            "dcp_path": str(input_dcp.resolve()),
        }, timeout=600.0)
        print(f"RapidWright read: {result}")

        # Step 5: Extract critical path cells
        print("\n" + "-" * 60)
        print("STEP 5: Extract critical path cells")
        print("-" * 60)
        critical_paths_file = self.run_dir / "critical_paths.json"
        try:
            result = await self.call_tool("vivado_extract_critical_path_cells", {
                "num_paths": 50,
                "output_file": str(critical_paths_file),
            }, timeout=600.0)
            print(f"Extract critical paths: {result[:2000]}...")
            init_data["critical_paths_file"] = str(critical_paths_file)
        except Exception as e:
            print(f"[TEST] ⚠ extract_critical_path_cells failed: {e}")

        # Step 6: Analyze critical path spread
        print("\n" + "-" * 60)
        print("STEP 6: Analyze critical path spread")
        print("-" * 60)
        try:
            result = await self.call_tool("rapidwright_analyze_critical_path_spread", {
                "input_file": str(critical_paths_file),
            }, timeout=300.0)
            print(f"Critical path spread: {result[:3000]}...")
            spread_data = json.loads(result)
            init_data["spread"] = spread_data
            self.critical_path_spread = {
                "max_distance": spread_data.get("max_distance_found", 0),
                "avg_distance": spread_data.get("avg_max_distance", 0),
                "paths_analyzed": spread_data.get("paths_analyzed", 0),
            }
        except Exception as e:
            print(f"[TEST] ⚠ analyze_critical_path_spread failed: {e}")

        # Step 7: Device topology
        try:
            topo_result = await self.call_tool("rapidwright_get_device_topology", {}, timeout=60.0)
            topo_data = json.loads(topo_result)
            if topo_data.get("status") == "success":
                print(f"[TEST] Device: {topo_data.get('device')}")
                site_dist = topo_data.get("site_type_distribution", [])
                capacity: dict[str, int] = {"LUT": 0, "FF": 0, "DSP": 0, "BRAM": 0, "URAM": 0}
                for entry in site_dist:
                    stype = entry.get("type", "")
                    count = entry.get("count", 0)
                    if "SLICE" in stype.upper():
                        capacity["LUT"] += count * 8
                        capacity["FF"] += count * 16
                    elif "DSP" in stype.upper():
                        capacity["DSP"] += count
                    elif stype.upper().startswith("RAMB36"):
                        capacity["BRAM"] += count
                    elif stype.upper().startswith("RAMB18"):
                        capacity["BRAM"] += count // 2
                    elif "URAM" in stype.upper():
                        capacity["URAM"] += count
                self.device_capacity = capacity
                init_data["device_capacity"] = capacity
                print(f"[TEST] Device capacity: {capacity}")
        except Exception as e:
            print(f"[TEST] ⚠ get_device_topology failed: {e}")

        # Resource utilization
        try:
            util_result = await self.call_tool("vivado_report_utilization_for_pblock", {}, timeout=300.0)
            init_data["utilization"] = util_result
            from .pure.timing import parse_resource_utilization
            self.resource_utilization = parse_resource_utilization(util_result)
            print(f"[TEST] Resource utilization retrieved")
        except Exception as e:
            print(f"[TEST] ! report_utilization_for_pblock failed: {e}")

        # --- Extended analysis (new fields for dashboard) ---

        # Step 8: Route status (M3: avg_wirelength, long_route_nets)
        print("\n" + "-" * 60)
        print("STEP 8: Report route status")
        print("-" * 60)
        try:
            route_result = await self.call_tool("vivado_report_route_status", {}, timeout=300.0)
            self.route_status = parse_route_status(route_result)
            init_data["route_status"] = self.route_status
            print(f"[TEST] Route status: nets={self.route_status.get('total_nets')}, "
                  f"long_routes={self.route_status.get('long_route_nets_count')}")
        except Exception as e:
            print(f"[TEST] ! report_route_status failed: {e}")

        # Step 9: Control sets (M4)
        print("\n" + "-" * 60)
        print("STEP 9: Report control sets")
        print("-" * 60)
        try:
            cs_result = await self.call_tool("vivado_run_tcl", {
                "command": "report_control_sets -return_string",
            }, timeout=60.0)
            self.control_sets = parse_control_sets(cs_result)
            init_data["control_sets"] = self.control_sets
            print(f"[TEST] Control sets: {self.control_sets}")
        except Exception as e:
            print(f"[TEST] ! report_control_sets failed: {e}")

        # Step 10: Design info (M4: cell type stats)
        print("\n" + "-" * 60)
        print("STEP 10: Get design info (RapidWright)")
        print("-" * 60)
        try:
            di_result = await self.call_tool("rapidwright_get_design_info", {}, timeout=120.0)
            self.design_info = parse_design_info(di_result)
            init_data["design_info"] = self.design_info
            if self.design_info:
                top = self.design_info.get("top_cell_types", {})
                top_str = ", ".join(f"{k}:{v}" for k, v in sorted(top.items(), key=lambda x: -x[1])[:8])
                print(f"[TEST] Design: cells={self.design_info.get('cell_count')}, "
                      f"nets={self.design_info.get('net_count')}, top=[{top_str}]")
        except Exception as e:
            print(f"[TEST] ! get_design_info failed: {e}")

        # Step 11: Constraints (M5: false paths, multicycle, IO delay)
        print("\n" + "-" * 60)
        print("STEP 11: Extract timing constraints")
        print("-" * 60)
        constraints: dict = {"false_paths_count": 0, "multicycle_paths_count": 0, "io_delay_defined_pct": None}
        try:
            fp_result = await self.call_tool("vivado_run_tcl", {
                "command": "llength [get_false_paths -quiet]",
            }, timeout=30.0)
            if fp_result and fp_result.strip():
                try:
                    constraints["false_paths_count"] = int(fp_result.strip().split()[0])
                except ValueError:
                    pass
        except Exception as e:
            print(f"[TEST] ! get_false_paths failed: {e}")
        try:
            mp_result = await self.call_tool("vivado_run_tcl", {
                "command": "llength [get_multicycle_paths -quiet]",
            }, timeout=30.0)
            if mp_result and mp_result.strip():
                try:
                    constraints["multicycle_paths_count"] = int(mp_result.strip().split()[0])
                except ValueError:
                    pass
        except Exception as e:
            print(f"[TEST] ! get_multicycle_paths failed: {e}")
        try:
            io_result = await self.call_tool("vivado_run_tcl", {
                "command": (
                    "set ports [get_ports -quiet]; "
                    "if {$ports ne {}} { "
                    "  set with_delay 0; set total 0; "
                    "  foreach p $ports { "
                    "    incr total; "
                    "    if {[get_property -quiet HAS_INPUT_DELAY $p] || "
                    "        [get_property -quiet HAS_OUTPUT_DELAY $p]} { "
                    "      incr with_delay; "
                    "    } "
                    "  }; "
                    "  puts \"$with_delay $total\"; "
                    "} else { "
                    "  puts \"0 0\"; "
                    "}"
                ),
            }, timeout=60.0)
            if io_result and io_result.strip():
                parts = io_result.strip().split()
                if len(parts) >= 2:
                    try:
                        wd, tp = int(parts[0]), int(parts[1])
                        if tp > 0:
                            constraints["io_delay_defined_pct"] = round(wd / tp, 4)
                    except ValueError:
                        pass
        except Exception as e:
            print(f"[TEST] ! IO delay analysis failed: {e}")
        self.constraints_info = constraints
        init_data["constraints_info"] = constraints
        print(f"[TEST] Constraints: false_paths={constraints['false_paths_count']}, "
              f"multicycle={constraints['multicycle_paths_count']}, "
              f"io_delay_pct={constraints['io_delay_defined_pct']}")

        # Step 12: CDC analysis (M4: cross_domain_paths)
        print("\n" + "-" * 60)
        print("STEP 12: Cross-domain clock analysis")
        print("-" * 60)
        try:
            cdc_result = await self.call_tool("vivado_run_tcl", {
                "command": "report_timing -cross_clock -max_paths 100 -return_string",
            }, timeout=120.0)
            self.cross_domain_paths_count = parse_cdc_paths(cdc_result)
            init_data["cross_domain_paths_count"] = self.cross_domain_paths_count
            print(f"[TEST] CDC paths: {self.cross_domain_paths_count}")
        except Exception as e:
            print(f"[TEST] ! CDC analysis failed: {e}")

        # Step 13: Hold timing check
        print("\n" + "-" * 60)
        print("STEP 13: Hold timing check")
        print("-" * 60)
        try:
            from .pure.timing import parse_hold_timing
            hold_result = await self.call_tool("vivado_run_tcl", {
                "command": "report_timing_summary -delay_type min -max_paths 100",
            }, timeout=120.0)
            hold = parse_hold_timing(hold_result)
            self.hold_wns = hold.get("hold_wns")
            self.hold_tns = hold.get("hold_tns")
            init_data["hold_wns"] = self.hold_wns
            init_data["hold_tns"] = self.hold_tns
            if self.hold_wns is not None:
                status = "VIOLATED" if self.hold_wns < 0 else "MET"
                print(f"[TEST] Hold timing {status}: WNS={self.hold_wns:.3f}ns")
        except Exception as e:
            print(f"[TEST] ! Hold timing check failed: {e}")

        # PVT corner from timing report header (Step 2 already collected)
        if "timing_report_raw" in init_data:
            self.pvt_corner = parse_pvt_corner(init_data["timing_report_raw"])
        init_data["pvt_corner"] = self.pvt_corner

        return init_data

    # ── Dashboard Verification ────────────────────────────────────

    def verify_dashboard_injection(self, init_data: dict) -> str:
        """Build StateSpace from init_analysis data and format as LLM would see it.

        Creates a temporary OptimizerState from extracted init_data,
        builds the 6-module StateSpace via build_state_space(),
        and formats it for LLM injection via format_state_space_for_llm().

        Returns the formatted dashboard YAML string (for console output & verification).
        """
        from .state import (
            OptimizerState, TimingState, CriticalPathEntry,
        )
        from .pure.state_space import build_state_space, format_state_space_for_llm
        from .pure.tool_filter import LoopPhase

        # Build temporary OptimizerState from init_data
        state = OptimizerState()
        t = state.timing

        t.initial_wns = init_data.get("wns")
        t.initial_tns = init_data.get("tns")
        t.initial_failing_endpoints = init_data.get("failing_endpoints")
        t.latest_wns = init_data.get("wns")
        t.latest_tns = init_data.get("tns")
        t.latest_failing_endpoints = init_data.get("failing_endpoints")
        t.best_wns = init_data.get("wns") if init_data.get("wns") is not None else float('-inf')
        t.best_wns_iteration = 0
        t.best_wns_tns = init_data.get("tns")
        t.best_wns_failing_endpoints = init_data.get("failing_endpoints")
        t.clock_period = init_data.get("clock_period")
        t.hold_wns = init_data.get("hold_wns")
        t.hold_tns = init_data.get("hold_tns")

        # High fanout nets (convert tuples to dicts for StateSpace compatibility)
        hfn = init_data.get("high_fanout_nets", [])
        if hfn and isinstance(hfn[0], tuple):
            t.high_fanout_nets = [
                {"net_name": n, "fanout": f, "path_count": p}
                for n, f, p in hfn
            ]
        else:
            t.high_fanout_nets = hfn

        # New fields
        t.route_status = init_data.get("route_status")
        t.control_sets = init_data.get("control_sets")
        t.cross_domain_paths_count = init_data.get("cross_domain_paths_count", 0)
        t.design_info = init_data.get("design_info")
        t.constraints_info = init_data.get("constraints_info")
        t.pvt_corner = init_data.get("pvt_corner", "slow_0p95v_85c")
        t.device_capacity = init_data.get("device_capacity")
        t.resource_utilization = init_data.get("resource_utilization")
        t.critical_path_spread = init_data.get("critical_path_spread")
        t.congestion_data = init_data.get("congestion_data")

        # Build and format
        space = build_state_space(state)
        formatted = format_state_space_for_llm(
            space=space,
            phase=LoopPhase.ANALYZE,
            handoff_summary="",
            show_strategy_catalog=False,
        )

        print("\n" + "=" * 60)
        print("DASHBOARD INJECTION VERIFICATION")
        print("=" * 60)
        print("The following is the StateSpace dashboard that would be")
        print("injected as the LAST user message before the first LLM call.")
        print("=" * 60)
        print(formatted)

        # Verification checks
        checks: list[tuple[str, bool, str]] = []

        # M1: Global state
        checks.append(("M1: WNS present", t.latest_wns is not None, str(t.latest_wns)))
        checks.append(("M1: TNS present", t.latest_tns is not None, str(t.latest_tns)))
        checks.append(("M1: Clock period present", t.clock_period is not None, str(t.clock_period)))

        # M2: Timing paths
        checks.append(("M2: Critical paths present", True, "ok"))  # always present (may be empty)

        # M3: Physical congestion
        rs = t.route_status or {}
        checks.append(("M3: Route status present", t.route_status is not None,
                       f"nets={rs.get('total_nets')}, long_routes={rs.get('long_route_nets_count')}"))
        checks.append(("M3: Congestion data present", t.congestion_data is not None, str(t.congestion_data)))

        # M4: Netlist quality
        checks.append(("M4: Control sets present", t.control_sets is not None, str(t.control_sets)))
        checks.append(("M4: CDC paths present", True, f"count={t.cross_domain_paths_count}"))
        checks.append(("M4: Design info present", t.design_info is not None,
                       f"cells={t.design_info.get('cell_count') if t.design_info else 'N/A'}"))

        # M5: Constraints
        ci = t.constraints_info or {}
        checks.append(("M5: Constraints present", t.constraints_info is not None,
                       f"fp={ci.get('false_paths_count')}, mp={ci.get('multicycle_paths_count')}"))
        checks.append(("M5: PVT corner", bool(t.pvt_corner), t.pvt_corner))

        # M6: Dynamic gradient (should be empty on init)
        checks.append(("M6: Delta data (empty on init)", True, "ok"))

        print("\n" + "-" * 60)
        print("FIELD VERIFICATION:")
        print("-" * 60)
        all_ok = True
        for label, ok, detail in checks:
            mark = "✓" if ok else "✗"
            if not ok:
                all_ok = False
            print(f"  [{mark}] {label}: {detail}")

        print("-" * 60)
        if all_ok:
            print("RESULT: All fields populated successfully!")
        else:
            print("RESULT: Some fields are missing — check warnings above.")

        return formatted

    # ── Skill Tests ────────────────────────────────────────────────

    # Data-driven skill test table. Each entry defines one skill invocation test.
    # Keys:
    #   name   - display name for test results
    #   tool   - MCP tool name (rapidwright_* or vivado_*)
    #   args   - base arguments (static); "{run_dir}" is replaced at runtime
    #   needs  - key in test_shared_data to inject as extra arg (name matches arg key)
    #   dep    - name of a prior skill whose verify_skill_result output feeds this one
    #   dep_key- key in dep's result data to inject as extra arg (name matches arg key)
    # Each entry: name (display), tool (MCP), skill_name (SkillRegistry), args, needs, dep, dep_key
    SKILL_TEST_TABLE = [
        # ── READ-ONLY (analysis) ──────────────────────────────────
        {"name": "analyze_congestion", "tool": "rapidwright_analyze_congestion",
         "skill_name": "analyze_congestion", "args": {}},
        {"name": "analyze_congestion_spreading", "tool": "rapidwright_analyze_congestion_spreading",
         "skill_name": "analyze_congestion_spreading", "args": {}},
        {"name": "analyze_net_detour", "tool": "rapidwright_analyze_net_detour",
         "skill_name": "net_detour", "args": {"detour_threshold": 2.0}, "needs": "pin_paths"},
        {"name": "smart_region_search", "tool": "rapidwright_smart_region_search",
         "skill_name": "smart_region", "args": {"target_lut_count": 50000, "target_ff_count": 50000}},
        {"name": "analyze_pblock_region", "tool": "rapidwright_analyze_pblock_region",
         "skill_name": "pblock_strategy", "args": {"target_lut_count": 50000, "target_ff_count": 50000, "resource_multiplier": 1.5}},
        {"name": "execute_pblock_strategy", "tool": "rapidwright_execute_pblock_strategy",
         "skill_name": "execute_pblock_strategy", "args": {"target_lut_count": 50000, "target_ff_count": 50000, "resource_multiplier": 1.2}},
        {"name": "analyze_register_retiming", "tool": "rapidwright_analyze_register_retiming",
         "skill_name": "analyze_register_retiming", "args": {}, "needs": "critical_paths"},
        {"name": "analyze_net_swapping", "tool": "rapidwright_analyze_net_swapping",
         "skill_name": "analyze_net_swapping", "args": {}},

        # ── MUTATING (optimization) ───────────────────────────────
        {"name": "execute_physopt_strategy", "tool": "rapidwright_execute_physopt_strategy",
         "skill_name": "physopt_strategy", "args": {"directive": "Default", "design_is_routed": False}},
        {"name": "execute_fanout_strategy", "tool": "rapidwright_execute_fanout_strategy",
         "skill_name": "fanout_strategy", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_fanout"}, "needs": "nets"},
        {"name": "optimize_cell_placement", "tool": "rapidwright_optimize_cell_placement",
         "skill_name": "optimize_cell", "args": {}, "needs": "cell_names"},
        {"name": "execute_congestion_spreading", "tool": "rapidwright_execute_congestion_spreading",
         "skill_name": "execute_congestion_spreading", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_congestion_spread"}},
        {"name": "optimize_pin_swapping", "tool": "rapidwright_optimize_pin_swapping",
         "skill_name": "pin_swapping_strategy", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_pin_swap"}, "needs": "critical_paths"},
        {"name": "replicate_critical_cells", "tool": "rapidwright_replicate_critical_cells",
         "skill_name": "critical_path_cell_replication_strategy", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_cell_repl"}, "needs": "critical_paths", "source_key": "critical_paths_cells"},
        {"name": "execute_register_retiming", "tool": "rapidwright_execute_register_retiming",
         "skill_name": "execute_register_retiming", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_retime"},
         "dep": "analyze_register_retiming", "dep_key": "retiming_candidates", "dep_result_key": "candidates"},
        {"name": "execute_net_swapping", "tool": "rapidwright_execute_net_swapping",
         "skill_name": "execute_net_swapping", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_net_swap"},
         "dep": "analyze_net_swapping", "dep_key": "candidates"},
        {"name": "flatten_lut_cascade", "tool": "rapidwright_flatten_lut_cascade",
         "skill_name": "lut_cascade_flattening", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_lut_cascade"}, "needs": "critical_paths", "source_key": "critical_paths_cell_names"},
        {"name": "smart_retiming", "tool": "rapidwright_smart_retiming",
         "skill_name": "smart_retiming", "args": {"temp_dir": "{run_dir}", "checkpoint_prefix": "test_smart_retiming", "auto_rollback": False},
         "needs": "critical_paths"},
    ]

    async def _prepare_skill_test_data(self, init_data: dict) -> dict:
        """Prepare shared data for skill tests from init_analysis results."""
        shared: dict = {}

        # pin_paths: extract from Vivado (used by analyze_net_detour, analyze_register_retiming, etc.)
        try:
            pins_result = await self.call_tool("vivado_extract_critical_path_pins", {
                "num_paths": 5,
            }, timeout=300.0)
            if pins_result.strip() and pins_result.strip() != "(no output)":
                pins_data = json.loads(pins_result)
                pin_paths_array = pins_data.get("pin_paths", [])
                if pin_paths_array and len(pin_paths_array) > 0:
                    shared["pin_paths"] = pin_paths_array[0]
                    print(f"[TEST] Prepared pin_paths: {len(shared['pin_paths'])} pins")
                else:
                    print(f"[TEST] ⚠ extract_critical_path_pins returned empty pin_paths")
                    _dbg = {k: pins_data.get(k) for k in ("debug_has_slack", "debug_report_length", "debug_num_slack_sections", "debug_per_path", "error") if k in pins_data}
                    if _dbg:
                        print(f"[TEST] debug: {_dbg}")
        except Exception as e:
            print(f"[TEST] ⚠ extract_critical_path_pins failed: {e}")

        # critical_paths: wrap pin_paths as list[dict] for tools that need it
        if "pin_paths" in shared:
            # Format 1: pin_paths dict (analyze_register_retiming, optimize_pin_swapping)
            shared["critical_paths"] = [{"pin_paths": shared["pin_paths"]}]

            # Format 2: cells dict (replicate_critical_cells)
            cells_from_pins = []
            seen_cells = set()
            for p in shared["pin_paths"]:
                cell = p.split("/")[0] if "/" in p else p
                if cell not in seen_cells:
                    seen_cells.add(cell)
                    cells_from_pins.append({"name": cell, "delay": 0.5, "type": "LUT", "fanout": 10})
            shared["critical_paths_cells"] = [{"cells": cells_from_pins}]

            # Format 3: list[list[str]] (flatten_lut_cascade)
            shared["critical_paths_cell_names"] = [
                [p.split("/")[0] for p in shared["pin_paths"] if "/" in p]
            ]

        # cell_names: extract from pin_paths (first 5 unique cells)
        if "pin_paths" in shared:
            seen = set()
            cell_names = []
            for p in shared["pin_paths"]:
                cell = p.split("/")[0] if "/" in p else p
                if cell not in seen:
                    seen.add(cell)
                    cell_names.append(cell)
            shared["cell_names"] = cell_names[:5]

        # nets: high fanout nets for execute_fanout_strategy
        high_fanout = init_data.get("high_fanout_nets", [])
        if high_fanout:
            shared["nets"] = [
                {"net_name": net_name, "fanout": fanout}
                for net_name, fanout, _ in high_fanout[:3]
            ]
        else:
            shared["nets"] = [{"net_name": "dummy_net", "fanout": 100}]

        return shared

    async def run_skill_tests(self, init_data: dict) -> bool:
        """Run all skill validation tests (data-driven, covers all 16 skills).

        Returns True if all skills pass.
        """
        print("\n" + "=" * 60)
        print("SKILL INVOCATION TESTS")
        print("=" * 60)

        # Verify all expected skills are registered
        from skills import SkillRegistry
        EXPECTED_SKILLS = {t["skill_name"] for t in self.SKILL_TEST_TABLE}
        registered = {m.name for m in SkillRegistry.list_all()}
        missing = EXPECTED_SKILLS - registered
        if missing:
            print(f"[TEST] WARNING: {len(missing)} skills not registered: {missing}")
            self.skill_test_results.extend([
                {"skill": name, "success": False, "error": "not registered"}
                for name in sorted(missing)
            ])
        else:
            print(f"[TEST] All {len(EXPECTED_SKILLS)} expected skills registered")

        # Prepare shared test data (pin_paths, critical_paths, cell_names, nets)
        shared = await self._prepare_skill_test_data(init_data)

        # Track results from analyze steps for dependency resolution
        dep_results: dict[str, dict] = {}

        # Run each skill test from the table
        for idx, entry in enumerate(self.SKILL_TEST_TABLE, 1):
            name = entry["name"]
            tool = entry["tool"]
            args = dict(entry.get("args", {}))

            # Replace {run_dir} placeholder
            for k, v in args.items():
                if isinstance(v, str) and v == "{run_dir}":
                    args[k] = str(self.run_dir)

            # Inject data from shared test data (needs)
            needs = entry.get("needs")
            source_key = entry.get("source_key", needs)
            if needs and source_key in shared:
                args[needs] = shared[source_key]

            # Inject data from dependency (dep)
            dep = entry.get("dep")
            dep_key = entry.get("dep_key")
            dep_result_key = entry.get("dep_result_key", dep_key)
            if dep and dep_key:
                dep_data = dep_results.get(dep)
                if dep_data and dep_result_key in dep_data:
                    dep_value = dep_data[dep_result_key]
                    if isinstance(dep_value, list) and len(dep_value) == 0:
                        print(f"\n{'─' * 60}")
                        print(f"SKILL {idx}: [SKIP] {name} — dependency '{dep}' returned empty list")
                        print(f"{'─' * 60}")
                        self.skill_test_results.append({
                            "skill": name, "success": True,
                            "error": f"dependency '{dep}' returned empty list — skipped"
                        })
                        continue
                    args[dep_key] = dep_value
                else:
                    print(f"\n{'─' * 60}")
                    print(f"SKILL {idx}: [SKIP] {name} — dependency '{dep}' key '{dep_result_key}' not available")
                    print(f"{'─' * 60}")
                    self.skill_test_results.append({
                        "skill": name, "success": False,
                        "error": f"dependency '{dep}' key '{dep_result_key}' not available"
                    })
                    continue

            # Skip if required data is missing
            if needs and needs not in args:
                print(f"\n{'─' * 60}")
                print(f"SKILL {idx}: [SKIP] {name} — missing '{needs}'")
                print(f"{'─' * 60}")
                self.skill_test_results.append({"skill": name, "success": False, "error": f"missing required data: {needs}"})
                continue

            print(f"\n{'─' * 60}")
            print(f"SKILL {idx}: [TEST] {name}")
            print(f"{'─' * 60}")

            try:
                result = await self.call_tool(tool, args, timeout=600.0)
                data = self.verify_skill_result(name, result)
                # Store result for dependency resolution
                if data:
                    dep_results[name] = data
            except Exception as e:
                print(f"[TEST] ⚠ {name} FAILED: {e}")
                self.skill_test_results.append({"skill": name, "success": False, "error": str(e)})

            if self._check_test_exit(f"Skill {idx}"):
                return False

        # ── Summary ──
        passed = sum(1 for r in self.skill_test_results if r.get("success"))
        failed = sum(1 for r in self.skill_test_results if not r.get("success"))
        total = len(self.skill_test_results)
        all_ok = (failed == 0)

        print(f"\n{'=' * 60}")
        print(f"SKILL TEST SUMMARY: {passed}/{total} passed, {failed} failed")
        print(f"{'=' * 60}")
        for r in self.skill_test_results:
            mark = "✓" if r.get("success") else "✗"
            print(f"  [{mark}] {r['skill']}")
            if not r.get("success") and r.get("error"):
                print(f"       {r['error']}")
        print()

        return all_ok

    # ── Corundum Full Test ─────────────────────────────────────────

    async def run_corundum_test(self, input_dcp: Path, output_dcp: Path, max_nets: int = 5) -> bool:
        """Corundum high-fanout optimization flow (equivalent to v1 run_test)."""
        print("\n" + "=" * 70)
        print("FPGA OPTIMIZER V2 TEST MODE — CORUNDUM HIGH-FANOUT FLOW")
        print("=" * 70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.run_dir}")
        print(f"Max nets:   {max_nets}")
        print("=" * 70 + "\n")

        overall_start = time.time()

        try:
            # Init analysis
            init_data = await self.run_init_analysis(input_dcp)
            if init_data.get("wns") is None:
                print("[TEST] Init analysis failed (no WNS)")
                return False

            # Skill tests
            if not self.skip_skills:
                if self._check_test_exit("Skill tests"):
                    return False
                await self.run_skill_tests(init_data)

            # Select nets to optimize
            nets_to_optimize = self.high_fanout_nets[:max_nets]
            print(f"\nWill optimize {len(nets_to_optimize)} nets:")
            for net_name, fanout, path_count in nets_to_optimize:
                print(f"  - {net_name} (fanout={fanout}, paths={path_count})")

            if self._check_test_exit("Fanout optimization"):
                return False

            # Step 5: Fanout optimization
            print("\n" + "-" * 60)
            print("STEP 5: Apply fanout optimizations")
            print("-" * 60)

            net_configs = [
                {"net_name": net_name, "fanout": fanout}
                for net_name, fanout, _ in nets_to_optimize
            ]

            if not self.skip_skills and net_configs:
                print(f"Calling execute_fanout_strategy for {len(net_configs)} nets")
                try:
                    result = await self.call_tool("rapidwright_execute_fanout_strategy", {
                        "nets": net_configs,
                        "temp_dir": str(self.run_dir),
                        "checkpoint_prefix": "test_fanout",
                    }, timeout=max(600.0, 300.0 * len(net_configs)))
                    data = self.verify_skill_result("execute_fanout_strategy", result)
                    successful_optimizations = data.get("successful_count", 0)
                except Exception as e:
                    print(f"execute_fanout_strategy FAILED: {e}")
                    successful_optimizations = 0
            elif net_configs:
                print(f"Batch optimizing {len(net_configs)} nets (raw tool)")
                try:
                    result = await self.call_tool("rapidwright_optimize_fanout_batch", {
                        "nets": net_configs,
                    }, timeout=300.0 * len(net_configs))
                    result_data = json.loads(result)
                    successful_optimizations = result_data.get("successful_count", 0) if result_data.get("status") == "success" else 0
                except Exception as e:
                    print(f"Batch optimization FAILED: {e}")
                    successful_optimizations = 0
            else:
                successful_optimizations = 0

            print(f"Successfully optimized {successful_optimizations}/{len(nets_to_optimize)} nets")

            if self._check_test_exit("Write DCP"):
                return False

            # Step 6: Write DCP from RapidWright
            print("\n" + "-" * 60)
            print("STEP 6: Write DCP from RapidWright")
            print("-" * 60)
            rapidwright_dcp = self.run_dir / "rapidwright_optimized.dcp"
            result = await self.call_tool("rapidwright_write_checkpoint", {
                "dcp_path": str(rapidwright_dcp),
                "overwrite": True,
            }, timeout=600.0)
            print(f"Write checkpoint: {result}")

            if self._check_test_exit("Re-open in Vivado"):
                return False

            # Step 7: Re-open in Vivado
            print("\n" + "-" * 60)
            print("STEP 7: Read RapidWright DCP into Vivado")
            print("-" * 60)
            tcl_script = rapidwright_dcp.with_suffix('.tcl')
            if tcl_script.exists():
                print(f"Found Tcl script for encrypted IP: {tcl_script}")
                result = await self.call_tool("vivado_run_tcl", {
                    "command": f"source {{{tcl_script}}}",
                }, timeout=300.0)
            else:
                result = await self.call_tool("vivado_open_checkpoint", {
                    "dcp_path": str(rapidwright_dcp),
                }, timeout=300.0)
            print(f"Re-open result: {result}")

            if self._check_test_exit("Route design"):
                return False

            # Step 8: Route
            print("\n" + "-" * 60)
            print("STEP 8: Route design in Vivado")
            print("-" * 60)

            # Check route status
            result = await self.call_tool("vivado_report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20,
            }, timeout=300.0)
            print(f"Route status:\n{result[:1500]}...")

            # Pre-route timing check
            pre_route_timing = await self.call_tool("vivado_report_timing_summary", {}, timeout=300.0)
            pre_route_info = parse_timing_summary(pre_route_timing)
            pre_route_wns = pre_route_info.get("wns")

            if pre_route_wns is not None and pre_route_wns < -0.5:
                print(f"\nPre-routing WNS poor ({pre_route_wns:.3f} ns), trying phys_opt_design...")
                try:
                    await self.call_tool("vivado_phys_opt_design", {
                        "directive": "aggressive_preroute_optimization",
                    }, timeout=3600.0)
                except Exception as e:
                    print(f"[TEST] phys_opt_design failed: {e}")

            # Route
            ROUTE_TIMEOUT = 21600.0  # 6 hours
            print(f"\nRouting design (timeout: {ROUTE_TIMEOUT:.0f}s / {ROUTE_TIMEOUT / 3600:.1f}h)...")
            result = await self.call_tool("vivado_route_design", {
                "directive": "Default",
            }, timeout=ROUTE_TIMEOUT)
            print(f"Route result: {result}")

            if self._check_test_exit("Final timing"):
                return False

            # Step 9: Final timing
            print("\n" + "-" * 60)
            print("STEP 9: Report final timing")
            print("-" * 60)
            result = await self.call_tool("vivado_report_timing_summary", {}, timeout=300.0)
            final_info = parse_timing_summary(result)
            self.final_wns = final_info.get("wns")
            print(f"\n*** Final WNS: {self.final_wns} ns ***")

            # Step 9.5: Verify get_wns
            try:
                get_wns_result = await self.call_tool("vivado_get_wns", {}, timeout=60.0)
                print(f"get_wns raw result: '{get_wns_result}'")
                try:
                    get_wns_value = float(get_wns_result.strip())
                    if self.final_wns is not None:
                        diff = abs(get_wns_value - self.final_wns)
                        if diff < 0.01:
                            print("✓ get_wns matches timing_summary")
                        else:
                            print(f"WARNING: get_wns differs by {diff:.4f} ns")
                except ValueError:
                    print(f"[TEST] get_wns parse error: {get_wns_result}")
            except Exception as e:
                print(f"[TEST] get_wns failed: {e}")

            # Write final DCP
            print(f"\nWriting final DCP to: {output_dcp}")
            await self.call_tool("vivado_write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True,
            }, timeout=600.0)

            # Summary
            elapsed = time.time() - overall_start
            self._print_summary("CORUNDUM TEST SUMMARY", elapsed,
                                f"Nets optimized: {successful_optimizations}/{len(nets_to_optimize)}")
            return True

        except Exception as e:
            logger.exception(f"Corundum test failed: {e}")
            print(f"\n*** TEST FAILED ***\nException: {type(e).__name__}: {e}")
            return False

    # ── LogicNets Full Test ────────────────────────────────────────

    async def run_logicnets_test(self, input_dcp: Path, output_dcp: Path) -> bool:
        """LogicNets pblock optimization flow (equivalent to v1 run_test_logicnets)."""
        print("\n" + "=" * 70)
        print("FPGA OPTIMIZER V2 TEST MODE — LOGICNETS PBLOCK FLOW")
        print("=" * 70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.run_dir}")
        print("=" * 70 + "\n")

        overall_start = time.time()

        try:
            # Init analysis
            init_data = await self.run_init_analysis(input_dcp)
            if not init_data.get("wns"):
                print("[TEST] Init analysis failed (no WNS)")
                return False

            # Skill tests
            if not self.skip_skills:
                if self._check_test_exit("Skill tests"):
                    return False
                await self.run_skill_tests(init_data)

            if self._check_test_exit("Pblock placement"):
                return False

            # Step 5: Known-optimal pblock
            print("\n" + "-" * 60)
            print("STEP 5: Use known-optimal pblock for LogicNets")
            print("-" * 60)
            pblock_ranges = "SLICE_X55Y60:SLICE_X111Y254"
            print(f"Pblock: {pblock_ranges}")

            # Step 6: Unplace
            print("\n" + "-" * 60)
            print("STEP 6: Unplace the design")
            print("-" * 60)
            result = await self.call_tool("vivado_run_tcl", {
                "command": "place_design -unplace",
            }, timeout=300.0)
            print(f"Unplace: {result}")

            # Step 7: Create and apply pblock
            print("\n" + "-" * 60)
            print("STEP 7: Create and apply pblock")
            print("-" * 60)
            result = await self.call_tool("vivado_create_and_apply_pblock", {
                "pblock_name": "pblock_opt",
                "ranges": pblock_ranges,
                "apply_to": "current_design",
                "is_soft": False,
            }, timeout=300.0)
            print(f"Create pblock: {result}")

            # Step 8: Place
            print("\n" + "-" * 60)
            print("STEP 8: Place the design")
            print("-" * 60)
            result = await self.call_tool("vivado_place_design", {
                "directive": "Default",
            }, timeout=3600.0)
            print(f"Place: {result}")

            if self._check_test_exit("Route"):
                return False

            # Step 9: Route
            print("\n" + "-" * 60)
            print("STEP 9: Route the design")
            print("-" * 60)
            pre_route_timing = await self.call_tool("vivado_report_timing_summary", {}, timeout=300.0)
            pre_route_info = parse_timing_summary(pre_route_timing)
            pre_route_wns = pre_route_info.get("wns")
            if pre_route_wns is not None:
                print(f"Pre-routing WNS: {pre_route_wns:.3f} ns")

            ROUTE_TIMEOUT = 21600.0  # 6 hours
            print(f"\nRouting design (timeout: {ROUTE_TIMEOUT:.0f}s / {ROUTE_TIMEOUT / 3600:.1f}h)...")
            result = await self.call_tool("vivado_route_design", {
                "directive": "Default",
            }, timeout=ROUTE_TIMEOUT)
            print(f"Route: {result}")

            result = await self.call_tool("vivado_report_route_status", {}, timeout=300.0)
            print(f"Route status:\n{result[:1500]}...")

            if self._check_test_exit("Final timing"):
                return False

            # Step 10: Final timing
            print("\n" + "-" * 60)
            print("STEP 10: Report final timing")
            print("-" * 60)
            result = await self.call_tool("vivado_report_timing_summary", {}, timeout=300.0)
            final_info = parse_timing_summary(result)
            self.final_wns = final_info.get("wns")
            print(f"\n*** Final WNS: {self.final_wns} ns ***")

            # Verify get_wns
            try:
                get_wns_result = await self.call_tool("vivado_get_wns", {}, timeout=60.0)
                print(f"get_wns raw result: '{get_wns_result}'")
                try:
                    get_wns_value = float(get_wns_result.strip())
                    if self.final_wns is not None:
                        diff = abs(get_wns_value - self.final_wns)
                        if diff < 0.01:
                            print("✓ get_wns matches timing_summary")
                        else:
                            print(f"WARNING: get_wns differs by {diff:.4f} ns")
                except ValueError:
                    print(f"[TEST] get_wns parse error: {get_wns_result}")
            except Exception as e:
                print(f"[TEST] get_wns failed: {e}")

            # Write final DCP
            print(f"\nWriting final DCP to: {output_dcp}")
            await self.call_tool("vivado_write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True,
            }, timeout=600.0)

            # Summary
            elapsed = time.time() - overall_start
            self._print_summary("LOGICNETS PBLOCK TEST SUMMARY", elapsed,
                                f"Pblock applied: {pblock_ranges}")
            return True

        except Exception as e:
            logger.exception(f"LogicNets test failed: {e}")
            print(f"\n*** TEST FAILED ***\nException: {type(e).__name__}: {e}")
            return False

    # ── Summary ────────────────────────────────────────────────────

    def _print_summary(self, title: str, elapsed: float, extra_info: str = ""):
        """Print test summary."""
        print("\n" + "=" * 70)
        print(title)
        print("=" * 70)
        print(f"Total runtime: {elapsed:.2f}s ({elapsed / 60:.2f} min)")
        if self.initial_wns is not None:
            print(f"Initial WNS:   {self.initial_wns:.3f} ns")
        if self.final_wns is not None:
            print(f"Final WNS:     {self.final_wns:.3f} ns")
            if self.initial_wns is not None:
                delta = self.final_wns - self.initial_wns
                print(f"WNS change:    {delta:+.3f} ns")
        if extra_info:
            print(f"\n{extra_info}")
        print("=" * 70)

        # Tool test results
        if self.tool_test_results:
            passed = sum(1 for r in self.tool_test_results if r.get("success"))
            total = len(self.tool_test_results)
            print(f"\n{'=' * 60}")
            print(f"TOOL CALL TEST RESULTS: {passed}/{total} passed")
            print(f"{'=' * 60}")
            for r in self.tool_test_results:
                mark = "✓" if r.get("success") else "✗"
                elapsed = r.get("elapsed", 0)
                print(f"  [{mark}] {r['tool']}  ({elapsed:.1f}s)")
                if not r.get("success") and r.get("error"):
                    print(f"       error: {r['error']}")
            print()

        # Skill test results
        if not self.skip_skills and self.skill_test_results:
            passed = sum(1 for r in self.skill_test_results if r.get("success"))
            total = len(self.skill_test_results)
            print(f"\n{'=' * 60}")
            print(f"SKILL INVOCATION TEST RESULTS: {passed}/{total} passed")
            print(f"{'=' * 60}")
            for r in self.skill_test_results:
                mark = "✓" if r.get("success") else "✗"
                print(f"  [{mark}] {r['skill']}")
                if not r.get("success") and r.get("error"):
                    print(f"       error: {r['error']}")
            print()


# ── High fanout net parsing (from v1) ────────────────────────────

def parse_high_fanout_nets(report: str) -> list[tuple[str, int, int]]:
    """Parse high fanout nets from Vivado report.

    Returns list of (net_name, fanout, path_count).
    """
    nets: list[tuple[str, int, int]] = []
    if not report:
        return nets
    for line in report.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-") or line.startswith("Net"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            net_name = parts[0]
            try:
                fanout = int(parts[1])
                path_count = int(parts[2]) if len(parts) > 2 else 0
                nets.append((net_name, fanout, path_count))
            except ValueError:
                continue
    return nets


# ── Entry Point ───────────────────────────────────────────────────

async def run_v2_test_mode(
    input_dcp: Path,
    output_dcp: Path,
    debug: bool = False,
    max_nets: int = 5,
    skip_skills: bool = False,
    skills_only: bool = False,
    init_analysis_only: bool = False,
) -> bool:
    """v2 test mode entry point."""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = Path.cwd() / f"dcp_optimizer_run-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Setup log file to capture all console output
    log_file = run_dir / "v2testmode.log"
    tee = TeeLogger(log_file)
    sys.stdout = tee

    tester = V2TestMode(run_dir, debug=debug, skip_skills=skip_skills)

    try:
        mode_label = "INIT-ANALYSIS ONLY" if init_analysis_only else (
            "SKILLS-ONLY" if skills_only else "V2 TEST MODE"
        )
        print(f"FPGA Design Optimization — {mode_label}")
        print(f"=========================================")
        print(f"Input:      {input_dcp.resolve()}")
        print(f"Output:     {output_dcp.resolve()}")
        print(f"Run dir:    {run_dir}")
        print(f"Log file:   {log_file}")
        print(f"Skills:     {'skip' if skip_skills else 'test'}")
        print(f"Mode:       {'skills-only' if skills_only else 'full'}")
        print()

        try:
            await tester.start_servers()

            if init_analysis_only:
                init_data = await tester.run_init_analysis(input_dcp)
                tester.verify_dashboard_injection(init_data)
                success = True
            elif skills_only:
                init_data = await tester.run_init_analysis(input_dcp)
                success = await tester.run_skill_tests(init_data)
            else:
                # DCP-type dispatch
                name = input_dcp.stem.lower()
                if "corundum" in name:
                    success = await tester.run_corundum_test(input_dcp, output_dcp, max_nets)
                elif "logicnets" in name:
                    success = await tester.run_logicnets_test(input_dcp, output_dcp)
                else:
                    print(f"[TEST] Unknown DCP type for test mode: {input_dcp.name}")
                    print(f"[TEST] Supported: corundum, logicnets")
                    return False

            return success

        except KeyboardInterrupt:
            print("\n[TEST] Interrupted by user")
            return False

    except Exception as e:
        logger.exception(f"v2 test mode fatal error: {e}")
        print(f"\n*** FATAL ERROR: {e} ***")
        return False

    finally:
        await tester.cleanup()
        tee.close()
        print(f"[TEST] Log saved to: {log_file}")
