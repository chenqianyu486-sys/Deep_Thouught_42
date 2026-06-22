"""EXECUTE phase: run the chosen optimization strategy.

Full execution tools are exposed. Auto chain actions and post-eval
hooks are applied after critical tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time

from pathlib import Path

from optimizer.state import OptimizerState, PhaseEntry, ToolCallRecord, LLMCallRecord, record_flow_signal, record_strategy_failure
from optimizer.deps import NodeDeps
from optimizer.edges import NodeName
from optimizer.pure.tool_filter import LoopPhase, filter_tools_for_phase, get_phase_max_rounds
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.tool_router import call_tool as call_tool_fn
from optimizer.pure.model_select import classify_task
from optimizer.pure.step_state import extract_step_state
from optimizer.pure.timing import parse_timing_summary, is_valid_wns
from optimizer.pure.constants import WNS_TARGET_THRESHOLD, DASHBOARD_REFRESH_MAP, SKILL_CHAIN_ACTIONS, HEAVY_CHAIN_SKILLS, PHASE_TOOL_RATE_LIMITS, _TOOL_TIMEOUT_DEFAULTS, build_llm_extra_body, RAPIDWRIGHT_PRECHECK_ENABLED, RAPIDWRIGHT_PRECHECK_REGRESS_THRESHOLD, PLACE_ONLY_CHECK_ENABLED, PLACE_ONLY_REGRESS_THRESHOLD, PLACE_ONLY_CHECK_SKILLS
from optimizer.pure.critical_path import parse_critical_path_cells, update_critical_paths, refresh_violation_summary
from optimizer.nodes.subgraphs.phase_handoff import build_phase_handoff, transition_phase
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.color import green, yellow

logger = logging.getLogger(__name__)

# Tools that trigger mandatory WNS evaluation
POST_EVAL_TOOLS = frozenset({
    "vivado_route_design",
    "rapidwright_execute_pblock_strategy",
    "vivado_physopt_and_route",  # triggers WNS eval after PhysOpt+route
    "vivado_phys_opt_design",  # standalone phys_opt_design for split physopt chain
})

# Tools that modify the design (side-effect tools).
# Used for no-progress detection: if LLM only calls read-only tools for
# NO_PROGRESS_LIMIT consecutive rounds, exit the phase early.
SIDE_EFFECT_TOOLS = frozenset({
    "vivado_place_design",
    "vivado_route_design",
    "vivado_phys_opt_design",
    "vivado_physopt_and_route",
    "vivado_opt_design",
    "vivado_create_and_apply_pblock",
    "rapidwright_execute_pblock_strategy",
    "rapidwright_execute_fanout_strategy",
    "rapidwright_execute_congestion_spreading",
    "rapidwright_optimize_pin_swapping",
    "rapidwright_flatten_lut_cascade",
    "rapidwright_replicate_critical_cells",
    "rapidwright_execute_register_retiming",
    "rapidwright_smart_retiming",
    "rapidwright_execute_net_swapping",
    "rapidwright_optimize_cell_placement",
    "rapidwright_optimize_lut_input_cone",
})
# No-progress detection threshold. After this many consecutive rounds
# without any side-effect tool call, exit the EXECUTE phase early.
#
# Coordinated with _TOOL_TIMEOUT_DEFAULTS:
#   - Longest read-only tool: vivado_run_tcl (120s base × design_size_factor)
#   - 4 rounds × ~120s worst-case tool wait = ~480s before exit
#   - Execution tools (place_design=1800s, route_design=1800s) always reset counter
#   - LLM round-trip latency ~5-15s → ~20-60s overhead per 4-round window
NO_PROGRESS_LIMIT = 4

# Once a timing-evaluated execution tool has a clear verdict, EXECUTE has
# produced enough signal. Hand control to EVALUATE instead of asking the LLM
# for another execution round just to decide what the verdict already implies.
POST_EVAL_EXIT_VERDICTS = frozenset({"IMPROVED", "UNCHANGED", "REGRESSED"})


def _execute_exit_reason_after_timing_update(
    tool_name: str,
    post_eval_verdict: str | None,
    target_met: bool,
) -> str:
    """Return why EXECUTE should yield after an evaluated tool, or empty string."""
    if target_met:
        return "wns_target_met"
    if tool_name in POST_EVAL_TOOLS and post_eval_verdict in POST_EVAL_EXIT_VERDICTS:
        return f"post_eval_{post_eval_verdict.lower()}"
    return ""


async def _ensure_iteration_start_checkpoint(
    state: OptimizerState,
    deps: NodeDeps,
) -> bool:
    """Persist one rollback checkpoint per optimizer iteration.

    EXECUTE may be entered several times while strategies are switched within
    one iteration. Rewriting the same path on every entry wastes Vivado time;
    duplicate tracking entries can also make retention delete the newest file.
    """
    if state.control.run_dir is None or deps.vivado_session is None:
        return False

    iteration = state.iteration.current
    tracked = state.control.iteration_checkpoints
    existing_path = next(
        (path for saved_iteration, path in tracked
         if saved_iteration == iteration and path.exists()),
        None,
    )
    if existing_path is not None:
        deduplicated = []
        seen_iterations = set()
        for saved_iteration, path in reversed(tracked):
            if saved_iteration in seen_iterations:
                continue
            seen_iterations.add(saved_iteration)
            deduplicated.append((saved_iteration, path))
        state.control.iteration_checkpoints = list(reversed(deduplicated))
        logger.info(
            f"[CHECKPOINT] Reusing iteration {iteration} start DCP: {existing_path}"
        )
        return False

    # A stale tracking entry must not suppress reconstruction of a missing DCP.
    state.control.iteration_checkpoints = [
        (saved_iteration, path)
        for saved_iteration, path in tracked
        if saved_iteration != iteration
    ]
    iter_ckpt = state.control.run_dir / f"iteration_{iteration}_start.dcp"
    await call_tool_fn(
        "vivado_write_checkpoint",
        {"dcp_path": str(iter_ckpt.resolve()), "force": True},
        deps.rapidwright_session,
        deps.vivado_session,
        design_size_factor=state.timing.design_size_factor,
    )
    state.control.iteration_checkpoints.append((iteration, iter_ckpt))
    while len(state.control.iteration_checkpoints) > 3:
        _, old_path = state.control.iteration_checkpoints.pop(0)
        try:
            old_path.unlink(missing_ok=True)
        except Exception:
            pass
    logger.info(f"[CHECKPOINT] Saved iteration {iteration} start DCP")
    return True


async def run_execute_phase(state: OptimizerState, deps: NodeDeps) -> LoopPhase:
    """Run the EXECUTE phase: execute the chosen strategy via tool calls.

    Returns:
        LoopPhase.EVALUATE (always, even on failure).
    """
    max_rounds = get_phase_max_rounds(
        LoopPhase.EXECUTE,
        state.strategy.current_strategy,
    )
    tool_round = 0
    tools_called: list[str] = []
    wns_before = state.timing.latest_wns
    unplaced_without_replace = False
    pre_unplace_path: Path | None = None
    llm_summary = ""
    state.context.tool_phase_call_counts.clear()
    state.context.consecutive_empty_responses = 0
    reached_callback = False  # track if the strategy reached callback indicating completion
    no_progress_count = 0  # consecutive rounds without side-effect tool calls

    # Record phase entry
    phase_entry = PhaseEntry(
        phase="EXECUTE_STRATEGY",
        strategy=state.strategy.current_strategy,
        iteration=state.iteration.current,
        tool_round=0,
        wns_at_entry=state.timing.latest_wns,
    )
    state.strategy.phase_history.append(phase_entry)
    if len(state.strategy.phase_history) > 100:
        state.strategy.phase_history = state.strategy.phase_history[-100:]
    state.strategy.current_phase = "EXECUTE_STRATEGY"

    # Save the rollback baseline once even if this iteration switches strategy.
    try:
        await _ensure_iteration_start_checkpoint(state, deps)
    except Exception as e:
        logger.warning(f"[CHECKPOINT] Failed to save iteration checkpoint: {e}")

    while True:
        tool_round += 1
        state.iteration.tool_round = tool_round

        if _check_phase_exit(state, tool_round, max_rounds):
            break

        # Call LLM with execution tools
        phase_tools = filter_tools_for_phase(
            deps.tools,
            LoopPhase.EXECUTE,
            strategy=state.strategy.current_strategy,
        )
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

        # Track cost
        _track_cost(state, response)

        # Extract step state
        step_state = extract_step_state(message)
        state.control.step_state = step_state

        if step_state:
            state.context.step_state_misses = 0
            flow_signal = step_state.flow_control

            # EXEC_DONE -> move to evaluate
            if flow_signal == "EXEC_DONE":
                llm_summary = assistant_content
                logger.info(green(f"[EXECUTE] LLM signaled EXEC_DONE at round {tool_round}"))
                record_flow_signal(state, "EXEC_DONE", "execution_complete",
                                   phase="EXECUTE_STRATEGY", result_status=step_state.result_status or "")
                break

            # EXHAUSTED during execution
            if flow_signal == "EXHAUSTED":
                logger.info("[EXECUTE] LLM signaled EXHAUSTED")
                record_flow_signal(state, "EXHAUSTED", "execution_exhausted",
                                   phase="EXECUTE_STRATEGY", result_status=step_state.result_status or "")
                break

        else:
            state.context.step_state_misses += 1
            if deps.compat is not None:
                deps.compat.add_message("user", "[NOTE] report_step_state missing. Include it next turn.")

        # Execute tool calls
        if message.tool_calls:
            round_had_side_effect = False
            force_exit = False  # set by inner loop to break outer while
            _pending_tool_count = len([tc for tc in message.tool_calls if tc.function])  # Track pending tool calls
            for tc in message.tool_calls:
                if not tc.function:
                    continue

                tool_name = tc.function.name
                state.iteration.tools_used.append(tool_name)
                tools_called.append(tool_name)
                if tool_name in SIDE_EFFECT_TOOLS:
                    round_had_side_effect = True

                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}
                task_type = classify_task(tool_name, tool_args)
                if task_type == "optimization" or (
                    task_type != "unknown" and state.model.current_task_type != "optimization"
                ):
                    state.model.current_task_type = task_type

                # Auto-inject critical_path_cells for pblock tools
                if tool_name in ("rapidwright_execute_pblock_strategy", "rapidwright_analyze_pblock_region"):
                    if not tool_args.get("critical_path_cells") and state.timing.critical_paths:
                        cells = []
                        seen = set()
                        for cp in state.timing.critical_paths[:10]:
                            for cell_name in cp.cells:
                                if cell_name not in seen:
                                    seen.add(cell_name)
                                    cells.append(cell_name)
                                    if len(cells) >= 50:
                                        break
                            if len(cells) >= 50:
                                break
                        if cells:
                            tool_args["critical_path_cells"] = cells
                            logger.info(f"[EXECUTE] Injected {len(cells)} critical path cells for {tool_name}")

                # Auto-compute adaptive resource_multiplier for pblock strategy
                if tool_name == "rapidwright_execute_pblock_strategy":
                    if "resource_multiplier" not in tool_args:
                        tool_args["resource_multiplier"] = _compute_adaptive_pblock_multiplier(state)
                        logger.info(f"[EXECUTE] Adaptive resource_multiplier: {tool_args['resource_multiplier']:.2f}")

                # Auto-inject critical_paths for LUT cascade tool
                if tool_name == "rapidwright_flatten_lut_cascade":
                    if not tool_args.get("critical_paths") and state.timing.critical_paths:
                        paths = [cp.cells for cp in state.timing.critical_paths[:10] if cp.cells]
                        if paths:
                            tool_args["critical_paths"] = paths
                            logger.info(f"[EXECUTE] Injected {len(paths)} critical paths for {tool_name}")

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
                        _pending_tool_count -= 1  # Rate-limited tools count as completed
                        continue

                # Save pre-unplace checkpoint BEFORE the unplace tool call,
                # so rollback restores to the placed state (not already-unplaced).
                if tool_name == "vivado_place_design":
                    directive = tool_args.get("directive", "").lower()
                    if directive == "unplace":
                        try:
                            pre_unplace_ckpt = Path(f"/tmp/pre_unplace_{state.iteration.current}_{tool_round}.dcp")
                            await call_tool_fn(
                                "vivado_write_checkpoint", {"dcp_path": str(pre_unplace_ckpt), "force": True},
                                deps.rapidwright_session, deps.vivado_session,
                                design_size_factor=state.timing.design_size_factor,
                            )
                            pre_unplace_path = pre_unplace_ckpt
                        except Exception as e:
                            logger.warning(f"[EXECUTE] Failed to save pre-unplace checkpoint: {e}")

                tool_start = time.time()
                logger.info(f"[EXECUTE] Calling {tool_name}")

                # Capture WNS before tool call for post-chain re-evaluation.
                # Analysis-only skills (e.g. pblock_strategy) don't change WNS
                # themselves — the auto-chain (place+route) does. Post-eval runs
                # before the chain, so we need the pre-tool WNS to detect chain
                # improvements afterwards.
                pre_tool_wns = state.timing.latest_wns

                # Capture RapidWright timing baseline for directional pre-check.
                # The pre-check compares RW-after vs RW-before (same engine) to
                # detect directional regression. Comparing RW vs Vivado is
                # unreliable due to systematic timing differences between engines.
                rw_precheck_baseline = None
                if (RAPIDWRIGHT_PRECHECK_ENABLED
                        and tool_name in SKILL_CHAIN_ACTIONS
                        and tool_name != "rapidwright_execute_pblock_strategy"
                        and deps.rapidwright_session):
                    rw_precheck_baseline = await _get_rw_timing_estimate(state, deps)
                    if rw_precheck_baseline is not None:
                        logger.info(
                            f"[PRECHECK] RW baseline (before skill): "
                            f"WNS={rw_precheck_baseline:.3f}ns"
                        )

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
                )
                tool_elapsed = time.time() - tool_start
                logger.info(f"[EXECUTE] {tool_name} completed in {tool_elapsed:.1f}s")
                _pending_tool_count -= 1  # This tool call is no longer pending

                summary = summarize_tool_result(
                    tool_name, result,
                    latest_wns=state.timing.latest_wns,
                    latest_tns=state.timing.latest_tns,
                    latest_failing_endpoints=state.timing.latest_failing_endpoints,
                    prev_best_wns=state.timing.prev_best_wns,
                )

                # Store raw output
                state.context.raw_tool_outputs[(state.iteration.current, tool_round)] = (tool_name, result)
                if len(state.context.raw_tool_outputs) > state.context.raw_tool_output_max:
                    oldest_key = min(state.context.raw_tool_outputs.keys(), key=lambda k: (k[0], k[1]))
                    del state.context.raw_tool_outputs[oldest_key]

                # Record tool call trace
                trace_record = ToolCallRecord(
                    tool_name=tool_name,
                    tool_call_id=tc.id,
                    arguments=tool_args,
                    summary=summary[:500],
                    result_chars=len(result) if isinstance(result, str) else 0,
                    elapsed_seconds=tool_elapsed,
                    iteration=state.iteration.current,
                    tool_round=tool_round,
                    status="completed",
                )
                state.context.tool_call_trace.append(trace_record)
                if len(state.context.tool_call_trace) > state.context.tool_call_trace_max:
                    state.context.tool_call_trace = state.context.tool_call_trace[-state.context.tool_call_trace_max:]

                if deps.compat is not None:
                    deps.compat.add_message("tool", summary, {
                        "tool_call_id": tc.id, "name": tool_name,
                    })

                # Track WNS from timing results
                _track_wns_from_result(state, tool_name, result)
                await _try_save_best_checkpoint(state, deps)

                # Track place_design -unplace for rollback guard
                if tool_name == "vivado_place_design":
                    directive = tool_args.get("directive", "").lower()
                    if directive == "unplace":
                        unplaced_without_replace = True
                    else:
                        unplaced_without_replace = False

                # Track critical path data
                if tool_name == "vivado_extract_critical_path_cells":
                    cell_paths = parse_critical_path_cells(result)
                    if cell_paths:
                        update_critical_paths(state, cell_paths, iteration=state.iteration.current)

                # Mark critical paths stale after layout changes
                if tool_name in ("vivado_phys_opt_design", "vivado_route_design",
                                 "vivado_place_design", "vivado_create_and_apply_pblock"):
                    state.timing.critical_paths_stale = True

                # Dashboard freshness
                refreshable = DASHBOARD_REFRESH_MAP.get(tool_name)
                if refreshable:
                    state.timing.refreshed_fields |= refreshable

                # Post-eval hook for critical tools
                post_eval_verdict = None
                if tool_name in POST_EVAL_TOOLS:
                    # For physopt_and_route, WNS is already in the JSON result
                    if tool_name == "vivado_physopt_and_route":
                        try:
                            data = json.loads(result) if result else {}
                            post = data.get("post_optimization", {})
                            if isinstance(post, dict) and post.get("wns") is not None:
                                prev_wns = state.timing.latest_wns
                                new_wns = float(post["wns"])
                                raw_tns = post.get("tns")
                                new_tns = float(raw_tns) if isinstance(raw_tns, (int, float)) else None
                                raw_fe = post.get("failing_endpoints")
                                new_fe = int(raw_fe) if isinstance(raw_fe, (int, float)) else None
                                state.timing.latest_wns = new_wns
                                if new_tns is not None:
                                    state.timing.latest_tns = new_tns
                                if new_fe is not None:
                                    state.timing.latest_failing_endpoints = new_fe
                                if new_wns > state.timing.best_wns:
                                    state.timing.best_wns = new_wns
                                    state.timing.best_wns_iteration = state.iteration.current
                                    state.timing.best_wns_tns = new_tns
                                    state.timing.best_wns_failing_endpoints = new_fe
                                    state.control.needs_save = True
                                delta = new_wns - prev_wns if prev_wns is not None else 0.0
                                verdict = "IMPROVED" if delta > 0.001 else ("UNCHANGED" if abs(delta) <= 0.001 else "REGRESSED")
                                post_eval_verdict = verdict
                                eval_notice = f"[EVAL] After {tool_name}: WNS={new_wns:.3f}ns (delta={delta:+.3f}ns vs previous). {verdict}."
                                if new_tns is not None:
                                    eval_notice += f" TNS={new_tns:.3f}ns"
                                if deps.compat is not None:
                                    deps.compat.add_message("user", eval_notice)
                                logger.info(f"[EXECUTE] Post-eval (from result): {tool_name} -> WNS={new_wns:.3f}ns (delta={delta:+.3f}, {verdict})")
                            else:
                                # Fallback to full timing report if JSON doesn't have WNS
                                post_eval_verdict = await _post_eval_hook(state, deps, tool_name)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            post_eval_verdict = await _post_eval_hook(state, deps, tool_name)
                    else:
                        try:
                            post_eval_verdict = await _post_eval_hook(state, deps, tool_name)
                        except Exception as e:
                            logger.warning(f"[EXECUTE] Post-eval hook failed for {tool_name}: {e}")
                await _try_save_best_checkpoint(state, deps)

                # Inject guidance when post-eval shows no improvement
                if (post_eval_verdict == "UNCHANGED"
                        and tool_name in POST_EVAL_TOOLS
                        and deps.compat is not None):
                    deps.compat.add_message("user",
                        f"[GUIDANCE] {tool_name} produced no WNS improvement. "
                        f"EXECUTE will yield to EVALUATE for strategy selection.")

                # ── Level 1: RapidWright directional pre-check ──────────────
                # Before paying the cost of the Vivado P&R chain (~900s), use
                # RapidWright's timing estimator (~2.5s) to directionally check
                # whether the skill's placement change is likely harmful.
                #
                # RapidWright is reliable for *directional* comparison (which of
                # two placements is better) but NOT for absolute WNS values.
                # See docs/plans/p-r-rollback-abundant-puffin.md for details.
                precheck_verdict = None
                if (RAPIDWRIGHT_PRECHECK_ENABLED
                        and tool_name in SKILL_CHAIN_ACTIONS
                        and tool_name != "rapidwright_execute_pblock_strategy"  # analysis-only
                        and deps.rapidwright_session
                        and state.timing.latest_wns is not None):
                    precheck_verdict = await _rapidwright_direction_check(state, deps, rw_precheck_baseline)
                    if precheck_verdict == "REGRESS":
                        logger.warning(yellow(
                            f"[EXECUTE] Pre-check REGRESS for {tool_name}: "
                            f"skipping Vivado P&R chain (~900s saved)"
                        ))
                        state.control.done_reason = "precheck_direction_regress"
                        record_flow_signal(
                            state, "SYSTEM_EXIT", "precheck_direction_regress",
                            phase="EXECUTE_STRATEGY",
                        )
                        if deps.compat is not None:
                            deps.compat.add_message("user",
                                f"[PRECHECK] {tool_name}: RapidWright timing estimate "
                                f"shows directional WNS regression. Skipping Vivado "
                                f"place+route chain. Strategy marked as ineffective."
                            )
                        # Record strategy failure so select_model won't retry it
                        record_strategy_failure(
                            state, state.strategy.current_strategy,
                            "strategy_ineffective", tool=tool_name,
                            detail="precheck_direction_regress"
                        )
                        force_exit = True
                        break

                # ── Chain actions for skills ────────────────────────────
                # Existing logic: gated by post-eval verdict.
                # HEAVY_CHAIN_SKILLS can skip expensive chains when UNCHANGED.
                if tool_name in SKILL_CHAIN_ACTIONS:
                    if (post_eval_verdict == "UNCHANGED"
                            and tool_name in HEAVY_CHAIN_SKILLS
                            and deps.vivado_session):
                        logger.info(
                            f"[EXECUTE] Post-eval UNCHANGED for {tool_name}, "
                            f"skipping heavy chain (saves ~180s). Running lightweight validation."
                        )
                        if deps.compat is not None:
                            deps.compat.add_message("user",
                                f"[CHAIN GATE] {tool_name}: post-eval UNCHANGED — "
                                f"skill did not modify the netlist. Skipping "
                                f"place+create_pblock+place+route chain. "
                                f"Running lightweight place_design to verify."
                            )
                        await _lightweight_chain_validation(state, deps, tool_name, tools_called)
                    else:
                        try:
                            skill_data = json.loads(result) if result else {}
                            if isinstance(skill_data, dict) and "error" in skill_data:
                                logger.warning(f"[EXECUTE] Skill {tool_name} returned error, skipping chain: {skill_data['error']}")
                            else:
                                await _execute_chain_actions(state, deps, tool_name, skill_data, tools_called)
                                reached_callback = True
                        except Exception as e:
                            logger.warning(f"[EXECUTE] Chain actions failed for {tool_name}: {e}")

                # ── Post-chain verdict re-evaluation ──────────────────────
                # Analysis-only skills (e.g. pblock_strategy) don't change WNS
                # themselves — post-eval ran before the chain and saw no change.
                # The auto-chain (place+route) is what actually changes WNS.
                # Re-evaluate the verdict by comparing current WNS (updated by
                # the chain's vivado_report_timing_summary step) against the
                # pre-tool WNS.
                if (tool_name in SKILL_CHAIN_ACTIONS
                        and post_eval_verdict == "UNCHANGED"
                        and pre_tool_wns is not None
                        and state.timing.latest_wns is not None
                        and abs(state.timing.latest_wns - pre_tool_wns) > 0.001):
                    chain_delta = state.timing.latest_wns - pre_tool_wns
                    if chain_delta > 0.001:
                        post_eval_verdict = "IMPROVED"
                    else:
                        post_eval_verdict = "REGRESSED"
                    logger.info(
                        f"[EXECUTE] Post-chain re-eval: {tool_name} -> "
                        f"WNS={state.timing.latest_wns:.3f}ns "
                        f"(delta={chain_delta:+.3f}ns vs pre-tool {pre_tool_wns:.3f}ns). "
                        f"{post_eval_verdict}."
                    )
                    if deps.compat is not None:
                        deps.compat.add_message("user",
                            f"[EVAL] After chain for {tool_name}: "
                            f"WNS={state.timing.latest_wns:.3f}ns "
                            f"(delta={chain_delta:+.3f}ns vs pre-tool). "
                            f"{post_eval_verdict}."
                        )

                # Force exit on large WNS regression (>0.5ns below best).
                # EVALUATE will detect regression via detect_rollback_needed()
                # and trigger automatic rollback — no need for LLM to spin here.
                if (state.timing.latest_wns is not None
                        and state.timing.best_wns != float('-inf')
                        and state.timing.latest_wns < state.timing.best_wns - 0.5):
                    logger.warning(
                        yellow(f"[EXECUTE] Large WNS regression: {state.timing.latest_wns:.3f} "
                               f"< best {state.timing.best_wns:.3f} - 0.5, forcing exit")
                    )
                    record_flow_signal(
                        state, "SYSTEM_EXIT", "large_regression",
                        phase="EXECUTE_STRATEGY",
                    )
                    force_exit = True
                    break

                exit_reason = _execute_exit_reason_after_timing_update(
                    tool_name,
                    post_eval_verdict,
                    _check_wns_target_met(state),
                )
                if exit_reason:
                    if exit_reason == "wns_target_met":
                        logger.info(
                            green(
                                f"[EXECUTE] WNS target met after {tool_name}; "
                                f"yielding immediately"
                            )
                        )
                        record_flow_signal(
                            state, "DONE", exit_reason,
                            phase="EXECUTE_STRATEGY",
                        )
                    else:
                        logger.info(
                            f"[EXECUTE] {tool_name} verdict={post_eval_verdict}; "
                            f"yielding to EVALUATE"
                        )
                        record_flow_signal(
                            state, "EXEC_DONE", exit_reason,
                            phase="EXECUTE_STRATEGY",
                        )
                    force_exit = True
                    break

                # Track tool errors
                result_lower = summary.lower() if summary else ""
                if "error" in result_lower and "success" not in result_lower:
                    state.iteration.tool_errors.append({
                        "tool": tool_name,
                        "result": summary[:2000],
                    })

            # Auto-refresh critical paths if stale
            if state.timing.critical_paths_stale:
                try:
                    await _auto_refresh_critical_paths(state, deps)
                except Exception as e:
                    logger.warning(f"[EXECUTE] Critical path auto-refresh failed: {e}")

            # Break outer while if inner for loop requested force exit
            if force_exit:
                break

            # No-progress detection: exit if LLM only calls read-only tools.
            # Only count a round when ALL tools have completed (none pending).
            # This prevents penalizing rounds where slow tools (place_design/route_design)
            # are still executing and the LLM hasn't had a chance to call execution tools yet.
            if round_had_side_effect:
                no_progress_count = 0
            elif _pending_tool_count <= 0:
                no_progress_count += 1
            # else: tools still pending — do NOT count this round toward no-progress

            if no_progress_count >= NO_PROGRESS_LIMIT:
                logger.warning(
                    f"[EXECUTE] No-progress limit reached ({no_progress_count} rounds "
                    f"without side-effect tools)"
                )
                record_flow_signal(
                    state, "SYSTEM_EXIT", "no_progress",
                    phase="EXECUTE_STRATEGY",
                )
                break

            continue

        # No tool calls — count as no-progress round
        no_progress_count += 1

        # Track consecutive empty responses (no content AND no tool calls)
        if not assistant_content.strip() and not message.tool_calls:
            state.context.consecutive_empty_responses += 1
            if state.context.consecutive_empty_responses >= 3:
                logger.warning(
                    f"[EXECUTE] {state.context.consecutive_empty_responses} consecutive "
                    f"empty responses, forcing EXEC_DONE"
                )
                record_flow_signal(state, "SYSTEM_EXIT", "empty_responses",
                                   phase="EXECUTE_STRATEGY")
                break
        else:
            state.context.consecutive_empty_responses = 0

        if no_progress_count >= NO_PROGRESS_LIMIT:
            logger.warning(
                f"[EXECUTE] No-progress limit reached ({no_progress_count} rounds "
                f"without side-effect tools)"
            )
            record_flow_signal(
                state, "SYSTEM_EXIT", "no_progress",
                phase="EXECUTE_STRATEGY",
            )
            break

        if _check_wns_target_met(state):
            break

    # Auto-rollback if design left unplaced at phase exit
    if unplaced_without_replace and pre_unplace_path is not None:
        logger.warning(
            f"[EXECUTE] WARNING: Design left unplaced at phase exit — "
            f"restoring from pre-unplace checkpoint"
        )
        try:
            await call_tool_fn(
                "vivado_open_checkpoint", {"dcp_path": str(pre_unplace_path)},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            # Refresh WNS after restore
            restore_result = await call_tool_fn(
                "vivado_report_timing_summary", {},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            restore_timing = parse_timing_summary(restore_result)
            if restore_timing.get("wns") is not None:
                state.timing.latest_wns = restore_timing["wns"]
                state.timing.latest_tns = restore_timing.get("tns")
                state.timing.latest_failing_endpoints = restore_timing.get("failing_endpoints")
        except Exception as e:
            logger.warning(f"[EXECUTE] Auto-rollback failed: {e}")

    # Phase exit: build handoff
    llm_summary = llm_summary or f"Execution of {state.strategy.current_strategy} completed."
    wns_after = state.timing.latest_wns

    handoff = build_phase_handoff(
        source_phase=LoopPhase.EXECUTE,
        llm_summary=llm_summary,
        wns=wns_after,
        tns=state.timing.latest_tns,
        failing_endpoints=state.timing.latest_failing_endpoints,
        tools_called=tools_called,
        key_findings={
            "strategy_name": state.strategy.current_strategy,
            "wns_before": wns_before,
            "wns_after": wns_after,
        },
        message_count=tool_round,
    )
    state.strategy.last_handoff_text = handoff.to_phase_context_string()
    await transition_phase(deps, LoopPhase.EXECUTE, LoopPhase.EVALUATE, handoff, tool_cache=state.context.tool_cache)
    return LoopPhase.EVALUATE


def _check_phase_exit(state: OptimizerState, tool_round: int, max_rounds: int) -> bool:
    if tool_round > max_rounds:
        logger.warning(f"[EXECUTE] Max rounds reached ({tool_round} > {max_rounds})")
        record_flow_signal(state, "SYSTEM_EXIT", "max_rounds", phase="EXECUTE_STRATEGY")
        return True
    if state.control.start_time:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            record_flow_signal(state, "SYSTEM_EXIT", "wall_clock_timeout", phase="EXECUTE_STRATEGY")
            return True
    if state.control.user_exit_requested:
        record_flow_signal(state, "SYSTEM_EXIT", "user_requested", phase="EXECUTE_STRATEGY")
        return True
    if state.cost.total_cost >= state.cost.cost_hard_limit:
        state.control.is_done = True
        state.control.done_reason = "cost_limit"
        record_flow_signal(state, "SYSTEM_EXIT", "cost_limit", phase="EXECUTE_STRATEGY")
        return True
    return False


async def _call_phase_llm(state, deps, phase_tools, max_retries=3, retry_delay=2.0):
    """Call LLM with execution tools and retry logic."""
    if deps.openai_client is None or deps.compat is None:
        return None
    try:
        api_messages = deps.compat.get_formatted_for_api()
    except Exception:
        return None

    # Inject merged handoff + dashboard as last user message
    inject_merged_dashboard(api_messages, state, LoopPhase.EXECUTE)

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
                kwargs["extra_body"] = extra_body
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
                    response=response, phase="EXECUTE",
                )
            return response
        except Exception as e:
            last_exception = e
            if "429" in str(e):
                fallback = _get_fallback_model(state, model)
                if fallback and fallback != model:
                    state.model.current_model = fallback
                    model = fallback
                    await asyncio.sleep(retry_delay * (2 ** retry))
                    continue
            if retry < max_retries - 1:
                await asyncio.sleep(retry_delay * (2 ** retry))

    if last_exception:
        raise last_exception
    return None


def _track_wns_from_result(state: OptimizerState, tool_name: str, raw_result: str) -> None:
    if tool_name not in ("vivado_report_timing_summary", "vivado_phys_opt_design",
                         "vivado_route_design", "vivado_get_wns", "vivado_physopt_and_route"):
        return
    if tool_name != "vivado_report_timing_summary":
        state.timing.design_not_routed = False  # clear stale flag for non-timing tools
    # Try JSON path for physopt_and_route
    wns = None
    tns = None
    fe = None
    if tool_name == "vivado_physopt_and_route":
        try:
            data = json.loads(raw_result)
            post = data.get("post_optimization", {})
            if isinstance(post, dict) and "wns" in post and post.get("wns") is not None:
                wns = float(post["wns"])
                raw_tns = post.get("tns")
                tns = float(raw_tns) if isinstance(raw_tns, (int, float)) else None
                raw_fe = post.get("failing_endpoints")
                fe = int(raw_fe) if isinstance(raw_fe, (int, float)) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Fallback to string parsing
    if wns is None:
        # Warn if timing report comes from an unrouted design
        if tool_name == "vivado_report_timing_summary":
            not_routed = "Design State" in raw_result and "Routed" not in raw_result
            state.timing.design_not_routed = not_routed
            if not_routed:
                logger.warning("[EXECUTE] WARNING: Timing report from unplaced/unrouted design — WNS may be inaccurate")
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


async def _save_best_checkpoint(state: OptimizerState, deps: NodeDeps) -> None:
    """Save a DCP checkpoint when best_wns improves, for rollback support.

    Writes to {run_dir}/best_checkpoint.dcp, overwriting previous best.
    Does nothing if no run_dir is configured or if vivado_session is unavailable.
    """
    if state.control.run_dir is None:
        return
    try:
        ckpt_path = state.control.run_dir / "best_checkpoint.dcp"
        await call_tool_fn(
            "vivado_write_checkpoint",
            {"dcp_path": str(ckpt_path.resolve()), "force": True},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        state.control.best_checkpoint_path = ckpt_path
        logger.info(f"Saved best checkpoint: WNS={state.timing.best_wns:.3f}ns")
    except Exception as e:
        logger.warning(f"Failed to save best checkpoint: {e}")


async def _try_save_best_checkpoint(state: OptimizerState, deps: NodeDeps) -> None:
    """Save best checkpoint when needs_save flag is set, then clear it."""
    if state.control.needs_save and deps.vivado_session is not None:
        await _save_best_checkpoint(state, deps)
        state.control.needs_save = False


def _track_cost(state: OptimizerState, response) -> None:
    try:
        usage = getattr(response, 'usage', None)
        if usage:
            prompt = getattr(usage, 'prompt_tokens', 0) or 0
            completion = getattr(usage, 'completion_tokens', 0) or 0
            raw_total = getattr(usage, 'total_tokens', 0) or 0
            total = raw_total if raw_total > 0 else (prompt + completion)
            cost = float(getattr(usage, 'cost', 0.0) or 0.0)
            state.cost.total_prompt_tokens += prompt
            state.cost.total_completion_tokens += completion
            state.cost.total_tokens += total
            state.cost.total_cost += cost
            completion_details = getattr(usage, 'completion_tokens_details', None)
            if completion_details:
                reasoning = getattr(completion_details, 'reasoning_tokens', 0) or 0
                state.cost.total_reasoning_tokens += reasoning
    except Exception as e:
        logger.debug(f"[EXECUTE] Cost tracking failed: {e}")


async def _get_rw_timing_estimate(state: OptimizerState, deps: NodeDeps) -> float | None:
    """Get RapidWright timing estimate (WNS) from the current in-memory design.

    Returns the WNS as a float, or None if the estimate could not be obtained.
    """
    try:
        timing_result = await call_tool_fn(
            "rapidwright_report_timing", {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        try:
            data = json.loads(timing_result)
            if isinstance(data, dict) and "wns_ns" in data:
                return float(data["wns_ns"])
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        timing = parse_timing_summary(timing_result)
        return timing.get("wns")
    except Exception as e:
        logger.debug(f"[PRECHECK] Could not get RW timing estimate: {e}")
        return None


async def _rapidwright_direction_check(
    state: OptimizerState, deps: NodeDeps, rw_baseline: float | None = None,
) -> str:
    """Level 1 pre-check: RapidWright timing estimate for directional regression.

    Called AFTER a RapidWright skill has modified the in-memory design
    but BEFORE executing the expensive Vivado P&R chain (~900s).

    Uses RapidWright's built-in TimingGraph (~2.5s, ~2% error on same-SLR
    paths) to estimate whether the skill's placement change directionally
    improved or regressed WNS.

    ⚠️ Limitations for UltraScale+ multi-SLR designs (>200K cells):
      - RapidWright CANNOT predict route-congestion-induced timing
      - Absolute WNS values are unreliable (error can reach 0.5ns+ on
        cross-SLR / long-distance paths)
      - Only *directional* comparison is trustworthy
    See plan at docs/plans/p-r-rollback-abundant-puffin.md for details.

    Args:
        rw_baseline: RapidWright WNS estimate captured BEFORE the skill
            modified the design. When provided, the comparison is same-engine
            (RW-after vs RW-before), which is reliable. When None, falls back
            to comparing against Vivado WNS (cross-engine, less reliable).

    Returns:
        "IMPROVED"  — RW estimate shows WNS improvement → proceed to chain
        "REGRESS"   — RW estimate shows significant directional regression
                       → skip chain, let EVALUATE switch strategy
        "UNCERTAIN" — cannot determine (no baseline, tool error, etc.)
                       → fall through to existing chain logic (conservative)
    """
    # Prefer same-engine baseline (RW-before) for reliable directional comparison.
    # Fall back to Vivado WNS only if no RW baseline was captured.
    if rw_baseline is not None:
        baseline = rw_baseline
        baseline_source = "RW"
    else:
        baseline = state.timing.latest_wns
        baseline_source = "Vivado"
    if baseline is None:
        logger.debug("[PRECHECK] No WNS baseline available, skipping pre-check")
        return "UNCERTAIN"

    try:
        est_wns = await _get_rw_timing_estimate(state, deps)
        if est_wns is None:
            logger.warning("[PRECHECK] Could not parse RapidWright timing result")
            return "UNCERTAIN"

        delta = est_wns - baseline
        logger.info(
            f"[PRECHECK] RapidWright direction check: "
            f"baseline={baseline:.3f}ns ({baseline_source}), "
            f"est={est_wns:.3f}ns, delta={delta:+.3f}ns"
        )

        if delta > 0.001:
            logger.info(green(f"[PRECHECK] Direction looks IMPROVED (delta={delta:+.3f})"))
            return "IMPROVED"
        elif delta < -RAPIDWRIGHT_PRECHECK_REGRESS_THRESHOLD:
            logger.warning(yellow(
                f"[PRECHECK] Direction shows REGRESS (delta={delta:+.3f}ns, "
                f"threshold={RAPIDWRIGHT_PRECHECK_REGRESS_THRESHOLD:.3f})"
            ))
            return "REGRESS"
        else:
            logger.info(f"[PRECHECK] Direction UNCERTAIN (delta={delta:+.3f}, within dead band)")
            return "UNCERTAIN"

    except Exception as e:
        logger.warning(f"[PRECHECK] RapidWright direction check failed: {e}")
        return "UNCERTAIN"


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


async def _post_eval_hook(state: OptimizerState, deps: NodeDeps, tool_name: str) -> str | None:
    """Force WNS evaluation after critical tools.

    Returns:
        Verdict string ("IMPROVED", "UNCHANGED", "REGRESSED") or None on failure.
    """
    prev_wns = state.timing.latest_wns
    timing_result = await call_tool_fn(
        "vivado_report_timing_summary", {},
        deps.rapidwright_session, deps.vivado_session,
        design_size_factor=state.timing.design_size_factor,
    )
    # Detect false-positive timing from unplaced/unrouted designs
    design_not_routed = "Design State" in timing_result and "Routed" not in timing_result
    state.timing.design_not_routed = design_not_routed
    if design_not_routed:
        logger.warning("[EXECUTE] WARNING: Timing report from unplaced/unrouted design — WNS may be inaccurate")
    timing = parse_timing_summary(timing_result)
    wns = timing.get("wns")
    tns = timing.get("tns")
    fe = timing.get("failing_endpoints")
    if wns is None:
        return None

    state.timing.latest_wns = wns
    if tns is not None:
        state.timing.latest_tns = tns
    if fe is not None:
        state.timing.latest_failing_endpoints = fe
    if wns > state.timing.best_wns:
        state.timing.best_wns = wns
        state.timing.best_wns_iteration = state.iteration.current
        state.timing.best_wns_tns = tns
        state.timing.best_wns_failing_endpoints = fe
        state.control.needs_save = True

    delta = wns - prev_wns if prev_wns is not None else 0.0
    verdict = "IMPROVED" if delta > 0.001 else ("UNCHANGED" if abs(delta) <= 0.001 else "REGRESSED")
    eval_notice = (
        f"[EVAL] After {tool_name}: WNS={wns:.3f}ns "
        f"(delta={delta:+.3f}ns vs previous). {verdict}."
    )
    if design_not_routed:
        eval_notice += " [WARNING: design not routed]"
    if tns is not None:
        eval_notice += f" TNS={tns:.3f}ns"
    if deps.compat is not None:
        deps.compat.add_message("user", eval_notice)
    logger.info(f"[EXECUTE] Post-eval: {tool_name} -> WNS={wns:.3f}ns (delta={delta:+.3f}, {verdict})")
    return verdict


async def _lightweight_chain_validation(state, deps, tool_name, tools_called):
    """Lightweight validation when a heavy-chain skill reports UNCHANGED.

    Runs a single vivado_place_design (no pblock, no unplace) to confirm
    the skill didn't change anything, then checks WNS. If WNS improves
    (false UNCHANGED), the full chain is re-run. Otherwise we skip the
    ~180s place+create_pblock+place+route chain.
    """
    logger.info(f"[chain-gate] Lightweight validation for {tool_name}: place_design only")
    try:
        result = await call_tool_fn(
            "vivado_place_design", {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        state.iteration.tools_used.append("vivado_place_design")
        tools_called.append("vivado_place_design")
        state.timing.critical_paths_stale = True

        # Re-evaluate WNS
        verdict = await _post_eval_hook(state, deps, "vivado_place_design")
        if verdict == "IMPROVED":
            logger.info(
                f"[chain-gate] Lightweight validation found IMPROVED for {tool_name}, "
                f"re-running full chain"
            )
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[CHAIN GATE] Lightweight validation found WNS improvement after "
                    f"{tool_name}. Running full chain (create_pblock + place + route)."
                )
            # Re-run the skill to get fresh data, then execute chain
            skill_result = await call_tool_fn(
                tool_name, {},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            skill_data = json.loads(skill_result) if isinstance(skill_result, str) else {}
            await _execute_chain_actions(state, deps, tool_name,
                                         skill_data if isinstance(skill_data, dict) else {},
                                         tools_called)
        else:
            logger.info(
                f"[chain-gate] Lightweight validation confirms UNCHANGED for {tool_name}, "
                f"chain fully skipped"
            )
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[CHAIN GATE] Lightweight validation confirmed UNCHANGED — "
                    f"full chain skipped. Saved ~180s."
                )
    except Exception as e:
        logger.warning(f"[chain-gate] Lightweight validation failed: {e}, falling back to full chain")
        # If validation itself fails, run the full chain as fallback
        skill_result = await call_tool_fn(
            tool_name, {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        skill_data = json.loads(skill_result) if isinstance(skill_result, str) else {}
        await _execute_chain_actions(state, deps, tool_name,
                                     skill_data if isinstance(skill_data, dict) else {},
                                     tools_called)


async def _execute_chain_actions(state, deps, tool_name, skill_result_data, tools_called):
    """Auto-execute chained MCP tools after a skill completes.

    Workflow:
      1. Save pre-chain checkpoint to /tmp/pre_chain_pblock.dcp for rollback
      2. Iterate through SKILL_CHAIN_ACTIONS[tool_name] steps:
         - Resolve args_from_skill from skill_result_data
         - Call each MCP tool via call_tool_fn
         - Track WNS changes via _track_wns_from_result
         - Mark critical_paths_stale = True for placement-affecting tools
      3. On any step failure:
         - Restore from pre-chain checkpoint
         - Break (do not continue with remaining steps)
         - Inject [AUTO-CHAIN ERROR] notification into LLM context
    """
    chain = SKILL_CHAIN_ACTIONS.get(tool_name)
    if not chain:
        return

    # Capture pre-chain WNS baseline (before Vivado opens the skill's DCP).
    # Used by the Level 2 place-only check to compare against post-place timing.
    chain_baseline_wns = state.timing.latest_wns

    # Save pre-chain state for rollback on failure
    pre_chain_path = None
    try:
        pre_ckpt_result = await call_tool_fn(
            "vivado_write_checkpoint", {"dcp_path": "/tmp/pre_chain_pblock.dcp", "force": True},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        pre_chain_path = "/tmp/pre_chain_pblock.dcp"
    except Exception as e:
        logger.warning(f"[chain] Could not save pre-chain checkpoint: {e}")

    for step in chain:
        target_tool = step["tool"]
        args = dict(step.get("args", {}))
        for key, skill_key in step.get("args_from_skill", {}).items():
            if isinstance(skill_key, str) and skill_key in skill_result_data:
                args[key] = skill_result_data[skill_key]
            elif isinstance(skill_key, bool):
                args[key] = skill_key

        # Handle route reuse: keep -reuse only if design has been routed
        if args.pop("reuse", False):
            if state.timing.route_status and state.timing.route_status.get("total_nets", 0) > 0:
                args["reuse"] = True
                logger.info("[chain] Route reuse enabled (design has prior routing)")
            else:
                logger.info("[chain] Route reuse requested but no prior routing — falling back to normal route")

        try:
            logger.info(f"[chain] Auto-executing {target_tool} after {tool_name}")
            raw_result = await call_tool_fn(
                target_tool, args,
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            summary = summarize_tool_result(
                target_tool, raw_result,
                latest_wns=state.timing.latest_wns,
                latest_tns=state.timing.latest_tns,
                latest_failing_endpoints=state.timing.latest_failing_endpoints,
                prev_best_wns=state.timing.prev_best_wns,
            )
            # Determine chain step status from raw_result (JSON error check)
            step_failed = False
            try:
                parsed = json.loads(raw_result) if isinstance(raw_result, str) else {}
                if isinstance(parsed, dict) and "error" in parsed:
                    step_failed = True
            except (json.JSONDecodeError, TypeError):
                pass
            # Also detect Vivado ERROR messages in plain-text tool output
            # (place_design/route_design return text, not JSON)
            if not step_failed and isinstance(raw_result, str):
                if re.search(r'^ERROR: \[', raw_result, re.MULTILINE):
                    step_failed = True
            status_label = "failed" if step_failed else "completed"
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[AUTO-CHAIN] After {tool_name}: {target_tool} {status_label} — {summary[:400]}")
            if step_failed:
                raise RuntimeError(f"{target_tool} reported error in result: {summary[:200]}")
            _track_wns_from_result(state, target_tool, raw_result)

            # Update current_dcp_path after opening a new checkpoint
            if target_tool == "vivado_open_checkpoint" and "dcp_path" in args:
                state.control.current_dcp_path = Path(args["dcp_path"]).resolve()

            # Mark critical paths stale after placement-affecting chain tools
            if target_tool in ("vivado_place_design", "vivado_create_and_apply_pblock"):
                state.timing.critical_paths_stale = True

            # ── Level 2: Vivado Place-Only timing check ──────────────
            # After a real place_design (not unplace), evaluate place-only
            # WNS via a timing report. If it shows regression vs the pre-skill
            # baseline, skip the remaining route_design step(s) — place-level
            # regression is unlikely to be fixed by routing.
            is_unplace = (target_tool == "vivado_place_design"
                          and args.get("directive", "").lower() == "unplace")
            if (PLACE_ONLY_CHECK_ENABLED
                    and target_tool == "vivado_place_design"
                    and not is_unplace
                    and tool_name in PLACE_ONLY_CHECK_SKILLS
                    and deps.vivado_session
                    and chain_baseline_wns is not None):
                try:
                    po_result = await call_tool_fn(
                        "vivado_report_timing_summary", {},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                    po_timing = parse_timing_summary(po_result)
                    po_wns = po_timing.get("wns")
                    # Guard: skip place-only WNS check if design is not actually placed.
                    # An unplaced design (Design State: "Optimized") reports estimated
                    # delays that are falsely optimistic.
                    po_design_state = ""
                    state_match = re.search(r'Design\s+State\s*:\s*(\w+)', po_result or "")
                    if state_match:
                        po_design_state = state_match.group(1)
                    if po_design_state and po_design_state.lower() not in ("placed", "routed"):
                        logger.warning(
                            f"[PLACE-ONLY] Skipping WNS check for {tool_name}: "
                            f"design state is '{po_design_state}' (not placed/routed). "
                            f"WNS would be based on estimated delays."
                        )
                        if deps.compat is not None:
                            deps.compat.add_message("user",
                                f"[PLACE-ONLY] {tool_name}: design not placed "
                                f"(state={po_design_state}), skipping place-only WNS check."
                            )
                    elif po_wns is not None:
                        po_delta = po_wns - chain_baseline_wns
                        logger.info(
                            f"[PLACE-ONLY] {tool_name}: place-only WNS={po_wns:.3f}ns "
                            f"(delta={po_delta:+.3f}ns vs baseline={chain_baseline_wns:.3f}ns)"
                        )
                        if deps.compat is not None:
                            deps.compat.add_message("user",
                                f"[PLACE-ONLY] {tool_name}: post-place WNS={po_wns:.3f}ns "
                                f"(delta={po_delta:+.3f}ns vs pre-skill). "
                                f"Route step is {'proceeding' if po_delta >= -PLACE_ONLY_REGRESS_THRESHOLD else 'SKIPPED (regression)'}."
                            )
                        if po_delta < -PLACE_ONLY_REGRESS_THRESHOLD:
                            logger.warning(yellow(
                                f"[PLACE-ONLY] Skipping route for {tool_name}: place-only "
                                f"WNS regressed {po_delta:+.3f}ns below baseline "
                                f"(threshold={PLACE_ONLY_REGRESS_THRESHOLD:.3f})"
                            ))
                            # Skip remaining chain steps (typically route_design)
                            # Keep the placed design — no rollback needed.
                            break
                except Exception as e:
                    logger.warning(f"[PLACE-ONLY] Timing check failed: {e}")
                    # Fall through: continue with route (conservative)

            await _try_save_best_checkpoint(state, deps)
            state.iteration.tools_used.append(target_tool)
            tools_called.append(target_tool)
        except Exception as e:
            logger.error(f"[chain] Tool {target_tool} failed: {e}")
            if pre_chain_path:
                try:
                    logger.warning(f"[chain] Restoring from pre-chain checkpoint: {pre_chain_path}")
                    await call_tool_fn(
                        "vivado_open_checkpoint", {"dcp_path": pre_chain_path},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                    state.control.current_dcp_path = Path(pre_chain_path).resolve()
                    # Refresh WNS after restore so state matches Vivado
                    try:
                        restore_result = await call_tool_fn(
                            "vivado_report_timing_summary", {},
                            deps.rapidwright_session, deps.vivado_session,
                            design_size_factor=state.timing.design_size_factor,
                        )
                        restore_timing = parse_timing_summary(restore_result)
                        if isinstance(restore_timing, dict):
                            state.timing.latest_wns = restore_timing.get("wns")
                            if state.timing.latest_wns is not None:
                                logger.info(f"[chain] Post-restore WNS: {state.timing.latest_wns:.3f}")
                            else:
                                logger.warning("[chain] Could not parse WNS from timing after restore")
                        else:
                            state.timing.latest_wns = restore_timing
                            logger.warning("[chain] Could not parse timing after restore")
                    except Exception as timing_err:
                        state.timing.latest_wns = None
                        logger.warning(f"[chain] Timing report after restore failed: {timing_err}")
                except Exception as restore_err:
                    logger.error(f"[chain] Pre-chain restore also failed: {restore_err}")
            if deps.compat is not None:
                deps.compat.add_message("user",
                    f"[AUTO-CHAIN ERROR] {target_tool} failed, design restored to pre-chain state.")
            break


async def _auto_refresh_critical_paths(state: OptimizerState, deps: NodeDeps) -> None:
    """Re-extract critical paths after layout/routing changes."""
    result = await call_tool_fn(
        "vivado_extract_critical_path_cells",
        {"num_paths": 10},
        deps.rapidwright_session, deps.vivado_session,
        design_size_factor=state.timing.design_size_factor,
    )
    cell_paths = parse_critical_path_cells(result)
    if cell_paths:
        update_critical_paths(state, cell_paths, iteration=state.iteration.current)
        state.timing.critical_paths_stale = False
        logger.info(f"[EXECUTE] Auto-refreshed {len(cell_paths)} critical paths")
    # cell_paths 为空时不修改 stale 标志 — 下次触发时重试


def _compute_adaptive_pblock_multiplier(state) -> float:
    """Compute adaptive PBLOCK resource_multiplier using Formula C.

    M = max(1.10, 1.2 + util_local x 0.3 - 0.1 x log10(N_LUT))
    Clamp: [1.10, 1.50]

    Uses global utilization as fallback when local utilization is unavailable.
    When PBLOCK is retried (same iteration or previous iterations), the
    multiplier is varied to explore different packing densities.
    """
    # Local utilization (preferred) — fallback to global if not available
    util = _get_local_pblock_utilization(state)
    if util is None:
        lut_used = state.timing.resource_utilization.get("LUT", 0) if state.timing.resource_utilization else 0
        lut_total = state.timing.device_capacity.get("LUT", 1) if state.timing.device_capacity else 1
        util = lut_used / max(lut_total, 1)

    # Size-aware decay: larger modules need less relative multiplier
    lut_count = state.timing.resource_utilization.get("LUT", 0) if state.timing.resource_utilization else 0
    size_penalty = 0.1 * math.log10(max(lut_count, 1))

    multiplier = 1.2 + util * 0.3 - size_penalty
    base = max(1.10, min(1.50, multiplier))

    # Variation: add +0.1 per previous PBLOCK attempt, cycling through
    # [base, base+0.1, base+0.2, ...] up to 1.50, then wrapping back.
    prev = state.context.previous_pblock_multipliers
    n_attempts = len(prev)
    if n_attempts > 0:
        offset = 0.1 * n_attempts
        candidate = base + offset
        # Wrap around if we exceed the ceiling
        if candidate > 1.50:
            candidate = 1.10 + (candidate - 1.10) % 0.40
        base = max(1.10, min(1.50, candidate))

    state.context.previous_pblock_multipliers.append((state.iteration.current, base))
    return base


def _get_local_pblock_utilization(state) -> float | None:
    """Extract local utilization within the critical-path bounding box.

    TODO: Implement by querying cell coordinates from RapidWright.
    Returns None to fall back to global utilization.
    """
    return None
