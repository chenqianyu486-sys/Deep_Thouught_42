"""ANALYZE phase: multi-dimensional timing analysis.

Gathers timing, placement, congestion, and fanout data to identify
dominant obstacles. Only analysis tools are exposed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from optimizer.state import OptimizerState, LLMCallRecord, record_flow_signal
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase, PHASE_MAX_ROUNDS, filter_tools_for_phase
from optimizer.pure.tool_summary import summarize_tool_result, compact_tool_summary
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.model_select import classify_task
from optimizer.pure.json_repair import parse_tool_arguments
from optimizer.pure.phase_policy import PhaseExitContract, build_phase_exit_contract
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary, is_valid_wns, parse_high_fanout_nets
from optimizer.pure.critical_path import refresh_violation_summary
from optimizer.pure.freshness import run_phase_entry_refresh
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, build_llm_extra_body
from optimizer.pure.cost_tracking import track_llm_call_cost
from optimizer.pure.pblock_plan import extract_selected_plan_from_payload
from optimizer.pure.tool_runtime_policy import DASHBOARD_REFRESH_MAP, PHASE_TOOL_RATE_LIMITS
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard, inject_pinned_cell_registry, extract_system_message
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)


async def run_analyze_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the ANALYZE phase: gather multi-dimensional timing data.

    Returns:
        LoopPhase.SELECT_STRATEGY (always, even if analysis was incomplete).
    """
    is_first_iteration = (state.iteration.current == 1)
    is_post_rollback = state.control.post_rollback_analyze
    if is_first_iteration or is_post_rollback:
        max_rounds = 8
        reason = "dashboard pre-filled" if is_first_iteration else "post-rollback (dashboard refreshed)"
        logger.info(f"[ANALYZE] Reduced max rounds to {max_rounds} ({reason})")
    else:
        max_rounds = PHASE_MAX_ROUNDS.get(LoopPhase.ANALYZE, 12)
    state.control.post_rollback_analyze = False
    tool_round = 0
    tools_called: list[str] = []
    llm_summary = ""
    assistant_content = ""
    state.context.tool_phase_call_counts.clear()
    state.context.consecutive_empty_responses = 0
    # Persist the active phase so the LLM call-log header, dashboard, and
    # state_transitions reflect ANALYZE (previously current_phase stayed "" for
    # the whole analysis phase, leaving the log header's phase/strategy blank).
    state.strategy.current_phase = "ANALYZE"

    # ── Auto-refresh stale dashboard fields on ANALYZE entry (data-driven) ──
    # Replaces 4 hardcoded per-field blocks; see freshness.py RefreshSpec table.
    # Covers: timing_summary, design_info (post-rollback), resource_utilization,
    # high_fanout_nets, route_status, congestion_data. critical_path_cells is
    # deliberately excluded — re-extraction is expensive and targeting-dependent.
    await run_phase_entry_refresh(state, deps, LoopPhase.ANALYZE)

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round
        _pending_signal: str | None = None  # defer exit until pending tools execute

        # Check exit conditions
        if _check_phase_exit(state, tool_round, max_rounds).should_exit:
            break

        # Call LLM with phase-filtered tools
        phase_tools = filter_tools_for_phase(deps.tools, LoopPhase.ANALYZE)
        response = await _call_phase_llm(state, deps, phase_tools)
        if response is None:
            break

        message = response.choices[0].message
        assistant_content = message.content or ""
        state.context.latest_assistant_response = assistant_content[:2000]
        state.context.llm_call_history.append(LLMCallRecord(
            timestamp=time.time(),
            phase=state.strategy.current_phase,
            model=state.model.current_model,
            iteration=state.iteration.current,
            user_prompt=state.context.latest_user_prompt,
            assistant_response=assistant_content[:2000],
        ))
        if len(state.context.llm_call_history) > state.context.llm_call_history_max:
            state.context.llm_call_history = state.context.llm_call_history[-state.context.llm_call_history_max:]

        if deps.compat is not None:
            metadata = {"tool_calls": message.tool_calls} if message.tool_calls else None
            deps.compat.add_message("assistant", assistant_content, metadata)

        # Extract step state
        step_state = extract_step_state(message)
        state.control.step_state = step_state

        if step_state:
            state.context.step_state_misses = 0
            flow_signal = step_state.flow_control

            # ANALYZE_DONE -> exit phase
            if flow_signal == "ANALYZE_DONE":
                llm_summary = assistant_content
                logger.info(green(f"[ANALYZE] LLM signaled ANALYZE_DONE at round {tool_round}"))
                record_flow_signal(state, "ANALYZE_DONE", "analysis_complete",
                                   phase="ANALYZE", result_status=step_state.result_status or "")
                if message.tool_calls:
                    _pending_signal = "ANALYZE_DONE"
                else:
                    break

            # Terminal signals: forward to outer loop
            if flow_signal in ("DONE", "EXHAUSTED"):
                logger.info(f"[ANALYZE] Terminal signal {flow_signal}, exiting")
                record_flow_signal(state, flow_signal, "terminal_during_analysis",
                                   phase="ANALYZE", result_status=step_state.result_status or "")
                state.control.done_reason = flow_signal
                if message.tool_calls:
                    _pending_signal = flow_signal
                else:
                    return LoopPhase.EVALUATE  # Skip to evaluate for final decision

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None and state.context.step_state_misses % 3 == 1:
                deps.compat.add_message("user", "[NOTE] report_step_state missing. Include it next turn.")

        # Execute analysis tools
        if message.tool_calls:
            for tc in message.tool_calls:
                if not tc.function:
                    continue
                tool_name = tc.function.name
                state.iteration.tools_used.append(tool_name)
                tools_called.append(tool_name)

                tool_args = parse_tool_arguments(tc.function.arguments, tool_name)
                task_type = classify_task(tool_name, tool_args)
                if task_type == "optimization" or (
                    task_type != "unknown" and state.model.current_task_type != "optimization"
                ):
                    state.model.current_task_type = task_type

                # Rate limiting for read-only tools
                if tool_name in PHASE_TOOL_RATE_LIMITS:
                    call_count = state.context.tool_phase_call_counts.get(tool_name, 0) + 1
                    state.context.tool_phase_call_counts[tool_name] = call_count
                    if call_count > PHASE_TOOL_RATE_LIMITS[tool_name]:
                        result = (f"[RATE LIMITED] Tool '{tool_name}' called {call_count} times this phase "
                                  f"(limit: {PHASE_TOOL_RATE_LIMITS[tool_name]}). ")
                        if tool_name == "rapidwright_search_cells":
                            result += ("Use cell_types parameter to batch multiple types in one call, "
                                       "or get_design_info for type overview.")
                        elif tool_name == "vivado_run_tcl":
                            result += (
                                "Dashboard already contains fresh timing data from init_analysis. "
                                "Use vivado_report_timing_summary or Dashboard values directly "
                                "instead of raw Tcl. For execution commands (place_design, "
                                "route_design, phys_opt_design), use the dedicated MCP tools."
                            )
                        elif tool_name == "vivado_get_cached_high_fanout_nets":
                            result += (
                                "High-fanout net data is already in Dashboard Module 4 "
                                "(Netlist Quality). Do not re-fetch — use the Dashboard values."
                            )
                        elif tool_name == "vivado_check_design_status":
                            result += (
                                "Design placement/routing status is shown in Dashboard Module 1 "
                                "(Global State, current_stage field). Do not re-check."
                            )
                        if deps.compat is not None:
                            deps.compat.add_message("tool", result, {
                                "tool_call_id": tc.id, "name": tool_name,
                            })
                        continue

                tool_start = time.time()
                result = await call_tool_fn(
                    tool_name=tool_name, arguments=tool_args,
                    rapidwright_session=deps.rapidwright_session,
                    vivado_session=deps.vivado_session,
                    raw_tool_outputs=state.context.raw_tool_outputs,
                    iteration=state.iteration.current,
                    tool_round=tool_round,
                    high_fanout_nets=state.timing.high_fanout_nets,
                    tool_cache=state.context.tool_cache,
                    design_size_factor=state.timing.design_size_factor,
                    entity_registry=state.entity_registry,
                    run_dir=state.control.run_dir,
                )
                tool_elapsed = time.time() - tool_start
                logger.info(f"[ANALYZE] {tool_name} completed in {tool_elapsed:.1f}s")

                # Summarize and inject
                summary = summarize_tool_result(
                    tool_name, result,
                    latest_wns=state.timing.latest_wns,
                    latest_tns=state.timing.latest_tns,
                    latest_failing_endpoints=state.timing.latest_failing_endpoints,
                    prev_best_wns=state.timing.prev_best_wns,
                    prev_best_tns=state.timing.prev_best_tns,
                )

                # Store raw output
                state.context.raw_tool_outputs[(state.iteration.current, "ANALYZE", tool_round, tool_name)] = result
                # Persist raw output to disk for design_data_read access
                if state.control.run_dir is not None:
                    try:
                        from optimizer.pure.design_data import DesignDataManager
                        ddm = DesignDataManager(state.control.run_dir)
                        ddm.store_raw_output(
                            tool_name=tool_name,
                            iteration=state.iteration.current,
                            phase="ANALYZE",
                            round_index=tool_round,
                            raw_text=result,
                        )
                    except Exception:
                        pass
                if len(state.context.raw_tool_outputs) > state.context.raw_tool_output_max:
                    oldest_key = min(state.context.raw_tool_outputs.keys(), key=lambda k: (k[0], k[2]))
                    del state.context.raw_tool_outputs[oldest_key]

                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id, "name": tool_name,
                    })

                # Track WNS
                _track_wns_from_result(state, tool_name, result)
                if tool_name in ("rapidwright_analyze_pblock_region", "rapidwright_execute_pblock_strategy"):
                    _update_pending_pblock_state_from_result(state, result)

                # Persist live-fetched high-fanout nets into state so they
                # survive into the snapshot / cached tool / EXECUTE phase.
                # Without this the data the LLM fetched only lives in
                # conversation history and is lost on rollback
                # (run-20260712_013828: Fanout EXECUTE saw an empty cache and
                # hallucinated net names, causing a -1.220ns regression).
                if tool_name == "vivado_get_critical_high_fanout_nets":
                    try:
                        _nets = parse_high_fanout_nets(result)
                        if _nets:
                            state.timing.high_fanout_nets = _nets
                            logger.info(
                                f"[ANALYZE] Persisted {len(_nets)} high-fanout nets "
                                f"from live fetch into state"
                            )
                    except Exception as _e_hf:
                        logger.debug(f"[ANALYZE] parse_high_fanout_nets failed: {_e_hf}")

                # Sync canonical cell names into the entity registry from
                # search_cells results (compression-resistant SSOT).
                if tool_name == "rapidwright_search_cells":
                    try:
                        from optimizer.pure.entities import sync_search_cells_result
                        added = sync_search_cells_result(
                            state.entity_registry, result,
                            iteration=state.iteration.current,
                        )
                        if added:
                            logger.info(
                                f"[ANALYZE] Registered {added} new canonical cell(s) "
                                f"from search_cells (total={len(state.entity_registry.cells)})"
                            )
                    except Exception as e:
                        logger.debug(f"[ANALYZE] search_cells registry sync failed: {e}")

                # Track dashboard freshness
                refreshable = DASHBOARD_REFRESH_MAP.get(tool_name)
                if refreshable:
                    for field in refreshable:
                        state.timing.field_freshness[field] = "fresh"

            if _pending_signal == "ANALYZE_DONE":
                break
            if _pending_signal in ("DONE", "EXHAUSTED"):
                return LoopPhase.EVALUATE

            continue

        # No tool calls — check if we should continue
        if _check_wns_target_met(state):
            state.control.is_done = True
            state.control.done_reason = "wns_target_met"
            return LoopPhase.EVALUATE

        # Track consecutive empty responses (no content AND no tool calls)
        if not assistant_content.strip() and not message.tool_calls:
            state.context.consecutive_empty_responses += 1
            empty_exit = build_phase_exit_contract(
                consecutive_empty_responses=state.context.consecutive_empty_responses,
                empty_response_limit=2,
            )
            if empty_exit.should_exit:
                logger.warning(
                    f"[ANALYZE] {state.context.consecutive_empty_responses} consecutive "
                    f"empty responses, forcing ANALYZE_DONE"
                )
                record_flow_signal(state, "SYSTEM_EXIT", "empty_responses",
                                   phase="ANALYZE")
                break
        else:
            state.context.consecutive_empty_responses = 0

    # Phase exit: build handoff and transition
    llm_summary = llm_summary or assistant_content or "Analysis phase completed."
    recent_tool_results = _extract_recent_tool_results(state)
    handoff = build_phase_handoff(
        source_phase=LoopPhase.ANALYZE,
        llm_summary=llm_summary,
        wns=state.timing.latest_wns,
        tns=state.timing.latest_tns,
        failing_endpoints=state.timing.latest_failing_endpoints,
        tools_called=tools_called,
        key_findings=_extract_analyze_key_findings(state),
        message_count=tool_round,
        tool_results=recent_tool_results,
        design_stage=getattr(state.timing, 'current_stage', ''),
        critical_paths_count=len(state.timing.critical_paths),
        stalled_strategies=list(state.iteration.blocked_strategies),
    )
    state.strategy.analysis_summary = llm_summary
    state.strategy.last_handoff_text = handoff.to_phase_context_string()
    await transition_phase(deps, LoopPhase.ANALYZE, LoopPhase.SELECT_STRATEGY, handoff, tool_cache=state.context.tool_cache, design_fingerprint=str(state.control.best_checkpoint_path), iteration=state.iteration.current)
    return LoopPhase.SELECT_STRATEGY


def _check_phase_exit(
    state: OptimizerState,
    tool_round: int,
    max_rounds: int,
) -> PhaseExitContract:
    """Check common exit conditions for the phase loop."""
    contract = build_phase_exit_contract(
        round_count=tool_round,
        max_rounds=max_rounds,
        start_time=state.control.start_time,
        wall_clock_timeout=state.control.wall_clock_timeout,
        now=time.time(),
        user_exit_requested=state.control.user_exit_requested,
        total_cost=state.cost.total_cost,
        cost_hard_limit=state.cost.cost_hard_limit,
    )
    if not contract.should_exit:
        return contract
    if contract.event == "max_rounds":
        logger.warning(f"[ANALYZE] Max rounds reached ({tool_round} > {max_rounds})")
    elif contract.event == "wall_clock_timeout":
        elapsed = time.time() - state.control.start_time if state.control.start_time else 0.0
        logger.warning(f"[ANALYZE] Wall-clock timeout: {elapsed:.0f}s")
    elif contract.event == "user_requested":
        logger.info("[ANALYZE] User exit requested")
    elif contract.event == "cost_limit":
        logger.warning("[ANALYZE] Cost limit reached")
    if contract.set_is_done:
        state.control.is_done = True
    if contract.done_reason:
        state.control.done_reason = contract.done_reason
    if contract.record_reason:
        record_flow_signal(state, "SYSTEM_EXIT", contract.record_reason, phase="ANALYZE")
    return contract


def _update_pending_pblock_state_from_result(state: OptimizerState, raw_result: str) -> None:
    """Freeze PBLOCK candidates produced during ANALYZE for later EXECUTE use."""
    try:
        payload = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    selected_plan = extract_selected_plan_from_payload(payload)
    candidate_plans = payload.get("candidate_plans")
    if selected_plan is not None:
        state.context.pending_pblock_plan = selected_plan.to_dict()
    if isinstance(candidate_plans, list):
        state.context.pending_pblock_candidates = [dict(item) for item in candidate_plans if isinstance(item, dict)]
        state.context.attempted_pblock_candidate_ids.clear()


async def _call_phase_llm(state, deps, phase_tools, max_retries=3, retry_delay=2.0):
    """Call LLM with phase-specific tools and retry logic."""
    if deps.openai_client is None or deps.compat is None:
        return None

    try:
        api_messages = deps.compat.get_formatted_for_api()
    except Exception:
        return None

    # Extract the first system message for the top-level API ``system``
    # parameter (prompt caching). Remaining system messages (FORMAT_GUARD,
    # handoff, budget) stay in the conversation as system-role messages.
    system_text, api_messages = extract_system_message(api_messages)

    # Inject merged handoff + dashboard as last user message
    # Inject Pinned cell-registry layer (right after system message),
    # then merged handoff + dashboard as last user message.
    inject_pinned_cell_registry(api_messages, state)
    inject_merged_dashboard(api_messages, state, LoopPhase.ANALYZE)

    model = state.model.current_model
    state.model.llm_call_count += 1

    extra_body = build_llm_extra_body(
        deps.reasoning_config, model,
        state.model.planner_model, state.model.worker_model,
    )

    last_exception = None
    for retry in range(max_retries):
        try:
            kwargs = dict(
                model=model,
                messages=api_messages,
                tools=phase_tools if phase_tools else None,
                timeout=600.0,
            )
            if extra_body:
                if system_text:
                    extra_body["system"] = system_text
                kwargs["extra_body"] = extra_body
            elif system_text:
                kwargs["extra_body"] = {"system": system_text}
            # Log prompt for observability
            if deps.prompt_logger:
                deps.prompt_logger.log_prompt(
                    model=model,
                    messages=api_messages,
                    iteration=state.iteration.current,
                    job_id=state.control.run_dir.name if state.control.run_dir else ""
                )
            state.context.latest_user_prompt = (
                api_messages[-1].get("content", "") if api_messages else ""
            )[:2000]
            response = await deps.openai_client.chat.completions.create(**kwargs)
            # Log LLM call with state snapshot
            if deps.llm_call_logger:
                deps.llm_call_logger.log_call(
                    state, model=model, messages=api_messages, tools=phase_tools,
                    response=response, phase="ANALYZE",
                )
            # Track cost so ANALYZE calls count toward the budget accumulator.
            track_llm_call_cost(state, response)
            return response
        except Exception as e:
            last_exception = e
            error_str = str(e)
            if "429" in error_str:
                fallback = _get_fallback_model(state, model)
                if fallback and fallback != model:
                    logger.warning(f"[ANALYZE] Rate limit, fallback: {model} -> {fallback}")
                    state.model.current_model = fallback
                    model = fallback
                    wait_time = retry_delay * (2 ** retry)
                    await asyncio.sleep(wait_time)
                    continue
            if retry < max_retries - 1:
                wait_time = retry_delay * (2 ** retry)
                logger.warning(f"[ANALYZE] Retry {retry+1}/{max_retries}: {e}")
                await asyncio.sleep(wait_time)

    if last_exception:
        raise last_exception
    return None


def _track_wns_from_result(state: OptimizerState, tool_name: str, raw_result: str) -> None:
    """Parse WNS from timing tool results."""
    if tool_name not in ("vivado_report_timing_summary", "vivado_phys_opt_design",
                         "vivado_route_design", "vivado_get_wns", "vivado_physopt_and_route"):
        return

    if tool_name == "vivado_physopt_and_route":
        try:
            data = json.loads(raw_result)
            post = data.get("post_optimization", {})
            if isinstance(post, dict) and post.get("wns") is not None:
                wns = float(post["wns"])
                tns = float(post.get("tns")) if post.get("tns") is not None else None
                fe = int(post.get("failing_endpoints")) if post.get("failing_endpoints") is not None else None
                if wns is not None:
                    state.timing.latest_wns = wns
                    if tns is not None:
                        state.timing.latest_tns = tns
                    if fe is not None:
                        state.timing.latest_failing_endpoints = fe
                        refresh_violation_summary(state)
                    if wns > state.timing.best_wns:
                        state.timing.best_wns = wns
                        state.timing.best_wns_iteration = state.iteration.current
                        state.timing.best_wns_tns = tns
                        state.timing.best_wns_failing_endpoints = fe
                        state.control.needs_save = True
                return  # skip parse_timing_summary if we got WNS from JSON
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    timing = parse_timing_summary(raw_result)
    wns = timing.get("wns")
    tns = timing.get("tns")
    fe = timing.get("failing_endpoints")
    if wns is not None:
        state.timing.latest_wns = wns
        if tns is not None:
            state.timing.latest_tns = tns
        if fe is not None:
            state.timing.latest_failing_endpoints = fe
            refresh_violation_summary(state)
        if wns > state.timing.best_wns:
            state.timing.best_wns = wns
            state.timing.best_wns_iteration = state.iteration.current
            state.timing.best_wns_tns = tns
            state.timing.best_wns_failing_endpoints = fe
            state.control.needs_save = True


def _check_wns_target_met(state: OptimizerState) -> bool:
    return (
        state.timing.latest_wns is not None
        and state.timing.latest_wns >= WNS_TARGET_THRESHOLD
        and is_valid_wns(state.timing.latest_wns, state.timing.clock_period, state.timing.best_wns)
    )


def _get_fallback_model(state: OptimizerState, current_model: str) -> str | None:
    if current_model == state.model.worker_model:
        fallbacks = state.model.worker_fallback_models
        idx = state.model.worker_fallback_index
        if idx < len(fallbacks):
            state.model.worker_fallback_index = idx + 1
            return fallbacks[idx]
    if current_model == state.model.planner_model:
        fallbacks = state.model.planner_fallback_models
        idx = state.model.planner_fallback_index
        if idx < len(fallbacks):
            state.model.planner_fallback_index = idx + 1
            return fallbacks[idx]
    return None


# ── Analysis tools used for recent-results extraction ────────────────
# Canonical set lives in tool_summary.compact_tool_summary; re-export here
# for backward compatibility with any external callers.
from optimizer.pure.tool_summary import _ANALYSIS_TOOL_NAMES as _ANALYSIS_TOOL_NAMES  # noqa: F401


def _extract_recent_tool_results(state: OptimizerState) -> list[str]:
    """Extract compact summaries of recent analysis-phase tool calls.

    Reads raw_tool_outputs for the current iteration, returns one-line
    summaries of analysis tools. Used to populate PhaseHandoff.tool_results
    so SELECT_STRATEGY sees concrete tool findings.
    """
    current_iter = state.iteration.current
    results: list[str] = []

    for (it, _phase, rd, name), raw in sorted(state.context.raw_tool_outputs.items()):
        if it != current_iter:
            continue
        if name not in _ANALYSIS_TOOL_NAMES:
            continue

        summary = compact_tool_summary(name, raw)
        if summary:
            entry = f"[{name}] {summary}"
            if entry not in results:
                results.append(entry)

    return results


def _extract_analyze_key_findings(state: OptimizerState) -> dict:
    """Extract structured diagnostic findings from OptimizerState.

    Called at ANALYZE phase exit to populate PhaseHandoff.key_findings,
    so SELECT_STRATEGY sees structured data, not just 600-char llm_summary.
    """
    findings: dict = {}

    # --- dominant_obstacle inference ---
    obstacles: list[str] = []

    # Logic depth: check critical paths for avg logic levels
    if state.timing.critical_paths:
        depths = [p.levels for p in state.timing.critical_paths if p.levels and p.levels > 0]
        if depths:
            avg_depth = sum(depths) / len(depths)
            findings["avg_logic_depth"] = round(avg_depth, 1)
            if avg_depth >= 5:
                obstacles.append("logic_depth")

    # Fanout: check high fanout nets
    if state.timing.high_fanout_nets:
        fanouts = [n.get("fanout", 0) for n in state.timing.high_fanout_nets if isinstance(n, dict)]
        if fanouts:
            findings["max_fanout"] = max(fanouts)
            findings["high_fanout_count"] = len(fanouts)
            if max(fanouts) >= 200:
                obstacles.append("fanout")

    # Placement spread: check critical path spread
    spread = state.timing.critical_path_spread or {}
    if isinstance(spread, dict):
        spread_max = spread.get("max_distance") or spread.get("avg_max_distance") or 0
        if spread_max:
            findings["cp_spread_max_tiles"] = int(spread_max)
            if int(spread_max) >= 70:
                obstacles.append("placement_spread")

    # Resource counts
    res = state.timing.resource_utilization or {}
    if res:
        findings["resource_lut"] = int(res.get("LUT", res.get("lut", 0)))
        findings["resource_ff"] = int(res.get("FF", res.get("ff", 0)))

    # Design type
    ff_count = findings.get("resource_ff", None)
    if ff_count is not None and ff_count == 0:
        findings["design_type"] = "combinational_only"
    elif ff_count is not None and ff_count > 0:
        findings["design_type"] = "pipelined"

    # Number of failing endpoints
    if state.timing.latest_failing_endpoints is not None:
        findings["failing_endpoints"] = state.timing.latest_failing_endpoints

    # ── Structural cell type signals ──────────────────────────────
    di = state.timing.design_info or {}
    top_types = di.get("top_cell_types", {})
    if top_types:
        muxf7 = top_types.get("MUXF7", 0)
        muxf8 = top_types.get("MUXF8", 0)
        muxf_total = muxf7 + muxf8
        if muxf_total:
            findings["muxf_count"] = muxf_total
        lut = top_types.get("LUT6", 0) + top_types.get("LUT5", 0) \
              + top_types.get("LUT4", 0) + top_types.get("LUT3", 0) \
              + top_types.get("LUT2", 0) + top_types.get("LUT1", 0)
        ff = top_types.get("FDRE", 0) + top_types.get("FDSE", 0) \
             + top_types.get("FDCE", 0) + top_types.get("FDPE", 0)
        if lut and ff:
            findings["ff_to_lut_ratio"] = round(ff / lut, 4)
        if muxf_total and muxf7 and muxf8:
            findings["has_muxf_cascade"] = True

    # Dominant obstacle
    findings["dominant_obstacle"] = obstacles[0] if obstacles else "unknown"
    if len(obstacles) > 1:
        findings["secondary_obstacles"] = ",".join(obstacles[1:])

    return findings
