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
from .entities import (
    EntityRegistry,
    validate_and_sanitize_cell_args,
)
from .tool_contracts import ToolCallResult, build_tool_call_result, coerce_payload_dict, is_mcp_error_response
from .tool_catalog import DESIGN_MODIFICATION_TOOLS
from .tool_runtime_policy import (
    _TOOL_TIMEOUT_DEFAULTS,
    _DEFAULT_TOOL_TIMEOUT,
    _TOOL_TIMEOUT_MAX,
)

logger = logging.getLogger(__name__)

# Tools that should NOT be cached (side-effect tools or execution tools)
_NO_CACHE_TOOLS: frozenset[str] = frozenset({
    "vivado_place_design",
    "vivado_route_design",
    "vivado_phys_opt_design",
    "vivado_physopt_and_route",
    "vivado_open_checkpoint",
    "vivado_write_checkpoint",
    "vivado_create_and_apply_pblock",
    "rapidwright_execute_pblock_strategy",
    "rapidwright_execute_fanout_strategy",
    "rapidwright_execute_congestion_spreading",
    "rapidwright_optimize_pin_swapping",
    "rapidwright_flatten_lut_cascade",
    "rapidwright_replicate_critical_cells",
    "rapidwright_execute_register_retiming",
    "rapidwright_smart_retiming",
    "vivado_opt_design",
    "rapidwright_execute_net_swapping",
    "rapidwright_optimize_cell_placement",
    "rapidwright_smart_region_search",
    "rapidwright_optimize_lut_input_cone",
    "rapidwright_execute_opt_design_strategy",
    "rapidwright_execute_combinational_rebalancing_strategy",
    "rapidwright_execute_lut_muxf_repack_strategy",
    "rapidwright_execute_muxf_tree_reorder_strategy",
    "design_data_read",
    "design_data_list_snapshots",
})

# LLMs occasionally serialize array-typed arguments as a comma-separated
# string (observed: rapidwright_analyze_net_detour pin_paths), which the MCP
# schema rejects with "is not of type 'array'". Coerce these known array
# params from string->list so the call succeeds. Framework-injected values
# are already lists and are left untouched.
_ARRAY_STRING_PARAMS: frozenset[str] = frozenset({
    "pin_paths",
    "critical_paths",
    "critical_path_cells",
    "cell_names",
})


def _coerce_array_arguments(arguments: dict) -> None:
    """In-place coerce known array params from comma-string to list."""
    for key in _ARRAY_STRING_PARAMS:
        val = arguments.get(key)
        if isinstance(val, str):
            arguments[key] = [s.strip() for s in val.split(",") if s.strip()]


async def call_tool(
    tool_name: str,
    arguments: dict,
    rapidwright_session: Any,
    vivado_session: Any,
    raw_tool_outputs: dict | None = None,
    iteration: int = 0,
    tool_round: int = 0,
    high_fanout_nets: list | None = None,
    tool_cache: dict | None = None,
    design_size_factor: float = 1.0,
    entity_registry: "EntityRegistry | None" = None,
    run_dir: "Path | None" = None,
) -> str:
    """Execute a tool call on the appropriate MCP server.

    Routes to rapidwright_ or vivado_ session based on tool name prefix.
    Handles internal tools (get_raw_tool_output, get_cached_high_fanout_nets,
    report_step_state, design_data_read, design_data_list_snapshots).

    LLM->tool boundary validation: when entity_registry is provided, cell-name
    arguments are validated & sanitized via validate_and_sanitize_cell_args
    (partial-pass+warn policy). All-invalid names return a structured rejection
    to the LLM without invoking the MCP tool.

    Args:
        tool_name: Full tool name with prefix (e.g., "vivado_place_design").
        arguments: Tool arguments dict.
        rapidwright_session: MCP ClientSession for RapidWright.
        vivado_session: MCP ClientSession for Vivado.
        raw_tool_outputs: Side buffer of raw tool outputs {(iter, round): (name, text)}.
        iteration: Current iteration number.
        tool_round: Current tool round number.
        high_fanout_nets: Cached high-fanout nets from initial analysis.
        entity_registry: Canonical cell-name registry for boundary validation.

    Returns:
        Tool result as string.
    """
    # TCL command interception: detect LLM trying to manually extract
    # critical path data via raw TCL commands. These are fragile (TCL syntax
    # errors, fanout-contaminated cells) and wasteful — state already has
    # verified data that strategy tools auto-inject.
    if tool_name == "vivado_run_tcl":
        command = (arguments.get("command") or arguments.get("cmd") or "")
        # Patterns suggesting manual critical path cell extraction
        _crit_path_patterns = [
            "get_timing_paths" in command and "get_cells" in command,
            "get_timing_paths" in command and "get_nets -of" in command,
            "report_timing" in command and "get_cells" in command,
        ]
        if any(_crit_path_patterns):
            return (
                "[AUTO-GUIDANCE] Detected TCL command trying to extract critical path cells. "
                "Critical path data is already available in Dashboard Module 1 "
                "(critical_paths_available_in_state) and will be automatically injected "
                "into strategy tools (rapidwright_execute_pblock_strategy, etc.). "
                "Use vivado_extract_critical_path_cells (the dedicated MCP tool) instead "
                "of raw TCL for reliable, validated extraction."
            )

    # ``timeout`` is a router-level control, not an MCP tool argument. Keep it
    # out of schema-validated MCP calls while preserving all real tool args.
    arguments = dict(arguments or {})
    user_timeout = arguments.pop("timeout", None)
    _coerce_array_arguments(arguments)

    # Internal tool: retrieve raw tool output from side buffer
    if tool_name == "vivado_get_raw_tool_output":
        if raw_tool_outputs is None:
            return json.dumps({"error": "No raw tool outputs available"})

        iter_arg = arguments.get("iteration", iteration)
        round_idx = arguments.get("round_index")
        target_tool = arguments.get("tool_name", "")

        candidates = []
        for (it, _phase, rd, tname), txt in raw_tool_outputs.items():
            if it == iter_arg and (round_idx is None or rd == round_idx):
                candidates.append(((it, _phase, rd, tname), tname, txt))

        if target_tool and candidates:
            filtered = [c for c in candidates if c[1] == target_tool]
            if not filtered:
                search_key = target_tool.replace("vivado_", "").replace("rapidwright_", "")
                filtered = [c for c in candidates if search_key in c[2][:2000]]
            candidates = filtered if filtered else []

        if not candidates:
            return json.dumps({"error": f"No raw output found for iteration={iter_arg}, round={round_idx}, tool={target_tool}"})

        candidates.sort(key=lambda x: (x[0][0], x[0][2]), reverse=True)
        (it, _phase, rd, tname), tname, txt = candidates[0]
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

    # Internal tool: design_data_read — retrieve persisted design data
    if tool_name == "design_data_read":
        if run_dir is None:
            return json.dumps({"error": "No run_dir configured, design data not available"})
        from .design_data import DesignDataManager
        ddm = DesignDataManager(run_dir)
        result = ddm.read_design_data(
            iteration=arguments.get("iteration", iteration),
            data_type=arguments.get("data_type", ""),
        )
        return result

    # Internal tool: design_data_list_snapshots — list available iterations
    if tool_name == "design_data_list_snapshots":
        if run_dir is None:
            return json.dumps({"error": "No run_dir configured, design data not available"})
        from .design_data import DesignDataManager
        ddm = DesignDataManager(run_dir)
        return ddm.list_all_iterations()

    # ── LLM->tool boundary: validate & sanitize cell-name arguments ──
    # Partial-pass+warn policy (confirmed decision). Returns a structured
    # rejection when ALL provided cell names are invalid, without calling MCP.
    if entity_registry is not None:
        # Stricter validation for design-modification tools: reject
        # unverified cell names (not in registry) to prevent LLM
        # hallucinated names from reaching MCP servers.
        strict = tool_name in DESIGN_MODIFICATION_TOOLS
        sanitized_args, cell_error = validate_and_sanitize_cell_args(
            tool_name, arguments, entity_registry,
            allow_unverified=not strict,
        )
        if cell_error is not None:
            logger.warning(
                f"[ROUTER] Rejected cell-name args for {tool_name}: "
                f"{cell_error[:300]}"
            )
            return cell_error
        if sanitized_args is not arguments:
            # Arguments were modified (invalid names stripped); log and proceed
            dropped = []
            for k in ("cell_names", "critical_path_cells", "critical_paths", "hierarchical_input_pins"):
                if k in arguments and k in sanitized_args:
                    if len(arguments[k]) != len(sanitized_args[k]):
                        dropped.append(f"{k}:{len(arguments[k])}->{len(sanitized_args[k])}")
            if dropped:
                logger.info(
                    f"[ROUTER] Sanitized cell args for {tool_name}: "
                    f"{', '.join(dropped)}"
                )
            arguments = sanitized_args

    # Tool result cache: skip cache for internal/side-effect tools.
    # Cache is cleared at phase transitions (phase_handoff.py transition_phase),
    # so within a single phase, read-only tools with identical arguments always
    # return the cached result — no round window restriction needed.
    if tool_cache is not None and tool_name not in _NO_CACHE_TOOLS:
        cache_key = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
        if cache_key in tool_cache:
            cached_round, cached_result = tool_cache[cache_key]
            logger.info(f"[CACHE_HIT] {tool_name} (cached from round {cached_round})")
            return f"[CACHED from round {cached_round}]\n{cached_result}"

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
    start_time = time.monotonic()

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
            hb_elapsed = time.monotonic() - start_time
            logger.info(
                f"[HEARTBEAT #{heartbeat_count}] Tool '{tool_name}' still running after {hb_elapsed:.1f}s"
            )

    heartbeat_task = asyncio.create_task(_heartbeat())

    # Application-level timeout: use per-tool defaults scaled by design size,
    # with user-specified timeout taking priority.
    if user_timeout is not None:
        request_timeout = float(user_timeout)
    else:
        base_timeout = _TOOL_TIMEOUT_DEFAULTS.get(tool_name, _DEFAULT_TOOL_TIMEOUT)
        request_timeout = min(base_timeout * design_size_factor, _TOOL_TIMEOUT_MAX)
    app_timeout = max(request_timeout * 1.5, 120)  # Minimum 120s application timeout

    try:
        try:
            result = await asyncio.wait_for(
                session.call_tool(actual_name, arguments),
                timeout=app_timeout,
            )
            elapsed = time.monotonic() - start_time

            if result and hasattr(result, 'content') and result.content:
                text_parts = []
                for block in result.content:
                    if hasattr(block, 'text'):
                        text_parts.append(block.text)
                result_text = "\n".join(text_parts) if text_parts else "(no output)"
                result_size = sum(len(p) for p in text_parts) if text_parts else 0
                # MCP protocol-level failures (e.g. inputSchema validation
                # rejections) arrive as isError=True with plain text that does
                # not match _MCP_ERROR_PATTERNS. Wrap them in the standard
                # {"error": ...} envelope so downstream consumers
                # (ToolCallResult, auto-chain fast-fail, post-eval) see a
                # failure instead of silently continuing. A silently dropped
                # place_design unplace step is how run-20260708_012142 lost
                # its PBLOCK re-place trajectory.
                if getattr(result, "isError", False) and not is_mcp_error_response(result_text):
                    logger.error(
                        f"[MCP_PROTOCOL_ERROR] tool={tool_name}: {result_text[:300]}"
                    )
                    result_text = json.dumps({
                        "error": f"MCP tool error: {result_text[:1500]}",
                        "tool": tool_name,
                    })
                logger.info(
                    f"[MCP_RESPONSE] tool={tool_name}, elapsed={elapsed:.1f}s, "
                    f"result_size={result_size} chars, heartbeats={heartbeat_count}"
                )
                if tool_name == "vivado_open_checkpoint":
                    dcp_path = arguments.get("dcp_path", "?")
                    logger.warning(f"--- [DESIGN_LOAD] Vivado design switched to: {dcp_path} ---")
                # Detect MCP error responses — must not be cached and must invalidate cache
                is_error_response = is_mcp_error_response(result_text)
                # Log tool errors at ERROR level with the actual message so they
                # reach fpl26-error.log. Previously only a boolean error=True was
                # logged at INFO, leaving the error log empty and the failure
                # reason (e.g. "Directive '...' is not a recognized directive")
                # invisible to operators.
                if is_error_response:
                    logger.error(
                        f"[TOOL_ERROR] tool={tool_name} (round {tool_round}): "
                        f"{result_text[:500]}"
                    )
                # Cache logic: error responses and side-effect tools both invalidate cache
                if tool_cache is not None:
                    if is_error_response or tool_name in _NO_CACHE_TOOLS:
                        tool_cache.clear()
                        logger.info(f"[CACHE_INVALIDATED] by {tool_name} (round {tool_round}, error={is_error_response})")
                    else:
                        cache_key = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
                        tool_cache[cache_key] = (tool_round, result_text)
                return result_text
            logger.info(
                f"[MCP_RESPONSE] tool={tool_name}, elapsed={elapsed:.1f}s, "
                f"result_size=0 chars, heartbeats={heartbeat_count}"
            )
            # Cache empty result too, or invalidate cache after side-effect tool
            if tool_cache is not None:
                if tool_name in _NO_CACHE_TOOLS:
                    tool_cache.clear()
                    logger.info(f"[CACHE_INVALIDATED] by {tool_name} (round {tool_round})")
                else:
                    cache_key = f"{tool_name}:{json.dumps(arguments, sort_keys=True)}"
                    tool_cache[cache_key] = (tool_round, "(no output)")
            return "(no output)"
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start_time
            logger.error(
                f"[MCP_RESPONSE] tool={tool_name}, elapsed={elapsed:.1f}s, "
                f"FAILED: Application-level timeout ({app_timeout:.0f}s), "
                f"heartbeats={heartbeat_count}"
            )
            # Invalidate cache — MCP server may restart with fresh state after timeout
            if tool_cache is not None:
                tool_cache.clear()
                logger.info(f"[CACHE_INVALIDATED] by {tool_name} timeout (round {tool_round})")
            return json.dumps({
                "error": f"Application-level timeout after {app_timeout:.0f}s",
                "tool": tool_name,
                "suggest_recovery": "restart_vivado",
            })
        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.error(
                f"[MCP_RESPONSE] tool={tool_name}, elapsed={elapsed:.1f}s, "
                f"FAILED: {e}, heartbeats={heartbeat_count}"
            )
            # Invalidate cache — MCP server may restart with fresh state after failure
            if tool_cache is not None:
                tool_cache.clear()
                logger.info(f"[CACHE_INVALIDATED] by {tool_name} error (round {tool_round})")
            return json.dumps({"error": str(e), "tool": tool_name})
    finally:
        heartbeat_done.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass


def is_routing_failure(error_msg: str) -> bool:
    """Check if error message indicates a routing failure (non-timeout)."""
    error_lower = error_msg.lower()
    return any(phrase in error_lower for phrase in ROUTING_FAILURE_PHRASES)


async def call_tool_structured(
    tool_name: str,
    arguments: dict,
    rapidwright_session: Any,
    vivado_session: Any,
    raw_tool_outputs: dict | None = None,
    iteration: int = 0,
    tool_round: int = 0,
    high_fanout_nets: list | None = None,
    tool_cache: dict | None = None,
    design_size_factor: float = 1.0,
    entity_registry: "EntityRegistry | None" = None,
    run_dir: "Path | None" = None,
) -> ToolCallResult:
    """Execute a tool and return a structured result envelope."""
    raw_text = await call_tool(
        tool_name=tool_name,
        arguments=arguments,
        rapidwright_session=rapidwright_session,
        vivado_session=vivado_session,
        raw_tool_outputs=raw_tool_outputs,
        iteration=iteration,
        tool_round=tool_round,
        high_fanout_nets=high_fanout_nets,
        tool_cache=tool_cache,
        design_size_factor=design_size_factor,
        entity_registry=entity_registry,
        run_dir=run_dir,
    )
    return build_tool_call_result(tool_name, raw_text)


async def verify_design_routed(
    rapidwright_session: Any,
    vivado_session: Any,
    design_size_factor: float = 1.0,
) -> bool:
    """Return True iff the in-memory Vivado design is fully routed.

    Uses ``vivado_check_design_status``, whose ``is_routed`` falls back to
    parsing ``report_route_status`` (reliable) when the STATUS/IS_ROUTED
    properties return empty after ``open_checkpoint`` (architecture.md §15.1).
    This is the reliable counterpart to ``get_property STATUS``, whose sticky
    value can label a partially-placed DCP as "Routed".

    Conservative: any error or parse failure returns False so that
    best-checkpoint saves and output delivery are blocked rather than
    trusting an unverified state.
    """
    try:
        result = await call_tool(
            "vivado_check_design_status", {},
            rapidwright_session, vivado_session,
            design_size_factor=design_size_factor,
        )
        data = coerce_payload_dict(result)
        return bool(data and data.get("is_routed"))
    except Exception as e:
        logger.warning(f"[ROUTER] verify_design_routed check failed: {e}")
        return False
