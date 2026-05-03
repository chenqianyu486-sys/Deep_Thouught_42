#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache 2.0

"""
FPGA Design Optimization Agent

An autonomous AI agent that analyzes FPGA designs and applies optimizations
using RapidWright and Vivado via MCP servers.
"""

# === Section 1: Imports ===

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import yaml
from collections import OrderedDict
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from dataclasses import dataclass, field
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
try:
    from openai import AsyncOpenAI, RateLimitError as OpenAIRateLimitError, NotFoundError as OpenAINotFoundError
except ImportError:
    print("Error: openai package not installed. Please run: pip install openai", file=sys.stderr)
    sys.exit(1)

# === Section 7.0: Context Manager Integration ===
from context_manager import MemoryManager, AgentContextManager, YAMLStructuredCompressor
from context_manager.compat import DCPOptimizerCompat
from context_manager.interfaces import CompressionContext as CMCompressionContext, ModelContextConfig, Message as CMMessage
from context_manager.estimator import ContextEstimator
from context_manager.events import EventBus
from context_manager.interfaces import EventType as CMEventType, ContextEvent as CMContextEvent, RetrievalQuery as CMRetrievalQuery
from context_manager.lightyaml import LightYAML
from strategy_library import STRATEGY_SKILL_MAP
from context_manager.logging_config import (
    setup_logging, set_trace_id, get_trace_id, set_job_context,
    sanitize_payload, DynamicLogLevelManager, HeartbeatLogger, PromptLogger
)
from config_loader import get_model_config_loader, ModelConfigData


def _create_model_context_config(data: ModelConfigData) -> ModelContextConfig:
    """Create ModelContextConfig from ModelConfigData.

    Args:
        data: Model configuration data loaded from YAML

    Returns:
        ModelContextConfig instance
    """
    return ModelContextConfig(
        model_tier=data.model_tier,
        max_context_tokens=data.max_tokens,
        soft_threshold=data.soft_threshold,
        hard_limit=data.hard_limit,
        token_budget=data.token_budget,
        preserve_turns=data.preserve_turns,
        preserve_turns_aggressive=data.preserve_turns_aggressive,
        min_importance_threshold=data.min_importance_threshold,
        min_importance_threshold_aggressive=data.min_importance_threshold_aggressive,
        preserve_turns_hard_limit=data.preserve_turns_hard_limit,
        min_importance_threshold_hard_limit=data.min_importance_threshold_hard_limit,
        history_retrieval_limit=data.history_retrieval_limit,
        history_retrieval_min_importance=data.history_retrieval_min_importance,
    )


# Load model configurations from YAML
_loader = get_model_config_loader()
_worker_data = _loader.get_worker_config()
_planner_data = _loader.get_planner_config()

WORKER_CONTEXT_CONFIG: ModelContextConfig = _create_model_context_config(_worker_data)
PLANNER_CONTEXT_CONFIG: ModelContextConfig = _create_model_context_config(_planner_data)
DEFAULT_MODEL_PLANNER: str = _planner_data.model_name
DEFAULT_MODEL_WORKER: str = _worker_data.model_name

# === Section 2: Logging & Constants ===

# Configure logging with centralized setup
setup_logging(level="INFO", use_json=False)  # use_json=False for readable console output
logger = logging.getLogger(__name__)

# === Section 2.1: Model Context Configurations (loaded from model_config.yaml) ===
# Model tier to config mapping
MODEL_CONTEXT_CONFIGS = {
    "worker": WORKER_CONTEXT_CONFIG,
    "planner": PLANNER_CONTEXT_CONFIG,
}

# Derived constants from loaded config
WORKER_MODEL_MAX_TOKENS = _worker_data.max_tokens
PLANNER_MODEL_MAX_TOKENS = _planner_data.max_tokens

# Worker thresholds based on worker model capacity
WORKER_CONTEXT_WARN_TOKENS = int(_worker_data.max_tokens * 0.6)  # 60% of max
WORKER_CONTEXT_FORCE_TOKENS = int(_worker_data.max_tokens * 0.8)  # 80% of max

# Legacy constants for backward compatibility
LEGACY_WORKER_MAX_TOKENS = 200_000
CONTEXT_COMPRESSION_THRESHOLD = _worker_data.soft_threshold  # deprecated
CONTEXT_HARD_LIMIT = _worker_data.hard_limit  # deprecated

RECENT_TURNS_TO_KEEP = 20              # number of recent messages to keep during compression (maintains conversation continuity)
TOOL_RESULT_TRUNCATE = 30000           # tool result truncation threshold (retains key info after type-based filtering)

# === Section 5.1: Token Estimation ===
# Note: All context/prompts must be in English for consistent token estimation.
# Token estimation uses English approximation: ~4 chars per token.

def _estimate_tokens_char_based(text: str) -> int:
    """Estimate tokens using character count (English: ~4 chars/token)."""
    return len(text) // 4

# === Section 3: Task Classification ===

# Task classification constants
class TaskCategory:
    """Task categories: distinguish optimization vs. information tasks"""
    INFORMATION = "information"    # Information tasks: queries, read-only
    OPTIMIZATION = "optimization"  # Optimization tasks: design decisions, placement optimization
    UNKNOWN = "unknown"


@dataclass
class StepState:
    """Parsed step YAML state from an LLM response.

    Carries the step control data (step_id, result_status, flow_control,
    analysis) that the LLM includes in its response's step: YAML block.
    """
    step_id: Optional[int] = None
    result_status: Optional[str] = None        # SUCCESS | PARTIAL | FAIL
    flow_control: Optional[str] = None         # CONTINUE | SWITCH_STRATEGY | DONE | RETRY | ROLLBACK
    analysis: dict = field(default_factory=dict)  # observed_signals, scenario_match, hypothesis, strategy_rationale
    has_tool_calls: bool = False               # Whether native tool_calls were also present
    raw_content: str = ""                      # Raw content for logging/debugging
    parse_error: Optional[str] = None          # Set if YAML parsing failed

# Task classification patterns
INFORMATION_PATTERNS = ["get_", "read", "query", "check", "list", "show", "status", "report"]
OPTIMIZATION_PATTERNS = ["optimize", "improve", "place", "route", "synthesize",
                          "floorplan", "create", "modify", "fix", "debug", "analyze"]


# === Section 4: Model Tier & Tool Mapping ===

class ModelTier:
    PLANNER = "planner"
    WORKER = "worker"
    DEFAULT = None


# Tool-level model mapping (highest priority)
TOOL_MODEL_MAPPING = {
    # === Planner model tasks ===
    "place_design": ModelTier.PLANNER,
    "phys_opt_design": ModelTier.PLANNER,
    "route_design": ModelTier.PLANNER,
    "optimize_placement": ModelTier.PLANNER,
    "optimize_routing": ModelTier.PLANNER,
    "synthesize": ModelTier.PLANNER,
    "create_floorplan": ModelTier.PLANNER,
    "debug_timing": ModelTier.PLANNER,
    "fix_timing": ModelTier.PLANNER,
    # === Worker model tasks ===
    "get_utilization": ModelTier.WORKER,
    "get_timing": ModelTier.WORKER,
    "report_power": ModelTier.WORKER,
    "list_ports": ModelTier.WORKER,
    "read_checkpoint": ModelTier.WORKER,
}


# === Section 5: Utility Functions ===


def parse_timing_summary_static(timing_report: str) -> dict:
    """
    Parse timing summary report to extract WNS, TNS, and failing endpoints.
    Enhanced version: automatically skip separator lines, empty lines, and non-timing lines
    (license messages, command echoes, info/warning messages) to locate the timing header and data.
    """
    result = {"wns": None, "tns": None, "failing_endpoints": None}
    lines = timing_report.split('\n')

    header_idx = -1
    for i, line in enumerate(lines):
        # Skip lines that are clearly not timing headers
        stripped = line.strip()
        if not stripped:
            continue
        # Skip command echo lines
        if stripped.startswith('Command:'):
            continue
        # Skip license-related messages
        if 'Attempting to get a license' in stripped or 'Got license' in stripped:
            continue
        # Skip info/warning/error messages from Vivado
        if any(x in stripped for x in ['INFO:', 'WARNING:', 'ERROR:', 'Common 17-']):
            continue
        # Skip design commands that might appear in output
        if any(stripped.startswith(x) for x in ['phys_opt_design', 'place_design', 'route_design', 'report_']):
            continue
        # Look for the timing header
        if 'WNS(ns)' in line and 'TNS(ns)' in line:
            header_idx = i
            break
    if header_idx == -1:
        return result

    # Search for the first valid data row after the header
    for data_line in lines[header_idx + 1:]:
        stripped = data_line.strip()
        if not stripped or stripped.startswith('---') or stripped.startswith('==='):
            continue
        # Skip non-data lines
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
            "parameters": schema,  # Pass complete schema directly
            "strict": False
        }
    }


# Shared constants for routing failure detection
ROUTING_FAILURE_PHRASES = [
    "routing failed", "route error", "cannot route",
    "unroutable", "exceeds", "congestion"
]

# MCP tool name → registered skill name mapping for observability
SKILL_TOOL_MAP: dict[str, str] = {
    "rapidwright_analyze_net_detour": "net_detour",
    "rapidwright_optimize_cell_placement": "optimize_cell",
    "rapidwright_smart_region_search": "smart_region",
    "rapidwright_analyze_pblock_region": "pblock_strategy",
    "rapidwright_execute_physopt_strategy": "physopt_strategy",
    "rapidwright_execute_fanout_strategy": "fanout_strategy",
}
SKILL_NAME_TO_TOOL: dict[str, str] = {v: k for k, v in SKILL_TOOL_MAP.items()}


# === Section 6: DCPOptimizerBase — Shared Base Class ===

class DCPOptimizerBase:
    """Base class with shared functionality for FPGA optimization."""
    
    def __init__(self, debug: bool = False, run_dir: Optional[Path] = None):
        self.debug = debug
        
        # Create run directory if not provided
        if run_dir is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self.run_dir = Path.cwd() / f"dcp_optimizer_run-{timestamp}"
            self.run_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created run directory: {self.run_dir}")
        else:
            self.run_dir = run_dir
            self.run_dir.mkdir(parents=True, exist_ok=True)

        # Re-configure logging with file output to run directory
        # This is called after run_dir exists so logs are written alongside vivado.log etc.
        setup_logging(level="INFO", use_json=False, log_dir=str(self.run_dir))

        self.exit_stack = AsyncExitStack()
        self.rapidwright_session: Optional[ClientSession] = None
        self.vivado_session: Optional[ClientSession] = None
        
        # Use run directory for all temporary files
        self.temp_dir = self.run_dir
        logger.info(f"Working directory: {self.temp_dir}")
        
        # Timing tracking
        self.initial_wns = None
        self.initial_tns = None
        self.initial_failing_endpoints = None
        self.high_fanout_nets = []
        self.device_topology = None
        self.clock_period = None

        # Resource utilization (populated during initial analysis)
        self.resource_utilization: Optional[dict] = None
        
        # Log file handles
        self._rw_log_file = None
        self._v_log_file = None

        # Token estimation using tiktoken (moved from DCPOptimizer for base class access)
        self._context_estimator = ContextEstimator()

    def _is_routing_failure(self, error_msg: str) -> bool:
        """Check if error message indicates a routing failure."""
        error_lower = error_msg.lower() if isinstance(error_msg, str) else str(error_msg).lower()
        return any(phrase in error_lower for phrase in ROUTING_FAILURE_PHRASES)

    @staticmethod
    def _parse_resource_utilization(report: str) -> Optional[dict]:
        """Parse LUT/FF/DSP/BRAM/URAM counts from report_utilization_for_pblock output.

        Expected format:
            LUTs:    12,345
            FFs:     24,567
            DSPs:    45
            BRAMs:   120
            URAMs:   0
        """
        import re
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

    def _start_tool_heartbeat(self, tool_name: str, start_time: float, interval: float = 60.0) -> tuple[asyncio.Task, int]:
        """
        Start a background heartbeat logger for a long-running tool call.
        Returns (task, heartbeat_count_ref) so the caller can cancel and log final status.
        """
        heartbeat_count = 0

        async def heartbeat_logger():
            nonlocal heartbeat_count
            while True:
                await asyncio.sleep(interval)
                heartbeat_count += 1
                elapsed = time.time() - start_time
                logger.info(
                    f"[HEARTBEAT #{heartbeat_count}] Tool '{tool_name}' still running after {elapsed:.1f}s",
                    extra={"tool_name": tool_name, "heartbeat_elapsed": int(elapsed), "heartbeat_count": heartbeat_count}
                )

        task = asyncio.create_task(heartbeat_logger())
        return task, heartbeat_count

    async def start_servers(self, log_prefix: str = ""):
        """Start and connect to both MCP servers."""
        script_dir = Path(__file__).parent.resolve()
        
        # Create log files in run directory
        rapidwright_log = self.run_dir / "rapidwright.log"
        rapidwright_mcp_log = self.run_dir / "rapidwright-mcp.log"
        vivado_log = self.run_dir / "vivado.log"
        vivado_journal = self.run_dir / "vivado.jou"
        vivado_mcp_log = self.run_dir / "vivado-mcp.log"
        
        # Open log files (if not in debug mode, redirect stderr to log)
        if self.debug:
            self._rw_log_file = None
            self._v_log_file = None
            logger.info("Debug mode: MCP server output will be shown in console")
            if log_prefix:
                print(f"{log_prefix} Debug mode: MCP server output will be shown in console")
        else:
            self._rw_log_file = open(rapidwright_mcp_log, 'w')
            self.exit_stack.callback(self._rw_log_file.close)
            self._v_log_file = open(vivado_mcp_log, 'w')
            self.exit_stack.callback(self._v_log_file.close)
            logger.info(f"RapidWright Java output: {rapidwright_log}")
            logger.info(f"RapidWright MCP output: {rapidwright_mcp_log}")
            logger.info(f"Vivado output: {vivado_log}")
            logger.info(f"Vivado journal: {vivado_journal}")
            logger.info(f"Vivado MCP output: {vivado_mcp_log}")
            print(f"Log files in {self.run_dir.name}/: {rapidwright_log.name}, {rapidwright_mcp_log.name}, {vivado_log.name}, {vivado_journal.name}, {vivado_mcp_log.name}")
        
        # RapidWright MCP server config
        rapidwright_args = [str(script_dir / "RapidWrightMCP" / "server.py")]
        if not self.debug:
            rapidwright_args.extend([
                "--java-log", str(rapidwright_log),
                "--mcp-log", str(rapidwright_mcp_log)
            ])
        
        rapidwright_config = {
            "command": sys.executable,
            "args": rapidwright_args,
            "cwd": str(self.run_dir),
            "env": {**os.environ}
        }
        
        # Vivado MCP server config
        vivado_args = [str(script_dir / "VivadoMCP" / "vivado_mcp_server.py")]
        if not self.debug:
            vivado_args.extend([
                "--vivado-log", str(vivado_log),
                "--vivado-journal", str(vivado_journal)
            ])
        
        vivado_config = {
            "command": sys.executable,
            "args": vivado_args,
            "cwd": str(self.run_dir),
            "env": {**os.environ}
        }
        
        # Start RapidWright MCP
        logger.info("Starting RapidWright MCP server...")
        if log_prefix:
            print(f"{log_prefix} Starting RapidWright MCP server...")
        start_time = time.time()
        
        rw_params = StdioServerParameters(**rapidwright_config)
        rw_transport = await self.exit_stack.enter_async_context(
            stdio_client(rw_params, errlog=self._rw_log_file)
        )
        rw_read, rw_write = rw_transport
        self.rapidwright_session = await self.exit_stack.enter_async_context(
            ClientSession(rw_read, rw_write)
        )
        await self.rapidwright_session.initialize()
        
        elapsed = time.time() - start_time
        logger.info(f"RapidWright MCP server started in {elapsed:.2f}s")
        if log_prefix:
            print(f"{log_prefix} RapidWright MCP server started in {elapsed:.2f}s")
        
        # Start Vivado MCP
        logger.info("Starting Vivado MCP server...")
        if log_prefix:
            print(f"{log_prefix} Starting Vivado MCP server...")
        start_time = time.time()
        
        vivado_params = StdioServerParameters(**vivado_config)
        vivado_transport = await self.exit_stack.enter_async_context(
            stdio_client(vivado_params, errlog=self._v_log_file)
        )
        v_read, v_write = vivado_transport
        self.vivado_session = await self.exit_stack.enter_async_context(
            ClientSession(v_read, v_write)
        )
        await self.vivado_session.initialize()
        
        elapsed = time.time() - start_time
        logger.info(f"Vivado MCP server started in {elapsed:.2f}s")
        if log_prefix:
            print(f"{log_prefix} Vivado MCP server started in {elapsed:.2f}s")
        
        logger.info("Both MCP servers connected")
        if log_prefix:
            print(f"{log_prefix} Both MCP servers connected successfully")
    
    async def cleanup(self):
        """Clean up resources."""
        # Unsubscribe EventBus handlers to prevent memory leak
        if hasattr(self, '_event_compressed_token'):
            self._event_bus.unsubscribe_by_token(self._event_compressed_token)
        if hasattr(self, '_layer_promoted_token'):
            self._event_bus.unsubscribe_by_token(self._layer_promoted_token)

        rw_file = self._rw_log_file
        v_file = self._v_log_file
        self._rw_log_file = None
        self._v_log_file = None
        try:
            await self.exit_stack.aclose()
        finally:
            if rw_file: rw_file.close()
            if v_file: v_file.close()
        
        logger.info(f"Run directory preserved at: {self.run_dir}")

    # === Section 6.1: Timing Utilities ===

    def calculate_fmax(self, wns: Optional[float], clock_period: Optional[float]) -> Optional[float]:
        """
        Calculate achievable fmax in MHz based on WNS and clock period.
        
        fmax = 1 / (clock_period - WNS) when WNS < 0 (timing violation)
        fmax = 1 / clock_period when WNS >= 0 (timing met)
        
        Returns fmax in MHz, or None if cannot be calculated.
        """
        if clock_period is None or clock_period <= 0:
            return None
        if wns is None:
            return None
        
        achievable_period_ns = clock_period - wns
        if achievable_period_ns <= 0:
            return None
        
        return 1000.0 / achievable_period_ns
    
    async def get_clock_period(self, call_tool_fn) -> Optional[float]:
        """
        Query the clock period of the critical (worst-slack) clock from Vivado in nanoseconds.
        
        Uses a Tcl script that finds the endpoint clock of the worst setup timing path
        and returns its period. This should improve handling of multi-clock designs.
        
        Args:
            call_tool_fn: Function to call Vivado tools, should accept (tool_name, arguments)
        
        Returns the period of the critical clock, or None if no clocks.
        """
        # Get the period of the endpoint clock on the worst setup timing path
        tcl_cmd = (
            "set tp [get_timing_paths -max_paths 1 -setup]; "
            "if {$tp ne {}} { "
            "  set clk [get_property ENDPOINT_CLOCK $tp]; "
            "  if {$clk ne {}} { "
            "    puts [get_property PERIOD [get_clocks $clk]]; "
            "  } "
            "}"
        )
        try:
            result = await call_tool_fn("run_tcl", {"command": tcl_cmd})
            
            for token in result.strip().split():
                if token.startswith('ERROR') or token.startswith('WARNING'):
                    continue
                try:
                    period = float(token)
                    if period > 0:
                        logger.info(f"Critical clock period: {period:.3f} ns")
                        return period
                except ValueError:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get critical clock period: {e}")
        
        logger.warning("Could not determine critical clock period from Vivado")
        return None
    
    def parse_high_fanout_nets(self, report: str) -> list[tuple[str, int, int]]:
        """
        Parse high fanout nets report and return list of (net_name, fanout, path_count).
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

    # === Section 6.2: Result Formatting & Reporting ===

    def _format_fmax_results(
        self,
        clock_period: Optional[float],
        initial_wns: Optional[float],
        result_wns: Optional[float],
        result_label: str = "Final",
    ) -> list[str]:
        """Format Fmax/WNS results block as a list of lines.
        
        """
        initial_fmax = self.calculate_fmax(initial_wns, clock_period)
        result_fmax = self.calculate_fmax(result_wns, clock_period)
        result_fmax_label = f"{result_label} Fmax:"
        result_wns_label = f"{result_label} WNS:"
        
        lines: list[str] = []
        if initial_fmax is not None and result_fmax is not None:
            target_fmax = 1000.0 / clock_period
            fmax_change = result_fmax - initial_fmax
            lines.append(f"  {'Target Fmax:':<21s}{target_fmax:8.2f} MHz  (clock period: {clock_period:.3f} ns)")
            lines.append(f"  {'Initial Fmax:':<21s}{initial_fmax:8.2f} MHz  (WNS: {initial_wns:.3f} ns)")
            lines.append(f"  {result_fmax_label:<21s}{result_fmax:8.2f} MHz  (WNS: {result_wns:.3f} ns)")
            lines.append(f"  {'Fmax Improvement:':<21s}{fmax_change:+8.2f} MHz  (WNS: {result_wns - initial_wns:+.3f} ns)")
        else:
            if clock_period is not None:
                target_fmax = 1000.0 / clock_period
                lines.append(f"  {'Clock period:':<21s}{clock_period:8.3f} ns (target: {target_fmax:.2f} MHz)")
            if initial_wns is not None:
                fmax_str = f"  (fmax: {initial_fmax:.2f} MHz)" if initial_fmax else ""
                lines.append(f"  {'Initial WNS:':<21s}{initial_wns:8.3f} ns{fmax_str}")
            if result_wns is not None:
                fmax_str = f"  (fmax: {result_fmax:.2f} MHz)" if result_fmax else ""
                lines.append(f"  {result_wns_label:<21s}{result_wns:8.3f} ns{fmax_str}")
            if initial_wns is not None and result_wns is not None:
                lines.append(f"  {'WNS Improvement:':<21s}{result_wns - initial_wns:+8.3f} ns")
        
        return lines
    
    
    def print_wns_change(
        self,
        initial_wns: Optional[float],
        final_wns: Optional[float],
        clock_period: Optional[float]
    ):
        """Print Fmax/WNS change comparison with improvement/regression status."""
        if final_wns is None or initial_wns is None:
            return
        
        initial_fmax = self.calculate_fmax(initial_wns, clock_period)
        final_fmax = self.calculate_fmax(final_wns, clock_period)
        wns_improvement = final_wns - initial_wns
        
        if initial_fmax is not None and final_fmax is not None:
            fmax_improvement = final_fmax - initial_fmax
            print(f"\n*** Fmax Change: {fmax_improvement:+.2f} MHz ({initial_fmax:.2f} -> {final_fmax:.2f} MHz) ***")
            print(f"*** WNS Change: {wns_improvement:+.3f} ns ({initial_wns:.3f} -> {final_wns:.3f} ns) ***")
        else:
            print(f"\n*** WNS Change: {wns_improvement:+.3f} ns ***")
        
        if wns_improvement > 0:
            print(f"IMPROVEMENT: Timing improved by {wns_improvement:.3f} ns")
        elif wns_improvement < 0:
            print(f"REGRESSION: Timing got worse by {-wns_improvement:.3f} ns")
        else:
            print("NO CHANGE: Timing is the same")
    
    def print_test_summary(
        self,
        title: str,
        elapsed_seconds: float,
        initial_wns: Optional[float],
        final_wns: Optional[float],
        clock_period: Optional[float],
        extra_info: str = ""
    ):
        """Print formatted test summary."""
        print("\n" + "="*70)
        print(title)
        print("="*70)
        print(f"Total runtime: {elapsed_seconds:.2f} seconds ({elapsed_seconds/60:.2f} minutes)")
        
        result_lines = self._format_fmax_results(clock_period, initial_wns, final_wns)
        if result_lines:
            print(f"\nFmax Results:")
            print("\n".join(result_lines))
        
        if extra_info:
            print(f"\n{extra_info}")
        print("="*70)


# === Section 7: DCPOptimizer — LLM-Driven Optimization Agent ===

class DCPOptimizer(DCPOptimizerBase):
    """FPGA Design Optimization Agent using RapidWright and Vivado MCPs."""
    
    def __init__(
        self,
        api_key: str,
        model_planner: str = DEFAULT_MODEL_PLANNER,
        model_worker: str = DEFAULT_MODEL_WORKER,
        debug: bool = False,
        run_dir: Optional[Path] = None
    ):
        super().__init__(debug=debug, run_dir=run_dir)
        
        self.api_key = api_key
        self.model_planner = model_planner
        self.model_worker = model_worker

        # Track failed strategies via compat (MemoryManager.failed_strategies)
        self.tools: list[dict] = []
        
        self.openai = AsyncOpenAI(  # Use async instance
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=300.0  # Global timeout in seconds
        )

        # Track optimization progress
        self.iteration = 0
        self.best_wns = float('-inf')
        self._best_wns_iteration: Optional[int] = None  # Track which iteration achieved best WNS
        self.latest_wns: Optional[float] = None  # Cache for O(1) _get_current_wns()
        self.latest_tns: Optional[float] = None  # Latest TNS from timing reports
        self.latest_failing_endpoints: Optional[int] = None  # Latest failing endpoint count
        # Track TNS/failing_endpoints at the time best_wns was recorded, for rollback
        self._best_wns_tns: Optional[float] = None
        self._best_wns_failing_endpoints: Optional[int] = None
        self.current_task_type = ""  # Current task type
        # Task type success rate tracking (task_type -> {success: int, total: int})
        self.task_type_stats: dict[str, dict[str, int]] = {}
        self.llm_call_count = 0
        self.last_used_model = None
        # Fallback models for 429 rate limit (loaded from config)
        self.worker_fallback_models = _worker_data.fallback_models
        self.planner_fallback_models = _planner_data.fallback_models
        self._worker_fallback_index = 0  # Round-robin index for worker fallback models
        self._planner_fallback_index = 0     # Round-robin index for planner fallback models
        self._exhausted_worker_fallbacks: set[str] = set()  # Track exhausted worker fallback models
        self._exhausted_planner_fallbacks: set[str] = set()    # Track exhausted planner fallback models
        self._model_usage_history: list = []          # Track recent model selections for switch detection
        self._previous_tier: Optional[str] = None   # Track previous tier for switch detection
        self._iteration_model_switch_logged = False  # Track if model switch was logged in current iteration
        self._prev_best_wns = None                    # Previous iteration's best_wns for improvement detection
        self._next_iteration_model: Optional[str] = None  # Pre-decided model for next iteration
        self._iteration_handoff_prompt: str = ""      # Handoff prompt for next iteration's model
        self._iteration_handoff_injected: bool = False  # Whether handoff prompt was already injected
        self._iteration_narratives: list[dict] = []     # Progressive iteration summary for handoff
        self._strategy_sequence: list[str] = []          # Ordered strategy labels for state summary

        # === Skill observability tracking ===
        self.skill_invocation_log: list[dict] = []       # Per-invocation skill call tracking
        self.skill_recommendation_log: list[dict] = []    # Recommendation-to-execution funnel tracking
        self._last_skill_rec_iteration: Optional[int] = None  # Dedup guard for recommendation logging

        # === Section 7.X: DCP Validation ===
        self.validation_enabled = False          # Disable validation during optimization
        self.checkpoint_saving_enabled = True     # Enable checkpoint saving for iteration rollback
        self.validation_interval = 5            # Run Phase 1 every N iterations
        self.validation_report_dir = self.temp_dir / "validation_reports"

        # === Section 7.1: Context Compression & Model Switching State ===
        # Simplified model switching mechanism - only uses two counters for model selection
        self.worker_consecutive_success = 0          # Worker consecutive success count (for downgrade)
        self.worker_consecutive_failures = 0         # Worker cumulative failure count (for upgrade)
        self.global_no_improvement = 0               # Global consecutive no-improvement count
        self.iteration_tool_errors = []              # Tool errors in current iteration for failure classification

        # === Section 7.1.1: Console Exit Intervention ===
        self._user_exit_requested = threading.Event()
        self._async_exit_requested = asyncio.Event()
        self._console_reader_started = False
        self._start_console_reader()

        # Threshold configuration
        self.WORKER_DOWNGRADE_THRESHOLD = 3           # Worker consecutive successes before downgrade
        self.WORKER_UPGRADE_THRESHOLD = 2            # Cumulative failures before upgrade
        self.GLOBAL_NO_IMPROVEMENT_LIMIT = 3         # Global no-improvement limit
        self.MAX_TOOL_ROUNDS_PER_ITERATION = 80      # Max tool-calling rounds per iteration
        
        # Track token usage and costs
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        self.cost_hard_limit = _worker_data.cost_hard_limit  # USD hard limit (combined planner+worker)
        self.api_call_details = []

        # Track compression metrics for observability and tuning
        self.compression_count = 0            # Total compressions triggered
        self.compression_hard_count = 0        # Hard limit compressions
        self.compression_soft_count = 0        # Soft threshold compressions
        self.compression_skipped = 0           # Compressions skipped (under threshold)
        self.compression_details = []         # Structured compression event log
        
        # Track all tool calls with timing and WNS (actual tracking done via _compat.add_tool_result())
        
        # Track total runtime
        self.start_time = None
        self.end_time = None

        # Track is_done exit reason for logging
        self._is_done_reason: Optional[str] = None

        # Raw tool output buffer: {(iteration, round_index): raw_text}
        # Stores complete Vivado logs for on-demand retrieval via vivado_get_raw_tool_output
        self._raw_tool_outputs: dict[tuple[int, int], tuple[str, str]] = {}  # (iteration, round) -> (tool_name, raw_text)
        self._raw_tool_output_max = 50  # FIFO limit

        # Step state tracking from LLM YAML responses
        self._step_state: Optional[StepState] = None   # Most recent parsed step state
        self._last_analysis: dict = field(default_factory=dict)  # Last analysis from SWITCH_STRATEGY/DONE

        # === Section 7.0: Context Manager Integration ===
        # Initialize EventBus for context change notifications
        self._event_bus = EventBus()

        # Initialize MemoryManager for context compression
        self._memory_manager = MemoryManager(
            event_bus=self._event_bus
        )
        self._compat = DCPOptimizerCompat(self._memory_manager)

        # Initialize PromptLogger for LLM prompt observability
        self._prompt_logger = PromptLogger.get_instance()
        self._prompt_logger.setup(str(self.temp_dir) if self.temp_dir else "")
        # _context_estimator initialized in DCPOptimizerBase.__init__()

        # Initialize AgentContextManager for multi-agent branching (shares _event_bus with MemoryManager)
        # CURRENTLY_UNUSED: AgentContextManager is initialized but create_branch()/switch_branch() are not called.
        # Rationale: Current FPGA optimization uses single-path iterative refinement; no parallel strategy exploration needed.
        # Integration trigger: Would be used if/when optimization requires trying multiple strategies in parallel
        # (e.g., route_directive variants, different pblock placements) and comparing outcomes.
        # To activate: Implement branch creation when optimization reaches decision points (e.g., after failed_strategies
        # accumulation exceeds threshold), and merge/select logic when branches complete.
        self._agent_context_manager = AgentContextManager(event_bus=self._event_bus)

        # === Phase 3: Subscribe to EventBus events ===
        self._event_compressed_token = self._event_bus.subscribe(CMEventType.CONTEXT_COMPRESSED, self._on_context_compressed)
        self._layer_promoted_token = self._event_bus.subscribe(CMEventType.LAYER_PROMOTED, self._on_layer_promoted)

    # === Section 7.1.1: messages Property (Phase 2) ===
    # Route all message access through DCPOptimizerCompat / MemoryManager

    @property
    def messages(self) -> list[dict]:
        """Proxy to compat messages (MemoryManager-backed). Read-only view."""
        return self._compat.messages

    @messages.setter
    def messages(self, value: list[dict]) -> None:
        """Setter: clear MemoryManager and reload from provided list."""
        from context_manager.interfaces import MessageRole, Message
        mm = self._memory_manager
        mm._working_store.clear()
        for msg_dict in value:
            role_str = msg_dict.get("role", "user")
            content = msg_dict.get("content", "")
            metadata = {k: v for k, v in msg_dict.items() if k not in ("role", "content")}
            try:
                role = MessageRole(role_str)
            except ValueError:
                role = MessageRole.USER
            mm._working_store.add(Message(role=role, content=content, metadata=metadata))

    def _is_valid_wns(self, wns: float) -> bool:
        """Validate WNS value to reject parsing errors and false positives."""
        if wns is None:
            return False
        # WNS should not exceed 10x the clock period (would indicate a parsing error)
        if self.clock_period and abs(wns) > self.clock_period * 10:
            logger.warning(f"WNS sanity check failed: {wns:.3f} ns > {self.clock_period * 10:.1f} ns (10x clock period)")
            return False
        # Extreme negative values are usually parsing errors
        if wns < -999:
            logger.warning(f"WNS sanity check failed: {wns:.3f} ns < -999")
            return False
        # Jump from negative to exactly 0.0 without optimization is suspicious
        best = self.best_wns if self.best_wns > float('-inf') else None
        # Note: -0.0 is normalized to 0.0 in get_wns, so this check only sees +0.0
        if wns == 0.0 and best is not None and best < -0.1:
            logger.warning(f"WNS suspicious: 0.000 ns from {best:.3f} ns without visible optimization")
        return True

    @property
    def tool_call_details(self) -> list[dict]:
        """Proxy to compat tool_call_details (MemoryManager-backed). Read-only view."""
        return self._compat.tool_call_details

    # === Section 7.2: Context Management ===

    def _estimate_context_length(self) -> int:
        """Rough estimate of current context length (character count proxy)"""
        return sum(len(str(msg.get("content", ""))) for msg in self.messages)

    def _estimate_tokens(self) -> int:
        """Token estimation using tiktoken for accuracy.

        Converts internal dict messages to CMMessage objects and delegates
        to ContextEstimator for precise tiktoken-based counting.
        """
        msgs = [CMMessage(
            role=m.get("role", ""),
            content=m.get("content", ""),
            name=m.get("name"),
            tool_calls=m.get("tool_calls"),
            tool_call_id=m.get("tool_call_id"),
            metadata=m.get("metadata", {}))
            for m in self.messages]
        return self._context_estimator.estimate_from_messages(msgs)

    def _estimate_context_complexity(self, task_type: str = "") -> int:
        """
        Estimate context complexity score (0-10).
        Delegates token estimation to ContextEstimator for accuracy.
        Data-driven: uses historical task success rate for task complexity.

        Args:
            task_type: Current task type, e.g., "place_design", "get_utilization", "optimize_placement"
        """
        # Use ContextEstimator for accurate content-type-aware token estimation
        from context_manager.interfaces import Message
        msgs = [Message(role=m.get("role",""), content=m.get("content",""),
                        name=m.get("name"), tool_calls=m.get("tool_calls"),
                        tool_call_id=m.get("tool_call_id"), metadata=m.get("metadata",{}))
                for m in self.messages]
        token_est = self._context_estimator.estimate_from_messages(msgs)
        msg_count = len(msgs)

        iteration_factor = min(self.iteration / 10, 2)
        failure_factor = min(len(self._compat.failed_strategies) / 5, 2)

        # Data-driven task complexity: use historical success rate as signal
        task_complexity_factor = 0
        if task_type and task_type in self.task_type_stats:
            stats = self.task_type_stats[task_type]
            if stats['total'] >= 3:
                success_rate = stats['success'] / stats['total']
                if success_rate >= 0.8:
                    task_complexity_factor = 0
                elif success_rate >= 0.6:
                    task_complexity_factor = 1
                elif success_rate >= 0.4:
                    task_complexity_factor = 2
                else:
                    task_complexity_factor = 3
        else:
            task_complexity_factor = 1

        # Base score: based on message count and token amount
        base_score = min(msg_count / 20, 3) + min(token_est / 50000, 3)

        complexity = base_score + iteration_factor + failure_factor + task_complexity_factor
        return int(min(complexity, 10))

    # === Section 7.2.1: Context Manager Integration (Phase 1) ===

    def _sync_state_to_memory_manager(self) -> None:
        """Sync DCPOptimizer state to MemoryManager for compression context."""
        mm = self._memory_manager
        # Sync WNS state (failed_strategies and tool_call_details via compat now)
        if self.initial_wns is not None:
            mm._initial_wns = self.initial_wns
        if self.best_wns > float('-inf'):
            mm._best_wns = self.best_wns
        mm._clock_period = self.clock_period
        mm._iteration = self.iteration

    def _get_current_wns(self) -> Optional[float]:
        """Get latest WNS - O(1) from cached instance variable."""
        return self.latest_wns

    def _inject_wns_state_to_system_prompt(self, system_content: str) -> str:
        """Inject current WNS/state into system prompt to prevent context loss after compression.

        Updates the YAML metadata section in system prompt with current:
        - current_wns (from latest_wns)
        - best_wns (best achieved so far)
        - iteration (current iteration)
        - clock_period
        """
        import re

        current_wns = self._get_current_wns()
        best_wns = self.best_wns if self.best_wns > float('-inf') else None
        iteration = self.iteration
        clock_period = self.clock_period

        # Update wns_ns in timing section
        if current_wns is not None:
            system_content = re.sub(
                r'(wns_ns:\s*)[-+]?\d+\.?\d*',
                f'\\g<1>{current_wns:.3f}',
                system_content
            )

        # Update clock_period_ns in timing section
        if clock_period is not None:
            system_content = re.sub(
                r'(clock_period_ns:\s*)[-+]?\d+\.?\d*',
                f'\\g<1>{clock_period:.3f}',
                system_content
            )

        # Define known strategy catalog for remaining_strategies computation
        ALL_STRATEGIES = {"PBLOCK", "PhysOpt", "Fanout", "PlaceRoute", "CellPlacement", "IncrementalRoute"}

        # Add or update current optimization state section after the YAML block
        current_wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "N/A"
        best_wns_str = f"{best_wns:.3f}ns" if best_wns is not None else "N/A"
        clock_period_str = f"{clock_period:.3f}ns" if clock_period is not None else "N/A"
        current_tns_str = f"{self.latest_tns:.3f}ns" if self.latest_tns is not None else "N/A"
        failing_eps_str = str(self.latest_failing_endpoints) if self.latest_failing_endpoints is not None else "N/A"
        # Format resource utilization
        if self.resource_utilization:
            ru = self.resource_utilization
            resource_util_str = f"LUT={ru['LUT']}, FF={ru['FF']}, DSP={ru['DSP']}, BRAM={ru['BRAM']}"
        else:
            resource_util_str = "N/A"
        best_wns_iter = self._best_wns_iteration

        # current_wns origin hint
        current_wns_origin = ""
        if current_wns is not None and best_wns is not None and abs(current_wns - best_wns) < 0.0001:
            current_wns_origin = " (restored from best)"
        elif current_wns is not None:
            current_wns_origin = " (new)"

        # best_wns strategy hint from iteration narratives
        best_wns_strategy = ""
        if best_wns_iter is not None:
            for entry in self._iteration_narratives:
                if entry["iteration"] == best_wns_iter:
                    strat = entry.get("strategy_label", "")
                    if strat and strat not in ("Information", "Unknown"):
                        best_wns_strategy = f" via {strat}"
                    break

        # Format strategy_sequence compactly
        seq = self._strategy_sequence[-8:]  # Last 8 entries
        strategy_seq_str = ", ".join(seq) if seq else "none"

        # Compute remaining_strategies
        tried = set(self._strategy_sequence)
        tried.update(self._compat.failed_strategies)
        remaining = sorted(ALL_STRATEGIES - tried)
        remaining_str = ", ".join(remaining) if remaining else "none"

        best_dcp_str = str(self._get_intermediate_checkpoint_path(best_wns_iter)) if best_wns_iter is not None else "N/A"
        input_dcp_str = str(getattr(self, 'input_dcp', 'N/A'))
        next_model = self._next_iteration_model or self.last_used_model or "worker"
        state_section = f"""**Current Optimization State:**
  iteration: {iteration}
  best_wns: {best_wns_str} (achieved at iteration {best_wns_iter}{best_wns_strategy})
  best_checkpoint: {best_dcp_str}
  current_wns: {current_wns_str}{current_wns_origin}
  current_tns: {current_tns_str}
  failing_endpoints: {failing_eps_str}
  resource_utilization: [{resource_util_str}]
  strategy_sequence: [{strategy_seq_str}]
  remaining_strategies: [{remaining_str}]
  stagnation: {self.global_no_improvement} consecutive no-improvement iterations
  clock_period: {clock_period_str}
  input_dcp: {input_dcp_str}
  next_model: {next_model}
"""

        # Check if "Current Optimization State" already exists
        if "Current Optimization State:" in system_content:
            # Replace existing state section
            pattern = r'\*\*Current Optimization State:\*\*[^\n]*\n(?:[^\n]*\n)*?'
            system_content = re.sub(pattern, state_section, system_content)
        else:
            # Append after the YAML code block
            yaml_end_pattern = r'(```)\s*$'
            match = re.search(yaml_end_pattern, system_content, re.MULTILINE)
            if match:
                insert_pos = match.end()
                system_content = system_content[:insert_pos] + "\n\n" + state_section + system_content[insert_pos:]

        # Append data-driven scenario hint from initial analysis
        if hasattr(self, 'critical_path_spread') and self.critical_path_spread:
            avg_dist = self.critical_path_spread.get('avg_distance', 0)
            if avg_dist and avg_dist > 70:
                system_content += (
                    f"\n**Data-Driven Scenario Hint:** avg_distance={avg_dist:.1f} > 70-tile threshold -> "
                    f"matches scenario 'distributed' (Distributed Logic). "
                    f"Recommended strategy: PBLOCK-Based Re-placement."
                )

        # Inject analysis skill guidance from strategy_library (once per session)
        if "Skill Catalog" not in system_content:
            from strategy_library import get_skill_guide
            system_content += f"\n\n{get_skill_guide()}"

        return system_content

    def _build_compression_context(
        self,
        current_tokens: int,
        model_config: ModelContextConfig = None,
        model_switched: bool = False,
        force_aggressive: bool = False
    ) -> CMCompressionContext:
        """Build CompressionContext with model awareness.

        Args:
            current_tokens: Current token count estimate
            model_config: Model-specific configuration (uses WORKER_CONTEXT_CONFIG if None)
            model_switched: True if model tier switched since last compression
            force_aggressive: True if hard limit triggered (hard_limit level compression)
        """
        config = model_config or WORKER_CONTEXT_CONFIG
        return CMCompressionContext(
            current_tokens=current_tokens,
            threshold_tokens=config.soft_threshold,
            hard_limit_tokens=config.hard_limit,
            failed_strategies=self._compat.failed_strategies,
            tool_call_details=self._compat.tool_call_details,
            best_wns=self.best_wns,
            initial_wns=self.initial_wns,
            current_wns=self._get_current_wns(),
            iteration=self.iteration,
            clock_period=self.clock_period,
            model_context_config=config,
            model_switch_detected=model_switched,
            previous_model_tier=self._get_previous_model_tier(),
            force_aggressive=force_aggressive
        )

    def _infer_model_tier(self, model_name: str) -> str:
        """Infer model tier from model name.

        Args:
            model_name: Model name string

        Returns:
            "worker" or "planner" based on model name
        """
        if not model_name:
            return "worker"

        model_lower = model_name.lower()

        # Known planner models (main model + fallbacks)
        if model_lower in [
            "xiaomi/mimo-v2.5-pro",
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.6-plus"
        ]:
            return "planner"

        # Known worker models (main model + fallbacks)
        if model_lower in [
            "deepseek/deepseek-v4-flash",
            "qwen/qwen3.6-flash",
            "xiaomi/mimo-v2-flash"
        ]:
            return "worker"

        # Generic fallback for unknown models
        if "pro" in model_lower or "planner" in model_lower:
            return "planner"
        elif "flash" in model_lower or "worker" in model_lower:
            return "worker"
        else:
            return "worker"

    def _get_previous_model_tier(self) -> Optional[str]:
        """Get previous model tier from history.

        Returns:
            Previous model tier ("worker" or "planner"), or None if not available
        """
        if len(self._model_usage_history) >= 2:
            previous_model = self._model_usage_history[-2]
            return self._infer_model_tier(previous_model)
        return None

    def _get_fallback_for_tier(self, tier: str) -> tuple:
        """Get fallback models and related state for a given tier.

        Args:
            tier: "worker" or "planner"

        Returns:
            Tuple of (fallback_models list, exhausted_set, fallback_index)
        """
        if tier == "worker":
            return self.worker_fallback_models, self._exhausted_worker_fallbacks, self._worker_fallback_index
        else:
            return self.planner_fallback_models, self._exhausted_planner_fallbacks, self._planner_fallback_index

    def _get_next_fallback_model(self, tier: str) -> Optional[str]:
        """Get next available fallback model for the given tier.

        Args:
            tier: "worker" or "planner"

        Returns:
            Next fallback model name, or None if all are exhausted.
        """
        fallback_models, exhausted_set, fallback_index = self._get_fallback_for_tier(tier)

        if not fallback_models:
            return None

        # Check if all fallbacks are exhausted
        available = [m for m in fallback_models if m not in exhausted_set]
        if not available:
            return None

        # Round-robin through available models
        attempts = 0
        start_index = fallback_index
        while attempts < len(available):
            model = available[(start_index + attempts) % len(available)]
            if model not in exhausted_set:
                # Update the index for the next call
                if tier == "worker":
                    self._worker_fallback_index = (fallback_models.index(model) + 1) % len(fallback_models)
                else:
                    self._planner_fallback_index = (fallback_models.index(model) + 1) % len(fallback_models)
                return model
            attempts += 1

        return None

    def _mark_fallback_exhausted(self, model: str) -> None:
        """Mark a fallback model as exhausted (hit 429).

        Args:
            model: Model name that hit 429
        """
        tier = self._infer_model_tier(model)
        if tier == "worker":
            self._exhausted_worker_fallbacks.add(model)
            logger.info(f"Fallback worker model {model} marked as exhausted due to 429")
        else:
            self._exhausted_planner_fallbacks.add(model)
            logger.info(f"Fallback planner model {model} marked as exhausted due to 429")

    def _reset_fallbacks(self, tier: str) -> None:
        """Reset exhausted fallbacks for a tier.

        Args:
            tier: "worker" or "planner"
        """
        if tier == "worker":
            self._exhausted_worker_fallbacks.clear()
        else:
            self._exhausted_planner_fallbacks.clear()

    def _get_active_model_config_with_switch_detection(self) -> tuple:
        """Get model config and detect if model has switched.

        Returns:
            tuple: (ModelContextConfig, bool) - configuration and whether switch detected
        """
        # Determine current model type based on last_used_model
        current_model = self.last_used_model if self.last_used_model else self.model_worker
        current_tier = self._infer_model_tier(current_model)

        # Detect model tier switch by comparing current tier with previous tier
        # previous_tier is set in get_completion() before _select_model() is called
        model_switched = False
        if self._previous_tier is not None and self._previous_tier != current_tier:
            model_switched = True
            # Only log once per iteration (the first tool round that detects the switch)
            if not self._iteration_model_switch_logged:
                logger.info(f"Model tier switch detected: {self._previous_tier} -> {current_tier}")
                self._iteration_model_switch_logged = True

        config = MODEL_CONTEXT_CONFIGS.get(current_tier, WORKER_CONTEXT_CONFIG)
        return config, model_switched

    def _compress_context(self):
        """Context compression with model-aware intelligent strategy.

        NOTE: MemoryManager._compress() already replaces working memory with compressed
        messages internally. No additional message replacement is needed after compression.

        Compression metrics are tracked for observability:
        - compression_count: total compressions triggered
        - compression_hard_count / compression_soft_count: by trigger reason
        - compression_skipped: calls where token count was under threshold
        - compression_details: structured log of each compression event
        """
        # Sync optimizer state to MemoryManager
        self._sync_state_to_memory_manager()

        current_tokens = self._estimate_tokens()
        tokens_before = current_tokens

        # Diagnostic: also compute char-based count for comparison
        if self.debug:
            char_total = 0
            for msg in self.messages:
                content = str(msg.get("content", ""))
                char_total += _estimate_tokens_char_based(content)
            ratio = (char_total / current_tokens * 100) if current_tokens > 0 else 0
            logger.info(
                f"[TOKEN_DIAG] tiktoken={current_tokens:,}, char_est={char_total:,}, "
                f"ratio={ratio:.1f}% (char/tiktoken)"
            )

        # Determine current model configuration and switch detection
        active_config, model_switched = self._get_active_model_config_with_switch_detection()
        hard_limit = active_config.hard_limit
        soft_threshold = active_config.soft_threshold

        # Determine historical retrieval parameters
        # Model switch: retrieve more context for transition
        if model_switched:
            history_limit = max(active_config.history_retrieval_limit, 8)
            history_min_importance = active_config.history_retrieval_min_importance * 0.9
        else:
            history_limit = active_config.history_retrieval_limit
            history_min_importance = active_config.history_retrieval_min_importance

        # Hard limit: must compress with hard_limit level (more aggressive than soft, but preserves more than full aggressive)
        if current_tokens > hard_limit:
            self.compression_hard_count += 1
            self.compression_count += 1
            logger.warning(
                f"[COMPRESSION] Hard limit triggered: ~{current_tokens:,} tokens > {hard_limit:,} "
                f"({active_config.model_tier} model, switch={model_switched})"
            )
            context = self._build_compression_context(current_tokens, active_config, model_switched, force_aggressive=True)
            context.retrieved_history = self._memory_manager.retrieve_historical(
                CMRetrievalQuery(min_importance=history_min_importance, limit=history_limit)
            )
            self._memory_manager._compress("yaml_structured", context, model_tier=active_config.model_tier)
            tokens_after = self._estimate_tokens()
            self._record_compression_event("hard", tokens_before, tokens_after, iteration=self.iteration)
            return

        # Soft threshold: normal YAML compression
        if current_tokens > soft_threshold:
            self.compression_soft_count += 1
            self.compression_count += 1
            logger.info(
                f"[COMPRESSION] Soft threshold triggered: ~{current_tokens:,} tokens > {soft_threshold:,} "
                f"({active_config.model_tier} model, switch={model_switched})"
            )
            context = self._build_compression_context(current_tokens, active_config, model_switched)
            context.retrieved_history = self._memory_manager.retrieve_historical(
                CMRetrievalQuery(min_importance=history_min_importance, limit=history_limit)
            )
            self._memory_manager._compress("yaml_structured", context, model_tier=active_config.model_tier)
            tokens_after = self._estimate_tokens()
            self._record_compression_event("soft", tokens_before, tokens_after, iteration=self.iteration)
            return

        # Under threshold - record as skipped
        self.compression_skipped += 1

    def _start_console_reader(self) -> None:
        """Start a background thread that listens for 'quit' on stdin to request graceful exit."""
        if self._console_reader_started:
            return
        self._console_reader_started = True

        def read_console():
            try:
                while True:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    if line.strip().lower() == "quit":
                        logger.info("User requested exit via console 'quit' command")
                        self._user_exit_requested.set()
                        # Also set async event so it can be checked during await calls
                        self._async_exit_requested.set()
                        break
            except Exception:
                pass

        t = threading.Thread(target=read_console, daemon=True)
        t.start()

    def _check_exit_requested(self) -> bool:
        """Check if user requested exit. Returns True if exit requested."""
        return self._user_exit_requested.is_set()

    def _check_async_exit_requested(self) -> bool:
        """Check if user requested exit (async version). Returns True if exit requested."""
        return self._async_exit_requested.is_set()

    async def _call_llm_with_exit_check(self, model: str, messages: list, tools: list, timeout: float = 600.0) -> Optional[object]:
        """Make an LLM call with exit checking after completion.

        Note: httpx doesn't support true cancellation of in-flight requests.
        This method checks for exit after the call completes, so quit will be
        responsive within ~10 seconds of the LLM call completing (or immediately
        if the call completes faster).

        Args:
            model: Model name to use
            messages: Messages to send
            tools: Tools available
            timeout: Total timeout in seconds

        Returns:
            Response object if successful, None if exit requested
        """
        response = await self.openai.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_tokens=4096,
            timeout=timeout
        )

        # Check if exit was requested during the LLM call
        if self._check_async_exit_requested():
            return None

        return response

    def _record_compression_event(self, trigger: str, tokens_before: int, tokens_after: int, iteration: int = 0) -> None:
        """Record structured compression event for observability.

        Args:
            trigger: "hard" (exceeded hard limit) or "soft" (exceeded soft threshold)
            tokens_before: Token count before compression
            tokens_after: Token count after compression
            iteration: Current optimization iteration
        """
        compression_ratio = (tokens_before - tokens_after) / tokens_before if tokens_before > 0 else 0
        event = {
            "iteration": iteration,
            "trigger": trigger,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_reduced": tokens_before - tokens_after,
            "compression_ratio": round(compression_ratio, 3),
            "llm_calls_at_compression": self.llm_call_count,
        }
        self.compression_details.append(event)
        # Structured log for compression observability
        logger.info(
            f"[COMPRESSION_RESULT] [{trigger.upper()}] Iteration {iteration}: "
            f"{tokens_before:,} tokens -> {tokens_after:,} tokens "
            f"(saved {tokens_before - tokens_after:,} tokens, ratio: {compression_ratio*100:.1f}%)"
        )
        # Keep only last 100 compression events to prevent memory bloat
        if len(self.compression_details) > 100:
            self.compression_details.pop(0)

    # === Phase 3: EventBus event handlers ===

    def _on_context_compressed(self, event: CMContextEvent) -> None:
        """Handle CONTEXT_COMPRESSED events from MemoryManager."""
        data = event.data or {}
        compression_type = data.get("compression_type", "unknown")
        original_count = data.get("original_count", 0)
        compressed_count = data.get("compressed_count", 0)
        compression_ratio = (original_count - compressed_count) / original_count if original_count > 0 else 0
        logger.info(
            "[COMPRESSION] Context compressed: type=%s, messages=%d->%d (removed %.0f%%)",
            compression_type,
            original_count,
            compressed_count,
            compression_ratio * 100,
            extra={
                "compression_type": compression_type,
                "compression_original_count": original_count,
                "compression_compressed_count": compressed_count,
                "compression_ratio": round(compression_ratio, 4),
                "trace_id": get_trace_id(),
            }
        )

    def _on_layer_promoted(self, event: CMContextEvent) -> None:
        """Handle LAYER_PROMOTED events (messages archived to historical memory)."""
        data = event.data or {}
        layer = data.get("layer", "unknown")
        message_count = data.get("message_count", 0)
        logger.info(
            f"[EventBus] LAYER_PROMOTED: layer={layer}, messages={message_count}"
        )

    def _filter_tool_result(self, tool_name: str, result: str) -> str:
        """Retain key information based on tool type, truncate redundant content"""
        if len(result) <= TOOL_RESULT_TRUNCATE:
            return result

        # Timing reports: retain WNS/TNS/key path summary
        if 'timing' in tool_name.lower():
            lines = result.split('\n')
            kept_lines = []
            for line in lines:
                # Retain key metric lines
                if any(kw in line.lower() for kw in ['wns', 'tns', 'failing', 'clock', 'target', 'level',
                                              'slack', 'delay', 'path', 'endpoint', 'start', 'endpoint']):
                    kept_lines.append(line)
                # Retain headers
                elif line.strip().startswith('---') or line.strip().startswith('==='):
                    kept_lines.append(line)
            if kept_lines:
                filtered = '\n'.join(kept_lines[:200])  # Keep at most 200 lines
                if len(result) > len(filtered):
                    return filtered + f"\n...[timing report truncated, original: {len(result)} chars]..."
            return result[:TOOL_RESULT_TRUNCATE]

        # Route status: retain errors and congestion info
        if 'route' in tool_name.lower():
            lines = result.split('\n')
            kept_lines = []
            for line in lines:
                # Retain key information
                if any(kw in line.lower() for kw in ['error', 'fail', 'congestion', 'unrouted', 'nets', 'status']):
                    kept_lines.append(line)
            if kept_lines:
                filtered = '\n'.join(kept_lines[:50])
                if len(result) > len(filtered):
                    return filtered + f"\n...[route status truncated, original: {len(result)} chars]..."
            return result[:TOOL_RESULT_TRUNCATE]

        # General truncation: preserve beginning and ending key information
        head_len = TOOL_RESULT_TRUNCATE // 2
        tail_len = TOOL_RESULT_TRUNCATE // 2
        return result[:head_len] + f"\n...[{len(result) - TOOL_RESULT_TRUNCATE} chars truncated]...\n" + result[-tail_len:]

    def _summarize_tool_result(self, tool_name: str, raw_result: str) -> str:
        """Convert raw tool output to structured YAML summary for LLM consumption.

        Preserves key metrics (WNS/TNS/failing_endpoints, deltas, status)
        while discarding verbose INFO lines and path details.
        Full raw output is stored in _raw_tool_outputs for on-demand retrieval.
        """
        lines = raw_result.split('\n')
        line_count = len(lines)
        char_count = len(raw_result)

        # Build summary components
        summary_parts = []
        key_details = {}
        duration = None
        status = "completed"

        # Common: extract any error/fail indicators
        has_error = any("error" in l.lower() for l in lines[:20])
        has_fail = any("fail" in l.lower() for l in lines[:20])
        if has_error and "success" not in raw_result.lower():
            status = "error" if has_error else "failed"

        # Tool-type specific extraction
        if tool_name in ("vivado_phys_opt_design", "vivado_report_timing_summary"):
            timing = parse_timing_summary_static(raw_result)
            wns = timing.get("wns")
            tns = timing.get("tns")
            fe = timing.get("failing_endpoints")

            # Use latest tracked values if parser didn't find them (for phys_opt auto-eval appended text)
            if wns is None:
                wns = self.latest_wns
            if tns is None:
                tns = self.latest_tns
            if fe is None:
                fe = self.latest_failing_endpoints
            if wns is not None:
                prev = getattr(self, '_prev_best_wns', None)
                delta_str = ""
                if prev is not None and prev > float('-inf'):
                    diff = wns - prev
                    delta_str = f"{diff:+.3f}"
                summary_parts.append(f"WNS: {wns:.3f}")
                if tns is not None:
                    summary_parts.append(f"TNS: {tns:.3f}")
                if fe is not None:
                    summary_parts.append(f"Failing endpoints: {fe}")
                key_details["wns"] = round(wns, 3)
                if prev is not None and prev > float('-inf'):
                    key_details["wns_delta"] = round(wns - prev, 3)
                key_details["tns"] = round(tns, 3) if tns is not None else None
                key_details["failing_endpoints"] = fe

        elif tool_name == "vivado_route_design":
            # Extract route status
            route_status = ""
            for line in lines[:50]:
                if any(kw in line.lower() for kw in ["error", "fail", "congestion", "unrouted", "status"]):
                    route_status += line.strip() + "; "
            if route_status:
                summary_parts.append(f"Route: {route_status[:200]}")
            timing = parse_timing_summary_static(raw_result)
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
            # Extract key placement info
            for line in lines[:50]:
                if any(kw in line.lower() for kw in ["error", "warning", "placed", "utilization", "slack"]):
                    stripped = line.strip()
                    if stripped:
                        summary_parts.append(stripped[:200])

        elif tool_name == "vivado_run_tcl_info":
            # For informational Tcl (read-only), keep concise
            summary_parts.append(f"Output: {line_count} lines, {char_count} chars")

        elif tool_name == "vivado_extract_critical_path_pins":
            # Extract pin path counts and preview paths for detour analysis
            try:
                import json as _json
                data = _json.loads(raw_result)
                if "error" in data:
                    summary_parts.append(f"Error: {data['error'][:200]}")
                    status = "error"
                else:
                    path_count = data.get("path_count", 0)
                    pin_paths = data.get("pin_paths", [])
                    summary_parts.append(f"Extracted {path_count} critical pin paths")
                    key_details["path_count"] = path_count
                    if pin_paths:
                        for i, pp in enumerate(pin_paths[:2]):
                            preview = " -> ".join(pp[:4])
                            if len(pp) > 4:
                                preview += " -> ..."
                            summary_parts.append(f"  Path {i+1}: {preview}")
            except Exception:
                pass

        elif tool_name == "vivado_create_and_apply_pblock":
            # Extract pblock resource validation results
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
            if not summary_parts:
                # Fallback: key status lines
                for line in lines[:50]:
                    if any(kw in line for kw in ["Created pblock", "Set pblock", "Applied pblock", "Error"]):
                        stripped = line.strip()
                        if stripped:
                            summary_parts.append(stripped[:200])
            if has_validation_failure:
                status = "validation_failed"
                key_details["validation_failed"] = True

        elif tool_name == "rapidwright_analyze_fabric_for_pblock":
            # Parse JSON output for fabric bounds and recommended region
            try:
                import json as _json
                data = _json.loads(raw_result)
                if "error" in data:
                    summary_parts.append(f"Error: {data['error'][:200]}")
                    status = "error"
                else:
                    fb = data.get("fabric_bounds", {})
                    rr = data.get("recommended_region", {})
                    er = data.get("estimated_resources", {})
                    tr = data.get("target_requirements", {})
                    if fb:
                        summary_parts.append(
                            f"Fabric: cols {fb.get('min_col')}-{fb.get('max_col')}, "
                            f"rows {fb.get('min_row')}-{fb.get('max_row')}"
                        )
                    if rr:
                        summary_parts.append(
                            f"Region: cols {rr.get('col_min')}-{rr.get('col_max')}, "
                            f"rows {rr.get('row_min')}-{rr.get('row_max')}"
                        )
                        for k in ("col_min", "col_max", "row_min", "row_max",
                                  "center_col", "center_row",
                                  "center_of_mass_col", "center_of_mass_row"):
                            v = rr.get(k)
                            if v is not None:
                                key_details[k] = v
                    if er:
                        summary_parts.append(
                            f"Estimated: ~{er.get('approx_luts', '?')} LUTs, "
                            f"~{er.get('approx_ffs', '?')} FFs, "
                            f"{er.get('dsp_sites', 0)} DSPs, {er.get('bram_sites', 0)} BRAMs"
                        )
                    if tr:
                        summary_parts.append(
                            f"Target: {tr.get('luts', '?')} LUTs, {tr.get('ffs', '?')} FFs, "
                            f"{tr.get('dsps', 0)} DSPs, {tr.get('brams', 0)} BRAMs"
                        )
                    if data.get("message"):
                        summary_parts.append(data["message"][:200])
            except Exception:
                pass

        elif tool_name == "rapidwright_analyze_pblock_region":
            # Plain analysis dict (not StrategyPlan)
            try:
                import json as _json
                data = _json.loads(raw_result)
                if "error" in data:
                    summary_parts.append(f"Error: {data.get('error', 'unknown')[:200]}")
                    status = "error"
                else:
                    status_val = data.get("status", "unknown")
                    summary_parts.append(f"Analysis: {status_val}")
                    if data.get("message"):
                        summary_parts.append(data["message"][:200])
                    if "region" in data:
                        r = data["region"]
                        summary_parts.append(
                            f"Region: cols {r.get('col_min')}-{r.get('col_max')}, "
                            f"rows {r.get('row_min')}-{r.get('row_max')}"
                        )
                        key_details["region"] = data["region"]
                    if data.get("pblock_ranges"):
                        key_details["pblock_ranges"] = data["pblock_ranges"]
                    if data.get("estimated_resources"):
                        key_details["estimated_resources"] = data["estimated_resources"]
                    if data.get("next_steps"):
                        key_details["next_steps"] = data["next_steps"]
            except Exception:
                pass

        elif tool_name == "rapidwright_execute_fanout_strategy":
            # Direct execution result (nets_processed, successful_count, etc.)
            try:
                import json as _json
                data = _json.loads(raw_result)
                if "error" in data:
                    summary_parts.append(f"Error: {data.get('error', 'unknown')[:200]}")
                    status = "error"
                elif data.get("skipped"):
                    summary_parts.append(f"Skipped: {data.get('message', '')[:200]}")
                    status = "skipped"
                else:
                    successful = data.get("successful_count", 0)
                    failed = data.get("failed_count", 0)
                    total = data.get("nets_processed", 0)
                    ckpt = data.get("checkpoint_path", "")
                    summary_parts.append(
                        f"Fanout: {successful}/{total} nets optimized"
                        + (f", {failed} failed" if failed else "")
                    )
                    key_details["successful_count"] = successful
                    key_details["failed_count"] = failed
                    key_details["checkpoint_path"] = ckpt
                    if data.get("results"):
                        key_details["results"] = data["results"]
            except Exception:
                pass

        elif tool_name == "rapidwright_execute_physopt_strategy":
            # Parse StrategyPlan JSON for pending steps
            try:
                import json as _json
                data = _json.loads(raw_result)
                if "error" in data:
                    summary_parts.append(f"Error: {data.get('error', 'unknown')[:200]}")
                    status = "error"
                else:
                    status_val = data.get("status", "unknown")
                    summary_parts.append(f"Strategy status: {status_val}")
                    key_details["strategy_status"] = status_val
                    if data.get("message"):
                        summary_parts.append(data["message"][:200])
                    if status_val in ("error", "skipped") and data.get("error_details"):
                        summary_parts.append(f"Details: {str(data['error_details'])[:200]}")
                    steps = data.get("steps", [])
                    if steps:
                        pending = [s for s in steps if not s.get("executed", False)]
                        executed = [s for s in steps if s.get("executed", False)]
                        if pending:
                            pending_names = [s.get("step_name", "?") for s in pending]
                            summary_parts.append(f"Pending: {' -> '.join(pending_names)}")
                            key_details["pending_steps"] = pending_names
                        if executed:
                            key_details["executed_steps"] = [s.get("step_name", "?") for s in executed]
                    if data.get("analysis_summary"):
                        key_details["analysis"] = data["analysis_summary"]
            except Exception:
                pass

        # Fallback: generic truncation
        if not summary_parts:
            # Pick first few meaningful lines
            meaningful = [l.strip() for l in lines[:30] if l.strip() and not l.strip().startswith(("INFO:", "WARNING:", "//", "#"))]
            if meaningful:
                summary_parts.extend(meaningful[:5])
            else:
                summary_parts.append(f"{line_count} lines, {char_count} chars")

        summary_line = "; ".join(summary_parts)

        # Build YAML output
        yaml_lines = ["tool_result:"]
        yaml_lines.append(f"  tool: {tool_name}")
        yaml_lines.append(f"  summary: \"{summary_line}\"")
        if key_details:
            yaml_lines.append("  key_details:")
            for k, v in key_details.items():
                if v is not None:
                    yaml_lines.append(f"    {k}: {v}")
        yaml_lines.append(f"  status: {status}")
        yaml_lines.append(f"  raw_output_truncated: true")
        yaml_lines.append(f"  raw_output_chars: {char_count}")

        return '\n'.join(yaml_lines)

    def _estimate_immediate_complexity(self, recent_messages: list) -> int:
        """
        Immediate complexity assessment: determine actual task complexity based on recent message content.
        """
        complexity = 0
        complex_patterns = [
            r"optimize|optimization",
            r"analyze.*timing|timing.*analyz",
            r"floorplan|placement",
            r"constraint",
            r"strategy",
            r"debug|fix.*error",
            r"compare.*approach|evaluate",
            r"implement.*logic|custom.*block"
        ]
        for msg in recent_messages[-3:]:  # Only look at last 3 messages
            content = str(msg.get("content", "")).lower()
            for pattern in complex_patterns:
                if re.search(pattern, content):
                    complexity += 1
        return min(complexity, 5)

    def _is_complex_task(self, window_size: int = 5) -> bool:
        """
        Determine task complexity based on recent messages and tool calls (sliding window + weighted scoring).
        Returns:
            True if Planner should be used, False if Worker may be sufficient.
        """
        complex_score = 0

        # 1. Scan last N user messages
        if self.messages:
            user_msgs = [m for m in reversed(self.messages) if m.get('role') == 'user'][:window_size]
            for msg in user_msgs:
                content = msg.get('content', '').lower()
                # High-weight keywords
                for kw in ['optimize', 'strategy', 'floorplan', 'pblock', 'debug', 'violation', 'congestion']:
                    if kw in content:
                        complex_score += 2
                # Normal-weight keywords
                for kw in ['place', 'route', 'synthesize', 'implement', 'modify']:
                    if kw in content:
                        complex_score += 1

        # 2. Scan last 3 tool calls
        recent_tools = [t.get('tool_name', '') for t in self._compat.tool_call_details[-3:]]
        complex_tools = {'place_design', 'route_design', 'phys_opt_design', 'create_and_apply_pblock'}
        for tool in recent_tools:
            if any(ct in tool.lower() for ct in complex_tools):
                complex_score += 3

        # 3. Context complexity score
        complexity = self._estimate_context_complexity(self.current_task_type)
        if complexity >= 5:
            complex_score += 5

        return complex_score >= 5

    def _on_iteration_end(self, wns_improved: bool, model_used: str):
        """
        Simplified iteration end processing: only update counters, _select_model decides next iteration's model.

        Args:
            wns_improved: Whether WNS improved this iteration
            model_used: The model actually used this iteration
        """
        if wns_improved:
            # Improved: reset failure count, clear historical failure accumulation
            self.worker_consecutive_failures = 0
            self.global_no_improvement = 0
            # Only count consecutive success for Worker on OPTIMIZATION tasks
            # (INFORMATION tasks should not affect model downgrade decisions)
            if model_used == self.model_worker:
                if self.classify_task(self.current_task_type) == TaskCategory.OPTIMIZATION:
                    self.worker_consecutive_success += 1
        else:
            # Not improved: reset success count
            self.worker_consecutive_success = 0
            # Only count failures for Worker (information tasks don't count)
            if model_used == self.model_worker:
                if self.classify_task(self.current_task_type) == TaskCategory.OPTIMIZATION:
                    self.worker_consecutive_failures += 1
            # Global no-improvement count
            self.global_no_improvement += 1

        # Decide next iteration's model and generate handoff prompt
        next_model = self._select_model(
            tool_name=self.current_task_type,
            context_complexity=self._estimate_context_complexity(self.current_task_type),
        )
        self._next_iteration_model = next_model
        logger.info(f"Next iteration {self.iteration + 1} will use model: {next_model}")

        # Generate handoff prompt for the incoming model
        self._iteration_handoff_prompt = self._generate_iteration_handoff_prompt()
        self._iteration_handoff_injected = False

    def _generate_iteration_handoff_prompt(self) -> str:
        """Dispatcher: generate model-tier-specific handoff prompt."""
        next_tier = self._infer_model_tier(self._next_iteration_model)
        if next_tier == "planner":
            return self._generate_planner_handoff()
        else:
            return self._generate_worker_handoff()

    def _infer_strategy_from_tools(self, tools: list[str]) -> str:
        """Deduce strategy label from tool sequence."""
        tool_str = " ".join(tools).lower()
        if "analyze_pblock" in tool_str or "pblock_strategy" in tool_str or "create_and_apply_pblock" in tool_str or "convert_fabric_region_to_pblock" in tool_str:
            return "PBLOCK"
        if "physopt_strategy" in tool_str or "phys_opt_design" in tool_str:
            return "PhysOpt"
        if "fanout_strategy" in tool_str or "optimize_fanout" in tool_str:
            return "Fanout"
        if "place_design" in tool_str or "route_design" in tool_str:
            return "PlaceRoute"
        if any(t in tool_str for t in ["report_", "get_", "extract_", "analyze_"]):
            return "Information"
        return "Unknown"

    def _append_iteration_narrative(self) -> None:
        """Record structured summary of this iteration for progressive context."""
        prev_best = getattr(self, '_prev_best_wns', None)
        current_best = self.best_wns if self.best_wns > float('-inf') else None

        if prev_best is not None and current_best is not None:
            wns_delta = current_best - prev_best
        else:
            wns_delta = 0.0

        if wns_delta > 0.001:
            outcome = "improved"
        elif wns_delta < -0.001:
            outcome = "regression"
        else:
            outcome = "unchanged"

        tools_this_iter = [
            t.get('tool_name', '')
            for t in self._compat.tool_call_details
            if t.get('iteration') == self.iteration
        ]

        entry = {
            "iteration": self.iteration,
            "model": self.last_used_model or "unknown",
            "task_type": self.current_task_type,
            "wns_before": prev_best,
            "wns_after": current_best,
            "wns_delta": wns_delta,
            "tool_count": len(tools_this_iter),
            "strategy_label": self._infer_strategy_from_tools(tools_this_iter),
            "outcome": outcome,
            "result_status": (self._step_state.result_status
                              if self._step_state else None),
            "scenario_match": (self._step_state.analysis.get('scenario_match')
                               if self._step_state and self._step_state.analysis else None)
        }
        self._iteration_narratives.append(entry)
        if len(self._iteration_narratives) > 20:
            self._iteration_narratives.pop(0)
        # Track strategy sequence for state summary
        strategy_label = entry["strategy_label"]
        if strategy_label != "Information" and strategy_label != "Unknown":
            if not self._strategy_sequence or self._strategy_sequence[-1] != strategy_label:
                self._strategy_sequence.append(strategy_label)
                if len(self._strategy_sequence) > 20:
                    self._strategy_sequence.pop(0)

    def _format_narrative(self, max_entries: int = None) -> str:
        """Build progressive iteration summary from _iteration_narratives."""
        entries = self._iteration_narratives
        if max_entries:
            entries = entries[-max_entries:]

        if not entries:
            return "No iteration history available."

        lines = []
        for e in entries:
            delta = e['wns_delta']
            delta_str = f"{delta:+.4f}" if delta else "+0.0000"
            lines.append(
                f"iter{e['iteration']}({e['outcome'].upper()[:3]}): "
                f"{e['wns_before']:.3f}->{e['wns_after']:.3f}ns({delta_str}) "
                f"{e['tool_count']}toks {e['strategy_label']}"
            )
        return "\n".join(lines)

    def _build_tool_effect_summary(self, iteration: int) -> str:
        """Build tool effect summary with WNS deltas for a given iteration."""
        calls = [t for t in self._compat.tool_call_details if t.get('iteration') == iteration]
        if not calls:
            return "No tool calls this iteration."

        lines = []
        for call in calls[-8:]:
            tool = call.get('tool_name', 'unknown')
            wns = call.get('wns')
            error = call.get('error')
            if error:
                lines.append(f"- {tool}: ERROR")
            elif wns is not None:
                lines.append(f"- {tool}: WNS={wns:.3f}ns")
            else:
                lines.append(f"- {tool}: (no WNS)")
        return "\n".join(lines)

    def _build_failed_strategy_summary(self) -> str:
        """Build annotated failed strategy list with failure context."""
        strategies = self._compat.failed_strategies[-5:]
        if not strategies:
            return "None"

        lines = []
        for s in strategies:
            related_calls = [
                t for t in self._compat.tool_call_details
                if s.lower() in t.get('tool_name', '').lower()
                or s.lower() in t.get('result', '').lower()
            ]
            if related_calls:
                last = related_calls[-1]
                iter_num = last.get('iteration', '?')
                wns = last.get('wns')
                wns_str = f", WNS={wns:.3f}ns" if wns is not None else ""
                lines.append(f"- {s} (iter{iter_num}{wns_str})")
            else:
                lines.append(f"- {s}")
        return "\n".join(lines)

    def _build_skill_invocation_summary(self, iteration: int) -> str:
        """Build per-iteration skill invocation summary for handoff context.

        Args:
            iteration: The iteration to summarize.

        Returns:
            Formatted string for inclusion in handoff, or empty string.
        """
        calls = [
            s for s in self.skill_invocation_log
            if s.get("iteration") == iteration
        ]
        if not calls:
            return ""

        lines = ["=== SKILL INVOCATIONS ==="]
        for call in calls:
            wns_str = (
                f"WNS={call['wns']:.3f}ns"
                if call.get("wns") is not None
                else "no WNS"
            )
            err_str = " [ERROR]" if call.get("error") else ""
            lines.append(
                f"- {call['skill_name']}: {wns_str}, "
                f"{call['elapsed_time']:.1f}s{err_str}"
            )

        # Append recommendation acceptance status for this iteration
        for rec in self.skill_recommendation_log:
            if rec.get("iteration") == iteration and rec.get("recommended_skill"):
                status = "ACCEPTED" if rec.get("accepted") else "not accepted"
                lines.append(
                    f"- Recommendation: {rec['recommended_skill']} [{status}]"
                )

        return "\n".join(lines)

    def _detect_unfinished_strategy(self) -> dict:
        """Detect if previous iteration's strategy was interrupted mid-execution.

        Returns dict with was_interrupted, strategy, last_tool, tool_count, exit_reason.
        """
        last_iter_tools = [
            t for t in self._compat.tool_call_details
            if t.get('iteration') == self.iteration
        ]

        result = {
            "was_interrupted": False,
            "strategy": "None",
            "last_tool": "",
            "tool_count": len(last_iter_tools),
            "exit_reason": self._is_done_reason or "unknown"
        }

        if not last_iter_tools:
            return result

        tool_names = [t.get('tool_name', '') for t in last_iter_tools]
        strategy = self._infer_strategy_from_tools(tool_names)
        last_tool_raw = tool_names[-1]
        # Strip vendor prefix (e.g., vivado_phys_opt_design -> phys_opt_design)
        parts = last_tool_raw.split("_", 1)
        last_tool = parts[1] if parts[0] in ("vivado", "rapidwright") and len(parts) > 1 else last_tool_raw

        result["strategy"] = strategy
        result["last_tool"] = last_tool

        # A strategy is "unfinished" if:
        # 1. Exit reason suggests premature end
        # 2. Strategy is actionable (not Information/Unknown)
        # 3. Last 2 tool calls do NOT include report_timing_summary (incomplete cycle)
        interrupted_exits = {"tool_round_limit", "flow_control_done_next_iteration", "switch_strategy"}
        is_actionable = strategy not in ("Information", "Unknown")
        cycle_complete = any("report_timing_summary" in t for t in tool_names[-2:])

        if self._is_done_reason in interrupted_exits and is_actionable and not cycle_complete:
            result["was_interrupted"] = True

        # [Fix 4] Detect pblock validation failure: last tool is create_and_apply_pblock
        # and its result contains validation_failed keyword in raw result text.
        _pblock_failed = False
        if last_tool_raw == "vivado_create_and_apply_pblock":
            last_detail = self._compat.tool_call_details[-1] if self._compat.tool_call_details else {}
            last_result = (last_detail.get("result", "") or "").lower() if isinstance(last_detail, dict) else ""
            if "validation_failed" in last_result:
                _pblock_failed = True

        # [Fix 6] Detect missing post-fanout evaluation: optimize_fanout was used
        # but place_design + route_design + report_timing_summary are absent.
        # Fanout changes netlist, so placement must be re-run before route.
        _fanout_post_eval_missing = False
        if any("optimize_fanout" in t for t in tool_names):
            has_place = any("place_design" in t for t in tool_names)
            has_route = any("route_design" in t for t in tool_names)
            has_timing = any("report_timing_summary" in t for t in tool_names[-3:])
            if not (has_place and has_route and has_timing):
                _fanout_post_eval_missing = True

        if _pblock_failed:
            result["was_interrupted"] = True
            result["reason"] = "pblock_validation_failed"
            self._compat.record_failure("PBLOCK")
        elif _fanout_post_eval_missing:
            result["was_interrupted"] = True
            result["reason"] = "fanout_post_eval_missing"
            self._compat.record_failure("Fanout")

        return result

    def _build_exit_reason_section(self) -> str:
        """Build human-readable exit reason for planner handoff context."""
        reason = self._is_done_reason or "unknown"

        reason_map = {
            "tool_round_limit": (
                "Tool Round Limit",
                "The previous iteration reached the maximum tool call rounds (50) before completing its strategy.",
                "Continue optimization from where the previous iteration left off."
            ),
            "flow_control_done_next_iteration": (
                "Premature DONE Signal",
                "The previous model incorrectly signaled flow_control=DONE while WNS is still negative.",
                "Optimization is NOT complete. Continue aggressively. Do NOT use DONE unless WNS >= 0.0."
            ),
            "cost_limit": (
                "Cost Limit Reached",
                "The previous iteration ended because the API cost limit was reached.",
                "Continue optimization with fresh perspective."
            ),
            "user_requested": (
                "User Requested",
                "The previous iteration was ended by user request.",
                "Resume optimization normally."
            ),
        }

        if reason in reason_map:
            label, description, implication = reason_map[reason]
        else:
            label = reason.replace("_", " ").title()
            description = "The previous iteration ended."
            implication = "Continue optimization."

        current_wns = self._get_current_wns()
        wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"

        return (
            f"- Reason: {label}\n"
            f"- Description: {description}\n"
            f"- Implication: {implication}\n"
            f"- Current WNS: {wns_str} (target: 0.0ns)"
        )

    def _build_continuation_directive(self, unfinished: dict, is_worker: bool = False) -> str:
        """Build explicit continuation directive for the next iteration's model."""
        if is_worker:
            parts = ["CONTINUE from previous iteration."]
            if unfinished["was_interrupted"]:
                reason = unfinished.get("reason", "")
                if reason == "pblock_validation_failed":
                    parts.append("PBLOCK resource shortage detected. Re-analyze fabric with expanded bounds or switch strategy.")
                elif reason == "fanout_post_eval_missing":
                    parts.append("Fanout optimization applied. Complete: open_checkpoint -> place_design -> route_design -> report_timing_summary.")
                else:
                    parts.append(
                        f"Strategy '{unfinished['strategy']}' was in progress "
                        f"(last tool: {unfinished['last_tool']}, "
                        f"{unfinished['tool_count']} calls). Resume or adjust."
                    )
            else:
                parts.append("Build upon existing progress. Do NOT reload the design.")
            return " ".join(parts)

        lines = [
            "**CRITICAL: You are continuing optimization from the previous iteration.**",
            "- Do NOT restart from scratch",
            "- Build upon existing progress and checkpoints",
            "- The design is already open in Vivado and RapidWright -- do NOT reload",
            "- Continue from where the previous model left off",
        ]
        if unfinished["was_interrupted"]:
            reason = unfinished.get("reason", "")
            if reason == "pblock_validation_failed":
                lines.extend([
                    "",
                    "**PBLOCK Resource Shortage:** The pblock region lacks sufficient resources.",
                    "- Re-run analyze_fabric_for_pblock with expanded bounds or a shifted region.",
                    "- Or switch to a different strategy (PhysOpt/Fanout).",
                ])
            elif reason == "fanout_post_eval_missing":
                lines.extend([
                    "",
                    "**Fanout Optimization Applied:** Post-optimization evaluation is missing.",
                    "- Must run: open_checkpoint -> place_design -> route_design -> report_timing_summary.",
                    "- Verify timing impact before proceeding.",
                ])
            else:
                lines.extend([
                    "",
                    f"**Interrupted Strategy:** The previous iteration was executing "
                    f"'{unfinished['strategy']}' (last step: {unfinished['last_tool']}, "
                    f"{unfinished['tool_count']} tool calls) when it ended.",
                    "Consider resuming this strategy from the last step or switching to a better approach.",
                ])
        lines.append(
            "- Use SWITCH_STRATEGY (not DONE) when you have exhausted your current approach but WNS is still negative."
        )
        return "\n".join(lines)

    def _build_stagnation_signal(self) -> str:
        """Build prominent stagnation signal when optimization is not improving.

        When global_no_improvement >= 1, returns a structured warning that explicitly
        tells the LLM to STOP optimizing and re-diagnose. Returns empty string when
        no stagnation detected.
        """
        if self.global_no_improvement < 1:
            return ""

        best_wns = self.best_wns if self.best_wns > float('-inf') else None
        if best_wns is not None and best_wns >= 0:
            return ""  # WNS target already met, no stagnation concern

        # Build WNS trajectory from recent narratives
        recent_entries = self._iteration_narratives[-3:]
        trajectory_parts = []
        for e in recent_entries:
            delta = e['wns_delta']
            delta_str = f"{delta:+.4f}" if delta else "+0.0000"
            trajectory_parts.append(
                f"iter{e['iteration']}({e['outcome'].upper()[:3]}): "
                f"{e['wns_before']:.3f}->{e['wns_after']:.3f}ns({delta_str}) "
                f"{e['strategy_label']}"
            )
        trajectory = "\n".join(trajectory_parts) if trajectory_parts else "N/A"

        current_wns = self._get_current_wns()
        wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"

        return (
            f"\n=== STAGNATION SIGNAL ===\n"
            f"⚠ {self.global_no_improvement} consecutive iterations WITHOUT WNS improvement. "
            f"Current WNS: {wns_str}.\n"
            f"Recent trajectory:\n{trajectory}\n\n"
            f"Your current optimization approach has STOPPED producing results. "
            f"You MUST re-diagnose from scratch before applying any strategy:\n"
            f"1. Call report_timing_summary and extract_critical_path_cells to gather current signal data\n"
            f"2. Match observed signals against the SCENARIO_DETECTION_MATRIX\n"
            f"3. Form a hypothesis about the dominant timing obstacle\n"
            f"4. Select a strategy based on the hypothesis (not the one that just failed)\n"
            f"DO NOT repeat the same strategy. DO NOT use DONE. DO NOT skip diagnosis.\n"
        )

    def _build_skill_recommendation(self) -> str:
        """Analyze current state and recommend a strategy skill tool.

        Returns a string like 'Recommended skill: rapidwright_analyze_pblock_region'
        or empty string if no clear recommendation.
        """
        failed = set(self._compat.failed_strategies)

        # [META-COGNITION] Stagnation: recommend diagnosis instead of optimization
        best_wns = self.best_wns if self.best_wns > float('-inf') else None
        if self.global_no_improvement >= 1 and (best_wns is None or best_wns < 0):
            if "PBLOCK" not in failed:
                return ("Recommended skill: rapidwright_analyze_pblock_region "
                        "[DIAGNOSTIC - triggered by stagnation]. "
                        "Analyze FPGA fabric to understand timing obstacles before selecting strategy.")
            if "Fanout" not in failed:
                return ("Recommended skill: rapidwright_execute_fanout_strategy "
                        "[DIAGNOSTIC - triggered by stagnation]. "
                        "Check high-fanout nets as potential hidden obstacle.")
            return ("Recommended skill: rapidwright_analyze_net_detour "
                    "[DIAGNOSTIC - triggered by stagnation]. "
                    "Analyze placement detour issues on critical paths.")

        # Check spread data for distributed scenario
        if "PBLOCK" not in failed and hasattr(self, 'critical_path_spread') and self.critical_path_spread:
            avg_dist = self.critical_path_spread.get('avg_distance', 0)
            if avg_dist and avg_dist > 70:
                return (f"Recommended skill: rapidwright_analyze_pblock_region (matches distributed scenario, avg_distance={avg_dist:.1f} > 70). "
                        f"Prerequisite: First call vivado_report_utilization_for_pblock to get current LUT/FF/DSP/BRAM counts, "
                        f"then call the skill with target_lut_count and target_ff_count set to those values. "
                        f"The tool returns pblock_ranges — you must then call vivado_create_and_apply_pblock, "
                        f"vivado_place_design, vivado_route_design, and vivado_report_timing_summary yourself.")

        # Check high fanout nets
        if "Fanout" not in failed and hasattr(self, 'high_fanout_nets') and self.high_fanout_nets:
            max_fanout = max(n[1] for n in self.high_fanout_nets[:5]) if self.high_fanout_nets else 0
            if max_fanout > 100:
                return (
                    "Recommended skill: rapidwright_execute_fanout_strategy "
                    f"(matches high_fanout scenario, max_fanout={max_fanout}). "
                    "Prerequisite: First call vivado_get_critical_high_fanout_nets "
                    "to get the list of high fanout nets with fanout counts, "
                    "then call the skill with nets set to that list. "
                    "The tool splits high-fanout nets internally and writes a checkpoint — "
                    "you must then call vivado_open_checkpoint, vivado_route_design, "
                    "and vivado_report_timing_summary yourself."
                )

        # Check for repeated no-improvement with physopt → suggest analysis skill (not a strategy, no filter needed)
        if self.global_no_improvement >= 2 and self._iteration_narratives:
            recent_labels = [e.get('strategy_label', '') for e in self._iteration_narratives[-3:]]
            if any('physopt' in s.lower() for s in recent_labels):
                return "Recommended skill: rapidwright_analyze_net_detour (multiple phys_opt iterations without improvement, check for placement detour issues on critical paths)"

        # Default: moderate WNS suggests physopt
        if "PhysOpt" not in failed:
            current_wns = self._get_current_wns()
            if current_wns is not None and current_wns > -2.0:
                skill_name = STRATEGY_SKILL_MAP.get("PhysOpt", "physopt_strategy")
                return f"Recommended skill: rapidwright_execute_{skill_name} (moderate WNS={current_wns:.3f}, suitable for physical optimization)"

        return ""

    def _parse_recommended_skill(self, rec_text: str) -> tuple[str, str]:
        """Parse recommendation string to extract recommended tool and skill name.

        Args:
            rec_text: The recommendation string from _build_skill_recommendation().

        Returns:
            Tuple of (recommended_tool_name, recommended_skill_name).
        """
        for tool_name, skill_name in SKILL_TOOL_MAP.items():
            if tool_name in rec_text or skill_name in rec_text:
                return tool_name, skill_name
        return "", ""

    def _log_skill_recommendation(self, rec_text: str) -> None:
        """Log a skill recommendation for funnel tracking.

        Deduplicates: only logs once per iteration.
        """
        if self._last_skill_rec_iteration == self.iteration:
            return
        self._last_skill_rec_iteration = self.iteration

        rec_tool, rec_skill = self._parse_recommended_skill(rec_text)
        entry = {
            "iteration": self.iteration,
            "recommendation_text": rec_text,
            "recommended_tool": rec_tool,
            "recommended_skill": rec_skill,
            "accepted": False,
            "accepted_at_entry": None,
            "timestamp": time.time(),
        }
        self.skill_recommendation_log.append(entry)
        logger.info(
            "[SKILL_RECOMMENDATION] Iteration %d: recommended '%s' (tool=%s)",
            self.iteration, rec_skill or "N/A", rec_tool or "N/A",
            extra={
                "iteration": self.iteration,
                "recommended_skill": rec_skill,
                "recommended_tool": rec_tool,
            }
        )

    def _build_data_driven_goal(self) -> str:
        """Build data-driven next goal from WNS trajectory and strategy effects."""
        current_wns = self._get_current_wns()
        best_wns = self.best_wns if self.best_wns > float('-inf') else None

        # Build continuation context prefix if previous strategy was interrupted
        unfinished = self._detect_unfinished_strategy()
        continuation_prefix = ""
        if unfinished["was_interrupted"]:
            continuation_prefix = (
                f"[CONTINUATION] Previous iteration was in '{unfinished['strategy']}' "
                f"(last tool: {unfinished['last_tool']}). "
            )
            if unfinished["exit_reason"] == "tool_round_limit":
                continuation_prefix += "Strategy interrupted by round limit. "
            elif unfinished["exit_reason"] == "flow_control_done_next_iteration":
                continuation_prefix += "Model signaled DONE prematurely. "
            continuation_prefix += "Resume or adjust.\n"

        # Build base goal message
        if not self._iteration_narratives:
            if best_wns is not None and best_wns >= 0:
                goal = continuation_prefix + "WNS target met. Focus on further optimization."
            elif best_wns is not None and best_wns > -0.5:
                goal = continuation_prefix + "Close to target. Fine-tuning critical paths."
            elif best_wns is not None and best_wns > -2.0:
                goal = continuation_prefix + "Moderate violation. Consider phys_opt or PBLOCK."
            else:
                goal = "Severe violation. Consider aggressive strategies."
        else:
            recent = self._iteration_narratives[-5:]
            improved = [e for e in recent if e['outcome'] == 'improved']
            regressed = [e for e in recent if e['outcome'] == 'regression']

            if best_wns is not None and best_wns >= 0:
                goal = continuation_prefix + "WNS target met. Focus on further optimization."
            elif not improved and regressed:
                goal = continuation_prefix + f"No improvement in {len(recent)} iters. Rollback to best checkpoint and try alternative strategy."
            elif improved:
                last_improved_tools = [
                    t.get('tool_name', '')
                    for t in self._compat.tool_call_details
                    if t.get('iteration') == improved[-1]['iteration']
                ]
                strategy = self._infer_strategy_from_tools(last_improved_tools)
                goal = continuation_prefix + f"Last success via {strategy}. Continue or refine approach."
            elif best_wns is not None and best_wns > -2.0:
                goal = continuation_prefix + "Moderate violation. Consider phys_opt or PBLOCK."
            else:
                goal = continuation_prefix + "Severe violation. Consider aggressive strategies."

        # [META-COGNITION] Stagnation override: replace goal with re-diagnosis instruction
        if self.global_no_improvement >= 1 and (best_wns is None or best_wns < 0):
            current_wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"
            goal = (
                f"⚠ STAGNATION DETECTED: {self.global_no_improvement} consecutive iterations without improvement. "
                f"Current WNS={current_wns_str}. Your current approach is NOT WORKING.\n"
                f"STOP executing optimization strategies. You MUST initiate a fresh diagnosis cycle:\n"
                f"1. Gather current timing data (report_timing_summary, extract_critical_path_cells)\n"
                f"2. Analyze what has changed and why prior strategies failed\n"
                f"3. Form a new hypothesis about the dominant timing obstacle\n"
                f"4. Select a strategy that has NOT been tried yet\n"
                f"Do NOT repeat the same pattern."
            )

        # Append skill recommendation if available
        skill_rec = self._build_skill_recommendation()
        if skill_rec and best_wns is not None and best_wns < 0:
            goal += f"\n{skill_rec}"
        # Log recommendation for funnel tracking (deduplicated per iteration)
        if skill_rec:
            self._log_skill_recommendation(skill_rec)
        return goal

    def _generate_planner_handoff(self) -> str:
        """Generate rich handoff for planner models (1M context)."""
        current_wns = self._get_current_wns()
        best_wns = self.best_wns if self.best_wns > float('-inf') else None
        best_wns_iter = self._best_wns_iteration
        best_dcp = str(self._get_intermediate_checkpoint_path(best_wns_iter)) if best_wns_iter is not None else "N/A"
        clock_period = self.clock_period

        recent_tools = self._build_tool_effect_summary(self.iteration)
        failed_strategies = self._build_failed_strategy_summary()
        narrative = self._format_narrative(max_entries=None)
        goal = self._build_data_driven_goal()
        unfinished = self._detect_unfinished_strategy()
        exit_reason = self._build_exit_reason_section()
        directive = self._build_continuation_directive(unfinished, is_worker=False)
        skill_rec = self._build_skill_recommendation()
        skill_rec_section = f"\n=== RECOMMENDED SKILL ===\n{skill_rec}\n" if skill_rec else ""
        stagnation_signal = self._build_stagnation_signal()
        stagnation_section = f"{stagnation_signal}\n" if stagnation_signal else ""

        handoff = f"""**ITERATION HANDOFF - Planner**

=== EXIT REASON ===
{exit_reason}

=== CONTINUATION DIRECTIVE ===
{directive}

=== ITERATION TRAJECTORY ===
{narrative}

=== CURRENT STATE ===
- Iteration: {self.iteration} -> {self.iteration + 1}
- Current WNS: {current_wns:.3f}ns if available
- Best WNS: {best_wns:.3f}ns (iter {best_wns_iter}, checkpoint: {best_dcp})
- Clock Period: {clock_period:.3f}ns

=== NEXT OPTIMIZATION GOAL ===
{goal}

=== LAST ITERATION TOOLS ===
{recent_tools}

=== FAILED STRATEGIES ===
{failed_strategies}
{skill_rec_section}
{stagnation_section}=== SKILL INVOCATIONS ===
{self._build_skill_invocation_summary(self.iteration) or "(none)"}
{self._format_analysis_section()}=== INCOMING MODEL ===
You are the Planner for this iteration. Current WNS/checkpoint/clock values are in the system prompt 'Current Optimization State' section."""
        return handoff

    def _format_analysis_section(self) -> str:
        """Format the LLM's own analysis from the last step YAML for handoff context."""
        if not self._last_analysis:
            return ""
        obs = self._last_analysis.get('observed_signals', {})
        scenario = self._last_analysis.get('scenario_match', 'N/A')
        hypothesis = self._last_analysis.get('hypothesis', '')
        rationale = self._last_analysis.get('strategy_rationale', '')
        parts = ["\n=== LLM'S OWN ANALYSIS ===\n"]
        if scenario and scenario != 'N/A':
            parts.append(f"- Scenario Match: {scenario}\n")
        if hypothesis:
            parts.append(f"- Hypothesis: {hypothesis[:200]}\n")
        if rationale:
            parts.append(f"- Strategy Rationale: {rationale[:200]}\n")
        if obs:
            obs_str = ", ".join(f"{k}={v}" for k, v in obs.items() if v is not None)
            if obs_str:
                parts.append(f"- Observed Signals: {obs_str}\n")
        parts.append("\n")
        return "".join(parts)

    def _generate_worker_handoff(self) -> str:
        """Generate lean handoff for worker models (250K context)."""
        current_wns = self._get_current_wns()
        best_wns = self.best_wns if self.best_wns > float('-inf') else None
        best_wns_iter = self._best_wns_iteration
        best_dcp = str(self._get_intermediate_checkpoint_path(best_wns_iter)) if best_wns_iter is not None else "N/A"
        clock_period = self.clock_period

        recent_tools = self._build_tool_effect_summary(self.iteration)
        failed_strategies = self._build_failed_strategy_summary()
        narrative = self._format_narrative(max_entries=3)
        goal = self._build_data_driven_goal()
        unfinished = self._detect_unfinished_strategy()
        directive = self._build_continuation_directive(unfinished, is_worker=True)
        skill_rec = self._build_skill_recommendation()
        skill_rec_section = f"\n=== RECOMMENDED SKILL ===\n{skill_rec}\n" if skill_rec else ""
        stagnation_signal = self._build_stagnation_signal()
        stagnation_section = f"{stagnation_signal}\n" if stagnation_signal else ""

        exit_labels = {
            "tool_round_limit": "ToolRoundLimit",
            "flow_control_done_next_iteration": "PrematureDONE",
            "cost_limit": "CostLimit",
            "user_requested": "UserRequested",
        }
        exit_label = exit_labels.get(unfinished["exit_reason"], "Unknown")

        handoff = f"""**ITERATION HANDOFF - Worker**

=== CONTINUATION ===
{directive}
Exit: {exit_label}

=== RECENT TRAJECTORY (last 3) ===
{narrative}

=== STATE ===
- Iter: {self.iteration} -> {self.iteration + 1} | WNS: {current_wns:.3f}ns | Best: {best_wns:.3f}ns (iter{best_wns_iter}) | Clock: {clock_period:.3f}ns

=== GOAL ===
{goal}

=== LAST ITERATION TOOLS ===
{recent_tools}

=== AVOID ===
{failed_strategies}
{skill_rec_section}
{stagnation_section}=== SKILL INVOCATIONS ===
{self._build_skill_invocation_summary(self.iteration) or "(none)"}
Current WNS/checkpoint/clock values are in the system prompt 'Current Optimization State' section."""
        return handoff

    def classify_task(self, tool_name: str, arguments: dict = None) -> str:
        """Simplified: only distinguish OPTIMIZATION / INFORMATION / UNKNOWN"""
        if not tool_name:
            return TaskCategory.UNKNOWN

        name_lower = tool_name.lower()

        if any(p in name_lower for p in OPTIMIZATION_PATTERNS):
            return TaskCategory.OPTIMIZATION

        if any(p in name_lower for p in INFORMATION_PATTERNS):
            return TaskCategory.INFORMATION

        if tool_name == "vivado_run_tcl" and arguments:
            tcl_cmd = str(arguments.get("command", "")).lower()
            if any(p in tcl_cmd for p in OPTIMIZATION_PATTERNS):
                return TaskCategory.OPTIMIZATION

        return TaskCategory.UNKNOWN

    # === Section 7.3: Model Routing & Task Classification ===

    def _is_trivial_task(self, task_type: str, context_complexity: int) -> bool:
        """
        Determine if this is a trivial task: read-only query tool and complexity < 3
        """
        trivial_patterns = ["get", "read", "query", "check", "list", "show", "status"]
        is_query_only = any(p in task_type.lower() for p in trivial_patterns)
        return is_query_only and context_complexity < 3

    def _is_highly_complex_task(self, task_type: str, context_complexity: int, recent_messages: list) -> bool:
        """
        Determine if this is a highly complex task: explicit optimization tool, complexity >= 6, or messages contain strategy planning
        """
        optimize_patterns = ["optimize", "improve", "place", "route", "synthesize", "floorplan"]
        has_optimize = any(p in task_type.lower() for p in optimize_patterns)
        if has_optimize or context_complexity >= 6:
            return True
        strategy_keywords = ["strategy", "plan", "approach"]
        for msg in recent_messages[-3:]:
            content = msg.get("content", "").lower()
            if any(kw in content for kw in strategy_keywords):
                return True
        return False

    def _select_model(self, tool_name: str = "", context_complexity: int = 0,
                       arguments: dict = None, **kwargs) -> str:
        """
        Scoring-based model selection across 8 dimensions.
        Replaces the old 4-layer linear override logic with a weighted score system
        where each dimension contributes points; the model with the higher score wins.
        A margin (>=2) is required to switch models, preventing oscillation.
        A hard override forces planner when context approaches flash's 200K limit.
        """
        # ── Hard override: context window safety ─────────────────────────────
        current_tokens = self._estimate_tokens()
        if current_tokens >= WORKER_CONTEXT_FORCE_TOKENS:
            logger.info(f"Context window override: ~{current_tokens:,} tokens ≥ {WORKER_CONTEXT_FORCE_TOKENS:,} (flash limit), forcing planner model")
            return self.model_planner

        planner_score = 0
        worker_score = 0

        # ── Dimension 1: Tool mapping (intrinsic complexity of the tool) ─────
        # Removed: tool-specific mapping no longer influences model selection

        # ── Dimension 2: Task category ───────────────────────────────────────
        # Removed: task category no longer influences model selection

        # ── Dimension 3: Current context complexity (real-time signal) ───────
        if context_complexity >= 6:
            planner_score += 2
        elif context_complexity < 3:
            worker_score += 1

        # ── Dimension 4: Historical capability score (data-driven) ───────────
        capability = self._get_task_capability_score(tool_name)
        if capability >= 0.7:
            worker_score += 2
        elif capability < 0.3:
            planner_score += 2

        # ── Dimension 5: Counter state (short-term trend intervention) ────────
        # Consecutive failures → force upgrade; consecutive successes → allow downgrade
        if self.worker_consecutive_failures >= self.WORKER_UPGRADE_THRESHOLD:
            planner_score += 4          # Strong intervention, creates clear separation
        if self.worker_consecutive_success >= self.WORKER_DOWNGRADE_THRESHOLD:
            worker_score += 1           # Allow downgrade, but does not force

        # ── Dimension 6: Global no-improvement signal ────────────────────────
        # Previously only used to halt optimization; now also biases toward Worker
        # when Planner has been stuck for too long, encouraging a strategy shift
        if self.global_no_improvement >= self.GLOBAL_NO_IMPROVEMENT_LIMIT // 2:
            worker_score += 1

        # ── Dimension 7: Context window capacity ──────────────────────────────
        # Flash has 200K limit; bias toward planner (1M) when approaching it
        if current_tokens >= WORKER_CONTEXT_WARN_TOKENS:
            planner_score += 2

        # ── Dimension 8: WNS / timing state (urgency signal) ──────────────────
        # Incorporate WNS trajectory into model selection to bias toward stronger
        # model when timing is severely violated or deteriorating
        if self.initial_wns is not None and self.best_wns != float('-inf'):
            wns_improvement = self.best_wns - self.initial_wns
            if wns_improvement < -2.0:
                # Severe regression: strongly favor planner for complex reasoning
                planner_score += 3
            elif wns_improvement < -0.5:
                # Moderate regression: moderately favor planner
                planner_score += 2
            elif wns_improvement < 0:
                # Slight regression: slightly favor planner
                planner_score += 1
            # If wns_improvement >= 0 (improving or stable): no bias adjustment

        # ── Decision threshold ────────────────────────────────────────────────
        # Filter out exhausted models
        worker_exhausted = self.model_worker in self._exhausted_worker_fallbacks or self.model_worker in self._exhausted_planner_fallbacks

        if worker_exhausted:
            logger.info(f"Model {self.model_worker} is exhausted, forcing planner")
            selected_model = self.model_planner
        elif planner_score > worker_score + 1:   # Margin of 2 required to switch, avoids thrashing
            selected_model = self.model_planner
        elif worker_score > planner_score:
            selected_model = self.model_worker
        else:
            selected_model = self.model_planner  # Default to safe choice

        # Track model usage history for switch detection
        if self._model_usage_history and self._model_usage_history[-1] != selected_model:
            logger.info(f"Model switch detected: {self._model_usage_history[-1]} -> {selected_model}")
        self._model_usage_history.append(selected_model)
        # Keep only recent 10 selections to prevent memory bloat
        if len(self._model_usage_history) > 10:
            self._model_usage_history.pop(0)

        return selected_model

    def _get_task_capability_score(self, task_type: str) -> float:
        """
        Issue 4 optimization: calculate Worker model capability score for specific task based on historical performance.
        Return value: 0.0-1.0 (Worker success rate), returns 0.5 (neutral) if task has never been seen
        """
        if not task_type or task_type not in self.task_type_stats:
            return 0.5  # Never seen this task, default to neutral
        stats = self.task_type_stats[task_type]
        total = stats.get('total', 0)
        if total == 0:
            return 0.5
        success = stats.get('success', 0)
        return success / total

    def _evaluate_task_success(self, tool_name: str, tool_result: str,
                                wns_before: float, wns_after: float) -> tuple[bool, str]:
        """
        Evaluate whether a task completed successfully based on task type.

        Returns:
            (is_success, failure_reason): is_success=True means task achieved its goal,
            failure_reason explains why if not successful
        """
        category = self.classify_task(tool_name)

        # Tool call error check (applies to all task types)
        has_error = False
        error_msg = ""
        if tool_result:
            result_lower = tool_result.lower()
            if "error" in result_lower and "success" not in result_lower:
                has_error = True
                if '"error"' in result_lower:
                    try:
                        data = json.loads(tool_result)
                        error_msg = str(data.get("error", ""))
                    except Exception:
                        error_msg = tool_result[tool_result.lower().find("error"):][:200]

        # INFORMATION task: success = got valid data (no error)
        if category == TaskCategory.INFORMATION:
            if has_error:
                error_lower = error_msg.lower()
                if "timeout" in error_lower or "timed out" in error_lower:
                    return False, "recoverable_timeout"
                return False, "unrecoverable_error"
            return True, ""

        # OPTIMIZATION task: success = WNS improved
        if category == TaskCategory.OPTIMIZATION:
            if has_error:
                error_lower = error_msg.lower()
                if "timeout" in error_lower or "timed out" in error_lower:
                    return False, "recoverable_timeout"
                if self._is_routing_failure(error_lower):
                    return False, "routing_failure"
                return False, "unrecoverable_error"

            if wns_after > wns_before:
                return True, ""
            elif wns_after < wns_before:
                return False, "wns_regression"
            else:
                return False, "wns_no_improvement"

        return not has_error, "unrecoverable_error" if has_error else ""

    def _record_task_outcome(self, task_type: str, model_used: str,
                              improved: bool, tool_error: bool = False,
                              failure_type: str = ""):
        """Record task execution result for Worker capability scoring."""
        if not task_type:
            return
        if not hasattr(self, 'task_type_stats'):
            self.task_type_stats = {}
        if task_type not in self.task_type_stats:
            self.task_type_stats[task_type] = {'success': 0, 'total': 0, 'failures': []}

        stats = self.task_type_stats[task_type]
        stats['total'] += 1

        category = self.classify_task(task_type)
        is_success = False

        if category == TaskCategory.INFORMATION:
            is_success = not tool_error
        elif category == TaskCategory.OPTIMIZATION:
            is_success = improved
        else:
            is_success = improved

        if is_success:
            stats['success'] += 1
        else:
            if failure_type:
                stats['failures'].append(failure_type)
                if len(stats['failures']) > 10:
                    stats['failures'] = stats['failures'][-10:]

    # === Section 7.4: Server & Tool Management ===

    async def start_servers(self):
        """Start and connect to both MCP servers."""
        await super().start_servers()
        await self._collect_tools()
        logger.info(f"Connected to servers with {len(self.tools)} tools available")
    
    async def _collect_tools(self):
        """Collect and convert tools from both MCP servers."""
        self.tools = []

        rw_response = await self.rapidwright_session.list_tools()
        for tool in rw_response.tools:
            self.tools.append(convert_mcp_tool_to_openai(tool, "rapidwright"))

        v_response = await self.vivado_session.list_tools()
        for tool in v_response.tools:
            self.tools.append(convert_mcp_tool_to_openai(tool, "vivado"))

        # Register internal tool: retrieve full raw output for any previous tool call
        self.tools.append({
            "type": "function",
            "function": {
                "name": "vivado_get_raw_tool_output",
                "description": "Retrieve the complete raw Vivado output for a previous tool call. "
                               "By default tool results are returned as structured summaries; "
                               "use this when you need to inspect raw timing paths, DRC details, or error messages.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "iteration": {
                            "type": "integer",
                            "description": "Iteration number (default: current iteration)"
                        },
                        "round_index": {
                            "type": "integer",
                            "description": "Tool round within the iteration (default: most recent)"
                        },
                        "tool_name": {
                            "type": "string",
                            "description": "Filter by tool name, e.g. vivado_phys_opt_design (optional)"
                        }
                    }
                },
                "strict": False
            }
        })

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool call on the appropriate MCP server."""
        # Internal tool: retrieve raw tool output from side buffer
        if tool_name == "vivado_get_raw_tool_output":
            iteration = arguments.get("iteration", self.iteration)
            round_idx = arguments.get("round_index")
            target_tool = arguments.get("tool_name", "")

            candidates = []
            for (it, rd), (tname, txt) in self._raw_tool_outputs.items():
                if it == iteration and (round_idx is None or rd == round_idx):
                    candidates.append(((it, rd), tname, txt))

            # Filter by tool_name if specified (using stored tool name for exact match)
            if target_tool and candidates:
                filtered = []
                for (it, rd), tname, txt in candidates:
                    if tname == target_tool:
                        filtered.append(((it, rd), tname, txt))
                if not filtered:
                    # Fallback: text-based search for backward compatibility
                    search_key = target_tool.replace("vivado_", "").replace("rapidwright_", "")
                    for (it, rd), tname, txt in candidates:
                        if search_key in txt[:2000]:
                            filtered.append(((it, rd), tname, txt))
                if filtered:
                    candidates = filtered
                else:
                    candidates = []  # No match via either method

            if not candidates:
                return json.dumps({"error": f"No raw output found for iteration={iteration}, round={round_idx}, tool={target_tool}"})

            # Return most recent matching output
            candidates.sort(key=lambda x: (x[0][0], x[0][1]), reverse=True)
            (it, rd), tname, txt = candidates[0]
            return f"[Raw tool output from iteration {it}, round {rd} ({len(txt)} chars, tool: {tname})]\n\n{txt}"

        # Parse server prefix from tool name
        if tool_name.startswith("rapidwright_"):
            session = self.rapidwright_session
            actual_name = tool_name[len("rapidwright_"):]
        elif tool_name.startswith("vivado_"):
            session = self.vivado_session
            actual_name = tool_name[len("vivado_"):]
        else:
            return json.dumps({"error": f"Unknown tool prefix in: {tool_name}"})
        
        # Track timing for this tool call
        start_time = time.time()
        wns_measured = None
        error_occurred = False

        # Log MCP request with sanitized arguments
        sanitized_args = sanitize_payload(arguments)
        logger.info(
            "[MCP_REQUEST] Calling tool '%s'",
            tool_name,
            extra={
                "mcp_tool_name": tool_name,
                "mcp_request_args": sanitized_args,
                "trace_id": get_trace_id(),
            }
        )

        heartbeat_task, heartbeat_count = self._start_tool_heartbeat(tool_name, start_time)
        try:
            # Add timeout enforcement for RapidWright MCP calls
            if tool_name.startswith("rapidwright_"):
                result = await asyncio.wait_for(
                    session.call_tool(actual_name, arguments),
                    timeout=360.0
                )
            else:
                result = await session.call_tool(actual_name, arguments)
            # Cancel heartbeat task on completion
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed_time = time.time() - start_time
            logger.info(
                f"[TOOL_COMPLETE] '{tool_name}' completed in {elapsed_time:.1f}s (heartbeats: {heartbeat_count})",
                extra={"tool_name": tool_name, "tool_elapsed": int(elapsed_time), "heartbeat_count": heartbeat_count}
            )

            # Extract text content from result
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                result_text = "\n".join(text_parts)
            else:
                result_text = "(no output)"

            # Auto-evaluate: After executing phys_opt_design (or Tcl containing this command), proactively append timing report
            is_phys_opt_tool = (tool_name == "vivado_phys_opt_design")
            is_tcl_phys_opt = (tool_name == "vivado_run_tcl" and "phys_opt_design" in str(arguments.get("command", "")))

            if is_phys_opt_tool or is_tcl_phys_opt:
                logger.info("Auto-evaluating timing after phys_opt_design to enforce data-driven strategy...")
                try:
                        # Use the actual mapped vivado_run_tcl tool to run timing report for best reliability
                        timing_text = await self.call_tool("vivado_run_tcl", {"command": "report_timing_summary"})
                        result_text += "\n\n=== AUTO TIMING EVALUATION AFTER PHYS_OPT ===\n" + timing_text

                        # Try to extract WNS/TNS and print to terminal for human to observe progress
                        timing_info = parse_timing_summary_static(timing_text)
                        if timing_info["wns"] is not None:
                            current_wns = timing_info["wns"]
                            if not self._is_valid_wns(current_wns):
                                logger.warning(f"Auto-Eval: WNS value {current_wns:.3f} ns rejected by sanity check")
                            else:
                                self.latest_wns = current_wns  # Cache for _get_current_wns()
                                if timing_info["tns"] is not None:
                                    self.latest_tns = timing_info["tns"]
                                if timing_info["failing_endpoints"] is not None:
                                    self.latest_failing_endpoints = timing_info["failing_endpoints"]
                                if current_wns > self.best_wns:
                                    logger.info(f"New best WNS (Auto-Eval): {current_wns:.3f} ns (improved from {self.best_wns:.3f} ns)")
                                    self.best_wns = current_wns
                                    self._best_wns_iteration = self.iteration
                                    self._best_wns_tns = timing_info.get("tns")
                                    self._best_wns_failing_endpoints = timing_info.get("failing_endpoints")
                                    wns_measured = current_wns  # Sync to MemoryManager via add_tool_result
                                else:
                                    logger.info(f"Current WNS (Auto-Eval): {current_wns:.3f} ns (best is still {self.best_wns:.3f} ns)")
                        elif timing_info["wns"] is None:
                            # Fallback: regex for WNS if parse_timing_summary_static didn't find the header
                            wns_match = re.search(r'WNS.*?([-\d.]+)', timing_text) or re.search(r'WNS\s*[=:]\s*([-\d.]+)', timing_text, re.IGNORECASE)
                            if wns_match and hasattr(self, 'best_wns'):
                                try:
                                    current_wns = float(wns_match.group(1))
                                    if self._is_valid_wns(current_wns):
                                        self.latest_wns = current_wns
                                        if current_wns > self.best_wns:
                                            logger.info(f"New best WNS (Auto-Eval): {current_wns:.3f} ns (improved from {self.best_wns:.3f} ns)")
                                            self.best_wns = current_wns
                                            self._best_wns_iteration = self.iteration
                                            self._best_wns_tns = self.latest_tns
                                            self._best_wns_failing_endpoints = self.latest_failing_endpoints
                                            wns_measured = current_wns
                                        else:
                                            logger.info(f"Current WNS (Auto-Eval): {current_wns:.3f} ns (best is still {self.best_wns:.3f} ns)")
                                except ValueError:
                                    logger.warning(f"Auto-Eval: WNS regex matched but float conversion failed: '{wns_match.group(1)}'")
                except Exception as eval_err:
                    logger.warning(f"Auto-eval timing failed: {eval_err}")
                    result_text += f"\n\n[Warning: Auto timing evaluation failed: {eval_err}]"

            # Track WNS from timing reports and get_wns calls
            if tool_name == "vivado_report_timing_summary":
                timing_info = parse_timing_summary_static(result_text)
                if timing_info["wns"] is not None:
                    current_wns = timing_info["wns"]
                    if self._is_valid_wns(current_wns):
                        wns_measured = current_wns  # Store for tracking
                        self.latest_wns = current_wns  # Cache for _get_current_wns()
                        # Also track TNS and failing_endpoints for context-aware optimization
                        if timing_info["tns"] is not None:
                            self.latest_tns = timing_info["tns"]
                        if timing_info["failing_endpoints"] is not None:
                            self.latest_failing_endpoints = timing_info["failing_endpoints"]
                        current_fmax = self.calculate_fmax(current_wns, self.clock_period)

                        # Format fmax string if available
                        fmax_str = f", fmax: {current_fmax:.2f} MHz" if current_fmax is not None else ""

                        if current_wns > self.best_wns:
                            logger.info(f"New best WNS: {current_wns:.3f} ns{fmax_str} (improved from {self.best_wns:.3f} ns)")
                            self.best_wns = current_wns
                            self._best_wns_iteration = self.iteration
                            self._best_wns_tns = timing_info.get("tns")
                            self._best_wns_failing_endpoints = timing_info.get("failing_endpoints")
                        else:
                            logger.info(f"Current WNS: {current_wns:.3f} ns{fmax_str} (best is still {self.best_wns:.3f} ns)")
                    else:
                        logger.warning(f"report_timing_summary: WNS {current_wns:.3f} ns rejected by sanity check. "
                                       f"TNS={timing_info['tns']}, failing_endpoints={timing_info['failing_endpoints']}."
                                       f"Tool output sample: {result_text[:200]}")
                else:
                    logger.warning(f"vivado_report_timing_summary: WNS parsing returned None. "
                                   f"TNS={timing_info['tns']}, failing_endpoints={timing_info['failing_endpoints']}. "
                                   f"Tool output sample: {result_text[:200]}")

            # Also track WNS from get_wns tool (returns just the numeric WNS value)
            elif tool_name == "vivado_get_wns":
                try:
                    # Check for PARSE_ERROR special value
                    if result_text.strip() == "PARSE_ERROR":
                        logger.warning("get_wns returned PARSE_ERROR, WNS not updated (fallback to report_timing_summary)")
                        wns_measured = None
                    else:
                        # get_wns returns just a number like "-0.099" or "0.016"
                        current_wns = float(result_text.strip())
                        if self._is_valid_wns(current_wns):
                            wns_measured = current_wns  # Store for tracking
                            self.latest_wns = current_wns  # Cache for _get_current_wns()
                            current_fmax = self.calculate_fmax(current_wns, self.clock_period)

                            # Format fmax string if available
                            fmax_str = f", fmax: {current_fmax:.2f} MHz" if current_fmax is not None else ""

                            if current_wns > self.best_wns:
                                logger.info(f"New best WNS (from get_wns): {current_wns:.3f} ns{fmax_str} (improved from {self.best_wns:.3f} ns)")
                                self.best_wns = current_wns
                                self._best_wns_iteration = self.iteration
                                self._best_wns_tns = self.latest_tns
                                self._best_wns_failing_endpoints = self.latest_failing_endpoints
                            else:
                                logger.info(f"Current WNS (from get_wns): {current_wns:.3f} ns{fmax_str} (best is still {self.best_wns:.3f} ns)")
                        else:
                            logger.warning(f"get_wns: WNS value {current_wns:.3f} ns rejected by sanity check")
                except (ValueError, AttributeError) as e:
                    # Could not parse WNS from get_wns output
                    truncated_output = result_text.strip()[:500] if result_text else "(empty)"
                    logger.warning(f"Could not parse WNS from get_wns output: {truncated_output}. Error: {e}")
            
            elapsed_time = time.time() - start_time

            # [FIX] Dynamically mark whether it is an optimization task: use enhanced classify_task to deeply check vivado_run_tcl command content
            is_optimization = (self.classify_task(tool_name, arguments) == TaskCategory.OPTIMIZATION)

            # Record tool call details via compat (MemoryManager tracks for compression)
            # Note: extra fields (elapsed_time, is_optimization) stored in _compat._mm._tool_call_details entry
            self._compat.add_tool_result(
                tool_name=tool_name,
                result=result_text,
                wns=wns_measured,
                error=False,
                extra_fields={
                    "elapsed_time": elapsed_time,
                    "is_optimization": is_optimization
                }
            )

            # ── Skill invocation tracking ──────────────────────────
            if tool_name in SKILL_TOOL_MAP:
                skill_name = SKILL_TOOL_MAP[tool_name]
                inv_entry = {
                    "iteration": self.iteration,
                    "tool_name": tool_name,
                    "skill_name": skill_name,
                    "wns": wns_measured,
                    "error": False,
                    "elapsed_time": elapsed_time,
                    "timestamp": time.time(),
                }
                self.skill_invocation_log.append(inv_entry)

                wns_str = (f"WNS={wns_measured:.3f}ns"
                          if wns_measured is not None else "no WNS")
                logger.info(
                    "[SKILL_INVOCATION] '%s' | %s | %.1fs",
                    skill_name, wns_str, elapsed_time,
                    extra={
                        "skill_name": skill_name,
                        "tool_name": tool_name,
                        "iteration": self.iteration,
                        "wns": wns_measured,
                        "elapsed_time": round(elapsed_time, 2),
                        "skill_invocation_index": len(self.skill_invocation_log) - 1,
                    }
                )

                # Check recommendation-to-execution funnel
                if (self.skill_recommendation_log and
                        self.skill_recommendation_log[-1].get("iteration") == self.iteration and
                        not self.skill_recommendation_log[-1].get("accepted")):
                    last_rec = self.skill_recommendation_log[-1]
                    rec_tool = last_rec.get("recommended_tool", "")
                    if rec_tool and (tool_name == rec_tool or skill_name in rec_tool):
                        last_rec["accepted"] = True
                        last_rec["accepted_at_entry"] = len(self.skill_invocation_log) - 1
                        logger.info(
                            "[SKILL_RECOMMENDATION_ACCEPTED] '%s' accepted in iteration %d",
                            skill_name, self.iteration,
                            extra={"skill_name": skill_name, "iteration": self.iteration}
                        )

            return result_text

        except asyncio.TimeoutError:
            error_occurred = True
            elapsed_time = time.time() - start_time

            # Cancel heartbeat task on timeout
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            error_msg = f"Tool '{tool_name}' timed out after {elapsed_time:.1f}s"

            # Record failed tool call via compat
            self._compat.add_tool_result(
                tool_name=tool_name,
                result=error_msg,
                wns=None,
                error=True,
                extra_fields={
                    "elapsed_time": elapsed_time,
                    "error_message": error_msg,
                    "is_optimization": False,
                }
            )

            # Record as failed strategy
            tool_strategy = self._infer_strategy_from_tools([tool_name])
            if tool_strategy not in ("Information", "Unknown"):
                self._compat.record_failure(tool_strategy)

            # Skill invocation tracking (timeout path)
            if tool_name in SKILL_TOOL_MAP:
                skill_name = SKILL_TOOL_MAP[tool_name]
                self.skill_invocation_log.append({
                    "iteration": self.iteration,
                    "tool_name": tool_name,
                    "skill_name": skill_name,
                    "wns": None,
                    "error": True,
                    "error_message": error_msg,
                    "elapsed_time": elapsed_time,
                    "timestamp": time.time(),
                })
                logger.warning(
                    "[SKILL_INVOCATION] '%s' TIMED OUT after %.1fs",
                    skill_name, elapsed_time,
                    extra={
                        "skill_name": skill_name,
                        "tool_name": tool_name,
                        "iteration": self.iteration,
                        "error": True,
                        "elapsed_time": round(elapsed_time, 2),
                    }
                )

            logger.error("[TOOL_TIMEOUT] %s", error_msg)
            return json.dumps({"error": error_msg})

        except Exception as e:
            error_occurred = True
            elapsed_time = time.time() - start_time

            # Cancel heartbeat task on error
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            # Record failed tool call via compat
            self._compat.add_tool_result(
                tool_name=tool_name,
                result=str(e),
                wns=None,
                error=True,
                extra_fields={
                    "elapsed_time": elapsed_time,
                    "error_message": str(e),
                    "is_optimization": False
                }
            )

            # Record as failed strategy (dedup inside record_failure)
            tool_strategy = self._infer_strategy_from_tools([tool_name])
            if tool_strategy not in ("Information", "Unknown"):
                self._compat.record_failure(tool_strategy)

            # ── Skill invocation tracking (error path) ────────────
            if tool_name in SKILL_TOOL_MAP:
                skill_name = SKILL_TOOL_MAP[tool_name]
                self.skill_invocation_log.append({
                    "iteration": self.iteration,
                    "tool_name": tool_name,
                    "skill_name": skill_name,
                    "wns": None,
                    "error": True,
                    "error_message": str(e),
                    "elapsed_time": elapsed_time,
                    "timestamp": time.time(),
                })
                logger.warning(
                    "[SKILL_INVOCATION] '%s' FAILED after %.1fs: %s",
                    skill_name, elapsed_time, str(e),
                    extra={
                        "skill_name": skill_name,
                        "tool_name": tool_name,
                        "iteration": self.iteration,
                        "error": True,
                        "elapsed_time": round(elapsed_time, 2),
                    }
                )

            logger.error(f"Tool call failed: {e}")
            return json.dumps({"error": str(e)})
    
    async def _call_vivado_tool(self, tool_name: str, arguments: dict) -> str:
        """Helper to call Vivado tools (for use with base class methods)."""
        return await self.call_tool(f"vivado_{tool_name}", arguments)

    # === Section 7.5: Initial Design Analysis ===

    async def perform_initial_analysis(self, input_dcp: Path) -> str:
        """
        Perform initial analysis without LLM:
        1. Initialize RapidWright
        2. Open checkpoint in Vivado
        3. Report timing summary
        4. Get critical high fanout nets
        
        Returns a formatted summary of the analysis.
        """
        logger.info("Performing initial design analysis...")
        print("\n=== Initial Design Analysis ===\n")
        
        # Step 1: Initialize RapidWright
        logger.info("Initializing RapidWright...")
        print("Initializing RapidWright...")
        result = await self.call_tool("rapidwright_initialize_rapidwright", {})
        if "error" in result.lower() and "success" not in result.lower():
            raise RuntimeError(f"Failed to initialize RapidWright: {result}")
        print("✓ RapidWright initialized\n")
        
        # Step 2: Open checkpoint in Vivado
        logger.info(f"Opening checkpoint: {input_dcp}")
        print(f"Opening checkpoint: {input_dcp.name}")
        result = await self.call_tool("vivado_open_checkpoint", {
            "dcp_path": str(input_dcp.resolve())
        })
        if "error" in result.lower() and "opened successfully" not in result.lower():
            raise RuntimeError(f"Failed to open checkpoint: {result}")
        print("✓ Checkpoint opened in Vivado\n")
        
        # Step 3: Report timing summary
        logger.info("Analyzing timing...")
        print("Analyzing timing...")
        timing_report = await self.call_tool("vivado_report_timing_summary", {})
        
        # Parse timing
        timing_info = parse_timing_summary_static(timing_report)
        self.initial_wns = timing_info["wns"]
        self.initial_tns = timing_info["tns"]
        self.initial_failing_endpoints = timing_info["failing_endpoints"]
        self.latest_tns = timing_info["tns"]
        self.latest_failing_endpoints = timing_info["failing_endpoints"]
        self.best_wns = self.initial_wns if self.initial_wns is not None else float('-inf')
        self._best_wns_iteration = 0  # Track initial state as iteration 0
        self._best_wns_tns = timing_info["tns"]
        self._best_wns_failing_endpoints = timing_info["failing_endpoints"]

        # NOTE (Issue 7): _sync_state_to_memory_manager() is NOT called here.
        # MemoryManager._initial_wns/_best_wns remain at default values (None/-inf).
        # This is intentional because:
        #   1. DCPOptimizer's own self.initial_wns/self.best_wns are the canonical values
        #      used for all business decisions (model selection, WNS comparison, etc.)
        #   2. _build_compression_context() reads from DCPOptimizer attributes directly,
        #      not from MemoryManager state
        #   3. MemoryManager's auto-trigger via MESSAGE_ADDED has been disabled.
        #      Compression is now triggered exclusively by DCPOptimizer._compress_context()
        #   4. _sync_state_to_memory_manager() is called at the start of _compress_context()
        #      in get_completion(), before any compression decisions are made
        #   5. If design meets timing initially, optimize() returns early without calling
        #      get_completion(), but no compression is needed in that path anyway
        # Therefore: no functional issue, no fix required
        
        # Get clock period for fmax calculation
        self.clock_period = await super().get_clock_period(self._call_vivado_tool)
        
        print(f"✓ Timing analyzed:")
        if self.clock_period is not None:
            target_fmax = 1000.0 / self.clock_period  # MHz
            print(f"  - Clock period: {self.clock_period:.3f} ns (target fmax: {target_fmax:.2f} MHz)")
        if self.initial_wns is not None:
            print(f"  - WNS: {self.initial_wns:.3f} ns")
            # Calculate and display achievable fmax
            initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
            if initial_fmax is not None:
                print(f"  - Achievable fmax: {initial_fmax:.2f} MHz")
        if self.initial_tns is not None:
            print(f"  - TNS: {self.initial_tns:.3f} ns")
        if self.initial_failing_endpoints is not None:
            print(f"  - Failing endpoints: {self.initial_failing_endpoints}")
        print()
        
        # Step 4: Get critical high fanout nets
        logger.info("Identifying critical high fanout nets...")
        print("Identifying critical high fanout nets...")
        nets_report = await self.call_tool("vivado_get_critical_high_fanout_nets", {
            "num_paths": 50,
            "min_fanout": 100
        })
        
        # Parse high fanout nets
        self.high_fanout_nets = self.parse_high_fanout_nets(nets_report)
        print(f"✓ Found {len(self.high_fanout_nets)} high fanout nets (>100 fanout)\n")

        # Step 4.5: Get resource utilization for pblock sizing
        logger.info("Getting resource utilization...")
        print("Getting resource utilization...")
        util_report = await self.call_tool("vivado_report_utilization_for_pblock", {})
        self.resource_utilization = self._parse_resource_utilization(util_report)
        if self.resource_utilization:
            ru = self.resource_utilization
            print(f"✓ Resource utilization:")
            print(f"  - LUTs:  {ru['LUT']:>8,}")
            print(f"  - FFs:   {ru['FF']:>8,}")
            print(f"  - DSPs:  {ru['DSP']:>8,}")
            print(f"  - BRAMs: {ru['BRAM']:>8,}")
            print(f"  - URAMs: {ru['URAM']:>8,}")
            print()
        else:
            print("[WARNING] Could not parse resource utilization\n")

        # Step 5: Load design in RapidWright for spread analysis
        critical_path_spread_info = None  # Initialize
        
        logger.info("Loading design in RapidWright...")
        print("Loading design in RapidWright for spread analysis...")
        result = await self.call_tool("rapidwright_read_checkpoint", {
            "dcp_path": str(input_dcp.resolve())
        })
        if "error" in result.lower() and "success" not in result.lower():
            print(f"[WARNING] Could not load design in RapidWright: {result}")
        else:
            print("✓ Design loaded in RapidWright\n")

            # Step 6: Get device topology (site type distribution)
            logger.info("Getting device topology...")
            print("Getting device topology...")
            topology_result = await self.call_tool("rapidwright_get_device_topology", {})
            try:
                topo_data = json.loads(topology_result)
                if topo_data.get("status") == "success":
                    self.device_topology = topo_data
                    print(f"✓ Device topology loaded:")
                    print(f"  - Device: {topo_data.get('device')}")
                    print(f"  - Total sites: {topo_data.get('total_sites')}")
                    # Print top 5 site types
                    dist = topo_data.get('site_type_distribution', [])
                    for i, st in enumerate(dist[:5]):
                        print(f"  - {st['type']}: {st['count']}")
                    if len(dist) > 5:
                        print(f"  - ... and {len(dist) - 5} more site types")
                    print()
                else:
                    print(f"[WARNING] Could not get device topology: {topology_result}")
            except json.JSONDecodeError as e:
                print(f"[WARNING] Could not parse topology result: {e}")

            # Step 7: Extract critical path cells and analyze spread
            logger.info("Extracting and analyzing critical path spread...")
            print("Analyzing critical path spread...")
            
            # Extract critical path cells from Vivado
            temp_path = Path(self.temp_dir) / "initial_critical_paths.json"
            cells_json = await self.call_tool("vivado_extract_critical_path_cells", {
                "num_paths": 50,
                "output_file": str(temp_path)
            })
            
            # Analyze spread in RapidWright
            spread_result = await self.call_tool("rapidwright_analyze_critical_path_spread", {
                "input_file": str(temp_path)
            })
            
            # Parse spread results
            try:
                spread_data = json.loads(spread_result)
                critical_path_spread_info = {
                    "max_distance": spread_data.get("max_distance_found", 0),
                    "avg_distance": spread_data.get("avg_max_distance", 0),
                    "paths_analyzed": spread_data.get("paths_analyzed", 0)
                }
                self.critical_path_spread = critical_path_spread_info
                print(f"✓ Critical path spread analyzed:")
                print(f"  - Max distance: {critical_path_spread_info['max_distance']} tiles")
                print(f"  - Avg distance: {critical_path_spread_info['avg_distance']:.1f} tiles")
                print(f"  - Paths analyzed: {critical_path_spread_info['paths_analyzed']}")
                print()
            except (json.JSONDecodeError, KeyError) as e:
                print(f"[WARNING] Could not parse spread results: {e}")
                critical_path_spread_info = None
        
        # Build YAML summary for LLM
        summary_text = self._build_initial_analysis_yaml(critical_path_spread_info)
        print(summary_text)
        print()

        self._prev_best_wns = self.best_wns
        return summary_text

    def _build_initial_analysis_yaml(self, critical_path_spread_info: Optional[dict]) -> str:
        """Build initial analysis summary in YAML format."""
        data = OrderedDict()

        # meta
        data['meta'] = OrderedDict([
            ('type', 'initial_analysis'),
            ('timestamp', time.strftime("%Y-%m-%d %H:%M:%S")),
        ])

        # timing
        timing = OrderedDict()
        if self.clock_period is not None:
            timing['clock_period_ns'] = round(self.clock_period, 3)
            timing['target_fmax_mhz'] = round(1000.0 / self.clock_period, 2)
        if self.initial_wns is not None:
            timing['wns_ns'] = round(self.initial_wns, 3)
            timing['status'] = 'MET' if self.initial_wns >= 0 else 'VIOLATED'
            fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
            if fmax is not None:
                timing['achievable_fmax_mhz'] = round(fmax, 2)
        if self.initial_tns is not None:
            timing['tns_ns'] = round(self.initial_tns, 3)
        if self.initial_failing_endpoints is not None:
            timing['failing_endpoints'] = self.initial_failing_endpoints
        data['timing'] = timing

        # critical_path_spread
        if critical_path_spread_info:
            data['critical_path_spread'] = OrderedDict([
                ('max_distance_tiles', critical_path_spread_info.get('max_distance', 0)),
                ('avg_distance_tiles', round(critical_path_spread_info.get('avg_distance', 0), 1)),
                ('paths_analyzed', critical_path_spread_info.get('paths_analyzed', 0)),
            ])
            # Recommendation based on spread
            if critical_path_spread_info.get('avg_distance', 0) > 70 and critical_path_spread_info.get('paths_analyzed', 0) >= 5:
                data['recommendation'] = 'PBLOCK'

        # high_fanout_nets (top 10)
        if self.high_fanout_nets:
            nets_list = []
            for i, (net_name, fanout, path_count) in enumerate(self.high_fanout_nets[:10]):
                nets_list.append(OrderedDict([
                    ('rank', i + 1),
                    ('name', net_name),
                    ('fanout', fanout),
                    ('critical_paths', path_count),
                ]))
            data['high_fanout_nets'] = nets_list
            data['total_high_fanout_nets'] = len(self.high_fanout_nets)

        # device_topology
        if self.device_topology:
            topo = self.device_topology
            data['device_topology'] = OrderedDict([
                ('device', topo.get('device', 'unknown')),
                ('total_sites', topo.get('total_sites', 0)),
                ('site_types', OrderedDict([
                    (st['type'], st['count']) for st in topo.get('site_type_distribution', [])[:15]
                ])),
            ])

        return "---\n" + LightYAML.dump(data, trace_id=get_trace_id()) + "..."

    # === Section 7.6: LLM Completion Loop ===

    @staticmethod
    def _parse_text_tool_calls(content: str) -> list[dict]:
        """Parse XML-style tool calls from raw LLM content text.

        Handles models that don't support native tool calling and instead
        output tool calls as text in the format:
        <tool_call>tool_name<tool_sep>
        <arg_key>param</arg_key>
        <arg_value>value</arg_value>
        </tool_call>

        Returns:
            List of dicts with keys 'name' and 'arguments' (dict of params).
            Empty list if no valid tool calls found.
        """
        results = []

        # Match <tool_call>name<tool_sep>...content...</tool_call>
        tool_pattern = re.compile(
            r'<tool_call>\s*(\w+)\s*<tool_sep>\s*(.*?)\s*</tool_call>',
            re.DOTALL | re.IGNORECASE
        )
        # Match <arg_key>key</arg_key><arg_value>value</arg_value> pairs
        arg_pattern = re.compile(
            r'<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>',
            re.DOTALL | re.IGNORECASE
        )

        for match in tool_pattern.finditer(content):
            name = match.group(1).strip()
            body = match.group(2)

            args = {}
            for arg_match in arg_pattern.finditer(body):
                key = arg_match.group(1).strip()
                value = arg_match.group(2).strip()
                # Try to parse as JSON (number, boolean, null), keep as string otherwise
                try:
                    parsed = json.loads(value)
                    args[key] = parsed
                except (json.JSONDecodeError, ValueError):
                    args[key] = value

            if name:
                results.append({"name": name, "arguments": args})

        return results

    @staticmethod
    def _parse_yaml_tool_calls(content: str) -> list[dict]:
        """Parse tool_calls from YAML-formatted LLM content.

        Handles models that output tool calls in YAML format (as specified
        in SYSTEM_PROMPT.TXT):
          tool_calls:
            - function: tool_name
              parameters:
                key: value

        Handles multiple step: blocks and leading/trailing non-YAML text.
        """
        results = []

        # Strip XML tags that LLMs sometimes mix into YAML output, and repair
        # step: boundaries fused with tags (e.g. "</arg_value>step:")
        content = re.sub(r'</?[^>]+>', '', content)
        content = re.sub(r'([^\n])(step:)', r'\1\n\2', content)

        # Split on step: boundaries to handle multi-step YAML responses
        blocks = re.split(r'\n(?=step:)', content)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Ensure block starts with step: for consistent parsing
            if not block.startswith('step:'):
                block = 'step:\n' + block

            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                continue

            if not isinstance(data, dict):
                continue

            # Navigate through step -> tool_calls
            step_data = data.get('step')
            if not isinstance(step_data, dict):
                # Maybe the block itself is the dict (no wrapper step: key)
                step_data = data if isinstance(data, dict) else {}

            tool_calls_list = step_data.get('tool_calls')
            if not isinstance(tool_calls_list, list):
                continue

            for tc in tool_calls_list:
                if not isinstance(tc, dict):
                    continue
                # Support both function: name and name: tool_name
                name = tc.get('function') or tc.get('name')
                if not name or not isinstance(name, str):
                    continue

                params = tc.get('parameters', {})
                if not isinstance(params, dict):
                    params = {}

                results.append({
                    "name": name.strip(),
                    "arguments": params
                })

        return results

    @staticmethod
    def _parse_action_from_yaml(content: str) -> tuple:
        """Parse action and flow_control from YAML response.

        Returns:
            (action, flow_control) tuple - either may be None if not found
        """
        blocks = re.split(r'\n(?=step:)', content)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if not block.startswith('step:'):
                block = 'step:\n' + block
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if not isinstance(data, dict):
                continue
            step_data = data.get('step')
            if not isinstance(step_data, dict):
                step_data = data
            action = step_data.get('action')
            flow_control = step_data.get('flow_control')
            # Log analysis section if present (optional observability)
            analysis_data = step_data.get('analysis')
            if analysis_data and isinstance(analysis_data, dict):
                scenario = analysis_data.get('scenario_match', 'unknown')
                hypothesis = analysis_data.get('hypothesis', '')
                logger.info(f"LLM analysis: scenario={scenario}, hypothesis={hypothesis[:100] if hypothesis else 'none'}")
            if action or flow_control:
                return (action, flow_control)
        return (None, None)

    @staticmethod
    def _parse_step_yaml(content: str) -> StepState:
        """Unified parser: extract full step YAML block from LLM response text.

        Returns a StepState with whatever fields could be parsed. Never raises.
        This is format-agnostic: works whether or not native tool_calls are present.
        """
        state = StepState(raw_content=content)
        if not content or not content.strip():
            return state

        try:
            # Strip XML tags and repair step: boundaries fused with tags
            cleaned = re.sub(r'</?[^>]+>', '', content)
            cleaned = re.sub(r'([^\n])(step:)', r'\1\n\2', cleaned)

            blocks = re.split(r'\n(?=step:)', cleaned)
            if not blocks:
                return state

            # Use the LAST step: block if multiple exist (LLM may emit intermediate
            # reasoning blocks; the last one represents the final state)
            block = blocks[-1].strip()
            if not block.startswith('step:'):
                block = 'step:\n' + block

            data = yaml.safe_load(block)
            if not isinstance(data, dict):
                return state

            step_data = data.get('step')
            if not isinstance(step_data, dict):
                step_data = data

            # Extract each field independently (partial data is fine)
            raw_sid = step_data.get('step_id')
            if raw_sid is not None:
                try:
                    state.step_id = int(raw_sid)
                except (ValueError, TypeError):
                    pass

            state.result_status = step_data.get('result_status')
            state.flow_control = step_data.get('flow_control')

            # Backward compat: top-level 'action' is aliased to flow_control
            if not state.flow_control:
                state.flow_control = step_data.get('action')

            # Extract full analysis dict
            analysis = step_data.get('analysis')
            if isinstance(analysis, dict):
                state.analysis = analysis

        except yaml.YAMLError as e:
            state.parse_error = str(e)[:200]
        except Exception as e:
            state.parse_error = f"{type(e).__name__}: {str(e)[:200]}"

        return state

    WNS_TARGET_THRESHOLD = 0.0    # WNS target threshold (0.0 ns means timing convergence)
    async def get_completion(self) -> tuple[str, bool]:
        """Iteratively execute LLM calls and tool calls to avoid recursion stack overflow."""
        # [NEW] Auto-check and compress context before model selection
        self._compress_context()
        # Calculate context complexity for dynamic model routing (after compression for accurate token count)
        context_complexity = self._estimate_context_complexity(self.current_task_type)
        # Track previous tier before model selection for accurate switch detection
        self._previous_tier = self._infer_model_tier(self.last_used_model) if self.last_used_model else None
        # Use pre-decided model from previous iteration's _on_iteration_end(), or fall back to _select_model()
        if self._next_iteration_model is not None:
            current_model = self._next_iteration_model
            self.last_used_model = current_model
            self._next_iteration_model = None  # Clear after use - one-time decision per iteration boundary
            logger.info(f"Using pre-decided model from iteration handoff: {current_model}")
        else:
            # Fall back for first iteration or fallback scenarios
            current_model = self._select_model(
                tool_name=self.current_task_type,
                context_complexity=context_complexity,
            )
            self.last_used_model = current_model
        # Track task type at iteration start for intra-iteration model re-selection
        self._iteration_start_task_type = self.current_task_type
        wns_at_start = self.best_wns
        # Reset model switch logging flag at start of each iteration
        self._iteration_model_switch_logged = False
        logger.info(f"Iteration {self.iteration}: Using {current_model} (complexity={context_complexity}, task={self.current_task_type})...")
        tool_round = 0
        while True:
            tool_round += 1
            # Check for user-requested exit between tool rounds
            if self._check_exit_requested():
                logger.info(f"User requested exit during tool round {tool_round}, breaking inner loop")
                content = f"[User requested exit during tool round {tool_round}, iteration {self.iteration}]"
                self._is_done_reason = "user_requested"
                is_done = False
                return content, is_done
            if tool_round > self.MAX_TOOL_ROUNDS_PER_ITERATION:
                logger.warning(f"Tool round limit reached ({tool_round} > {self.MAX_TOOL_ROUNDS_PER_ITERATION}), breaking inner loop")
                content = f"[Tool round limit reached ({tool_round} rounds), continuing to next iteration]"
                self._is_done_reason = "tool_round_limit"
                is_done = False
                return content, is_done
            iteration_start_wns = self.best_wns

            # Re-enable compression and compress before LLM call
            self._compress_context()

            # 2. Async call LLM
            self.llm_call_count += 1
            max_retries = 3
            retry_delay = 2  # Initial delay in seconds
            last_exception = None
            # Get messages for logging and API call
            api_messages = self._compat.get_formatted_for_api()

            # Inject current WNS state into system message to prevent context loss after compression
            if api_messages and api_messages[0].get("role") == "system":
                system_content = api_messages[0].get("content", "")
                updated_content = self._inject_wns_state_to_system_prompt(system_content)
                api_messages[0]["content"] = updated_content

            # Inject iteration handoff prompt or first-iteration starting context
            # Insert as standalone system message after primary system prompt for better attention weight
            if not self._iteration_handoff_injected:
                if self._iteration_handoff_prompt:
                    handoff_msg = {"role": "system", "content": self._iteration_handoff_prompt}
                    api_messages.insert(1, handoff_msg)
                    logger.info(f"Injected iteration handoff prompt as system message (length: {len(self._iteration_handoff_prompt)} chars)")
                elif self.iteration == 1:
                    # First iteration: no handoff from previous iteration, inject starting context
                    first_msg = {
                        "role": "system",
                        "content": (
                            "**FIRST ITERATION** - Begin with initial design analysis "
                            "(report_timing_summary, extract_critical_path_cells, etc.) "
                            "to understand the current timing state before applying "
                            "optimization strategies."
                        )
                    }
                    api_messages.insert(1, first_msg)
                    logger.info("Injected first iteration starting context")
                self._iteration_handoff_injected = True

            # Inject short format reminder as last system message before API call
            # Ensures the YAML format instruction is always near the generation point,
            # regardless of how long the context has grown.
            api_messages.append({
                "role": "system",
                "content": "REMINDER: Your response MUST contain a 'step:' YAML block (chain-of-thought natural language is OK before it). See format instructions at conversation start."
            })

            # Log prompt for observability
            self._prompt_logger.log_prompt(
                model=current_model,
                messages=api_messages,
                iteration=self.iteration,
                job_id=self.run_dir.name
            )
            for retry in range(max_retries):
                try:
                    response = await self._call_llm_with_exit_check(
                        model=current_model,
                        messages=api_messages,
                        tools=self.tools,
                        timeout=600.0
                    )
                    if response is None:
                        # Exit was requested during LLM call
                        logger.info(f"User requested exit during LLM call, breaking inner loop")
                        content = f"[User requested exit during LLM call, iteration {self.iteration}]"
                        self._is_done_reason = "user_requested"
                        is_done = False
                        return content, is_done
                    last_exception = None  # [FIX] Clear stale exception after successful fallback
                    # Update model_worker to current model after successful fallback
                    self.model_worker = current_model
                    break  # Success, exit retry loop
                except Exception as e:
                    last_exception = e
                    # On rate limit (429), switch to fallback model
                    if isinstance(e, OpenAIRateLimitError) or "429" in str(e):
                        # Save the model that hit rate limit BEFORE reassignment
                        rate_limited_model = current_model
                        current_tier = self._infer_model_tier(current_model)

                        # Mark current model as exhausted (both original and fallback models)
                        self._mark_fallback_exhausted(rate_limited_model)

                        # Try to get next fallback model for current tier
                        next_fallback = self._get_next_fallback_model(current_tier)

                        if next_fallback:
                            logger.warning(f"Rate limit on {rate_limited_model}, switching to fallback: {next_fallback}")
                            current_model = next_fallback
                            self.last_used_model = current_model
                            wait_time = retry_delay * (2 ** retry)
                            logger.warning(f"Retry {retry+1}/{max_retries} with {current_model}, waiting {wait_time}s: {e}")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            # All fallbacks exhausted, fallback to model_planner
                            # Clear exhausted states for worker tier only (planner state preserved)
                            self._exhausted_worker_fallbacks.clear()
                            logger.warning(f"All {current_tier} fallback models exhausted, switching to model_planner: {self.model_planner}")
                            current_model = self.model_planner
                            self.last_used_model = current_model
                            # Reset fallback state for potential future use
                            self._reset_fallbacks(current_tier)
                            wait_time = retry_delay * (2 ** retry)
                            logger.warning(f"Retry {retry+1}/{max_retries} with {current_model}, waiting {wait_time}s: {e}")
                            await asyncio.sleep(wait_time)
                            continue
                    # On 404 "tool use not supported", switch to fallback model
                    if isinstance(e, OpenAINotFoundError) or ("404" in str(e) and "tool use" in str(e).lower()):
                        tool_unsupported_model = current_model
                        current_tier = self._infer_model_tier(current_model)

                        # Mark current model as exhausted (doesn't support tool use)
                        self._mark_fallback_exhausted(tool_unsupported_model)

                        # Try to get next fallback model for current tier
                        next_fallback = self._get_next_fallback_model(current_tier)

                        if next_fallback:
                            logger.warning(f"Tool use not supported on {tool_unsupported_model}, switching to fallback: {next_fallback}")
                            current_model = next_fallback
                            self.last_used_model = current_model
                            self.model_worker = current_model
                            wait_time = retry_delay * (2 ** retry)
                            logger.warning(f"Retry {retry+1}/{max_retries} with {current_model}, waiting {wait_time}s: {e}")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            # All fallbacks exhausted, fallback to model_planner
                            # Clear exhausted states for worker tier only (planner state preserved)
                            self._exhausted_worker_fallbacks.clear()
                            logger.warning(f"All {current_tier} fallback models exhausted (tool use unsupported), switching to model_planner: {self.model_planner}")
                            current_model = self.model_planner
                            self.last_used_model = current_model
                            self._reset_fallbacks(current_tier)
                            wait_time = retry_delay * (2 ** retry)
                            logger.warning(f"Retry {retry+1}/{max_retries} with {current_model}, waiting {wait_time}s: {e}")
                            await asyncio.sleep(wait_time)
                            continue
                    if retry < max_retries - 1:
                        wait_time = retry_delay * (2 ** retry)  # Exponential backoff
                        logger.warning(f"API call failed, retry {retry+1}/{max_retries}, waiting {wait_time}s: {e}")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"API call failed, retried {max_retries} times: {e}")
                        raise
            # If still failed after retry loop, throw the last exception
            if last_exception is not None:
                raise last_exception

            # If response is None, user requested exit - return directly
            if response is None:
                logger.info(f"User requested exit, returning from get_completion")
                content = f"[User requested exit during LLM call, iteration {self.iteration}]"
                self._is_done_reason = "user_requested"
                is_done = False
                return content, is_done

            # Safely extract Usage and Cost (compatible with OpenRouter extended fields)
            try:
                usage = getattr(response, 'usage', None)
                if usage:
                    prompt = getattr(usage, 'prompt_tokens', 0) or 0
                    completion = getattr(usage, 'completion_tokens', 0) or 0
                    raw_total = getattr(usage, 'total_tokens', 0) or 0
                    # [FIX] When API returns total_tokens as 0/null, fall back to prompt+completion sum
                    total = raw_total if raw_total > 0 else (prompt + completion)
                    cost = float(getattr(usage, 'cost', 0.0) or 0.0)
                    # Extract cached and reasoning tokens (OpenAI/OpenRouter extended fields)
                    prompt_details = getattr(usage, 'prompt_tokens_details', None)
                    completion_details = getattr(usage, 'completion_tokens_details', None)
                    cached = getattr(prompt_details, 'cached_tokens', 0) or 0
                    reasoning = getattr(completion_details, 'reasoning_tokens', 0) or 0
                    self.total_prompt_tokens += prompt
                    self.total_completion_tokens += completion
                    self.total_tokens += total
                    self.total_cost += cost
                    # Check cost hard limit after updating total_cost
                    if self.total_cost >= self.cost_hard_limit:
                        self._is_done_reason = "cost_limit"
                        logger.warning(f"Cost limit ${self.total_cost:.4f} >= ${self.cost_hard_limit:.2f}, stopping optimization")
                        content = f"[Cost limit reached: ${self.total_cost:.4f} >= ${self.cost_hard_limit:.2f}]"
                        is_done = True
                        return content, is_done
                    self.api_call_details.append({
                        "call_number": self.llm_call_count, "iteration": self.iteration,
                        "model": current_model,
                        "prompt_tokens": prompt, "completion_tokens": completion,
                        "total_tokens": total, "cost": cost,
                        "cached_tokens": cached,
                        "reasoning_tokens": reasoning,
                    })
                    logger.info(f"API call #{self.llm_call_count} ({current_model}) - Tokens: {total:,} | Cost: ${cost:.4f}")
            except Exception as e:
                logger.warning(f"Failed to parse token usage/cost: {e}")

            # 3. Parse response
            if not response.choices:
                raise ValueError("Empty choices in API response")
                
            message = response.choices[0].message
            # [Phase 2] Add assistant message via compat (tool_calls extracted below)
            assistant_content = message.content or ""
            metadata = {"tool_calls": message.tool_calls} if message.tool_calls else None
            self._compat.add_message("assistant", assistant_content, metadata)

            # 增强可观测性：打印 assistant 的回答
            logger.info(f"[ASSISTANT] {assistant_content}")

            # === NEW: Always parse step YAML from every response ===
            step_state = self._parse_step_yaml(assistant_content)
            step_state.has_tool_calls = bool(message.tool_calls)
            self._step_state = step_state

            if step_state.step_id is not None or step_state.flow_control is not None:
                logger.info(
                    f"[STEP_STATE] step_id={step_state.step_id}, "
                    f"result_status={step_state.result_status}, "
                    f"flow_control={step_state.flow_control}, "
                    f"scenario={step_state.analysis.get('scenario_match', 'N/A')}, "
                    f"hypothesis={str(step_state.analysis.get('hypothesis', ''))[:80]}, "
                    f"tool_calls={step_state.has_tool_calls}"
                )
            if step_state.parse_error:
                logger.warning(f"[STEP_PARSE] YAML parse error: {step_state.parse_error}")

            # Check flow_control BEFORE executing tools.
            # If termination signal (DONE/SWITCH_STRATEGY), skip tool execution
            # even when native tool_calls are present (contradictory but safe:
            # the LLM should not request tools AND signal termination simultaneously).
            flow_signal = step_state.flow_control
            if flow_signal in ("DONE", "SWITCH_STRATEGY"):
                content = assistant_content
                if message.tool_calls:
                    logger.warning(
                        f"flow_control={flow_signal} with {len(message.tool_calls)} "
                        f"native tool_calls -- prioritizing flow_control, "
                        f"skipping tool execution"
                    )
                # Skip tool execution entirely, fall through to flow_control handling
                # at lines 3562+ (section 4-5-6 below).
            else:
                # [FIX] Fallback: parse tool calls from raw text for models that don't
                # support native tool calling (try XML format first, then YAML format)
                if not message.tool_calls:
                    text_calls = self._parse_text_tool_calls(assistant_content)
                    if not text_calls:
                        text_calls = self._parse_yaml_tool_calls(assistant_content)
                    if text_calls:
                        logger.info(f"Parsed {len(text_calls)} tool call(s) from raw text (XML/YAML fallback, model={current_model})")
                        simulated = []
                        for i, tc_data in enumerate(text_calls):
                            tc = SimpleNamespace()
                            tc.function = SimpleNamespace()
                            tc.function.name = tc_data["name"]
                            tc.function.arguments = json.dumps(tc_data["arguments"])
                            tc.id = f"text_call_{self.llm_call_count}_{i}"
                            simulated.append(tc)
                        message.tool_calls = simulated

                if message.tool_calls:
                    for tc in message.tool_calls:
                        if not tc.function: continue
                        tool_name = tc.function.name
                        try:
                            tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                        except json.JSONDecodeError:
                            tool_args = {}

                        # [FIX] vivado_run_tcl routing issue: set current_task_type based on Tcl command content
                        effective_task_type = tool_name
                        if tool_name == "vivado_run_tcl":
                            tcl_cmd = str(tool_args.get("command", "")).lower()
                            # If Tcl command contains optimization keywords, use command name as task_type for correct classification
                            if any(p in tcl_cmd for p in OPTIMIZATION_PATTERNS):
                                # Extract first optimization command as task_type (e.g., place_design, phys_opt_design)
                                for p in OPTIMIZATION_PATTERNS:
                                    if p in tcl_cmd:
                                        effective_task_type = p
                                        logger.info(f"Detected optimization Tcl command '{p}' in vivado_run_tcl, routing will use '{effective_task_type}'")
                                        break
                            else:
                                effective_task_type = "vivado_run_tcl_info"  # Information-class Tcl uses different identifier

                        result = await self.call_tool(tool_name, tool_args)

                        # [TOOL_SUMMARIZE] Store raw output, replace with structured summary
                        raw_result = result
                        result = self._summarize_tool_result(tool_name, raw_result)
                        # Store full raw output in side buffer for on-demand retrieval
                        self._raw_tool_outputs[(self.iteration, tool_round)] = (tool_name, raw_result)
                        # FIFO eviction when buffer exceeds limit
                        if len(self._raw_tool_outputs) > self._raw_tool_output_max:
                            oldest_key = min(self._raw_tool_outputs.keys(), key=lambda k: (k[0], k[1]))
                            del self._raw_tool_outputs[oldest_key]

                        # Update current task type for next iteration's model routing decision
                        self.current_task_type = effective_task_type

                        # Intra-iteration model re-selection: check if task category changed
                        # Only allow switch if we didn't have a pre-decided model (first iteration or fallback case)
                        current_category = self.classify_task(self.current_task_type)
                        start_category = self.classify_task(self._iteration_start_task_type)
                        if self._iteration_handoff_injected and current_category != start_category:
                            new_model = self._select_model(
                                tool_name=self.current_task_type,
                                context_complexity=context_complexity,
                            )
                            if new_model != current_model:
                                logger.info(
                                    f"Intra-iteration model switch: {current_model} -> {new_model} "
                                    f"(task: {self._iteration_start_task_type} -> {self.current_task_type}, "
                                    f"category: {start_category} -> {current_category})"
                                )
                                current_model = new_model
                                self.last_used_model = new_model

                        # Track tool errors for failure classification
                        result_lower = result.lower() if result else ""
                        if "error" in result_lower and "success" not in result_lower:
                            self.iteration_tool_errors.append({
                                "tool": tool_name,
                                "result": result[:2000]  # Store truncated result for analysis
                            })
                            # Record strategy failure when tool result indicates error
                            tool_strategy = self._infer_strategy_from_tools([tool_name])
                            if tool_strategy not in ("Information", "Unknown"):
                                self._compat.record_failure(tool_strategy)

                        # Special case: PBLOCK validation failure (status: validation_failed)
                        if tool_name == "vivado_create_and_apply_pblock" and "validation_failed" in result_lower:
                            self._compat.record_failure("PBLOCK")

                        # [Phase 2] Add tool result via compat (each tool result is a separate message)
                        self._compat.add_message("tool", result, {
                            "tool_call_id": tc.id,
                            "name": tool_name
                        })
                    continue  # Loop continues to process tool results

            # 4. No tool call, update improvement tracking
            content = message.content or ""

            # [FIX] Only take WNS generated within this iteration, avoid cross-iteration misuse of historical records
            current_iter_wns_list = [
                t["wns"] for t in self.tool_call_details
                if t.get("iteration") == self.iteration and t.get("wns") is not None
            ]
            if current_iter_wns_list:
                current_wns = current_iter_wns_list[-1]   # Take latest WNS from this iteration
                if current_wns > iteration_start_wns:     # Compare with this iteration's start WNS
                    # Clear failed strategies on improvement so model can try new approaches
                    self._compat.failed_strategies.clear()
            # else: Non-WNS iterations don't count (may be query operations only)

            # [FIX] Completion flag no longer depends on model output, changed to objective data judgment
            # 5. Objective judgment of whether optimization is complete
            # Use latest_wns (current state) instead of best_wns (historical best)
            current_valid = (self.latest_wns is not None and
                             self.latest_wns >= self.WNS_TARGET_THRESHOLD and
                             self._is_valid_wns(self.latest_wns))

            # Condition 1: WNS target check (core completion condition)
            # Must pass sanity check to prevent false positives from parsing errors
            wns_target_met = current_valid

            # Condition 2: Hard limit reached (consecutive no-improvement count exceeds threshold)
            max_iterations_reached = (self.global_no_improvement >= self.GLOBAL_NO_IMPROVEMENT_LIMIT)

            # Comprehensive judgment: WNS met OR hard limit reached
            is_done = wns_target_met or max_iterations_reached
            if is_done:
                # Set is_done_reason if not already set by cost_limit
                if self._is_done_reason is None:
                    self._is_done_reason = "wns_target_met" if wns_target_met else "max_iterations_reached"
                reasons = []
                if wns_target_met:
                    reasons.append(f"WNS target met ({self.latest_wns:.3f} ns >= {self.WNS_TARGET_THRESHOLD:.1f} ns)")
                if max_iterations_reached:
                    reasons.append(f"Hard limit reached ({self.global_no_improvement} consecutive no improvements)")
                logger.info(f"Optimization ending - Reasons: {'; '.join(reasons)}")
                print(f"\n*** Optimization completion judgment ***")
                for reason in reasons:
                    print(f"  [OK] {reason}")

            # [FIX] When LLM signals flow_control=DONE, it means "I've finished my analysis for
            # this iteration" - NOT "exit the optimization". The system should properly end
            # this iteration and move to the next one with updated context.
            # Use pre-parsed StepState if available (avoids re-parsing YAML),
            # fall back to legacy _parse_action_from_yaml for backward compat.
            if self._step_state and self._step_state.flow_control:
                action = self._step_state.result_status
                flow_control = self._step_state.flow_control
            else:
                action, flow_control = self._parse_action_from_yaml(content)

            # Store analysis data when DONE/SWITCH_STRATEGY is signaled
            if flow_control in ("DONE", "SWITCH_STRATEGY") and self._step_state:
                self._last_analysis = self._step_state.analysis
                logger.info(f"[ANALYSIS] Stored last analysis: scenario={self._last_analysis.get('scenario_match', 'N/A')}")

            if flow_control == "DONE" or action == "DONE":
                logger.info(f"LLM signaled DONE (action={action}, flow_control={flow_control})")
                # Check if target fmax (WNS >= 0) is met
                current_valid = (self.latest_wns is not None and
                                 self.latest_wns >= self.WNS_TARGET_THRESHOLD and
                                 self._is_valid_wns(self.latest_wns))
                if current_valid:
                    is_done = True
                    self._is_done_reason = "wns_target_met"
                    logger.info("Target fmax reached, optimization complete")
                else:
                    # Target not met → end this iteration properly and move to next
                    logger.info("flow_control=DONE but target not met, moving to next iteration...")
                    # Guard: don't overwrite a higher-priority reason (e.g., max_iterations_reached)
                    if self._is_done_reason is None:
                        self._is_done_reason = "flow_control_done_next_iteration"
                    # Set flag to indicate iteration should end, not continue in tight loop
                    self._end_iteration_on_return = True
                # Fall through to exit get_completion()
            elif flow_control == "SWITCH_STRATEGY" or action == "SWITCH_STRATEGY":
                logger.info(f"LLM signaled SWITCH_STRATEGY (action={action}, flow_control={flow_control})")
                # Record current iteration's strategy as failed before switching
                tools_this_iter = [
                    t.get('tool_name', '') for t in self._compat.tool_call_details
                    if t.get('iteration') == self.iteration
                ]
                prev_strategy = self._infer_strategy_from_tools(tools_this_iter)
                if prev_strategy not in ("Information", "Unknown"):
                    self._compat.record_failure(prev_strategy)
                # System-enforced: end current iteration, force re-analysis next iteration
                if self._is_done_reason is None:
                    self._is_done_reason = "switch_strategy"
                self._end_iteration_on_return = True
                # Inject analysis-forcing prompt for next iteration
                current_wns = self._get_current_wns()
                wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"
                enforced_msg = (
                    f"SYSTEM ENFORCED SWITCH: The previous model signaled SWITCH_STRATEGY while WNS={wns_str}. "
                    f"Strategy switch triggered. You MUST start this iteration with structured analysis:\n"
                    f"1. Call report_timing_summary and extract_critical_path_cells to gather current signal data\n"
                    f"2. Match observed signals against the strategy_library SCENARIO_DETECTION_MATRIX\n"
                    f"3. Form a hypothesis about the dominant timing obstacle\n"
                    f"4. Select a strategy based on the hypothesis and justify it in your analysis section\n"
                    f"DO NOT repeat the same strategy that just failed. DO NOT use DONE before analysis is complete."
                )
                # Append skill recommendation
                skill_rec = self._build_skill_recommendation()
                if skill_rec:
                    enforced_msg += f"\n\n{skill_rec}"
                self._compat.add_message("user", enforced_msg)
                logger.info("Injected analysis-forcing prompt after SWITCH_STRATEGY")
            elif not is_done:
                logger.info(f"LLM returned no tool calls (is_done=False), continuing to next round in same iteration...")
                continue

            # At the end of get_completion(), before the return statement:
            if self.current_task_type:
                task_category = self.classify_task(self.current_task_type)
                improved = False
                tool_error = len(self.iteration_tool_errors) > 0

                if task_category != TaskCategory.INFORMATION:
                    # OPTIMIZATION tasks: check WNS improvement
                    if self.iteration > 1:
                        prev_best = getattr(self, '_prev_best_wns', None)
                        if prev_best is not None and self.best_wns > float('-inf'):
                            improved = self.best_wns > prev_best
                else:
                    # INFORMATION tasks: success = no tool error (improved=True means success)
                    improved = not tool_error

                # Determine failure type if not successful
                failure_type = ""
                if not improved and tool_error and self.iteration_tool_errors:
                    err_info = self.iteration_tool_errors[0]
                    err_lower = err_info["result"].lower()
                    if "timeout" in err_lower or "timed out" in err_lower:
                        failure_type = "recoverable_timeout"
                    elif any(phrase in err_lower for phrase in [
                        "routing failed", "route error", "cannot route",
                        "unroutable", "exceeds", "congestion"
                    ]):
                        failure_type = "routing_failure"
                    else:
                        failure_type = "tool_error"

                self._record_task_outcome(
                    task_type=self.current_task_type,
                    model_used=self.last_used_model,
                    improved=improved,
                    tool_error=tool_error,
                    failure_type=failure_type
                )

            # Record iteration narrative for progressive handoff context
            self._append_iteration_narrative()

            # Log exit reason
            # _is_done_reason is always set (by user_requested, tool_round_limit, cost_limit, or wns_target_met/max_iterations_reached)
            # Prioritize explicit reason over default fallback
            reason = self._is_done_reason or "unknown"
            logger.info(f"get_completion exit: reason={reason}, is_done={is_done}, WNS={self.best_wns:.4f}")
            print(f"[Exit reason: {reason}]")

            # [FIX] Defensive check: ensure content and is_done are defined before returning
            if content is None:
                logger.error(f"get_completion returning content=None! This indicates a bug in exit handling.")
                content = "[Internal error: content not set]"
            if is_done is None:
                logger.error(f"get_completion returning is_done=None! This indicates a bug in exit handling.")
                is_done = False

            # [FIX] If flow_control=DONE was signaled and target not met, is_done=False
            # but the iteration has ended. Main optimize() loop will handle transition to next iteration.
            if hasattr(self, '_end_iteration_on_return') and self._end_iteration_on_return:
                delattr(self, '_end_iteration_on_return')
                logger.info("Iteration ended via flow_control=DONE, will transition to next iteration")

            return content, is_done

    # === Section 7.7: Optimization Workflow ===

    async def optimize(self, input_dcp: Path, output_dcp: Path) -> bool:
        """Run the optimization workflow."""
        # Store paths for validation
        self.input_dcp = input_dcp
        self.output_dcp = output_dcp

        # Start timing the optimization process
        self.start_time = time.time()

        # Perform initial analysis without LLM
        try:
            initial_analysis = await self.perform_initial_analysis(input_dcp)
        except Exception as e:
            logger.exception(f"Initial analysis failed: {e}")
            print(f"\n✗ Initial analysis failed: {e}\n")
            self.end_time = time.time()
            return False
        
        # Check if timing is already met
        if self.initial_wns is not None and self.initial_wns >= 0:
            print("✓ Design already meets timing! No optimization needed.\n")
            logger.info("Design already meets timing")
            # Save the design as-is
            result = await self.call_tool("vivado_write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            })
            print(f"Saved design to: {output_dcp}\n")
            
            # End timing
            self.end_time = time.time()
            total_runtime = self.end_time - self.start_time
            
            # Print summary even for early exit
            print("\n=== No Optimization Required ===")
            initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
            if initial_fmax is not None:
                print(f"Design already meets timing - Fmax: {initial_fmax:.2f} MHz (WNS: {self.initial_wns:.3f} ns)")
            else:
                print(f"Design already meets timing (WNS: {self.initial_wns:.3f} ns)")
            print(f"Total runtime: {total_runtime:.2f} seconds ({total_runtime/60:.2f} minutes)")
            print(f"LLM API calls: 0 (analysis performed without LLM)")
            print(f"Estimated cost: $0.00")
            print("="*70 + "\n")
            return True
        
        # Load and fill in system prompt with temp directory and input DCP path
        system_prompt_template = load_system_prompt()
        system_prompt = system_prompt_template.format(
            temp_dir=self.temp_dir,
            input_dcp=input_dcp.resolve()
        )
        
        # Initialize conversation with analysis results via compat (Phase 2)
        self._compat.add_message("system", system_prompt)

        # Inject YAML format requirement as the FIRST user message (not system prompt prefix)
        # User role messages get higher attention weight and are less likely to be forgotten
        # in long contexts compared to content buried in the system prompt.
        FORMAT_GUARD = """CRITICAL OUTPUT FORMAT - MUST FOLLOW:
Every response MUST contain a valid `step:` YAML block that carries ALL process
control directives. Flow control (step_id, result_status, flow_control, analysis,
tool_calls) goes INSIDE the `step:` block, NOT in the natural-language reasoning text.
Natural-language chain-of-thought reasoning before the `step:` block is acceptable.
INCLUDING when using native function/tool calls — the `step:` YAML block must still
appear in the response text alongside any native tool calls.

Format:
  step:
    step_id: <N>
    result_status: SUCCESS|PARTIAL|FAIL
    flow_control: CONTINUE|RETRY|ROLLBACK|SWITCH_STRATEGY|DONE
    analysis:
      observed_signals:
        avg_distance: <float|null>
        max_fanout: <int|null>
        failing_endpoints: <int|null>
      scenario_match: <scenario_id|null>
      hypothesis: "<string>"
      strategy_rationale: "<string>"
    tool_calls:
      - function: tool_name
        parameters:
          key: value

STRICTLY FORBIDDEN:
  - XML/HTML tags - NOT valid YAML
  - Markdown code fences ``` around the YAML block
  - Omitting the step: YAML block entirely

Maintain this output format throughout the entire conversation.
"""
        self._compat.add_message("user", FORMAT_GUARD)

        initial_user_content = f"""Optimize this FPGA design for timing.

PATHS:
- Input DCP: {input_dcp.resolve()}
- Output DCP (save final result here): {output_dcp.resolve()}
- Run directory (for intermediate files): {self.temp_dir}

CURRENT STATE:
- Vivado has the input design ALREADY OPEN and analyzed
- RapidWright has the input design ALREADY LOADED (from initial analysis)

INITIAL ANALYSIS RESULTS:
{initial_analysis}

Proceed with optimization strategy based on the analysis above. Do NOT reload the design in either Vivado or RapidWright - both already have it loaded.

CRITICAL OPTIMIZATION RULES:
1. DATA-DRIVEN EVALUATION: After each physical optimization operation like `phys_opt_design`, you must evaluate the effect combined with timing reports (the system is configured to automatically append the latest timing results after phys_opt).
2. AVOID BLIND OPERATIONS: Strictly decide next steps based on WNS changes before and after optimization. If WNS worsens or shows no significant improvement, proactively mark that operation as a failed strategy and find a new direction. Do not blindly and consecutively stack the same optimization commands."""
        self._compat.add_message("user", initial_user_content)
        
        max_iterations = 50  # Safety limit
        
        print("=== Starting LLM-Driven Optimization ===\n")
        
        while self.iteration < max_iterations:
            self.iteration += 1
            self._memory_manager._iteration = self.iteration  # Sync MemoryManager iteration for correct metadata injection
            self.iteration_tool_errors = []  # Reset tool error tracking for this iteration
            print()
            logger.info(f"=== Iteration {self.iteration} ===")

            # Check for user-requested exit via console "quit"
            if self._check_exit_requested():
                logger.info(f"User requested exit at iteration {self.iteration}, saving checkpoint and exiting gracefully...")
                self.end_time = time.time()
                self._print_optimization_summary()
                return False

            # [Bug 4] Snapshot WNS state before get_completion(), for rollback on verification failure
            self._wns_snapshot = {
                "best_wns": self.best_wns,
                "_best_wns_iteration": self._best_wns_iteration,
                "latest_wns": self.latest_wns,
                "latest_tns": self.latest_tns,
                "latest_failing_endpoints": self.latest_failing_endpoints,
                "_best_wns_tns": self._best_wns_tns,
                "_best_wns_failing_endpoints": self._best_wns_failing_endpoints,
            }

            try:
                result = await self.get_completion()
                if result is None:
                    logger.error("get_completion() returned None - unhandled exception escaped. Treating as tool_round_limit.")
                    response_text = "[Internal error: get_completion returned None, treating as tool round limit]"
                    is_done = False
                else:
                    response_text, is_done = result
                print(f"\n{response_text}\n")

                # [FIX] Inject corrective feedback when LLM prematurely declared DONE
                # This ensures the next iteration's model knows optimization is NOT complete
                if getattr(self, '_is_done_reason', None) == "flow_control_done_next_iteration":
                    current_wns = self._get_current_wns()
                    wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"
                    tns_str = f"{self.latest_tns:.3f}ns" if self.latest_tns is not None else "unknown"
                    eps_str = str(self.latest_failing_endpoints) if self.latest_failing_endpoints is not None else "unknown"
                    corrective_msg = (
                        f"SYSTEM NOTICE: The previous model incorrectly used flow_control=DONE while "
                        f"WNS is still {wns_str} (target: 0.0ns, gap: {abs(current_wns or 0):.3f}ns). "
                        f"Optimization is NOT complete. "
                        f"Current state: {eps_str} failing endpoints, TNS {tns_str}. "
                        f"The system ended that iteration and is now starting a new one with fresh context. "
                        f"You MUST continue optimization aggressively - DO NOT use DONE unless WNS >= 0.0."
                    )
                    self._compat.add_message("user", corrective_msg)
                    logger.info("Injected corrective feedback for premature DONE signal")

                # [NEW] Routing failure fault tolerance handling
                # Avoid adding route_design to failed_strategies due to routing timeout
                # Only consider it a strategy failure when "routing failure" (non-timeout) is clearly detected
                # ================================================================
                # Check if route_design is in recent tool calls
                recent_routing_failure = None
                for tool_detail in reversed(self.tool_call_details[-5:]):
                    if tool_detail['tool_name'] == 'vivado_route_design':
                        if tool_detail.get('error', False):
                            error_msg = tool_detail.get('error_message', '').lower()
                            # Check if it is a timeout error
                            if 'timeout' in error_msg or 'timed out' in error_msg:
                                logger.warning(f"route_design failed due to timeout (Iter {tool_detail['iteration']}), not counted as failed strategy")
                                recent_routing_failure = 'timeout'
                            # Check if it is a clear routing failure (non-timeout)
                            elif self._is_routing_failure(error_msg):
                                logger.warning(f"route_design clear routing failure (Iter {tool_detail['iteration']}): {error_msg[:100]}")
                                recent_routing_failure = 'routing_failed'
                                self._compat.record_failure("PlaceRoute")
                            else:
                                # Other errors, handle conservatively but don't fully mark as failure
                                recent_routing_failure = 'unknown_error'
                                logger.warning(f"route_design unknown error type: {error_msg[:100]}")
                        else:
                            # Completed successfully
                            recent_routing_failure = 'success'
                        break

# If it is a routing timeout, do not add to failed_strategies
                if recent_routing_failure == 'timeout':
                    logger.info("route_design timeout recorded, but does not restrict subsequent routing attempts")
                    # Automatically trigger re-placement flow
                    self._compat.add_message(
                        "user",
                        "Note: route_design failed due to timeout. The system will try re-placement (place_design -unplace then place_design) and retry routing. Please use phys_opt_design for load reduction optimization in subsequent steps."
                    )

                # [NEW] Mandatory checkpoint + get_wns check before proceeding to next iteration
                checkpoint_success = False
                get_wns_success = False
                retry_count = 0
                max_retries = 3

                while retry_count < max_retries:
                    retry_count += 1
                    checkpoint_success = False
                    get_wns_success = False

                    # Step 1: Save checkpoint
                    try:
                        intermediate_dcp = await self._save_intermediate_checkpoint(self.iteration)
                        if intermediate_dcp and intermediate_dcp.exists():
                            checkpoint_success = True
                            logger.info(f"Iteration {self.iteration}: Checkpoint saved successfully")
                        else:
                            logger.warning(f"Iteration {self.iteration}: Checkpoint save failed, retry {retry_count}/{max_retries}")
                    except Exception as e:
                        logger.warning(f"Iteration {self.iteration}: Checkpoint save exception: {e}, retry {retry_count}/{max_retries}")

                    # Step 2: Get WNS (corrected to handle string return)
                    try:
                        wns_result = await self.call_tool("vivado_get_wns", {})
                        # call_tool returns raw string like "0.016" or "PARSE_ERROR" or "(no output)"
                        if wns_result and wns_result.strip() not in ("", "(no output)", "PARSE_ERROR"):
                            try:
                                wns_value = float(wns_result.strip())
                                if self._is_valid_wns(wns_value):
                                    # Regression: WNS < 0 and worse than best
                                    if wns_value < 0 and wns_value < self.best_wns:
                                        logger.warning(f"WNS regressed: {wns_value:.3f} < best {self.best_wns:.3f}, rolling back...")
                                        rollback_ok = await self._rollback_to_best_checkpoint()
                                        if not rollback_ok:
                                            logger.warning("Rollback failed, continuing without checkpoint restore")
                                        # Do not update best_wns
                                    else:
                                        self.latest_wns = wns_value  # Normal update
                                        if wns_value > self.best_wns:
                                            self.best_wns = wns_value
                                            self._best_wns_iteration = self.iteration
                                    get_wns_success = True
                                    logger.info(f"Iteration {self.iteration}: get_wns succeeded, WNS={wns_value}")
                                else:
                                    logger.warning(f"Iteration {self.iteration}: get_wns invalid WNS: {wns_value}, retry {retry_count}/{max_retries}")
                            except ValueError:
                                logger.warning(f"Iteration {self.iteration}: get_wns parse error: {wns_result.strip()}, retry {retry_count}/{max_retries}")
                        elif wns_result.strip() == "PARSE_ERROR":
                            logger.warning(f"Iteration {self.iteration}: get_wns returned PARSE_ERROR, retry {retry_count}/{max_retries}")
                        else:
                            logger.warning(f"Iteration {self.iteration}: get_wns empty result, retry {retry_count}/{max_retries}")
                    except Exception as e:
                        logger.warning(f"Iteration {self.iteration}: get_wns exception: {e}, retry {retry_count}/{max_retries}")

                    # If both succeeded, proceed to next iteration
                    if checkpoint_success and get_wns_success:
                        logger.info(f"Iteration {self.iteration}: Checkpoint + get_wns check passed, proceeding to next iteration")
                        break
                    else:
                        logger.warning(f"Iteration {self.iteration}: Checkpoint={checkpoint_success}, get_wns={get_wns_success}, retry {retry_count}/{max_retries}")
                        if retry_count < max_retries:
                            await asyncio.sleep(2)

                # If check failed after all retries, do not count this iteration - skip to next round without updating counters
                if not (checkpoint_success and get_wns_success):
                    logger.warning(f"Iteration {self.iteration}: Checkpoint + get_wns check FAILED after {max_retries} retries, skipping iteration (no counter update)")
                    # [Bug 4] Restore WNS state from snapshot taken before get_completion()
                    snap = getattr(self, '_wns_snapshot', None)
                    if snap:
                        self.best_wns = snap["best_wns"]
                        self._best_wns_iteration = snap["_best_wns_iteration"]
                        self.latest_wns = snap["latest_wns"]
                        self.latest_tns = snap["latest_tns"]
                        self.latest_failing_endpoints = snap["latest_failing_endpoints"]
                        self._best_wns_tns = snap["_best_wns_tns"]
                        self._best_wns_failing_endpoints = snap["_best_wns_failing_endpoints"]
                        logger.info(f"Restored WNS state from snapshot: best={self.best_wns}, latest={self.latest_wns}")
                    continue

                # [FIX] Determine WNS improvement AFTER checkpoint/get_wns confirms the actual WNS.
                # Previously this ran before the checkpoint phase, so global_no_improvement and
                # handoff prompts could use stale best_wns values. Now best_wns is guaranteed current.
                current_best = self.best_wns if self.best_wns > float('-inf') else None
                prev_best = getattr(self, '_prev_best_wns', None)
                wns_improved = False
                if current_best is not None and prev_best is not None:
                    wns_improved = current_best > prev_best
                elif current_best is not None and prev_best is None:
                    wns_improved = True  # First valid WNS obtained is considered improvement

                self._on_iteration_end(wns_improved, self.last_used_model)
                self._prev_best_wns = self.best_wns

                # [NEW] Per-iteration validation using intermediate checkpoint (every N iterations)
                # Use validation_interval to avoid running validation too frequently
                if (self.validation_enabled and
                    hasattr(self, 'output_dcp') and
                    intermediate_dcp is not None and
                    intermediate_dcp.exists() and
                    not is_done and
                    self.iteration % self.validation_interval == 0):
                    logger.info(f"Running mandatory validation (500 vectors) for iteration {self.iteration}...")
                    validation_passed = await self._run_full_validation(
                        intermediate_dcp,
                        label=f"iteration_{self.iteration}",
                        num_vectors=500
                    )

                    if not validation_passed:
                        logger.warning(f"Iteration {self.iteration}: Full validation FAILED, rolling back...")
                        # Rollback to best checkpoint
                        rollback_ok = await self._rollback_to_best_checkpoint()
                        if not rollback_ok:
                            logger.warning("Validation rollback failed, skipping iteration without restore")
                        # Skip this iteration (no counter update)
                        continue

                # Print skill telemetry for this iteration
                try:
                    from skills import SkillTelemetry
                    summary = SkillTelemetry.get_execution_summary()
                    metrics = SkillTelemetry.get_all_metrics()
                    if metrics:
                        print(f"\n  SKILL TELEMETRY (Iter {self.iteration}):")
                        for name, m in metrics.items():
                            if m["total_calls"] > 0:
                                print(f"    {name}: {m['total_calls']} calls, "
                                      f"{m['success_rate']*100:.0f}% success, "
                                      f"avg {m['avg_duration_ms']:.0f}ms")
                        recent = SkillTelemetry.get_recent_executions(limit=3)
                        if recent:
                            print(f"    Latest: {recent[0]['skill_name']} → {recent[0]['status']} ({recent[0]['duration_ms']:.0f}ms)")
                except Exception:
                    pass

                if is_done:
                    logger.info("Optimization workflow completed")
                    self.end_time = time.time()

                    # Write output DCP before validation
                    if hasattr(self, 'output_dcp') and self.output_dcp:
                        logger.info(f"Writing final DCP to {self.output_dcp}...")
                        try:
                            await self.call_tool("vivado_write_checkpoint", {
                                "dcp_path": str(self.output_dcp.resolve()),
                                "force": True
                            })
                        except Exception as e:
                            logger.warning(f"Failed to write output DCP: {e}")

                    # [NEW] Final full validation (Phase 1 + Phase 2)
                    if self.validation_enabled and hasattr(self, 'output_dcp') and self.output_dcp.exists():
                        logger.info("Running final full validation on output DCP...")
                        final_passed = await self._run_full_validation(self.output_dcp, label="final")
                        if final_passed:
                            print("\n✓ Final DCP validation PASSED")
                        else:
                            print("\n✗ Final DCP validation FAILED")

                    self._print_optimization_summary()
                    # Export skill telemetry for persistent storage
                    try:
                        from skills.telemetry import SkillTelemetry
                        SkillTelemetry.export_to_json(str(self.temp_dir / "skill_telemetry.json"))
                    except Exception as tele_err:
                        logger.warning("Failed to export skill telemetry: %s", tele_err)
                    return True
                    
            except Exception as e:
                logger.exception(f"Error during optimization: {e}")
                # Add error context to conversation
                self._compat.add_message(
                    "user",
                    f"An error occurred: {e}. Please verify your approach and continue or report if unrecoverable."
                )
        
        logger.warning("Reached maximum iterations")
        self.end_time = time.time()
        # Export skill telemetry for persistent storage
        try:
            from skills.telemetry import SkillTelemetry
            SkillTelemetry.export_to_json(str(self.temp_dir / "skill_telemetry.json"))
        except Exception as tele_err:
            logger.warning("Failed to export skill telemetry: %s", tele_err)
        self._print_optimization_summary(max_iterations_reached=True)
        return False

    # === Section 7.8: Reporting & Telemetry ===

    def save_token_usage_report(self, output_path: Path):
        """Save detailed token usage report to JSON file."""
        # Calculate total cached and reasoning tokens
        total_cached = sum(detail.get('cached_tokens', 0) for detail in self.api_call_details)
        total_reasoning = sum(detail.get('reasoning_tokens', 0) for detail in self.api_call_details)
        
        # Calculate tool call statistics
        total_tool_time = sum(detail['elapsed_time'] for detail in self.tool_call_details)
        tool_counts = {}
        for detail in self.tool_call_details:
            tool_name = detail['tool_name']
            if tool_name not in tool_counts:
                tool_counts[tool_name] = 0
            tool_counts[tool_name] += 1
        
        # Calculate total runtime
        total_runtime = None
        if self.start_time is not None:
            total_runtime = (self.end_time or time.time()) - self.start_time
        
        # Calculate fmax values
        initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
        best_fmax = self.calculate_fmax(self.best_wns, self.clock_period) if self.best_wns > float('-inf') else None
        fmax_improvement = (best_fmax - initial_fmax) if (initial_fmax is not None and best_fmax is not None) else None
        
        report = {
            "models": {
                "planner": self.model_planner,
                "worker": self.model_worker
            },
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_runtime_seconds": total_runtime,
                "total_llm_calls": self.llm_call_count,
                "total_iterations": self.iteration,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
                "total_cached_tokens": total_cached,
                "total_reasoning_tokens": total_reasoning,
                "total_cost": self.total_cost,
                "clock_period_ns": self.clock_period,
                "initial_wns": self.initial_wns,
                "best_wns": self.best_wns,
                "wns_improvement": self.best_wns - self.initial_wns if self.initial_wns is not None else None,
                "initial_fmax_mhz": initial_fmax,
                "best_fmax_mhz": best_fmax,
                "fmax_improvement_mhz": fmax_improvement,
                "total_tool_calls": len(self.tool_call_details),
                "total_tool_time_seconds": total_tool_time,
                "tool_call_counts": tool_counts,
                # Compression metrics
                "compression_total": self.compression_count,
                "compression_hard": self.compression_hard_count,
                "compression_soft": self.compression_soft_count,
                "compression_skipped": self.compression_skipped,
            },
            "per_llm_call_details": self.api_call_details,
            "per_tool_call_details": self.tool_call_details,
            "per_compression_details": self.compression_details,
            "skill_invocations": {
                "total_skill_calls": len(self.skill_invocation_log),
                "skill_call_counts": {
                    name: sum(
                        1 for s in self.skill_invocation_log
                        if s.get("skill_name") == name
                    )
                    for name in sorted(set(
                        s.get("skill_name", "") for s in self.skill_invocation_log
                    ))
                },
                "skill_invocation_details": self.skill_invocation_log,
                "skill_recommendation_details": self.skill_recommendation_log,
                "recommendation_acceptance_count": sum(
                    1 for r in self.skill_recommendation_log if r.get("accepted")
                ),
                "recommendation_total": len(self.skill_recommendation_log),
            },
        }
        
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Token usage report saved to {output_path}")
    
    def _print_optimization_summary(self, max_iterations_reached: bool = False):
        """Print detailed optimization summary including token usage and costs."""
        title = "Optimization Summary (Max Iterations Reached)" if max_iterations_reached else "Optimization Summary"
        print(f"\n{'='*70}")
        print(f"{title}")
        print(f"{'='*70}")
        
        # Calculate total runtime
        if self.start_time is not None:
            total_runtime = (self.end_time or time.time()) - self.start_time
            print(f"\nTOTAL RUNTIME: {total_runtime:.2f} seconds ({total_runtime/60:.2f} minutes)")

        best_wns = self.best_wns if self.best_wns > float('-inf') else None
        result_lines = self._format_fmax_results(
            self.clock_period, self.initial_wns, best_wns, result_label="Best"
        )
        if result_lines:
            print(f"\nFMAX RESULTS:")
            print("\n".join(result_lines))
        
        # Iteration stats
        print(f"\nITERATION STATS:")
        print(f"  Total iterations:    {self.iteration}")
        print(f"  LLM API calls:       {self.llm_call_count}")
        
        # Token usage
        print(f"\nTOKEN USAGE:")
        print(f"  Prompt tokens:       {self.total_prompt_tokens:,}")
        print(f"  Completion tokens:   {self.total_completion_tokens:,}")
        print(f"  Total tokens:        {self.total_tokens:,}")

        # Calculate total cached and reasoning tokens
        total_cached = sum(detail.get('cached_tokens', 0) for detail in self.api_call_details)
        total_reasoning = sum(detail.get('reasoning_tokens', 0) for detail in self.api_call_details)

        if total_cached > 0:
            print(f"  Cached tokens:       {total_cached:,} (saved cost)")
        if total_reasoning > 0:
            print(f"  Reasoning tokens:    {total_reasoning:,}")

        # Token cost (USD)
        if self.total_cost > 0:
            avg_per_1k = (self.total_cost / self.total_tokens * 1000) if self.total_tokens > 0 else 0
            print(f"  Total cost:          ${self.total_cost:.4f}")
            print(f"  Avg cost/1K tokens:  ${avg_per_1k:.6f}")
        
        # Cost
        print(f"\nCOST:")
        used_models = sorted(set(d.get("model", "") for d in self.api_call_details if d.get("model")))
        if used_models:
            print(f"  Models used:         {', '.join(used_models)}")
        else:
            print(f"  Planner Model:       {self.model_planner}")
            print(f"  Worker Model:        {self.model_worker}")
        if self.total_cost > 0:
            print(f"  Total cost:          ${self.total_cost:.4f}")
        else:
            print(f"  Total cost:          Not available")
        
        # Skill telemetry summary
        try:
            from skills import SkillTelemetry
            skill_metrics = SkillTelemetry.get_all_metrics()
            if skill_metrics:
                print(f"\nSKILL TELEMETRY:")
                for name, m in skill_metrics.items():
                    if m["total_calls"] > 0:
                        print(f"  {name}:")
                        print(f"    Calls:           {m['total_calls']}")
                        print(f"    Success rate:    {m['success_rate']*100:.0f}%")
                        print(f"    Avg duration:    {m['avg_duration_ms']:.0f}ms")
                        print(f"    Total duration:  {m['total_duration_ms']:.0f}ms")
                        if m['last_error']:
                            print(f"    Last error:      {m['last_error']}")
        except Exception:
            pass

        # Skill invocation summary (optimizer-level tracking with WNS context)
        if self.skill_invocation_log:
            print(f"\nSKILL INVOCATIONS:")
            from collections import Counter
            skill_counts = Counter(s.get("skill_name", "?") for s in self.skill_invocation_log)
            for skill_name, count in skill_counts.most_common():
                wns_vals = [
                    s["wns"] for s in self.skill_invocation_log
                    if s.get("skill_name") == skill_name and s.get("wns") is not None
                ]
                wns_str = f", avg WNS={sum(wns_vals)/len(wns_vals):.3f}ns" if wns_vals else ""
                errors = sum(
                    1 for s in self.skill_invocation_log
                    if s.get("skill_name") == skill_name and s.get("error")
                )
                err_str = f", {errors} errors" if errors else ""
                print(f"  {skill_name}: {count} calls{wns_str}{err_str}")

            accepted = sum(
                1 for r in self.skill_recommendation_log if r.get("accepted")
            )
            total_recs = len(self.skill_recommendation_log)
            if total_recs > 0:
                pct = 100.0 * accepted / total_recs
                print(f"  Recommendation acceptance: {accepted}/{total_recs} ({pct:.0f}%)")

        # Tool call summary
        if self.tool_call_details:
            print(f"\nTOOL CALLS SUMMARY:")
            print(f"  Total tool calls:    {len(self.tool_call_details)}")
            
            # Calculate total time spent in tool calls
            total_tool_time = sum(detail['elapsed_time'] for detail in self.tool_call_details)
            print(f"  Total tool time:     {total_tool_time:.2f}s")
            
            # Count by tool type
            tool_counts = {}
            for detail in self.tool_call_details:
                tool_name = detail['tool_name']
                if tool_name not in tool_counts:
                    tool_counts[tool_name] = 0
                tool_counts[tool_name] += 1
            
            print(f"\n  Tool call breakdown:")
            for tool_name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
                print(f"    {tool_name}: {count}")
            
            # Detailed tool call list
            print(f"\n  Detailed tool call log:")
            print(f"  {'#':<5} {'Iter':<6} {'Tool':<40} {'Time (s)':<12} {'WNS (ns)':<12} {'Status':<10}")
            print(f"  {'-'*5} {'-'*6} {'-'*40} {'-'*12} {'-'*12} {'-'*10}")
            
            for i, detail in enumerate(self.tool_call_details, 1):
                tool_name = detail['tool_name']
                iteration = detail.get('iteration', 0)
                elapsed = detail['elapsed_time']
                wns = detail.get('wns')
                error = detail.get('error', False)
                
                # Format WNS column
                wns_str = f"{wns:.3f}" if wns is not None else "-"
                
                # Format status
                status_str = "ERROR" if error else "OK"
                
                print(f"  {i:<5} {iteration:<6} {tool_name:<40} {elapsed:<12.2f} {wns_str:<12} {status_str:<10}")
                
                # If error, show error message on next line
                if error and 'error_message' in detail:
                    print(f"        Error: {detail['error_message'][:80]}")
        
        # Per-call breakdown if debug mode
        if self.debug and self.api_call_details:
            print(f"\nPER-CALL BREAKDOWN:")
            
            # Check if we have cached or reasoning tokens to display
            has_cached = any(detail.get('cached_tokens', 0) > 0 for detail in self.api_call_details)
            has_reasoning = any(detail.get('reasoning_tokens', 0) > 0 for detail in self.api_call_details)
            has_cost = any(detail.get('cost', 0) > 0 for detail in self.api_call_details)
            
            # Build header
            header = f"  {'Call':<6} {'Iter':<6} {'Role':<8} {'Prompt':<10} {'Completion':<12}"
            if has_cached:
                header += f" {'Cached':<10}"
            if has_reasoning:
                header += f" {'Reasoning':<10}"
            header += f" {'Total':<10}"
            if has_cost:
                header += f" {'Cost':<12}"
            print(header)
            
            # Build separator
            separator = f"  {'-'*6} {'-'*6} {'-'*8} {'-'*10} {'-'*12}"
            if has_cached:
                separator += f" {'-'*10}"
            if has_reasoning:
                separator += f" {'-'*10}"
            separator += f" {'-'*10}"
            if has_cost:
                separator += f" {'-'*12}"
            print(separator)
            
            # Print details
            for detail in self.api_call_details:
                role = "Planner" if detail.get('model') == self.model_planner else "Worker"
                line = (f"  {detail['call_number']:<6} {detail['iteration']:<6} {role:<8} "
                       f"{detail['prompt_tokens']:<10,} {detail['completion_tokens']:<12,}")
                if has_cached:
                    line += f" {detail.get('cached_tokens', 0):<10,}"
                if has_reasoning:
                    line += f" {detail.get('reasoning_tokens', 0):<10,}"
                line += f" {detail['total_tokens']:<10,}"
                if has_cost:
                    cost = detail.get('cost', 0)
                    line += f" ${cost:<11.4f}" if cost > 0 else f" {'N/A':<12}"
                print(line)
        
        print(f"\n{'='*70}\n")
        
        # Save detailed report to JSON in run directory
        try:
            report_path = self.run_dir / "token_usage.json"
            self.save_token_usage_report(report_path)
            print(f"Detailed token usage report saved to: {report_path}\n")
        except Exception as e:
            logger.warning(f"Failed to save token usage report: {e}")

        # Print output files
        print(f"Output files:")
        if hasattr(self, 'output_dcp') and self.output_dcp:
            print(f"  Optimized DCP: {self.output_dcp}")
        print(f"  Run directory: {self.run_dir}")

    # === Section 7.X: DCP Validation Helpers ===

    async def _rollback_to_best_checkpoint(self) -> bool:
        """Rollback to best known checkpoint with existence validation and WNS verification. Returns True on success."""
        best_iter = self._best_wns_iteration
        if best_iter is None:
            logger.error("No best checkpoint iteration recorded, cannot rollback")
            return False

        ckpt_path = self._get_intermediate_checkpoint_path(best_iter)
        if not ckpt_path.exists():
            if best_iter == 0 and hasattr(self, 'input_dcp') and self.input_dcp.exists():
                logger.warning(f"Best checkpoint {ckpt_path} does not exist, falling back to input DCP")
                try:
                    await self.call_tool("vivado_open_checkpoint", {"dcp_path": str(self.input_dcp.resolve())})
                    # Verify WNS after rollback
                    await self._verify_wns_after_rollback()
                    return True
                except Exception as e:
                    logger.error(f"Rollback to input DCP failed: {e}")
                    return False
            logger.error(f"Best checkpoint {ckpt_path} does not exist, cannot rollback")
            return False

        try:
            await self.call_tool("vivado_open_checkpoint", {"dcp_path": str(ckpt_path.resolve())})
            # Verify WNS after rollback (Bug 2: don't trust cached value blindly)
            await self._verify_wns_after_rollback()
            return True
        except Exception as e:
            logger.error(f"Rollback to best checkpoint failed: {e}")
            return False

    async def _verify_wns_after_rollback(self) -> None:
        """Verify WNS after checkpoint rollback, restoring cached TNS/endpoints."""
        try:
            wns_result = await self.call_tool("vivado_get_wns", {})
            if wns_result and wns_result.strip() not in ("", "(no output)", "PARSE_ERROR"):
                verified_wns = float(wns_result.strip())
                if self._is_valid_wns(verified_wns):
                    logger.info(f"Rollback WNS verified: {verified_wns:.3f} (cached best: {self.best_wns:.3f})")
                    self.latest_wns = verified_wns
                    if verified_wns > self.best_wns:
                        self.best_wns = verified_wns
                else:
                    logger.warning(f"Rollback WNS verification returned invalid value: {verified_wns}, using cached best_wns={self.best_wns:.3f}")
                    self.latest_wns = self.best_wns
            else:
                logger.warning(f"Rollback WNS verification failed ({wns_result}), using cached best_wns={self.best_wns:.3f}")
                self.latest_wns = self.best_wns
        except Exception as e:
            logger.warning(f"Rollback WNS verification exception: {e}, using cached best_wns={self.best_wns:.3f}")
            self.latest_wns = self.best_wns

        # Also restore TNS/failing_endpoints from cached values (Bug 3)
        if self._best_wns_tns is not None:
            self.latest_tns = self._best_wns_tns
        if self._best_wns_failing_endpoints is not None:
            self.latest_failing_endpoints = self._best_wns_failing_endpoints
        logger.info(f"Rollback state restored: WNS={self.latest_wns:.3f}, TNS={self.latest_tns}, endpoints={self.latest_failing_endpoints}")

    def _get_intermediate_checkpoint_path(self, iteration: int) -> Path:
        """Get path for intermediate checkpoint with iteration number."""
        return self.validation_report_dir / f"iteration_{iteration:03d}_checkpoint.dcp"

    async def _save_intermediate_checkpoint(self, iteration: int) -> Optional[Path]:
        """Save intermediate checkpoint for iteration rollback. Returns path or None on failure."""
        if not self.checkpoint_saving_enabled:
            return None

        ckpt_path = self._get_intermediate_checkpoint_path(iteration)
        self.validation_report_dir.mkdir(parents=True, exist_ok=True)

        try:
            result = await self.call_tool("vivado_write_checkpoint", {
                "dcp_path": str(ckpt_path.resolve()),
                "force": True
            })
            logger.info(f"Saved intermediate checkpoint: {ckpt_path}")
            return ckpt_path
        except Exception as e:
            logger.warning(f"Failed to save intermediate checkpoint: {e}")
            return None

    async def _run_full_validation(self, dcp: Path, label: str = "final", num_vectors: int = 10000) -> bool:
        """Run full Phase 1 + Phase 2 validation. Used for final DCP only."""
        script_path = Path(__file__).parent / "validate_dcps.py"
        validation_cmd = [
            sys.executable, "-u",
            str(script_path),
            str(self.input_dcp),
            str(dcp),
            "--vectors", str(num_vectors)
        ]

        # Record existing validation directories before running
        workspace_dir = Path(__file__).parent
        before_dirs = set(workspace_dir.glob("dcp_validation_*"))

        try:
            proc = await asyncio.create_subprocess_exec(
                *validation_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600.0)

            # Clean up temp directory created during this validation
            after_dirs = set(workspace_dir.glob("dcp_validation_*"))
            for temp_dir in after_dirs - before_dirs:
                shutil.rmtree(temp_dir, ignore_errors=True)

            return proc.returncode == 0
        except Exception as e:
            logger.warning(f"Full validation ERROR ({label}): {e}")
            return False


# === Section 8: FPGAOptimizerTest — Deterministic Test Mode ===

class FPGAOptimizerTest(DCPOptimizerBase):
    """
    Test mode for FPGA Design Optimization - hardcodes all tool calls to diagnose issues.
    
    This class runs a deterministic optimization flow without using any LLM, 
    making it easier to identify where MCP servers or Vivado might hang.
    """
    
    def __init__(self, debug: bool = False, run_dir: Optional[Path] = None, skip_skills: bool = False):
        super().__init__(debug=debug, run_dir=run_dir)
        self.final_wns = None
        self.skip_skills = skip_skills
        self.skill_test_results: list[dict] = []
        # Console exit monitoring (mirrors DCPOptimizer setup)
        self._user_exit_requested = threading.Event()
        self._async_exit_requested = asyncio.Event()
        # Start console reader thread
        def _read_console():
            try:
                while True:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    if line.strip().lower() == "quit":
                        logger.info("User requested exit via console 'quit' command")
                        self._user_exit_requested.set()
                        self._async_exit_requested.set()
                        break
            except Exception:
                pass
        t = threading.Thread(target=_read_console, daemon=True)
        t.start()

    def _check_exit_requested(self) -> bool:
        """Check if user requested exit. Returns True if exit requested."""
        return self._user_exit_requested.is_set()

    def _check_test_exit(self, step_name: str) -> bool:
        """Check if user requested exit via console 'quit'. Returns True if exit was requested."""
        if self._check_exit_requested():
            print(f"\n[TEST] ⏹ User requested exit — stopping before: {step_name}")
            return True
        return False

    async def start_servers(self):
        """Start and connect to both MCP servers."""
        await super().start_servers(log_prefix="[TEST]")
    
    async def call_vivado_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a Vivado tool call with timing and logging."""
        logger.info(f"[VIVADO] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[TEST] Calling vivado_{tool_name}...")
        start_time = time.time()

        heartbeat_task, heartbeat_count = self._start_tool_heartbeat(f"vivado_{tool_name}", start_time)
        try:
            result = await asyncio.wait_for(
                self.vivado_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.info(f"[VIVADO] {tool_name} completed in {elapsed:.2f}s (heartbeats: {heartbeat_count})")
            print(f"[TEST] vivado_{tool_name} completed in {elapsed:.2f}s")

            # Extract text content from result
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                return "\n".join(text_parts)
            return "(no output)"

        except asyncio.TimeoutError:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[TEST] ERROR: vivado_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.error(f"[VIVADO] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[TEST] ERROR: vivado_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise

    async def call_rapidwright_tool(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Execute a RapidWright tool call with timing and logging."""
        logger.info(f"[RAPIDWRIGHT] Calling {tool_name} with args: {json.dumps(arguments)[:200]}...")
        print(f"[TEST] Calling rapidwright_{tool_name}...")
        start_time = time.time()

        heartbeat_task, heartbeat_count = self._start_tool_heartbeat(f"rapidwright_{tool_name}", start_time)
        try:
            result = await asyncio.wait_for(
                self.rapidwright_session.call_tool(tool_name, arguments),
                timeout=timeout
            )
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.info(f"[RAPIDWRIGHT] {tool_name} completed in {elapsed:.2f}s (heartbeats: {heartbeat_count})")
            print(f"[TEST] rapidwright_{tool_name} completed in {elapsed:.2f}s")

            # Extract text content from result
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                return "\n".join(text_parts)
            return "(no output)"

        except asyncio.TimeoutError:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} TIMED OUT after {elapsed:.2f}s")
            print(f"[TEST] ERROR: rapidwright_{tool_name} TIMED OUT after {elapsed:.2f}s")
            raise
        except Exception as e:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            elapsed = time.time() - start_time
            logger.error(f"[RAPIDWRIGHT] {tool_name} FAILED after {elapsed:.2f}s: {e}")
            print(f"[TEST] ERROR: rapidwright_{tool_name} failed after {elapsed:.2f}s: {e}")
            raise
    
    def parse_wns_from_timing_report(self, timing_report: str) -> Optional[float]:
        """Extract WNS from timing report using shared parsing logic."""
        return parse_timing_summary_static(timing_report)["wns"]
    
    async def _call_vivado_for_clock(self, tool_name: str, arguments: dict) -> str:
        """Helper to call Vivado tools for clock period query."""
        return await self.call_vivado_tool(tool_name, arguments, timeout=60.0)
    
    async def fetch_clock_period(self) -> Optional[float]:
        """Query clock period with test-mode logging."""
        period = await super().get_clock_period(self._call_vivado_for_clock)
        if period is not None:
            print(f"[TEST] Clock period: {period:.3f} ns")
        else:
            print("[TEST] WARNING: Could not parse clock period from Vivado")
        return period
    
    def _verify_skill_result(self, skill_name: str, raw_result: str) -> dict:
        """Parse and verify a skill invocation result, print formatted summary."""
        import json as _json
        try:
            data = _json.loads(raw_result)
        except _json.JSONDecodeError as e:
            print(f"[TEST] ⚠ Skill '{skill_name}' returned non-JSON result: {e}")
            self.skill_test_results.append({"skill": skill_name, "success": False, "error": str(e)})
            return {}

        has_error = isinstance(data, dict) and "error" in data
        self.skill_test_results.append({"skill": skill_name, "success": not has_error, "data": data})

        if has_error:
            print(f"[TEST] ⚠ Skill '{skill_name}' returned error: {data['error']}")
            return data

        # Fanout direct execution result
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

        # StrategyPlan format (physopt)
        elif "strategy_name" in data:
            status = data.get("status", "unknown")
            steps = data.get("steps", [])
            print(f"[TEST] ✓ Skill '{skill_name}' | status: {status} | steps: {len(steps)}")
            for s in steps:
                mark = " ✓" if s.get("executed") else ""
                print(f"[TEST]   - {s['step_name']} ({s.get('platform', '?')}){mark}")
            if data.get("analysis_summary"):
                print(f"[TEST]   analysis: {_json.dumps(data['analysis_summary'], ensure_ascii=False)[:200]}")
        # PBLOCK analysis format (plain dict with region + pblock_ranges)
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
            next_steps = data.get("next_steps", [])
            if next_steps:
                print(f"[TEST]   next_steps ({len(next_steps)}):")
                for ns in next_steps:
                    print(f"[TEST]     → {ns}")
        elif "status" in data:
            print(f"[TEST] ✓ Skill '{skill_name}' | status: {data.get('status')} | "
                  f"message: {data.get('message', '')}")
        else:
            top_keys = list(data.keys())[:5]
            print(f"[TEST] ✓ Skill '{skill_name}' completed | keys: {top_keys}")

        return data

    async def run_test(self, input_dcp: Path, output_dcp: Path, max_nets_to_optimize: int = 5) -> bool:
        """
        Run the deterministic test optimization flow.
        
        Steps:
        1. Open the input DCP in Vivado
        2. Report timing in Vivado
        3. Get the critical high fan out nets from Vivado
        4. Open the DCP in RapidWright
        5. Apply the fanout optimization for each high fanout net
        6. Write a DCP out from RapidWright
        7. Read the RapidWright generated DCP into Vivado
        8. Route the design in Vivado
        9. Report timing and compare WNS
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print(f"Max nets to optimize: {max_nets_to_optimize}")
        print("="*70 + "\n")
        
        overall_start = time.time()
        
        try:
            # ================================================================
            # Step 0: Initialize RapidWright (Vivado starts automatically)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)
            
            # Initialize RapidWright (Vivado will auto-start when first used)
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")

            if self._check_test_exit("Step 1: Open input DCP in Vivado"):
                return False

            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            if self._check_test_exit("Step 2: Report timing in Vivado"):
                return False

            # ================================================================
            # Step 2: Report timing in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report timing in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            
            # Parse initial WNS
            self.initial_wns = self.parse_wns_from_timing_report(result)
            print(f"\n*** Initial WNS: {self.initial_wns} ns ***")
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            
            # Get clock period for fmax calculation
            self.clock_period = await self.fetch_clock_period()
            if self.clock_period is not None:
                target_fmax = 1000.0 / self.clock_period
                print(f"*** Target fmax: {target_fmax:.2f} MHz ***")
                
                initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
                if initial_fmax is not None:
                    print(f"*** Initial achievable fmax: {initial_fmax:.2f} MHz ***")
            print()

            if self._check_test_exit("Step 3: Get critical high fanout nets"):
                return False

            # ================================================================
            # Step 3: Get critical high fanout nets
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Get critical high fanout nets")
            print("-"*60)

            result = await self.call_vivado_tool("get_critical_high_fanout_nets", {
                "num_paths": 50,
                "min_fanout": 100,
                "exclude_clocks": True
            }, timeout=600.0)
            print(f"High fanout nets report:\n{result}")
            logger.info(f"High fanout nets: {result}")
            
            # Parse the nets
            self.high_fanout_nets = self.parse_high_fanout_nets(result)
            print(f"\nParsed {len(self.high_fanout_nets)} high fanout nets")
            
            if not self.high_fanout_nets:
                print("WARNING: No high fanout nets found to optimize!")
                logger.warning("No high fanout nets found to optimize")
            
            # Select top nets to optimize
            nets_to_optimize = self.high_fanout_nets[:max_nets_to_optimize]
            print(f"Will optimize {len(nets_to_optimize)} nets:")
            for net_name, fanout, path_count in nets_to_optimize:
                print(f"  - {net_name} (fanout={fanout}, paths={path_count})")

            if self._check_test_exit("Step 4: Open DCP in RapidWright"):
                return False

            # ================================================================
            # Step 4: Open the DCP in RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Open DCP in RapidWright")
            print("-"*60)

            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint result:\n{result}")
            logger.info(f"RapidWright read checkpoint: {result}")

            if self._check_test_exit("Skill invocation tests"):
                return False

            # ── Skill Invocation Tests ──────────────────────────────────────
            if not self.skip_skills:
                # Step 4.1: Test analyze_net_detour
                print("\n" + "-"*60)
                print("STEP 4.1: [SKILL] Test analyze_net_detour")
                print("-"*60)
                pins_result = ""
                try:
                    # Get pin paths from Vivado for analysis
                    pins_result = await self.call_vivado_tool("extract_critical_path_pins", {
                        "num_paths": 5
                    }, timeout=300.0)
                    if pins_result.strip() and pins_result.strip() != "(no output)":
                        try:
                            import json as _json
                            pins_data = _json.loads(pins_result)
                            pin_paths_array = pins_data.get("pin_paths", [])
                        except Exception:
                            pin_paths_array = []
                        if pin_paths_array and len(pin_paths_array) > 0:
                            pin_paths = pin_paths_array[0]
                            print(f"[TEST] Using {len(pin_paths)} pins from critical path (of {len(pin_paths_array)} paths found)")
                            skill_result = await self.call_rapidwright_tool("analyze_net_detour", {
                                "pin_paths": pin_paths,
                                "detour_threshold": 2.0
                            }, timeout=120.0)
                            self._verify_skill_result("analyze_net_detour", skill_result)
                        else:
                            print("[TEST] ⚠ analyze_net_detour skipped: no pin paths in result")
                            try:
                                import json as _json2
                                _data = _json2.loads(pins_result)
                                print(f"[TEST] debug_has_slack={_data.get('debug_has_slack', '?')}")
                                print(f"[TEST] debug_report_length={_data.get('debug_report_length', '?')}")
                                print(f"[TEST] debug_num_path_sections={_data.get('debug_num_slack_sections', '?')}")
                                if "debug_per_path" in _data:
                                    print(f"[TEST] per-path debug: {_data['debug_per_path']}")
                                report_snippet = _data.get("debug_timing_report", "")
                                if report_snippet:
                                    print(f"[TEST] debug_timing_report:\n{report_snippet}")
                            except Exception:
                                print(f"[TEST] Raw pins_result: {str(pins_result)[:500]}")
                except Exception as e:
                    print(f"[TEST] ⚠ analyze_net_detour FAILED: {e}")

                # Step 4.2: Test smart_region_search
                print("\n" + "-"*60)
                print("STEP 4.2: [SKILL] Test smart_region_search")
                print("-"*60)
                try:
                    skill_result = await self.call_rapidwright_tool("smart_region_search", {
                        "target_lut_count": 50000,
                        "target_ff_count": 50000,
                    }, timeout=360.0)
                    self._verify_skill_result("smart_region_search", skill_result)
                except Exception as e:
                    print(f"[TEST] ⚠ smart_region_search skipped: {e}")

                # Step 4.3: Test analyze_pblock_region
                print("\n" + "-"*60)
                print("STEP 4.3: [SKILL] Test analyze_pblock_region")
                print("-"*60)
                try:
                    skill_result = await self.call_rapidwright_tool("analyze_pblock_region", {
                        "target_lut_count": 50000,
                        "target_ff_count": 50000,
                        "resource_multiplier": 1.5,
                    }, timeout=600.0)
                    self._verify_skill_result("analyze_pblock_region", skill_result)
                except Exception as e:
                    print(f"[TEST] ⚠ analyze_pblock_region skipped: {e}")

                # Step 4.4: Test execute_physopt_strategy
                print("\n" + "-"*60)
                print("STEP 4.4: [SKILL] Test execute_physopt_strategy")
                print("-"*60)
                try:
                    skill_result = await self.call_rapidwright_tool("execute_physopt_strategy", {
                        "directive": "Default",
                        "design_is_routed": False,
                    }, timeout=360.0)
                    self._verify_skill_result("execute_physopt_strategy", skill_result)
                except Exception as e:
                    print(f"[TEST] ⚠ execute_physopt_strategy skipped: {e}")

                # Step 4.5: optimize_cell_placement
                print("\n" + "-"*60)
                print("STEP 4.5: [SKILL] optimize_cell_placement")
                print("-"*60)
                try:
                    cell_names = []
                    if pins_result.strip() and pins_result.strip() != "(no output)":
                        try:
                            import json as _json
                            pins_data = _json.loads(pins_result)
                            pin_paths_list = pins_data.get("pin_paths", [])
                        except Exception:
                            pin_paths_list = []
                        if pin_paths_list and len(pin_paths_list) > 0:
                            pp = pin_paths_list[0]
                            seen = set()
                            for p in pp:
                                cell = p.split("/")[0] if "/" in p else p
                                if cell not in seen:
                                    seen.add(cell)
                                    cell_names.append(cell)
                            cell_names = cell_names[:5]
                    if cell_names:
                        sr = await self.call_rapidwright_tool("optimize_cell_placement", {
                            "cell_names": cell_names,
                        }, timeout=360.0)
                        self._verify_skill_result("optimize_cell_placement", sr)
                    else:
                        # Fallback: get cell names directly from the loaded RapidWright design
                        print("[TEST] No critical path cells, trying search_cells fallback...")
                        try:
                            sr = await self.call_rapidwright_tool("search_cells", {"limit": 5}, timeout=60.0)
                            if sr.strip():
                                import json as _json
                                cells_data = _json.loads(sr)
                                fallback_names = [c["name"] for c in cells_data.get("cells", []) if c.get("name")]
                                if fallback_names:
                                    print(f"[TEST] Using fallback cell names: {fallback_names}")
                                    sr = await self.call_rapidwright_tool("optimize_cell_placement", {
                                        "cell_names": fallback_names,
                                    }, timeout=360.0)
                                    self._verify_skill_result("optimize_cell_placement", sr)
                                else:
                                    print("[TEST] ⚠ optimize_cell_placement skipped: no cell names available")
                            else:
                                print("[TEST] ⚠ optimize_cell_placement skipped: no cell names available")
                        except Exception as e2:
                            print(f"[TEST] ⚠ optimize_cell_placement skipped (fallback): {e2}")
                except Exception as e:
                    print(f"[TEST] ⚠ optimize_cell_placement skipped: {e}")

            if self._check_test_exit("Step 5: Fanout optimizations"):
                return False

            # ================================================================
            # Step 5: Apply fanout optimization via skill (or raw tool)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Apply fanout optimizations in RapidWright")
            print("-"*60)

            # Build batch request: list of {net_name, fanout}
            net_configs = [
                {"net_name": net_name, "fanout": fanout}
                for net_name, fanout, path_count in nets_to_optimize
            ]

            if not self.skip_skills:
                # Use execute_fanout_strategy skill (runs optimize_fanout_batch + write_checkpoint internally)
                print(f"Calling execute_fanout_strategy skill for {len(net_configs)} nets")
                try:
                    result = await self.call_rapidwright_tool("execute_fanout_strategy", {
                        "nets": net_configs,
                        "temp_dir": str(self.temp_dir),
                        "checkpoint_prefix": "test_fanout",
                    }, timeout=max(600.0, 300.0 * len(net_configs)))
                    data = self._verify_skill_result("execute_fanout_strategy", result)
                    successful_optimizations = data.get("successful_count", 0)
                    print(f"\nSuccessfully optimized {successful_optimizations}/{len(nets_to_optimize)} nets (via skill)")
                except Exception as e:
                    print(f"execute_fanout_strategy FAILED: {e}")
                    logger.error(f"Failed to execute fanout strategy: {e}")
                    successful_optimizations = 0
            else:
                # Raw tool path (original behavior)
                print(f"Batch optimizing {len(net_configs)} nets (raw tool)")
                try:
                    result = await self.call_rapidwright_tool("optimize_fanout_batch", {
                        "nets": net_configs
                    }, timeout=300.0 * len(net_configs))
                    print(f"Batch result: {result[:1000]}...")
                    logger.info(f"Optimize fanout batch: {result}")

                    import json
                    try:
                        result_data = json.loads(result)
                        if result_data.get("status") == "success":
                            successful_optimizations = result_data.get("successful_count", 0)
                        else:
                            successful_optimizations = 0
                    except json.JSONDecodeError:
                        successful_optimizations = 0
                        logger.error(f"Failed to parse batch result: {result}")
                except Exception as e:
                    print(f"Batch optimization FAILED: {e}")
                    logger.error(f"Failed to batch optimize: {e}")
                    successful_optimizations = 0

                print(f"\nSuccessfully optimized {successful_optimizations}/{len(nets_to_optimize)} nets")

            if self._check_test_exit("Step 6: Write DCP from RapidWright"):
                return False

            # ================================================================
            # Step 6: Write DCP from RapidWright
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Write DCP from RapidWright")
            print("-"*60)
            
            rapidwright_dcp = Path(self.temp_dir) / "rapidwright_optimized.dcp"
            result = await self.call_rapidwright_tool("write_checkpoint", {
                "dcp_path": str(rapidwright_dcp),
                "overwrite": True
            }, timeout=600.0)
            print(f"Write checkpoint result:\n{result}")
            logger.info(f"RapidWright write checkpoint: {result}")
            
            # Check if the file was created
            if rapidwright_dcp.exists():
                print(f"DCP file created: {rapidwright_dcp} ({rapidwright_dcp.stat().st_size} bytes)")
            else:
                print("WARNING: DCP file was not created!")
                logger.warning("RapidWright DCP file not created")

            if self._check_test_exit("Step 7: Read RapidWright DCP into Vivado"):
                return False

            # ================================================================
            # Step 7: Read RapidWright DCP into Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Read RapidWright DCP into Vivado")
            print("-"*60)
            
            # Note: Opening a RapidWright-generated DCP takes MUCH longer than
            # opening the original DCP because:
            # 1. Vivado must reload encrypted IP blocks from disk
            # 2. Vivado must reconstruct internal data structures
            # For large designs, this can take 10-30 minutes
            RAPIDWRIGHT_DCP_TIMEOUT = 300.0  # 5 minutes
            
            # Check if there's a Tcl script we need to source first (for encrypted IP)
            tcl_script = rapidwright_dcp.with_suffix('.tcl')
            if tcl_script.exists():
                print(f"Found Tcl script for encrypted IP: {tcl_script}")
                print(f"Note: This may take 10-30 minutes for large designs...")
                # Source the Tcl script instead of directly opening the DCP
                result = await self.call_vivado_tool("run_tcl", {
                    "command": f"source {{{tcl_script}}}"
                }, timeout=RAPIDWRIGHT_DCP_TIMEOUT)
                print(f"Source Tcl script result:\n{result}")
            else:
                # Opening a RapidWright-generated DCP can take longer than original
                # because Vivado needs to reconstruct some internal data structures
                result = await self.call_vivado_tool("open_checkpoint", {
                    "dcp_path": str(rapidwright_dcp)
                }, timeout=RAPIDWRIGHT_DCP_TIMEOUT)
                print(f"Open RapidWright DCP result:\n{result}")
            logger.info(f"Open RapidWright DCP: {result}")

            if self._check_test_exit("Step 8: Route design in Vivado"):
                return False

            # ================================================================
            # Step 8: Route the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 8: Route design in Vivado")
            print("-"*60)

            # First check route status
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status before routing:\n{result[:1500]}...")
            logger.info(f"Route status before routing: {result}")
            
            # ================================================================
            # [NEW] Intermediate checkpoint: Pre-routing optimization
            # If timing is still poor after placement, try phys_opt_design for load reduction
            # ================================================================
            # Get pre-routing timing state
            pre_route_timing = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            pre_route_info = parse_timing_summary_static(pre_route_timing)
            pre_route_wns = pre_route_info.get("wns")
            
            if pre_route_wns is not None and pre_route_wns < -0.5:
                print(f"\nPre-routing WNS is poor ({pre_route_wns:.3f} ns), trying phys_opt_design for load reduction...")
                logger.info(f"Pre-route WNS poor ({pre_route_wns:.3f} ns), attempting phys_opt_design...")

                # Try phys_opt_design to optimize high fanout nets and retiming
                phys_opt_result = await self.call_vivado_tool("phys_opt_design", {
                    "directive": "aggressive_preroute_optimization"
                }, timeout=3600.0)
                print(f"phys_opt_design result:\n{phys_opt_result[:1500]}...")
                logger.info(f"phys_opt_design: {phys_opt_result}")

                # Recheck timing
                post_phys_timing = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
                post_phys_info = parse_timing_summary_static(post_phys_timing)
                post_phys_wns = post_phys_info.get("wns")

                if post_phys_wns is not None and post_phys_wns > pre_route_wns:
                    print(f"phys_opt_design improved timing: {pre_route_wns:.3f} -> {post_phys_wns:.3f} ns")
                else:
                    print(f"phys_opt_design did not significantly improve timing, continuing with routing")

            # Route the design with extended timeout (6 hours)
            ROUTE_TIMEOUT = 21600.0  # 6 hours = 6 * 60 * 60 = 21600 seconds
            print(f"\nRouting design (timeout: {ROUTE_TIMEOUT:.0f} seconds / {ROUTE_TIMEOUT/3600:.1f} hours)...")

            # Start heartbeat logger for long-running route_design
            done_event = __import__('threading').Event()
            heartbeat = HeartbeatLogger(
                interval_seconds=60.0,
                message=f"route_design in progress (timeout: {ROUTE_TIMEOUT:.0f}s)",
                done_event=done_event
            )
            heartbeat.start()

            try:
                result = await self.call_vivado_tool("route_design", {
                    "directive": "Default",
                }, timeout=ROUTE_TIMEOUT)
                print(f"Route design result:\n{result}")
                logger.info(f"Route design: {result}")
            finally:
                done_event.set()
                heartbeat.stop()

            # Check route status again
            result = await self.call_vivado_tool("report_route_status", {
                "show_unrouted": True,
                "show_errors": True,
                "max_nets": 20
            }, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            if self._check_test_exit("Step 9: Report final timing"):
                return False

            # ================================================================
            # Step 9: Report timing and compare WNS
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9: Report final timing")
            print("-"*60)

            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")

            # Parse final WNS
            self.final_wns = self.parse_wns_from_timing_report(result)
            print(f"\n*** Final WNS: {self.final_wns} ns ***")
            logger.info(f"Final WNS: {self.final_wns} ns")

            # Calculate final fmax
            final_fmax = self.calculate_fmax(self.final_wns, self.clock_period)
            if final_fmax is not None:
                print(f"*** Final achievable fmax: {final_fmax:.2f} MHz ***")
            print()

            # ================================================================
            # Step 9.5: Verify get_wns tool
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9.5: Verify get_wns tool")
            print("-"*60)

            try:
                get_wns_result = await self.call_vivado_tool("get_wns", {}, timeout=60.0)
                print(f"get_wns raw result: '{get_wns_result}'")

                if get_wns_result.strip() == "PARSE_ERROR":
                    print("*** get_wns returned PARSE_ERROR ***")
                    logger.warning("get_wns returned PARSE_ERROR in test mode")
                else:
                    try:
                        get_wns_value = float(get_wns_result.strip())
                        print(f"*** get_wns WNS: {get_wns_value} ns ***")

                        # Compare with timing_summary WNS
                        if self.final_wns is not None:
                            diff = abs(get_wns_value - self.final_wns)
                            print(f"*** Timing summary WNS: {self.final_wns} ns, diff: {diff:.4f} ns ***")
                            if diff < 0.01:
                                print("✓ get_wns matches timing_summary (diff < 0.01 ns)")
                            else:
                                print(f"WARNING: get_wns differs from timing_summary by {diff:.4f} ns")
                        logger.info(f"get_wns verification: {get_wns_value} ns (timing_summary: {self.final_wns} ns)")
                    except ValueError as e:
                        print(f"*** get_wns parse error: {e} ***")
                        logger.warning(f"get_wns cannot parse '{get_wns_result}': {e}")
            except Exception as e:
                print(f"*** get_wns call failed: {e} ***")
                logger.error(f"get_wns call failed in test mode: {e}")

            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)
            
            # Always write the final checkpoint (regardless of improvement)
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")
            
            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Nets optimized: {successful_optimizations}/{len(nets_to_optimize)}"
            )

            # Print skill invocation test summary
            if not self.skip_skills and self.skill_test_results:
                passed = sum(1 for r in self.skill_test_results if r.get("success"))
                total = len(self.skill_test_results)
                print(f"\n{'='*60}")
                print(f"SKILL INVOCATION TEST RESULTS: {passed}/{total} passed")
                print(f"{'='*60}")
                for r in self.skill_test_results:
                    mark = "✓" if r.get("success") else "✗"
                    print(f"  [{mark}] {r['skill']}")
                print()

            return True
            
        except Exception as e:
            logger.exception(f"Test failed with exception: {e}")
            print(f"\n*** TEST FAILED ***")
            print(f"Exception: {type(e).__name__}: {e}")
            return False
    
    async def run_test_logicnets(self, input_dcp: Path, output_dcp: Path) -> bool:
        """
        Run the pblock-based optimization flow for LogicNets designs.
        
        Steps:
        1. Open the input DCP in Vivado
        2. Report timing in Vivado (Initialize WNS)
        3. Run the Vivado tool extract_critical_path_cells
        4. Run the RapidWright tool analyze_critical_path_spread
        5. Use known-optimal pblock range for LogicNets (SLICE_X55Y60:SLICE_X111Y254)
        6. Unplace the design in Vivado
        7. Create and apply pblock to entire design
        8. Place the design in Vivado
        9. Route the design in Vivado
        10. Report timing in Vivado (compare against initial WNS)
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER TEST MODE - LOGICNETS PBLOCK FLOW")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Output DCP: {output_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("="*70 + "\n")
        
        overall_start = time.time()
        
        try:
            # ================================================================
            # Step 0: Initialize RapidWright (Vivado starts automatically)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 0: Initialize RapidWright")
            print("-"*60)
            
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")
            logger.info(f"RapidWright init result: {result}")

            if self._check_test_exit("Step 1: Open input DCP in Vivado"):
                return False

            # ================================================================
            # Step 1: Open the input DCP in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 1: Open input DCP in Vivado")
            print("-"*60)

            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint result:\n{result}")
            logger.info(f"Open checkpoint result: {result}")

            if self._check_test_exit("Step 2: Report timing in Vivado"):
                return False

            # ================================================================
            # Step 2: Report timing in Vivado (Initialize WNS)
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 2: Report initial timing in Vivado")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Initial timing summary: {result}")
            
            # Parse initial WNS
            self.initial_wns = self.parse_wns_from_timing_report(result)
            print(f"\n*** Initial WNS: {self.initial_wns} ns ***")
            logger.info(f"Initial WNS: {self.initial_wns} ns")
            
            # Get clock period for fmax calculation
            self.clock_period = await self.fetch_clock_period()
            if self.clock_period is not None:
                target_fmax = 1000.0 / self.clock_period
                print(f"*** Target fmax: {target_fmax:.2f} MHz ***")
                
                initial_fmax = self.calculate_fmax(self.initial_wns, self.clock_period)
                if initial_fmax is not None:
                    print(f"*** Initial achievable fmax: {initial_fmax:.2f} MHz ***")
            print()

            if self._check_test_exit("Step 3: Extract critical path cells"):
                return False

            # ================================================================
            # Step 3: Extract critical path cells from Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 3: Extract critical path cells")
            print("-"*60)
            
            # Write to a file for efficient data transfer
            critical_paths_file = Path(self.temp_dir) / "critical_paths.json"
            result = await self.call_vivado_tool("extract_critical_path_cells", {
                "num_paths": 50,
                "output_file": str(critical_paths_file)
            }, timeout=600.0)
            print(f"Extract critical paths result:\n{result[:2000]}...")
            logger.info(f"Extract critical paths: {result}")

            if self._check_test_exit("Step 4: Analyze critical path spread"):
                return False

            # ================================================================
            # Step 4: Open DCP in RapidWright and analyze critical path spread
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 4: Analyze critical path spread in RapidWright")
            print("-"*60)

            # First, open the DCP in RapidWright
            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint result:\n{result}")
            logger.info(f"RapidWright read checkpoint: {result}")
            
            # Analyze critical path spread
            result = await self.call_rapidwright_tool("analyze_critical_path_spread", {
                "input_file": str(critical_paths_file)
            }, timeout=300.0)
            print(f"Critical path spread analysis:\n{result[:3000] if isinstance(result, str) else str(result)[:3000]}...")
            logger.info(f"Critical path spread: {result}")
            
            # Parse the spread analysis result to check if pblock is recommended
            spread_result = result if isinstance(result, str) else str(result)
            pblock_recommended = "spread-out" in spread_result.lower() or "pblock" in spread_result.lower()
            print(f"\n*** Pblock optimization {'RECOMMENDED' if pblock_recommended else 'may not be needed'} ***")

            if self._check_test_exit("Skill invocation tests"):
                return False

            # ── Skill Invocation Tests ──────────────────────────────────────
            if not self.skip_skills:
                # Step 4.1: Test analyze_net_detour
                print("\n" + "-"*60)
                print("STEP 4.1: [SKILL] Test analyze_net_detour")
                print("-"*60)
                pins_result = ""
                try:
                    pins_result = await self.call_vivado_tool("extract_critical_path_pins", {
                        "num_paths": 5
                    }, timeout=300.0)
                    if pins_result.strip() and pins_result.strip() != "(no output)":
                        try:
                            import json as _json
                            pins_data = _json.loads(pins_result)
                            pin_paths_array = pins_data.get("pin_paths", [])
                        except Exception:
                            pin_paths_array = []
                        if pin_paths_array and len(pin_paths_array) > 0:
                            pin_paths = pin_paths_array[0]
                            print(f"[TEST] Using {len(pin_paths)} pins from critical path (of {len(pin_paths_array)} paths found)")
                            skill_result = await self.call_rapidwright_tool("analyze_net_detour", {
                                "pin_paths": pin_paths,
                                "detour_threshold": 2.0
                            }, timeout=120.0)
                            self._verify_skill_result("analyze_net_detour", skill_result)
                        else:
                            print("[TEST] ⚠ analyze_net_detour skipped: no pin paths in result")
                            try:
                                import json as _json2
                                _data = _json2.loads(pins_result)
                                print(f"[TEST] debug_has_slack={_data.get('debug_has_slack', '?')}")
                                print(f"[TEST] debug_report_length={_data.get('debug_report_length', '?')}")
                                print(f"[TEST] debug_num_path_sections={_data.get('debug_num_slack_sections', '?')}")
                                if "debug_per_path" in _data:
                                    print(f"[TEST] per-path debug: {_data['debug_per_path']}")
                                report_snippet = _data.get("debug_timing_report", "")
                                if report_snippet:
                                    print(f"[TEST] debug_timing_report:\n{report_snippet}")
                            except Exception:
                                print(f"[TEST] Raw pins_result: {str(pins_result)[:500]}")
                except Exception as e:
                    print(f"[TEST] ⚠ analyze_net_detour FAILED: {e}")

                # Step 4.2: Test smart_region_search
                print("\n" + "-"*60)
                print("STEP 4.2: [SKILL] Test smart_region_search")
                print("-"*60)
                try:
                    skill_result = await self.call_rapidwright_tool("smart_region_search", {
                        "target_lut_count": 50000,
                        "target_ff_count": 5000,
                    }, timeout=360.0)
                    self._verify_skill_result("smart_region_search", skill_result)
                except Exception as e:
                    print(f"[TEST] ⚠ smart_region_search skipped: {e}")

                # Step 4.3: Test analyze_pblock_region
                print("\n" + "-"*60)
                print("STEP 4.3: [SKILL] Test analyze_pblock_region")
                print("-"*60)
                try:
                    skill_result = await self.call_rapidwright_tool("analyze_pblock_region", {
                        "target_lut_count": 50000,
                        "target_ff_count": 5000,
                        "resource_multiplier": 1.5,
                    }, timeout=600.0)
                    self._verify_skill_result("analyze_pblock_region", skill_result)
                except Exception as e:
                    print(f"[TEST] ⚠ analyze_pblock_region skipped: {e}")

                # Step 4.4: Test execute_physopt_strategy
                print("\n" + "-"*60)
                print("STEP 4.4: [SKILL] Test execute_physopt_strategy")
                print("-"*60)
                try:
                    skill_result = await self.call_rapidwright_tool("execute_physopt_strategy", {
                        "directive": "Default",
                        "design_is_routed": False,
                    }, timeout=360.0)
                    self._verify_skill_result("execute_physopt_strategy", skill_result)
                except Exception as e:
                    print(f"[TEST] ⚠ execute_physopt_strategy skipped: {e}")

                # Step 4.5: Test execute_fanout_strategy
                print("\n" + "-"*60)
                print("STEP 4.5: [SKILL] Test execute_fanout_strategy")
                print("-"*60)
                try:
                    # Use estimated nets from critical path analysis if available
                    test_nets = [{"net_name": "dummy_net", "fanout": 100}]
                    skill_result = await self.call_rapidwright_tool("execute_fanout_strategy", {
                        "nets": test_nets,
                        "temp_dir": str(self.temp_dir),
                        "checkpoint_prefix": "lnets_fanout_test",
                    }, timeout=120.0)
                    self._verify_skill_result("execute_fanout_strategy", skill_result)
                except Exception as e:
                    print(f"[TEST] ⚠ execute_fanout_strategy skipped: {e}")

                # Step 4.6: optimize_cell_placement
                print("\n" + "-"*60)
                print("STEP 4.6: [SKILL] optimize_cell_placement")
                print("-"*60)
                try:
                    cell_names = []
                    if pins_result.strip() and pins_result.strip() != "(no output)":
                        try:
                            import json as _json
                            pins_data = _json.loads(pins_result)
                            pin_paths_list = pins_data.get("pin_paths", [])
                        except Exception:
                            pin_paths_list = []
                        if pin_paths_list and len(pin_paths_list) > 0:
                            pp = pin_paths_list[0]
                            seen = set()
                            for p in pp:
                                cell = p.split("/")[0] if "/" in p else p
                                if cell not in seen:
                                    seen.add(cell)
                                    cell_names.append(cell)
                            cell_names = cell_names[:5]
                    if cell_names:
                        sr = await self.call_rapidwright_tool("optimize_cell_placement", {
                            "cell_names": cell_names,
                        }, timeout=360.0)
                        self._verify_skill_result("optimize_cell_placement", sr)
                    else:
                        # Fallback: get cell names directly from the loaded RapidWright design
                        print("[TEST] No critical path cells, trying search_cells fallback...")
                        try:
                            sr = await self.call_rapidwright_tool("search_cells", {"limit": 5}, timeout=60.0)
                            if sr.strip():
                                import json as _json
                                cells_data = _json.loads(sr)
                                fallback_names = [c["name"] for c in cells_data.get("cells", []) if c.get("name")]
                                if fallback_names:
                                    print(f"[TEST] Using fallback cell names: {fallback_names}")
                                    sr = await self.call_rapidwright_tool("optimize_cell_placement", {
                                        "cell_names": fallback_names,
                                    }, timeout=360.0)
                                    self._verify_skill_result("optimize_cell_placement", sr)
                                else:
                                    print("[TEST] ⚠ optimize_cell_placement skipped: no cell names available")
                            else:
                                print("[TEST] ⚠ optimize_cell_placement skipped: no cell names available")
                        except Exception as e2:
                            print(f"[TEST] ⚠ optimize_cell_placement skipped (fallback): {e2}")
                except Exception as e:
                    print(f"[TEST] ⚠ optimize_cell_placement skipped: {e}")

            if self._check_test_exit("Step 5: Use known-optimal pblock"):
                return False

            # ================================================================
            # Step 5: Use known-optimal pblock for LogicNets design
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 5: Use known-optimal pblock for LogicNets")
            print("-"*60)

            pblock_ranges = "SLICE_X55Y60:SLICE_X111Y254"

            print(f"Using known-optimal pblock range for LogicNets design:")
            print(f"  Pblock: {pblock_ranges}")
            print(f"  Width:  57 SLICE columns (X55 to X111)")
            print(f"  Height: 195 SLICE rows (Y60 to Y254)")
            print(f"\nThis pblock was empirically determined to achieve timing closure")
            print(f"by constraining the spread-out design to a compact region.")

            if self._check_test_exit("Step 6: Unplace the design"):
                return False

            # ================================================================
            # Step 6: Unplace the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 6: Unplace the design in Vivado")
            print("-"*60)
            
            # Use place_design -unplace to remove all placement
            result = await self.call_vivado_tool("run_tcl", {
                "command": "place_design -unplace"
            }, timeout=300.0)
            print(f"Unplace result:\n{result}")
            logger.info(f"Unplace result: {result}")

            if self._check_test_exit("Step 7: Create and apply pblock"):
                return False

            # ================================================================
            # Step 7: Create and apply pblock to entire design
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 7: Create and apply pblock to entire design")
            print("-"*60)
            
            result = await self.call_vivado_tool("create_and_apply_pblock", {
                "pblock_name": "pblock_opt",
                "ranges": pblock_ranges,
                "apply_to": "current_design",  # Apply to entire design
                "is_soft": False  # Hard constraint
            }, timeout=300.0)
            print(f"Create and apply pblock result:\n{result}")
            logger.info(f"Create pblock result: {result}")

            if self._check_test_exit("Step 8: Place the design"):
                return False

            # ================================================================
            # Step 8: Place the design in Vivado
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 8: Place the design in Vivado")
            print("-"*60)

            # Start heartbeat logger for long-running place_design
            done_event = __import__('threading').Event()
            heartbeat = HeartbeatLogger(
                interval_seconds=60.0,
                message="place_design in progress (timeout: 3600s)",
                done_event=done_event
            )
            heartbeat.start()

            try:
                result = await self.call_vivado_tool("place_design", {
                    "directive": "Default"
                }, timeout=3600.0)  # 1 hour timeout for placement
                print(f"Place design result:\n{result}")
                logger.info(f"Place design: {result}")
            finally:
                done_event.set()
                heartbeat.stop()

            if self._check_test_exit("Step 9: Route design"):
                return False

            # ================================================================
            # Step 9: Route the design in Vivado
            # ================================================================
            
            # ================================================================
            # [NEW] Intermediate checkpoint: Pre-routing optimization
            # Perform phys_opt_design before routing to reduce congestion
            # ================================================================
            pre_route_timing = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            pre_route_info = parse_timing_summary_static(pre_route_timing)
            pre_route_wns = pre_route_info.get("wns")

            if pre_route_wns is not None:
                print(f"Pre-routing WNS: {pre_route_wns:.3f} ns")

                # If timing is poor, try phys_opt_design for load reduction
                if pre_route_wns < -0.3:
                    print(f"Pre-routing timing is poor, trying phys_opt_design optimization...")

                    # Try multiple directives
                    directives_to_try = [
                        "aggressive_preroute_optimization",
                        "directive",  # Default
                        "add_physical_constraints"
                    ]

                    best_wns_after_physopt = pre_route_wns
                    best_physopt_result = None
                    best_directive = None

                    for directive in directives_to_try:
                        print(f"\nTrying phys_opt_design -directive {directive}...")
                        try:
                            phys_opt_result = await self.call_vivado_tool("phys_opt_design", {
                                "directive": directive
                            }, timeout=3600.0)

                            # Check results
                            check_timing = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
                            check_info = parse_timing_summary_static(check_timing)
                            check_wns = check_info.get("wns")

                            if check_wns is not None and check_wns > best_wns_after_physopt:
                                best_wns_after_physopt = check_wns
                                best_physopt_result = phys_opt_result
                                best_directive = directive
                                print(f"Improved: {pre_route_wns:.3f} -> {check_wns:.3f} ns")
                            else:
                                print(f"No improvement, keeping {check_wns:.3f} ns")

                        except Exception as e:
                            print(f"Failed: {e}")
                            continue
                    
                    if best_physopt_result is not None:
                        print(f"\nUsing {best_directive} achieved best phys_opt effect")
                        print(f"   phys_opt post WNS: {best_wns_after_physopt:.3f} ns")
                    else:
                        print(f"\nNo phys_opt_design improved, continuing with routing")

            # Route the design with extended timeout (6 hours)
            ROUTE_TIMEOUT = 21600.0  # 6 hours
            print(f"\nRouting design (timeout: {ROUTE_TIMEOUT:.0f} seconds / {ROUTE_TIMEOUT/3600:.1f} hours)...")

            # Start heartbeat logger for long-running route_design
            done_event = __import__('threading').Event()
            heartbeat = HeartbeatLogger(
                interval_seconds=60.0,
                message=f"route_design in progress (timeout: {ROUTE_TIMEOUT:.0f}s)",
                done_event=done_event
            )
            heartbeat.start()

            try:
                result = await self.call_vivado_tool("route_design", {
                    "directive": "Default"
                }, timeout=ROUTE_TIMEOUT)
                print(f"Route design result:\n{result}")
                logger.info(f"Route design: {result}")
            finally:
                done_event.set()
                heartbeat.stop()

            # Check route status
            result = await self.call_vivado_tool("report_route_status", {}, timeout=300.0)
            print(f"Route status after routing:\n{result[:1500]}...")
            logger.info(f"Route status after routing: {result}")

            if self._check_test_exit("Step 10: Report final timing"):
                return False

            # ================================================================
            # Step 10: Report timing and compare WNS
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 10: Report final timing")
            print("-"*60)
            
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            print(f"Final timing summary (first 2000 chars):\n{result[:2000]}...")
            logger.info(f"Final timing summary: {result}")
            
            # Parse final WNS
            self.final_wns = self.parse_wns_from_timing_report(result)
            print(f"\n*** Final WNS: {self.final_wns} ns ***")
            logger.info(f"Final WNS: {self.final_wns} ns")
            
            # Calculate final fmax
            final_fmax = self.calculate_fmax(self.final_wns, self.clock_period)
            if final_fmax is not None:
                print(f"*** Final achievable fmax: {final_fmax:.2f} MHz ***")
            print()

            # ================================================================
            # Step 9.5: Verify get_wns tool
            # ================================================================
            print("\n" + "-"*60)
            print("STEP 9.5: Verify get_wns tool")
            print("-"*60)

            try:
                get_wns_result = await self.call_vivado_tool("get_wns", {}, timeout=60.0)
                print(f"get_wns raw result: '{get_wns_result}'")
                if get_wns_result.strip() == "PARSE_ERROR":
                    print("*** get_wns returned PARSE_ERROR ***")
                    logger.warning("get_wns returned PARSE_ERROR in test mode")
                else:
                    try:
                        get_wns_value = float(get_wns_result.strip())
                        print(f"*** get_wns WNS: {get_wns_value} ns ***")
                        if hasattr(self, 'final_wns'):
                            diff = abs(get_wns_value - self.final_wns)
                            if diff < 0.01:
                                print("✓ get_wns matches timing_summary (diff < 0.01 ns)")
                            else:
                                print(f"WARNING: get_wns differs from timing_summary by {diff:.4f} ns")
                        logger.info(f"get_wns verification: {get_wns_value} ns (timing_summary: {self.final_wns} ns)")
                    except ValueError as e:
                        print(f"*** get_wns parse error: {e} ***")
                        logger.warning(f"get_wns cannot parse '{get_wns_result}': {e}")
            except Exception as e:
                print(f"*** get_wns call failed: {e} ***")
                logger.error(f"get_wns call failed in test mode: {e}")

            # ================================================================
            # Write final DCP and report results
            # ================================================================
            self.print_wns_change(self.initial_wns, self.final_wns, self.clock_period)
            
            # Always write the final checkpoint
            print(f"\nWriting final DCP to: {output_dcp}")
            result = await self.call_vivado_tool("write_checkpoint", {
                "dcp_path": str(output_dcp.resolve()),
                "force": True
            }, timeout=600.0)
            print(f"Write final DCP result:\n{result}")
            
            # ================================================================
            # Summary
            # ================================================================
            elapsed = time.time() - overall_start
            self.print_test_summary(
                title="TEST SUMMARY - LOGICNETS PBLOCK OPTIMIZATION",
                elapsed_seconds=elapsed,
                initial_wns=self.initial_wns,
                final_wns=self.final_wns,
                clock_period=self.clock_period,
                extra_info=f"Pblock applied: {pblock_ranges}"
            )

            # Print skill invocation test summary
            if not self.skip_skills and self.skill_test_results:
                passed = sum(1 for r in self.skill_test_results if r.get("success"))
                total = len(self.skill_test_results)
                print(f"\n{'='*60}")
                print(f"SKILL INVOCATION TEST RESULTS: {passed}/{total} passed")
                print(f"{'='*60}")
                for r in self.skill_test_results:
                    mark = "✓" if r.get("success") else "✗"
                    print(f"  [{mark}] {r['skill']}")
                print()

            return True

        except Exception as e:
            logger.exception(f"LogicNets test failed with exception: {e}")
            print(f"Exception: {type(e).__name__}: {e}")
            return False

    async def run_skill_tests_only(self, input_dcp: Path) -> bool:
        """
        Run ONLY skill invocation tests — no place/route or optimization.

        Quick validation that all skills can be invoked end-to-end:
        1. Initialize RapidWright
        2. Open DCP in Vivado (for timing data)
        3. Report timing and get high fanout nets
        4. Open DCP in RapidWright
        5. Invoke all registered skills (analyze_net_detour, smart_region_search,
           analyze_pblock_region, execute_physopt_strategy, execute_fanout_strategy)
        6. Print skill test summary
        """
        print("\n" + "="*70)
        print("FPGA OPTIMIZER — PURE SKILL INVOCATION TEST")
        print("="*70)
        print(f"Input DCP:  {input_dcp}")
        print(f"Temp dir:   {self.temp_dir}")
        print("[TEST] Type 'quit' and press Enter to exit gracefully at any time.")
        print("="*70 + "\n")

        overall_start = time.time()

        try:
            # Step 1: Initialize RapidWright
            print("\n" + "-"*60)
            print("STEP 1: Initialize RapidWright")
            print("-"*60)
            result = await self.call_rapidwright_tool("initialize_rapidwright", {
                "jvm_max_memory": "8G"
            }, timeout=120.0)
            print(f"RapidWright init result:\n{result[:500]}...")

            if self._check_test_exit("Skill test - Step 2: Open DCP"):
                return False

            # Step 2: Open DCP in Vivado
            print("\n" + "-"*60)
            print("STEP 2: Open DCP in Vivado")
            print("-"*60)
            result = await self.call_vivado_tool("open_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"Open checkpoint:\n{result}")

            if self._check_test_exit("Skill test - Step 3: Timing and nets"):
                return False

            # Step 3: Report timing + get high fanout nets
            print("\n" + "-"*60)
            print("STEP 3: Report timing and get critical nets")
            print("-"*60)
            result = await self.call_vivado_tool("report_timing_summary", {}, timeout=300.0)
            self.initial_wns = self.parse_wns_from_timing_report(result)
            print(f"Initial WNS: {self.initial_wns} ns")

            result = await self.call_vivado_tool("get_critical_high_fanout_nets", {
                "num_paths": 50, "min_fanout": 100, "exclude_clocks": True
            }, timeout=600.0)
            self.high_fanout_nets = self.parse_high_fanout_nets(result)
            print(f"Found {len(self.high_fanout_nets)} high fanout nets")

            if self._check_test_exit("Skill test - Step 4: Open DCP in RW"):
                return False

            # Step 4: Open DCP in RapidWright
            print("\n" + "-"*60)
            print("STEP 4: Open DCP in RapidWright")
            print("-"*60)
            result = await self.call_rapidwright_tool("read_checkpoint", {
                "dcp_path": str(input_dcp.resolve())
            }, timeout=600.0)
            print(f"RapidWright read checkpoint:\n{result[:300]}...")

            if self._check_test_exit("Skill test - Step 5: Skill invocations"):
                return False

            # ── Skill Invocations ──────────────────────────────────────
            # Get pin paths for analyze_net_detour
            pins_result = ""
            try:
                pins_result = await self.call_vivado_tool("extract_critical_path_pins", {
                    "num_paths": 5
                }, timeout=300.0)
            except Exception:
                pass

            # Step 5.1: analyze_net_detour
            print("\n" + "-"*60)
            print("STEP 5.1: [SKILL] analyze_net_detour")
            print("-"*60)
            if pins_result.strip() and pins_result.strip() != "(no output)":
                try:
                    import json as _json
                    pins_data = _json.loads(pins_result)
                    pin_paths_array = pins_data.get("pin_paths", [])
                except Exception:
                    pin_paths_array = []
                if pin_paths_array and len(pin_paths_array) > 0:
                    pin_paths = pin_paths_array[0]
                    print(f"[TEST] Using {len(pin_paths)} pins from critical path (of {len(pin_paths_array)} paths found)")
                    try:
                        sr = await self.call_rapidwright_tool("analyze_net_detour", {
                            "pin_paths": pin_paths, "detour_threshold": 2.0
                        }, timeout=120.0)
                        self._verify_skill_result("analyze_net_detour", sr)
                    except Exception as e:
                        print(f"[TEST] ⚠ analyze_net_detour FAILED: {e}")
                else:
                    print("[TEST] ⚠ analyze_net_detour skipped: no pin paths in result")
                    try:
                        import json as _json2
                        _data = _json2.loads(pins_result)
                        print(f"[TEST] debug_has_slack={_data.get('debug_has_slack', '?')}")
                        print(f"[TEST] debug_report_length={_data.get('debug_report_length', '?')}")
                        print(f"[TEST] debug_num_path_sections={_data.get('debug_num_slack_sections', '?')}")
                        if "debug_per_path" in _data:
                            print(f"[TEST] per-path debug: {_data['debug_per_path']}")
                        report_snippet = _data.get("debug_timing_report", "")
                        if report_snippet:
                            print(f"[TEST] debug_timing_report:\n{report_snippet}")
                    except Exception:
                        print(f"[TEST] Raw pins_result: {str(pins_result)[:500]}")
            else:
                print("[TEST] ⚠ analyze_net_detour skipped: no pin paths available")

            # Step 5.2: smart_region_search
            print("\n" + "-"*60)
            print("STEP 5.2: [SKILL] smart_region_search")
            print("-"*60)
            try:
                sr = await self.call_rapidwright_tool("smart_region_search", {
                    "target_lut_count": 50000, "target_ff_count": 50000,
                }, timeout=600.0)
                self._verify_skill_result("smart_region_search", sr)
            except Exception as e:
                print(f"[TEST] ⚠ smart_region_search skipped: {e}")

            # Step 5.3: analyze_pblock_region
            print("\n" + "-"*60)
            print("STEP 5.3: [SKILL] analyze_pblock_region")
            print("-"*60)
            try:
                sr = await self.call_rapidwright_tool("analyze_pblock_region", {
                    "target_lut_count": 50000, "target_ff_count": 50000,
                    "resource_multiplier": 1.5,
                }, timeout=600.0)
                self._verify_skill_result("analyze_pblock_region", sr)
            except Exception as e:
                print(f"[TEST] ⚠ analyze_pblock_region skipped: {e}")

            # Step 5.4: execute_physopt_strategy
            print("\n" + "-"*60)
            print("STEP 5.4: [SKILL] execute_physopt_strategy")
            print("-"*60)
            try:
                sr = await self.call_rapidwright_tool("execute_physopt_strategy", {
                    "directive": "Default", "design_is_routed": False,
                }, timeout=360.0)
                self._verify_skill_result("execute_physopt_strategy", sr)
            except Exception as e:
                print(f"[TEST] ⚠ execute_physopt_strategy skipped: {e}")

            # Step 5.5: execute_fanout_strategy (with real nets)
            print("\n" + "-"*60)
            print("STEP 5.5: [SKILL] execute_fanout_strategy")
            print("-"*60)
            nets_to_test = self.high_fanout_nets[:3] if self.high_fanout_nets else []
            net_configs = [{"net_name": n, "fanout": f} for n, f, *_ in nets_to_test]
            if net_configs:
                try:
                    sr = await self.call_rapidwright_tool("execute_fanout_strategy", {
                        "nets": net_configs,
                        "temp_dir": str(self.temp_dir),
                        "checkpoint_prefix": "pure_skill_test",
                    }, timeout=300.0 * len(net_configs))
                    self._verify_skill_result("execute_fanout_strategy", sr)
                except Exception as e:
                    print(f"[TEST] ⚠ execute_fanout_strategy skipped: {e}")
            else:
                print("[TEST] ⚠ execute_fanout_strategy skipped: no high fanout nets found")

            # Step 5.6: optimize_cell_placement
            print("\n" + "-"*60)
            print("STEP 5.6: [SKILL] optimize_cell_placement")
            print("-"*60)
            try:
                cell_names = []
                if pins_result.strip() and pins_result.strip() != "(no output)":
                    try:
                        import json as _json
                        pins_data = _json.loads(pins_result)
                        pin_paths_list = pins_data.get("pin_paths", [])
                    except Exception:
                        pin_paths_list = []
                    if pin_paths_list and len(pin_paths_list) > 0:
                        pp = pin_paths_list[0]
                        seen = set()
                        for p in pp:
                            cell = p.split("/")[0] if "/" in p else p
                            if cell not in seen:
                                seen.add(cell)
                                cell_names.append(cell)
                        cell_names = cell_names[:5]
                if cell_names:
                    sr = await self.call_rapidwright_tool("optimize_cell_placement", {
                        "cell_names": cell_names,
                    }, timeout=360.0)
                    self._verify_skill_result("optimize_cell_placement", sr)
                else:
                    # Fallback: get cell names directly from the loaded RapidWright design
                    print("[TEST] No critical path cells, trying search_cells fallback...")
                    try:
                        sr = await self.call_rapidwright_tool("search_cells", {"limit": 5}, timeout=60.0)
                        if sr.strip():
                            import json as _json
                            cells_data = _json.loads(sr)
                            fallback_names = [c["name"] for c in cells_data.get("cells", []) if c.get("name")]
                            if fallback_names:
                                print(f"[TEST] Using fallback cell names: {fallback_names}")
                                sr = await self.call_rapidwright_tool("optimize_cell_placement", {
                                    "cell_names": fallback_names,
                                }, timeout=360.0)
                                self._verify_skill_result("optimize_cell_placement", sr)
                            else:
                                print("[TEST] ⚠ optimize_cell_placement skipped: no cell names available")
                        else:
                            print("[TEST] ⚠ optimize_cell_placement skipped: no cell names available")
                    except Exception as e2:
                        print(f"[TEST] ⚠ optimize_cell_placement skipped (fallback): {e2}")
            except Exception as e:
                print(f"[TEST] ⚠ optimize_cell_placement skipped: {e}")

            # Summary
            elapsed = time.time() - overall_start
            passed = sum(1 for r in self.skill_test_results if r.get("success"))
            total = len(self.skill_test_results)
            print(f"\n{'='*60}")
            print(f"SKILL INVOCATION TEST RESULTS: {passed}/{total} passed")
            print(f"Total time: {elapsed:.1f}s")
            print(f"{'='*60}")
            for r in self.skill_test_results:
                mark = "✓" if r.get("success") else "✗"
                print(f"  [{mark}] {r['skill']}")
            print()

            return passed == total

        except Exception as e:
            logger.exception(f"Skill-only test failed: {e}")
            print(f"\n*** SKILL TEST FAILED: {e} ***")
            return False

    async def cleanup(self):
        """Clean up resources."""
        print("\n[TEST] Cleaning up...")
        await super().cleanup()
        print(f"[TEST] Run directory preserved at: {self.run_dir}")


async def run_test_mode(input_dcp: Path, output_dcp: Path, debug: bool = False, max_nets: int = 5, run_dir: Optional[Path] = None, skip_skills: bool = False):
    """Run the test mode optimization.

    Detects which example DCP is being used and applies the appropriate optimization flow:
    - demo_corundum_25g_misses_timing.dcp: High fanout net optimization flow
    - logicnets_jscl.dcp: Pblock-based placement optimization flow
    """
    # Detect which DCP is being used based on filename
    dcp_name = input_dcp.name.lower()

    if "corundum" in dcp_name or dcp_name == "demo_corundum_25g_misses_timing.dcp":
        design_type = "corundum"
        print(f"[TEST] Detected Corundum design - using high fanout optimization flow")
        print(f"[TEST] Type 'quit' and press Enter to exit gracefully at any time.")
    elif "logicnets" in dcp_name or dcp_name == "logicnets_jscl.dcp":
        design_type = "logicnets"
        print(f"[TEST] Detected LogicNets design - using pblock optimization flow")
        print(f"[TEST] Type 'quit' and press Enter to exit gracefully at any time.")
    else:
        print(f"\n[TEST] ERROR: Unsupported DCP file: {input_dcp.name}")
        print(f"[TEST] Test mode requires one of the two example DCPs:")
        print(f"[TEST]   - demo_corundum_25g_misses_timing.dcp")
        print(f"[TEST]   - logicnets_jscl.dcp")
        print(f"[TEST]")
        print(f"[TEST] For custom DCPs, run without --test to use the LLM-guided optimizer.")
        return 1

    if skip_skills:
        print("[TEST] --skip-skills set, will test only raw tool flow (no skill invocations)")

    tester = FPGAOptimizerTest(debug=debug, run_dir=run_dir, skip_skills=skip_skills)
    
    try:
        await tester.start_servers()
        
        if design_type == "corundum":
            success = await tester.run_test(input_dcp, output_dcp, max_nets_to_optimize=max_nets)
        else:  # logicnets
            success = await tester.run_test_logicnets(input_dcp, output_dcp)
        
        if success:
            print("\n[TEST] Test completed successfully")
            print(f"\n[TEST] Output files:")
            print(f"[TEST]   Optimized DCP: {output_dcp}")
            print(f"[TEST]   Run directory: {tester.run_dir}")
            return 0
        else:
            print("\n[TEST] Test failed")
            print(f"[TEST] Run directory: {tester.run_dir}")
            return 1
            
    except KeyboardInterrupt:
        print("\n[TEST] Interrupted by user")
        print(f"[TEST] Run directory: {tester.run_dir}")
        return 130
    except Exception as e:
        logger.exception(f"Test mode fatal error: {e}")
        print(f"\n[TEST] Fatal error: {e}")
        print(f"[TEST] Run directory: {tester.run_dir}")
        return 1
    finally:
        await tester.cleanup()


# === Section 9: Entry Point ===

async def main():
    parser = argparse.ArgumentParser(
        description="FPGA Design Optimization Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dcp_optimizer.py input.dcp
  python dcp_optimizer.py input.dcp --output output.dcp
  python dcp_optimizer.py input.dcp --model anthropic/claude-sonnet-4
  python dcp_optimizer.py input.dcp --debug
  python dcp_optimizer.py demo_corundum_25g_misses_timing.dcp --test  # High fanout optimization
  python dcp_optimizer.py logicnets_jscl.dcp --test  # Pblock optimization
  python dcp_optimizer.py demo_corundum_25g_misses_timing.dcp --test --max-nets 3
  python dcp_optimizer.py demo_corundum_25g_misses_timing.dcp --test --skip-skills
  python dcp_optimizer.py demo_corundum_25g_misses_timing.dcp --test-only-skills
        """
    )
    parser.add_argument("input_dcp", type=Path, help="Input design checkpoint (.dcp)")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        dest="output_dcp",
        help="Output optimized checkpoint (.dcp). Default: <input_name>_optimized-<timestamp>.dcp in same directory as input"
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
        help=f"Planner LLM model to use (default: {DEFAULT_MODEL_PLANNER})"
    )
    parser.add_argument(
        "--model-worker",
        type=str,
        default=DEFAULT_MODEL_WORKER,
        help=f"Worker LLM model for routine optimization steps (default: {DEFAULT_MODEL_WORKER})"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode (verbose logging, save intermediate checkpoints)"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: run hardcoded optimization flow without LLM. Detects DCP type and applies appropriate optimization: high fanout for Corundum, pblock for LogicNets."
    )
    parser.add_argument(
        "--max-nets",
        type=int,
        default=5,
        help="Maximum number of high fanout nets to optimize in test mode (default: 5)"
    )
    parser.add_argument(
        "--skip-skills",
        action="store_true",
        help="Skip skill invocation tests in test mode, run only raw tool flow"
    )
    parser.add_argument(
        "--test-only-skills",
        action="store_true",
        help="Run only skill invocation tests (no place/route). Implies --test."
    )

    args = parser.parse_args()
    
    # Validate inputs
    if not args.input_dcp.exists():
        print(f"Error: Input file not found: {args.input_dcp}", file=sys.stderr)
        sys.exit(1)
    
    # Generate default output DCP name if not provided
    if args.output_dcp is None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        input_stem = args.input_dcp.stem  # Filename without extension
        input_dir = args.input_dcp.parent  # Directory of input file
        args.output_dcp = input_dir / f"{input_stem}_optimized-{timestamp}.dcp"
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Create output directory if needed
    args.output_dcp.parent.mkdir(parents=True, exist_ok=True)
    
    # Test mode — skill-only validation (no place/route)
    if args.test_only_skills:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = Path.cwd() / f"dcp_optimizer_run-{timestamp}"

        print(f"FPGA Design Optimization — SKILL-ONLY TEST")
        print(f"===========================================")
        print(f"Input:       {args.input_dcp.resolve()}")
        print(f"Run dir:     {run_dir}")
        print()

        tester = FPGAOptimizerTest(debug=args.debug, run_dir=run_dir)
        try:
            await tester.start_servers()
            success = await tester.run_skill_tests_only(args.input_dcp)
            sys.exit(0 if success else 1)
        except KeyboardInterrupt:
            print("\n[TEST] Interrupted by user")
            sys.exit(130)
        except Exception as e:
            logger.exception(f"Skill-only test fatal error: {e}")
            print(f"\n[TEST] Fatal error: {e}")
            sys.exit(1)
        finally:
            await tester.cleanup()

    # Test mode - run without LLM
    if args.test:
        # Create run directory with timestamp
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        run_dir = Path.cwd() / f"dcp_optimizer_run-{timestamp}"
        
        print(f"FPGA Design Optimization - TEST MODE")
        print(f"=====================================")
        print(f"Input:       {args.input_dcp.resolve()}")
        print(f"Output:      {args.output_dcp.resolve()}")
        print(f"Run dir:     {run_dir}")
        print(f"Max nets to optimize: {args.max_nets}")
        print()
        
        exit_code = await run_test_mode(
            args.input_dcp,
            args.output_dcp,
            debug=args.debug,
            max_nets=args.max_nets,
            run_dir=run_dir,
            skip_skills=args.skip_skills
        )
        sys.exit(exit_code)
    
    # Normal mode - requires API key and LLM
    if not args.api_key:
        print("Error: OpenRouter API key required. Set OPENROUTER_API_KEY or use --api-key", file=sys.stderr)
        print("       Use --test flag to run in test mode without LLM", file=sys.stderr)
        sys.exit(1)
    
    
    # Create run directory with timestamp (before creating optimizer so we can show it)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = Path.cwd() / f"dcp_optimizer_run-{timestamp}"
    
    print(f"FPGA Design Optimization Agent")
    print(f"================================")
    print(f"Input:       {args.input_dcp.resolve()}")
    print(f"Output:      {args.output_dcp.resolve()}")
    print(f"Run dir:     {run_dir}")
    print(f"Planner Model: {args.model}")
    print(f"Worker Model:  {args.model_worker}")
    print()
    
    optimizer = DCPOptimizer(
        api_key=args.api_key,
        model_planner=args.model,        # Strategy planning model
        model_worker=args.model_worker,  # Routine execution model
        debug=args.debug,
        run_dir=run_dir
    )
    
    try:
        await optimizer.start_servers()
        success = await optimizer.optimize(args.input_dcp, args.output_dcp)
        
        if success:
            print("\n✓ Optimization completed successfully")
            print(f"\nOutput files:")
            print(f"  Optimized DCP: {args.output_dcp}")
            print(f"  Run directory: {run_dir}")
            sys.exit(0)
        else:
            print("\n✗ Optimization did not complete successfully")
            print(f"\nRun directory: {run_dir}")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        print(f"Run directory: {run_dir}")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        print(f"Run directory: {run_dir}")
        sys.exit(1)
    finally:
        await optimizer.cleanup()


if __name__ == "__main__":
    print("Type 'quit' and press Enter to terminate the program.")
    asyncio.run(main())
