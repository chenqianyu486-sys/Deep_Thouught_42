"""Execution-phase contract helpers.

Keeps small, tool-specific EXECUTE rules out of ``phase_execute.py`` so the
phase consumes declarative helpers instead of re-encoding verdict logic.
"""

from __future__ import annotations

import json
import re

from optimizer.pure.pblock_plan import (
    PBLOCK_UNPLACE_GLOBAL,
    extract_selected_plan_from_payload,
    get_place_only_screening_threshold,
    order_candidate_execution_plans,
)
from optimizer.pure.tool_catalog import POST_EVAL_TOOLS
from optimizer.pure.tool_chain_policy import (
    KNOWN_BROKEN_DIRECTIVES,
    STRATEGY_DEFAULT_DIRECTIVES,
)
from optimizer.pure.tool_contracts import coerce_payload_dict


WNS_VERDICT_EPSILON = 0.001
NON_IMPROVING_VERDICTS: frozenset[str] = frozenset({"UNCHANGED", "REGRESSED"})

# P1 ②A: keys that distinguish a strategy's adjustable parameter combo. Only
# directive/multiplier-bearing strategies get a non-empty signature; data-only
# strategies (Fanout, NetSwap, CellReplication) return "" -> strategy-level
# failure (blocks the whole strategy, current behavior).
_PARAM_SIGNATURE_KEYS = ("directive", "place_directive", "route_directive", "resource_multiplier")


def compute_param_signature(strategy: str, tool_args: dict | None) -> str:
    """Compute a stable signature for the strategy's adjustable parameter combo.

    Returns "" for strategies without directive/multiplier params (strategy-level
    failures). For directive-bearing strategies, returns ``key=val|...`` sorted by
    key, with resource_multiplier quantized to 1 decimal to avoid float noise.
    Two calls with the same directive combo yield the same signature; a different
    directive combo yields a different signature (so a failed combo does not block
    retrying the strategy with a different combo).
    """
    if not tool_args:
        return ""
    parts = []
    for key in _PARAM_SIGNATURE_KEYS:
        if key not in tool_args:
            continue
        val = tool_args[key]
        if val is None:
            continue
        if key == "resource_multiplier":
            try:
                val = f"{float(val):.1f}"
            except (TypeError, ValueError):
                continue
        else:
            val = str(val)
        parts.append(f"{key}={val}")
    return "|".join(parts)


def combo_is_cooled(
    failed_strategies: list,
    strategy: str,
    param_signature: str,
    current_iter: int,
) -> tuple[bool, int]:
    """P1 ②A: is this (strategy, param_signature) combo in cooldown?

    Returns (is_cooled, remaining_iters). A combo is cooled when a prior
    tool_error escalation produced a strategy_ineffective record with the same
    param_signature whose TTL has not expired. Used by the EXECUTE combo guard
    to skip re-running an exhausted combo while letting the LLM retry the same
    strategy with a different combo.

    Pure (operates on the failed_strategies list + scalars) so it is unit-testable
    without the EXECUTE async loop.
    """
    if not param_signature:
        return False, 0
    for f in failed_strategies or []:
        if (
            f.strategy == strategy
            and f.param_signature == param_signature
            and f.reason == "strategy_ineffective"
            and current_iter < f.blocked_until_iter
        ):
            return True, f.blocked_until_iter - current_iter
    return False, 0


POST_EVAL_EXIT_VERDICTS: frozenset[str] = frozenset({
    "IMPROVED",
    "UNCHANGED",
    "REGRESSED",
})

# Some tools need follow-up dashboard/critical-path refresh after their chain
# completes because the chain, not the skill call itself, produces the real
# routed design state consumed by later phases.
POST_CHAIN_CRITICAL_PATH_REFRESH_TOOLS: frozenset[str] = frozenset({
    "rapidwright_execute_pblock_strategy",
})

CHAIN_DIRECTIVE_TOOLS: frozenset[str] = frozenset({
    "vivado_place_design",
    "vivado_route_design",
})


def verdict_from_wns_delta(delta: float, epsilon: float = WNS_VERDICT_EPSILON) -> str:
    """Classify a WNS delta into the canonical EXECUTE/EVALUATE verdicts."""
    if delta > epsilon:
        return "IMPROVED"
    if abs(delta) <= epsilon:
        return "UNCHANGED"
    return "REGRESSED"


def verdict_from_wns_values(
    previous_wns: float | None,
    current_wns: float,
    epsilon: float = WNS_VERDICT_EPSILON,
) -> tuple[str, float]:
    """Return (verdict, delta) for a previous/current WNS comparison."""
    delta = current_wns - previous_wns if previous_wns is not None else 0.0
    return verdict_from_wns_delta(delta, epsilon=epsilon), delta


def should_block_strategy(verdict: str | None) -> bool:
    """Whether a verdict should block the current strategy for this iteration."""
    return verdict in NON_IMPROVING_VERDICTS


def should_skip_reopen(
    current_dcp_path: object | None,
    resolved_target_path: str,
    live_design_dirty: bool,
) -> bool:
    """Whether _reload_baseline_on_switch may skip reopening the checkpoint.

    Skip only when the target checkpoint is already loaded AND the in-memory
    design is clean. A dirty design (a prior strategy ran place/route/opt
    without saving best) means Vivado memory diverged from ``current_dcp_path``
    even though the path still matches - reopening is then mandatory, or the
    dirty design's WNS pollutes the next strategy's baseline
    (run-20260711_015650: -0.602 reported instead of real best -0.542).
    """
    if live_design_dirty:
        return False
    if not current_dcp_path:
        return False
    return str(current_dcp_path) == str(resolved_target_path)


def build_post_eval_guidance(tool_name: str, verdict: str | None) -> str | None:
    """Return follow-up guidance after a post-eval verdict, if any."""
    if tool_name in POST_EVAL_TOOLS and verdict == "UNCHANGED":
        return (
            f"[GUIDANCE] {tool_name} produced no WNS improvement. "
            f"EXECUTE will yield to EVALUATE for strategy selection."
        )
    return None


def build_timing_update_exit_contract(
    tool_name: str,
    post_eval_verdict: str | None,
    *,
    target_met: bool,
) -> dict | None:
    """Return the structured EXECUTE exit contract after timing updates."""
    if target_met:
        return {
            "flow_signal": "DONE",
            "reason": "wns_target_met",
        }
    if tool_name in POST_EVAL_TOOLS and post_eval_verdict in POST_EVAL_EXIT_VERDICTS:
        return {
            "flow_signal": "EXEC_DONE",
            "reason": f"post_eval_{post_eval_verdict.lower()}",
        }
    return None


def should_exit_for_large_regression(
    latest_wns: float | None,
    best_wns: float,
    *,
    margin: float = 0.5,
) -> bool:
    """Whether EXECUTE should stop because timing regressed too far below best."""
    if latest_wns is None or best_wns == float("-inf"):
        return False
    return latest_wns < best_wns - margin


def next_no_progress_count(
    current_count: int,
    *,
    had_tool_calls: bool,
    round_had_side_effect: bool = False,
    pending_tool_count: int = 0,
) -> int:
    """Advance the no-progress counter for the current EXECUTE round."""
    if not had_tool_calls:
        return current_count + 1
    if round_had_side_effect:
        return 0
    if pending_tool_count <= 0:
        return current_count + 1
    return current_count


def should_exit_for_no_progress(no_progress_count: int, *, limit: int) -> bool:
    """Whether the current no-progress streak should end EXECUTE."""
    return no_progress_count >= limit


def next_empty_response_streak(
    current_streak: int,
    *,
    assistant_content: str,
    has_tool_calls: bool,
) -> int:
    """Advance the empty-response streak for the current LLM round."""
    if not assistant_content.strip() and not has_tool_calls:
        return current_streak + 1
    return 0


def should_exit_for_empty_responses(streak: int, *, limit: int = 2) -> bool:
    """Whether repeated empty assistant responses should end EXECUTE."""
    return streak >= limit


def detect_format_guard_violation(
    *,
    assistant_content: str,
    has_tool_calls: bool,
    has_step_state: bool,
) -> bool:
    """Detect a FORMAT_GUARD violation: the LLM returned text but called no
    tool, so it did not call report_step_state.

    Such a response is not a valid decision (no report_step_state means no
    strategy selection / flow control was recorded). The caller should retry
    without persisting the violating message, so the next round does not see
    its own unanswered "decision" text. has_tool_calls=False implies no
    report_step_state, but has_step_state is checked explicitly for clarity.
    """
    return bool(assistant_content.strip()) and not has_tool_calls and not has_step_state


def resolve_selected_pblock_plan(skill_result_data: dict | None):
    """Resolve the typed PBLOCK plan from the skill payload."""
    return extract_selected_plan_from_payload(skill_result_data)


def resolve_ordered_pblock_candidates(
    skill_result_data: dict | None,
    *,
    attempted_candidate_ids: list[str] | None = None,
):
    """Resolve ordered PBLOCK candidates for the current attempt."""
    if not isinstance(skill_result_data, dict):
        return []
    return order_candidate_execution_plans(
        skill_result_data.get("candidate_plans"),
        recommended_candidate_id=skill_result_data.get("recommended_candidate_id"),
        attempted_candidate_ids=attempted_candidate_ids,
    )


def get_pblock_place_only_threshold(skill_result_data: dict | None) -> float | None:
    """Return the fixed screening threshold for the selected PBLOCK plan."""
    plan = resolve_selected_pblock_plan(skill_result_data)
    if plan is None:
        return None
    return get_place_only_screening_threshold(plan.plan_mode)


def extract_skill_precheck_diagnostics(raw_result: object) -> tuple[bool, str]:
    """Extract pre-check skip diagnostics from a skill result payload."""
    skill_data = coerce_payload_dict(raw_result)
    if not isinstance(skill_data, dict) or skill_data.get("status") != "skipped":
        return False, ""

    analysis = skill_data.get("analysis_summary", {}) or {}
    if not isinstance(analysis, dict):
        return True, ""

    diagnosis = analysis.get("diagnosis", "no_match")
    cell_types = analysis.get("cell_type_distribution", {})
    if isinstance(cell_types, dict):
        top_cells = dict(sorted(cell_types.items(), key=lambda item: -item[1])[:5])
    else:
        top_cells = {}
    return (
        True,
        f"critical path cell types: {top_cells}, diagnosis: {diagnosis}",
    )


def build_precheck_failure_contract(
    tool_name: str,
    precheck_verdict: str | None,
    *,
    skill_was_skipped: bool = False,
    skill_diagnostics: str = "",
) -> dict | None:
    """Return structured handling for a decisive RW pre-check outcome."""
    if precheck_verdict == "REGRESS":
        return {
            "done_reason": "precheck_direction_regress",
            "failure_reason": "strategy_ineffective",
            "failure_detail": "precheck_direction_regress",
            "user_message": (
                f"[PRECHECK] {tool_name}: RapidWright timing estimate "
                f"shows directional WNS regression. Skipping Vivado "
                f"place+route chain. Strategy marked as ineffective."
            ),
        }
    if precheck_verdict == "UNCHANGED":
        return {
            "done_reason": "precheck_direction_unchanged",
            "failure_reason": "strategy_ineffective",
            "failure_detail": "precheck_direction_unchanged",
            "user_message": (
                f"[PRECHECK] {tool_name}: RapidWright timing estimate "
                f"shows no directional WNS change (delta within dead "
                f"band). Skipping Vivado place+route chain - the skill "
                f"produced no measurable benefit. Strategy marked as "
                f"ineffective."
            ),
        }
    if precheck_verdict == "NO_WORK":
        if skill_was_skipped and skill_diagnostics:
            return {
                "done_reason": "precheck_no_work",
                "failure_reason": "strategy_not_applicable",
                "failure_detail": f"no_applicable_cells: {skill_diagnostics}",
                "user_message": (
                    f"[PRECHECK] {tool_name}: {skill_diagnostics}. "
                    f"Strategy is not applicable to current critical path "
                    f"architecture - try a different strategy type."
                ),
            }
        return {
            "done_reason": "precheck_no_work",
            "failure_reason": "strategy_ineffective",
            "failure_detail": "precheck_no_work",
            "user_message": (
                f"[PRECHECK] {tool_name}: RapidWright timing estimate "
                f"shows no WNS change. Skipping Vivado place+route chain. "
                f"Strategy marked as ineffective."
            ),
        }
    return None


def should_recompute_chain_verdict(
    tool_name: str,
    prior_verdict: str | None,
    pre_tool_wns: float | None,
    latest_wns: float | None,
    epsilon: float = WNS_VERDICT_EPSILON,
) -> tuple[bool, str | None, float | None]:
    """Return whether a chained tool should overwrite a stale UNCHANGED verdict."""
    if prior_verdict != "UNCHANGED":
        return False, None, None
    if pre_tool_wns is None or latest_wns is None:
        return False, None, None
    delta = latest_wns - pre_tool_wns
    if abs(delta) <= epsilon:
        return False, None, delta
    return True, verdict_from_wns_delta(delta, epsilon=epsilon), delta


def tool_requires_post_chain_path_refresh(tool_name: str) -> bool:
    """Whether a tool's auto-chain should force a critical-path refresh."""
    return tool_name in POST_CHAIN_CRITICAL_PATH_REFRESH_TOOLS


def _default_chain_directive(tool_name: str, target_tool: str) -> str | None:
    """Return the strategy-default directive for a place/route chain step."""
    defaults = STRATEGY_DEFAULT_DIRECTIVES.get(tool_name)
    if not defaults:
        return None
    place_default, route_default = defaults
    if target_tool == "vivado_place_design":
        return place_default
    if target_tool == "vivado_route_design":
        return route_default
    return None


def resolve_chain_step_arguments(
    tool_name: str,
    step: dict,
    skill_result_data: dict | None,
) -> tuple[dict, str | None]:
    """Resolve chain-step args from static step config, skill output, and defaults."""
    target_tool = step["tool"]
    args = dict(step.get("args", {}))
    directive_from_skill = False
    args_from_skill = step.get("args_from_skill", {})

    for key, skill_key in args_from_skill.items():
        if isinstance(skill_key, str) and isinstance(skill_result_data, dict) and skill_key in skill_result_data:
            args[key] = skill_result_data[skill_key]
            if key == "directive":
                directive_from_skill = True
        elif isinstance(skill_key, bool):
            args[key] = skill_key

    if (
        not directive_from_skill
        and target_tool in CHAIN_DIRECTIVE_TOOLS
        and "args_from_skill" in step
    ):
        default_directive = _default_chain_directive(tool_name, target_tool)
        if default_directive:
            args["directive"] = default_directive

    if tool_name == "rapidwright_execute_pblock_strategy" and isinstance(skill_result_data, dict):
        notes: list[str] = []
        selected_plan = resolve_selected_pblock_plan(skill_result_data)
        if selected_plan is not None:
            if target_tool == "vivado_unplace_cells":
                args["cells"] = list(selected_plan.critical_path_cells_snapshot)
            elif target_tool == "vivado_create_and_apply_pblock":
                args["pblock_name"] = selected_plan.pblock_name
                args["ranges"] = selected_plan.pblock_ranges
                args["is_soft"] = selected_plan.is_soft
                if selected_plan.bind_cells_to_pblock:
                    args["cells"] = list(selected_plan.critical_path_cells_snapshot)
                else:
                    args.pop("cells", None)
                    notes.append(
                        "PBLOCK global replacement active: omitting cells binding and forcing the frozen plan settings"
                    )
            elif target_tool == "vivado_place_design":
                args["directive"] = selected_plan.place_directive
            elif target_tool == "vivado_route_design":
                args["directive"] = selected_plan.route_directive
            if selected_plan.fallback_reason:
                notes.append(selected_plan.fallback_reason)
        elif (
            target_tool == "vivado_create_and_apply_pblock"
            and skill_result_data.get("bind_critical_path_cells_to_pblock") is False
        ):
            args.pop("cells", None)
            args["is_soft"] = True
            notes.append(
                "PBLOCK whole-design fallback active: omitting cells binding and forcing IS_SOFT=1"
            )
        fallback_reason = skill_result_data.get("pblock_fallback_reason")
        # Dedupe: in the frozen-plan path selected_plan.fallback_reason (appended
        # above) and the top-level pblock_fallback_reason carry the same text, so
        # appending both produced "reason | reason" warnings (run-20260710_190708).
        if fallback_reason and fallback_reason not in notes:
            notes.append(fallback_reason)
        directive = args.get("directive")
        if directive in KNOWN_BROKEN_DIRECTIVES:
            replacement = _default_chain_directive(tool_name, target_tool)
            if replacement and replacement != directive:
                args["directive"] = replacement
                notes.append(
                    f"[DIRECTIVE] '{directive}' is blacklisted (known licensing issue). "
                    f"Falling back to '{replacement}' for {tool_name}"
                )
            else:
                del args["directive"]
                notes.append(
                    f"[DIRECTIVE] '{directive}' is blacklisted, no fallback found - removing directive"
                )
        return args, " | ".join(notes) if notes else None

    directive = args.get("directive")
    if directive not in KNOWN_BROKEN_DIRECTIVES:
        return args, None

    replacement = _default_chain_directive(tool_name, target_tool)
    if replacement and replacement != directive:
        args["directive"] = replacement
        return (
            args,
            f"[DIRECTIVE] '{directive}' is blacklisted (known licensing issue). "
            f"Falling back to '{replacement}' for {tool_name}",
        )

    del args["directive"]
    return (
        args,
        f"[DIRECTIVE] '{directive}' is blacklisted, no fallback found - removing directive",
    )


def resolve_chain_step_runtime_override(
    tool_name: str,
    target_tool: str,
    args: dict,
    skill_result_data: dict | None,
) -> tuple[str, dict, str | None]:
    """Apply runtime chain-step overrides driven by skill result semantics."""
    selected_plan = resolve_selected_pblock_plan(skill_result_data)
    if (
        tool_name == "rapidwright_execute_pblock_strategy"
        and target_tool == "vivado_unplace_cells"
        and selected_plan is not None
        and selected_plan.unplace_mode == PBLOCK_UNPLACE_GLOBAL
    ):
        reason = selected_plan.fallback_reason or "whole-design fallback active"
        return (
            "vivado_place_design",
            {"directive": "unplace"},
            "PBLOCK whole-design fallback active: replacing local "
            f"unplace_cells with global place_design -unplace | {reason}",
        )
    if (
        tool_name == "rapidwright_execute_pblock_strategy"
        and target_tool == "vivado_unplace_cells"
        and isinstance(skill_result_data, dict)
        and skill_result_data.get("bind_critical_path_cells_to_pblock") is False
    ):
        reason = skill_result_data.get("pblock_fallback_reason") or "whole-design fallback active"
        return (
            "vivado_place_design",
            {"directive": "unplace"},
            "PBLOCK whole-design fallback active: replacing local "
            f"unplace_cells with global place_design -unplace | {reason}",
        )
    return target_tool, args, None


def is_chain_step_failure_result(raw_result: object) -> bool:
    """Detect whether a chain step returned an error payload or Vivado error text."""
    if isinstance(raw_result, dict):
        return "error" in raw_result
    if not isinstance(raw_result, str):
        return False

    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None

    if isinstance(parsed, dict) and "error" in parsed:
        return True
    return bool(re.search(r"^ERROR: \[", raw_result, re.MULTILINE))


def extract_post_eval_metrics(tool_name: str, raw_result: str) -> dict | None:
    """Extract post-eval timing metrics embedded directly in a tool result.

    Currently only ``vivado_physopt_and_route`` returns authoritative post-route
    timing JSON that lets EXECUTE avoid an immediate extra timing-report call.
    """
    if tool_name != "vivado_physopt_and_route" or not raw_result:
        return None
    data = coerce_payload_dict(raw_result)
    if data is None:
        return None
    post = data.get("post_optimization", {})
    if not isinstance(post, dict) or post.get("wns") is None:
        return None
    metrics = {
        "wns": float(post["wns"]),
        "tns": float(post["tns"]) if isinstance(post.get("tns"), (int, float)) else None,
        "failing_endpoints": int(post["failing_endpoints"])
        if isinstance(post.get("failing_endpoints"), (int, float))
        else None,
    }
    return metrics
