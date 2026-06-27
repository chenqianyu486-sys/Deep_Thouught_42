#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache 2.0

"""
FPGA Design Optimization Agent (V2 — State Machine)

An autonomous AI agent that analyzes FPGA designs and applies optimizations
using RapidWright and Vivado via MCP servers. Driven by a 9-node state
machine graph (optimizer/ package).
"""

import argparse
import asyncio
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
try:
    from openai import AsyncOpenAI
except ImportError:
    print("Error: openai package not installed. Please run: pip install openai", file=sys.stderr)
    sys.exit(1)

from context_manager import MemoryManager
from context_manager.compat import DCPOptimizerCompat
from context_manager.events import EventBus
from context_manager.logging_config import setup_logging, PromptLogger, DynamicLogLevelManager
from config_loader import get_model_config_loader

# === Model Configuration ===

_loader = get_model_config_loader()
_worker_data = _loader.get_worker_config()
_planner_data = _loader.get_planner_config()

DEFAULT_MODEL_PLANNER: str = _planner_data.model_name
DEFAULT_MODEL_WORKER: str = _worker_data.model_name

# === Logging ===

setup_logging(level="INFO", use_json=os.environ.get("FPL26_LOG_JSON", "0") == "1")
logger = logging.getLogger(__name__)


# === Utility Functions ===


def parse_timing_summary_static(timing_report: str) -> dict:
    """Parse timing summary report to extract WNS, TNS, and failing endpoints."""
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


def load_system_prompt() -> str:
    """Load system prompt from SYSTEM_PROMPT.TXT file."""
    script_dir = Path(__file__).parent.resolve()
    prompt_file = script_dir / "SYSTEM_PROMPT.TXT"

    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"System prompt file not found: {prompt_file}")
        raise
    except Exception as e:
        logger.error(f"Failed to load system prompt: {e}")
        raise


def convert_mcp_tool_to_openai(tool, server_prefix: str) -> dict:
    """Convert MCP tool definition to OpenAI-compatible format with server prefix."""
    schema = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": f"{server_prefix}_{tool.name}",
            "description": tool.description or "",
            "parameters": schema,
            "strict": False
        }
    }


# === V2 State-Machine Optimizer Entry Point ===


async def optimize_v2(
    input_dcp: Path,
    output_dcp: Path,
    api_key: str = "",
    model_planner: str = DEFAULT_MODEL_PLANNER,
    model_worker: str = DEFAULT_MODEL_WORKER,
    wall_clock_timeout: float = 3600.0,
    debug: bool = False,
    dashboard: bool = False,
    dashboard_port: int = 8080,
) -> bool:
    """State-machine-driven optimizer entry point.

    Uses the explicit graph-based architecture from optimizer/ package.

    Args:
        input_dcp: Path to input design checkpoint.
        output_dcp: Path to write optimized checkpoint.
        api_key: OpenRouter API key.
        model_planner: Planner model identifier.
        model_worker: Worker model identifier.
        wall_clock_timeout: Max runtime in seconds.
        debug: Enable verbose logging.
        dashboard: Enable web dashboard for real-time state monitoring.
        dashboard_port: HTTP port for the dashboard.

    Returns:
        True if timing converged (WNS >= 0), False otherwise.
    """
    from optimizer import (
        build_optimizer_graph,
        OptimizerState,
        NodeDeps,
        StateTracer,
    )

    logger.info("=" * 60)
    logger.info("[optimize_v2] State-machine-driven optimizer")
    logger.info(f"  Input:  {input_dcp}")
    logger.info(f"  Output: {output_dcp}")
    logger.info(f"  Planner: {model_planner}")
    logger.info(f"  Worker:  {model_worker}")
    logger.info("=" * 60)

    # Step 1: Create run directory and configure logging
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = Path.cwd() / f"dcp_optimizer_run-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(level="INFO", use_json=False, log_dir=str(run_dir))
    logger.info(f"[optimize_v2] Run directory: {run_dir}")

    # Build initial state
    state = OptimizerState()
    state.control.input_dcp = input_dcp
    state.control.output_dcp = output_dcp
    state.control.wall_clock_timeout = wall_clock_timeout
    state.control.run_dir = run_dir
    state.model.planner_model = model_planner
    state.model.worker_model = model_worker

    # Load fallback models from config
    _v2_loader = get_model_config_loader()
    _v2_worker_data = _v2_loader.get_worker_config()
    _v2_planner_data = _v2_loader.get_planner_config()
    state.model.worker_fallback_models = _v2_worker_data.fallback_models
    state.model.planner_fallback_models = _v2_planner_data.fallback_models

    # Step 2: Start MCP sessions
    exit_stack = AsyncExitStack()
    script_dir = Path(__file__).parent.resolve()
    dashboard_runner = None

    try:
        # RapidWright MCP server
        rapidwright_log = run_dir / "rapidwright.log"
        rapidwright_mcp_log = run_dir / "rapidwright-mcp.log"
        vivado_log = run_dir / "vivado.log"
        vivado_journal = run_dir / "vivado.jou"
        vivado_mcp_log = run_dir / "vivado-mcp.log"

        rw_log_file = open(rapidwright_mcp_log, 'w')
        exit_stack.callback(rw_log_file.close)
        v_log_file = open(vivado_mcp_log, 'w')
        exit_stack.callback(v_log_file.close)

        rapidwright_config = {
            "command": sys.executable,
            "args": [
                str(script_dir / "RapidWrightMCP" / "server.py"),
                "--java-log", str(rapidwright_log),
                "--mcp-log", str(rapidwright_mcp_log),
            ],
            "cwd": str(run_dir),
            "env": {**os.environ},
        }
        vivado_config = {
            "command": sys.executable,
            "args": [
                str(script_dir / "VivadoMCP" / "vivado_mcp_server.py"),
                "--vivado-log", str(vivado_log),
                "--vivado-journal", str(vivado_journal),
            ],
            "cwd": str(run_dir),
            "env": {**os.environ},
        }

        logger.info("[optimize_v2] Starting RapidWright MCP server...")
        rw_params = StdioServerParameters(**rapidwright_config)
        rw_transport = await exit_stack.enter_async_context(
            stdio_client(rw_params, errlog=rw_log_file)
        )
        rw_read, rw_write = rw_transport
        rapidwright_session = await exit_stack.enter_async_context(
            ClientSession(rw_read, rw_write)
        )
        await rapidwright_session.initialize()
        logger.info("[optimize_v2] RapidWright MCP connected")

        logger.info("[optimize_v2] Starting Vivado MCP server...")
        vivado_params = StdioServerParameters(**vivado_config)
        vivado_transport = await exit_stack.enter_async_context(
            stdio_client(vivado_params, errlog=v_log_file)
        )
        v_read, v_write = vivado_transport
        vivado_session = await exit_stack.enter_async_context(
            ClientSession(v_read, v_write)
        )
        await vivado_session.initialize()
        logger.info("[optimize_v2] Vivado MCP connected")

        # Step 3: Collect tool definitions
        tools = []
        rw_response = await rapidwright_session.list_tools()
        for tool in rw_response.tools:
            tools.append(convert_mcp_tool_to_openai(tool, "rapidwright"))
        v_response = await vivado_session.list_tools()
        for tool in v_response.tools:
            tools.append(convert_mcp_tool_to_openai(tool, "vivado"))

        # Internal tool: retrieve raw tool output
        tools.append({
            "type": "function",
            "function": {
                "name": "vivado_get_raw_tool_output",
                "description": (
                    "Retrieve the complete raw Vivado output for a previous tool call. "
                    "By default tool results are returned as structured summaries; "
                    "use this when you need to inspect raw timing paths, DRC details, or error messages."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "iteration": {"type": "integer", "description": "Iteration number (default: current iteration)"},
                        "round_index": {"type": "integer", "description": "Tool round within the iteration (default: most recent)"},
                        "tool_name": {"type": "string", "description": "Filter by tool name, e.g. vivado_phys_opt_design (optional)"},
                    },
                },
                "strict": False,
            },
        })

        # Internal tool: cached high fanout nets
        tools.append({
            "type": "function",
            "function": {
                "name": "vivado_get_cached_high_fanout_nets",
                "description": (
                    "Retrieve cached high-fanout nets from initial analysis "
                    "(no Vivado call, no truncation risk). "
                    "Use this when vivado_get_critical_high_fanout_nets output is "
                    "truncated or incomplete. "
                    "NOTE: This data is also available in Dashboard Module 4 "
                    "(Netlist Quality) — avoid repeated calls; the cache does not "
                    "change within an iteration."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_nets": {"type": "integer", "description": "Maximum number of nets to return (0 = return all)"},
                        "min_fanout": {"type": "integer", "description": "Minimum fanout threshold to filter (optional)"},
                    },
                },
                "strict": False,
            },
        })

        # Internal tool: report_step_state
        tools.append({
            "type": "function",
            "function": {
                "name": "report_step_state",
                "description": (
                    "REQUIRED in every response. Reports process control state "
                    "(replaces old step: YAML block). Call ALONGSIDE other tool "
                    "calls, or alone if making no other calls."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "step_id": {"type": "integer", "description": "Incrementing per message in current strategy"},
                        "result_status": {
                            "type": "string",
                            "enum": ["SUCCESS", "PARTIAL", "FAIL"],
                            "description": "SUCCESS: WNS improved; PARTIAL: executed but insufficient; FAIL: regression or tool error",
                        },
                        "flow_control": {
                            "type": "string",
                            "enum": ["ANALYZE_DONE", "EXEC_DONE", "CONTINUE", "NEXT_ITERATION", "SWITCH_STRATEGY", "DONE", "ROLLBACK", "EXHAUSTED"],
                            "description": (
                                "ANALYZE_DONE: analysis phase complete, move to strategy selection; "
                                "EXEC_DONE: execution phase complete, move to evaluation; "
                                "CONTINUE: continue in current phase; "
                                "NEXT_ITERATION: significant improvement, diminishing returns, end iteration; "
                                "SWITCH_STRATEGY: strategy failed or try another strategy; "
                                "DONE: WNS>=0 achieved (ONLY when WNS>=0); "
                                "ROLLBACK: revert to best checkpoint; "
                                "EXHAUSTED: all strategies tried, no further improvement possible"
                            ),
                        },
                        "strategy_phase": {
                            "type": "string",
                            "enum": ["ANALYZE", "SELECT_STRATEGY", "EXECUTE_STRATEGY", "EVALUATE"],
                            "description": (
                                "Current phase in the 4-phase strategy lifecycle: "
                                "ANALYZE (gather timing data), "
                                "SELECT_STRATEGY (choose optimization strategy), "
                                "EXECUTE_STRATEGY (run strategy tools), "
                                "EVALUATE (check WNS delta, assess outcome)"
                            ),
                        },
                        "strategy_name": {
                            "type": "string",
                            "enum": ["PBLOCK", "PhysOpt", "Fanout", "PinSwap", "LUTCascade",
                                     "CellReplication", "CongestionSpreading", "RegisterRetiming", "NetSwap"],
                            "description": "The strategy being executed in this step",
                        },
                    },
                    "required": ["step_id", "result_status", "flow_control"],
                },
                "strict": False,
            },
        })

        logger.info(f"[optimize_v2] Collected {len(tools)} tool definitions")

        # Step 4: Initialize MemoryManager + Compat
        event_bus = EventBus()
        memory_manager = MemoryManager(event_bus=event_bus)
        compat = DCPOptimizerCompat(memory_manager)
        logger.info("[optimize_v2] MemoryManager + Compat initialized")

        # Step 5: Load system prompt and inject initial messages
        system_prompt_template = load_system_prompt()
        system_prompt = system_prompt_template.format(
            temp_dir=str(run_dir),
            input_dcp=str(input_dcp.resolve()),
        )
        compat.add_message("system", system_prompt)
        logger.info("[optimize_v2] System prompt injected")

        # Step 6: Create OpenAI client
        openai_client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=300.0,
        )

        # Step 7: Assemble NodeDeps
        prompt_logger = PromptLogger.get_instance()
        prompt_logger.setup(str(run_dir))

        from optimizer.llm_call_logger import LLMCallLogger
        llm_call_logger = LLMCallLogger()
        llm_call_logger.setup(str(run_dir))

        deps = NodeDeps(
            openai_client=openai_client,
            memory_manager=memory_manager,
            compat=compat,
            rapidwright_session=rapidwright_session,
            vivado_session=vivado_session,
            tools=tools,
            event_bus=event_bus,
            prompt_logger=prompt_logger,
            llm_call_logger=llm_call_logger,
            system_prompt=system_prompt,
            model_planner=model_planner,
            model_worker=model_worker,
            api_key=api_key,
            reasoning_config={
                "worker": {
                    "enabled": _v2_worker_data.reasoning_enabled,
                    "max_output_tokens": _v2_worker_data.reasoning_max_output_tokens,
                },
                "planner": {
                    "enabled": _v2_planner_data.reasoning_enabled,
                    "max_output_tokens": _v2_planner_data.reasoning_max_output_tokens,
                },
            },
        )

        # Step 8: Build and run graph
        if dashboard:
            from dashboard import DashboardStateTracer, start_dashboard
            tracer = DashboardStateTracer()
            dashboard_runner = await start_dashboard(state, tracer, port=dashboard_port)
            logger.info(f"[optimize_v2] Dashboard at http://localhost:{dashboard_port}")
        else:
            tracer = StateTracer()
        deps.tracer = tracer
        llm_call_logger.set_tracer(tracer)
        graph = build_optimizer_graph(tracer=tracer)

        # Start stdin listener for graceful quit
        def _v2_stdin_reader():
            try:
                for line in sys.stdin:
                    if line.strip().lower() == "quit":
                        logger.info("[optimize_v2] Quit requested by user")
                        state.control.user_exit_requested = True
                        break
            except (EOFError, OSError):
                pass

        threading.Thread(target=_v2_stdin_reader, daemon=True).start()

        final_state = await graph.run(state, deps, entry="init_analysis")

        # Export tracing
        if final_state.control.run_dir:
            trace_path = str(final_state.control.run_dir / "state_transitions.json")
            tracer.export(trace_path)

        converged = (
            final_state.timing.best_wns is not None
            and final_state.timing.best_wns >= 0.0
        )
        logger.info(
            f"[optimize_v2] Result: "
            f"best_wns={final_state.timing.best_wns:.3f}ns, "
            f"converged={converged}"
        )
        print(f"\n[optimize_v2] Result: best_wns={final_state.timing.best_wns:.3f}ns, converged={converged}")
        return converged

    except Exception as e:
        logger.exception(f"[optimize_v2] Fatal error: {e}")
        raise

    finally:
        if dashboard_runner:
            await dashboard_runner.cleanup()
            logger.info("[optimize_v2] Dashboard stopped")
        await exit_stack.aclose()
        logger.info("[optimize_v2] MCP sessions closed")


# === CLI Entry Point ===


async def main():
    parser = argparse.ArgumentParser(
        description="FPGA Design Optimization Agent (V2 State Machine)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dcp_optimizer.py input.dcp
  python dcp_optimizer.py input.dcp --output output.dcp
  python dcp_optimizer.py input.dcp --debug
  python dcp_optimizer.py input.dcp --dashboard
  python dcp_optimizer.py input.dcp --test-v2
  python dcp_optimizer.py input.dcp --test-v2-only-skills
  python dcp_optimizer.py input.dcp --test-init-analysis
        """
    )
    parser.add_argument("input_dcp", type=Path, help="Input design checkpoint (.dcp)")
    parser.add_argument(
        "--output", "-o",
        type=Path, dest="output_dcp",
        help="Output optimized checkpoint (.dcp). Default: <input_name>_optimized-<timestamp>.dcp"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("OPENROUTER_API_KEY"),
        help="OpenRouter API key (default: OPENROUTER_API_KEY env var)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL_PLANNER,
        help=f"Planner LLM model (default: {DEFAULT_MODEL_PLANNER})"
    )
    parser.add_argument(
        "--model-worker",
        type=str,
        default=DEFAULT_MODEL_WORKER,
        help=f"Worker LLM model (default: {DEFAULT_MODEL_WORKER})"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode (verbose logging)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="Wall-clock timeout in seconds (default: 3600)"
    )
    parser.add_argument(
        "--test-v2",
        action="store_true",
        help="Run V2 test mode: validate MCP tools and skills without LLM."
    )
    parser.add_argument(
        "--test-v2-only-skills",
        action="store_true",
        help="Run V2 skill-only test: quick validation without place/route."
    )
    parser.add_argument(
        "--test-init-analysis",
        action="store_true",
        help="Run init analysis only (no LLM): extract design data and verify dashboard."
    )
    parser.add_argument(
        "--max-nets",
        type=int,
        default=5,
        help="Maximum number of high fanout nets in test mode (default: 5)"
    )
    parser.add_argument(
        "--skip-skills",
        action="store_true",
        help="Skip skill invocation tests in test mode"
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Enable web dashboard for real-time state monitoring."
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
        help="Dashboard HTTP port (default: 8080)."
    )

    args = parser.parse_args()

    if not args.input_dcp.exists():
        print(f"Error: Input file not found: {args.input_dcp}", file=sys.stderr)
        sys.exit(1)

    if args.output_dcp is None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        input_stem = args.input_dcp.stem
        input_dir = args.input_dcp.parent
        args.output_dcp = input_dir / f"{input_stem}_optimized-{timestamp}.dcp"

    if args.debug:
        DynamicLogLevelManager().set_level("root", "DEBUG")

    args.output_dcp.parent.mkdir(parents=True, exist_ok=True)

    # V2 test modes (no LLM / API key needed)
    if args.test_v2 or args.test_v2_only_skills or args.test_init_analysis:
        from optimizer.test_mode import run_v2_test_mode

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = Path.cwd() / f"dcp_optimizer_run-{timestamp}"

        mode_label = "INIT-ANALYSIS ONLY" if args.test_init_analysis else (
            "SKILLS-ONLY" if args.test_v2_only_skills else "V2 TEST MODE"
        )
        print(f"FPGA Design Optimization - {mode_label}")
        print(f"=========================================")
        print(f"Input:       {args.input_dcp.resolve()}")
        print(f"Output:      {args.output_dcp.resolve()}")
        print(f"Run dir:     {run_dir}")
        print(f"Skills only: {args.test_v2_only_skills}")
        print()

        success = await run_v2_test_mode(
            input_dcp=args.input_dcp,
            output_dcp=args.output_dcp,
            debug=args.debug,
            max_nets=args.max_nets,
            skip_skills=args.skip_skills or args.test_v2_only_skills,
            skills_only=args.test_v2_only_skills,
            init_analysis_only=args.test_init_analysis,
        )
        sys.exit(0 if success else 1)

    # Normal mode - requires API key and LLM
    if not args.api_key:
        print("Error: OpenRouter API key required. Set OPENROUTER_API_KEY or use --api-key", file=sys.stderr)
        print("       Use --test-v2 to run test mode without LLM", file=sys.stderr)
        sys.exit(1)

    success = await optimize_v2(
        input_dcp=args.input_dcp,
        output_dcp=args.output_dcp,
        api_key=args.api_key,
        model_planner=args.model,
        model_worker=args.model_worker,
        wall_clock_timeout=args.timeout,
        debug=args.debug,
        dashboard=args.dashboard,
        dashboard_port=args.dashboard_port,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    print("Type 'quit' and press Enter to terminate the program.")
    asyncio.run(main())
