"""Tool routing pure functions.

Extracted from dcp_optimizer.py: call_tool (L3550-3700).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from context_manager.logging_config import sanitize_payload
from .constants import ROUTING_FAILURE_PHRASES

logger = logging.getLogger(__name__)


async def call_tool(
    tool_name: str,
    arguments: dict,
    rapidwright_session: Any,
    vivado_session: Any,
    raw_tool_outputs: dict | None = None,
    iteration: int = 0,
    tool_round: int = 0,
    high_fanout_nets: list | None = None,
) -> str:
    """Execute a tool call on the appropriate MCP server.

    Routes to rapidwright_ or vivado_ session based on tool name prefix.
    Handles internal tools (get_raw_tool_output, get_cached_high_fanout_nets,
    report_step_state).

    Args:
        tool_name: Full tool name with prefix (e.g., "vivado_place_design").
        arguments: Tool arguments dict.
        rapidwright_session: MCP ClientSession for RapidWright.
        vivado_session: MCP ClientSession for Vivado.
        raw_tool_outputs: Side buffer of raw tool outputs {(iter, round): (name, text)}.
        iteration: Current iteration number.
        tool_round: Current tool round number.
        high_fanout_nets: Cached high-fanout nets from initial analysis.

    Returns:
        Tool result as string.
    """
    # Internal tool: retrieve raw tool output from side buffer
    if tool_name == "vivado_get_raw_tool_output":
        if raw_tool_outputs is None:
            return json.dumps({"error": "No raw tool outputs available"})

        iter_arg = arguments.get("iteration", iteration)
        round_idx = arguments.get("round_index")
        target_tool = arguments.get("tool_name", "")

        candidates = []
        for (it, rd), (tname, txt) in raw_tool_outputs.items():
            if it == iter_arg and (round_idx is None or rd == round_idx):
                candidates.append(((it, rd), tname, txt))

        if target_tool and candidates:
            filtered = [c for c in candidates if c[1] == target_tool]
            if not filtered:
                search_key = target_tool.replace("vivado_", "").replace("rapidwright_", "")
                filtered = [c for c in candidates if search_key in c[2][:2000]]
            candidates = filtered if filtered else []

        if not candidates:
            return json.dumps({"error": f"No raw output found for iteration={iter_arg}, round={round_idx}, tool={target_tool}"})

        candidates.sort(key=lambda x: (x[0][0], x[0][1]), reverse=True)
        (it, rd), tname, txt = candidates[0]
        return f"[Raw tool output from iteration {it}, round {rd} ({len(txt)} chars, tool: {tname})]\n\n{txt}"

    # Internal tool: retrieve cached high-fanout nets
    if tool_name == "vivado_get_cached_high_fanout_nets":
        if not high_fanout_nets:
            return json.dumps({"error": "No cached high-fanout nets available."})

        max_nets = arguments.get("max_nets", 0)
        min_fanout = arguments.get("min_fanout", 0)

        filtered = high_fanout_nets
        if min_fanout > 0:
            filtered = [(n, f, p) for n, f, p in filtered if f >= min_fanout]
        if max_nets > 0:
            filtered = filtered[:max_nets]

        output_lines = [
            "=== Cached High-Fanout Nets (from initial analysis) ===",
            f"Total cached: {len(high_fanout_nets)}, showing: {len(filtered)}",
            "",
            "Rank  Paths  Fanout  Net Name",
            "----  -----  ------  --------------------",
        ]
        for i, (net_name, fanout, path_count) in enumerate(filtered, 1):
            output_lines.append(f"{i:<4}  {path_count:<5}  {fanout:<6}  {net_name}")

        return "\n".join(output_lines)

    # Internal tool: report_step_state (safety net)
    if tool_name == "report_step_state":
        return json.dumps({"status": "acknowledged"})

    # Parse server prefix
    if tool_name.startswith("rapidwright_"):
        session = rapidwright_session
        actual_name = tool_name[len("rapidwright_"):]
    elif tool_name.startswith("vivado_"):
        session = vivado_session
        actual_name = tool_name[len("vivado_"):]
    else:
        return json.dumps({"error": f"Unknown tool prefix in: {tool_name}"})

    if session is None:
        return json.dumps({"error": f"No MCP session available for {tool_name}"})

    # Execute via MCP
    logger.info(f"[MCP_REQUEST] tool={tool_name}, args={sanitize_payload(arguments)}")
    start_time = time.time()

    # Heartbeat for long-running MCP calls
    heartbeat_count = 0
    heartbeat_done = asyncio.Event()

    async def _heartbeat():
        nonlocal heartbeat_count
        while not heartbeat_done.is_set():
            await asyncio.sleep(60.0)
            if heartbeat_done.is_set():
                break
            heartbeat_count += 1
            hb_elapsed = time.time() - start_time
            logger.info(
                f"[HEARTBEAT #{heartbeat_count}] Tool '{tool_name}' still running after {hb_elapsed:.1f}s"
            )

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        result = await session.call_tool(actual_name, arguments)
        heartbeat_done.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        elapsed = time.time() - start_time

        if result and hasattr(result, 'content') and result.content:
            text_parts = []
            for block in result.content:
                if hasattr(block, 'text'):
                    text_parts.append(block.text)
            result_text = "\n".join(text_parts) if text_parts else "(no output)"
            result_size = sum(len(p) for p in text_parts) if text_parts else 0
            logger.info(
                f"[MCP_RESPONSE] tool={tool_name}, elapsed={elapsed:.1f}s, "
                f"result_size={result_size} chars, heartbeats={heartbeat_count}"
            )
            return result_text
        logger.info(
            f"[MCP_RESPONSE] tool={tool_name}, elapsed={elapsed:.1f}s, "
            f"result_size=0 chars, heartbeats={heartbeat_count}"
        )
        return "(no output)"
    except Exception as e:
        heartbeat_done.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        elapsed = time.time() - start_time
        logger.error(
            f"[MCP_RESPONSE] tool={tool_name}, elapsed={elapsed:.1f}s, "
            f"FAILED: {e}, heartbeats={heartbeat_count}"
        )
        return json.dumps({"error": str(e), "tool": tool_name})


def is_routing_failure(error_msg: str) -> bool:
    """Check if error message indicates a routing failure (non-timeout)."""
    error_lower = error_msg.lower()
    return any(phrase in error_lower for phrase in ROUTING_FAILURE_PHRASES)
