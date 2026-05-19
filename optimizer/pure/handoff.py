"""Handoff prompt generation pure functions.

Extracted from dcp_optimizer.py: _generate_planner_handoff (L3071-3121),
_generate_worker_handoff (L3123-3174), _build_data_driven_goal (L2997-3069).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .iteration_logic import infer_strategy_from_tools
from .context_snapshot import _format_narrative_summary

if TYPE_CHECKING:
    from ..state import OptimizerState

logger = logging.getLogger(__name__)


def build_data_driven_goal(
    state: OptimizerState,
    current_wns: float | None,
    tool_call_details: list[dict],
) -> str:
    """Build data-driven next goal from WNS trajectory and strategy effects."""
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None

    if not state.iteration.narratives:
        if best_wns is not None and best_wns >= 0:
            return "WNS target met. Focus on further optimization."
        elif best_wns is not None and best_wns > -0.5:
            return "Close to target. Fine-tuning critical paths."
        elif best_wns is not None and best_wns > -2.0:
            return "Moderate violation. Consider phys_opt or PBLOCK."
        else:
            return "Severe violation. Consider aggressive strategies."

    recent = state.iteration.narratives[-5:]
    improved = [e for e in recent if e.get('outcome') == 'improved']
    regressed = [e for e in recent if e.get('outcome') == 'regression']

    if best_wns is not None and best_wns >= 0:
        return "WNS target met. Focus on further optimization."
    elif not improved and regressed:
        return f"No improvement in {len(recent)} iters. Rollback to best checkpoint and try alternative strategy."
    elif improved:
        last_improved_tools = [
            t.get('tool_name', '')
            for t in tool_call_details
            if t.get('iteration') == improved[-1].get('iteration')
        ]
        strategy = infer_strategy_from_tools(last_improved_tools)
        return f"Last success via {strategy}. Continue or refine approach."
    elif best_wns is not None and best_wns > -2.0:
        return "Moderate violation. Consider phys_opt or PBLOCK."
    else:
        return "Severe violation. Consider aggressive strategies."


def build_stagnation_signal(state: OptimizerState) -> str:
    """Build stagnation warning signal if optimization is stuck."""
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None
    if state.iteration.global_no_improvement >= 1 and (best_wns is None or best_wns < 0):
        current_wns = state.timing.latest_wns
        current_wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "unknown"
        return (
            f"STAGNATION DETECTED: {state.iteration.global_no_improvement} consecutive iterations without improvement. "
            f"Current WNS={current_wns_str}. Your current approach is NOT WORKING.\n"
            f"STOP executing optimization strategies. You MUST initiate a fresh diagnosis cycle:\n"
            f"1. Gather current timing data (report_timing_summary, extract_critical_path_cells)\n"
            f"2. Analyze what has changed and why prior strategies failed\n"
            f"3. Form a new hypothesis about the dominant timing obstacle\n"
            f"4. Select a strategy that has NOT been tried yet\n"
            f"Do NOT repeat the same pattern."
        )
    return ""


def build_handoff_prompt(
    state: OptimizerState,
    tier: str,
    tool_call_details: list[dict],
    failed_strategies: list[dict],
    current_wns: float | None,
) -> str:
    """Generate model-tier-specific handoff prompt.

    Args:
        state: Current optimizer state.
        tier: "planner" or "worker".
        tool_call_details: Recent tool call details.
        failed_strategies: List of failed strategy dicts.
        current_wns: Current WNS value.
    """
    if tier == "planner":
        return _generate_planner_handoff(state, tool_call_details, failed_strategies, current_wns)
    else:
        return _generate_worker_handoff(state, tool_call_details, failed_strategies, current_wns)


def _format_failed_strategies(failed_strategies: list[dict]) -> str:
    """Format failed strategies list into readable text."""
    if not failed_strategies:
        return "(none)"
    lines = []
    for fs in failed_strategies[-5:]:
        name = fs.get("strategy", "?")
        reason = fs.get("reason", "unknown")
        lines.append(f"- {name}: {reason}")
    return "\n".join(lines)


def _format_recent_tools(tool_call_details: list[dict], iteration: int) -> str:
    """Format recent tool calls for this iteration."""
    tools = [t for t in tool_call_details if t.get('iteration') == iteration]
    if not tools:
        return "(none)"
    lines = []
    for t in tools[-10:]:
        name = t.get('tool_name', '?')
        wns = t.get('wns')
        error = t.get('error', False)
        status = "ERROR" if error else "OK"
        wns_str = f" WNS={wns:.3f}" if wns is not None else ""
        lines.append(f"- {name}: {status}{wns_str}")
    return "\n".join(lines)


def _generate_planner_handoff(
    state: OptimizerState,
    tool_call_details: list[dict],
    failed_strategies: list[dict],
    current_wns: float | None,
) -> str:
    """Generate rich handoff for planner models (1M context)."""
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None
    best_wns_iter = state.timing.best_wns_iteration
    clock_period = state.timing.clock_period

    recent_tools = _format_recent_tools(tool_call_details, state.iteration.current)
    failed_str = _format_failed_strategies(failed_strategies)
    narrative_lines = _format_narrative_summary(state.iteration.narratives, max_entries=10)
    narrative = "\n".join(narrative_lines) if narrative_lines else "(no history)"
    goal = build_data_driven_goal(state, current_wns, tool_call_details)
    stagnation = build_stagnation_signal(state)
    stagnation_section = f"\n=== STAGNATION SIGNAL ===\n{stagnation}\n" if stagnation else ""

    wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "N/A"
    best_str = f"{best_wns:.3f}ns" if best_wns is not None else "N/A"
    best_iter_str = f"iter {best_wns_iter}" if best_wns_iter is not None else "N/A"
    clock_str = f"{clock_period:.3f}ns" if clock_period is not None else "N/A"

    return f"""**ITERATION HANDOFF - Planner**

=== ITERATION TRAJECTORY ===
{narrative}

=== CURRENT STATE ===
- Iteration: {state.iteration.current} -> {state.iteration.current + 1}
- Current WNS: {wns_str}
- Best WNS: {best_str} ({best_iter_str})
- Clock Period: {clock_str}

=== NEXT OPTIMIZATION GOAL ===
{goal}

=== LAST ITERATION TOOLS ===
{recent_tools}

=== FAILED STRATEGIES ===
{failed_str}
{stagnation_section}
Current WNS/checkpoint/clock values are in the system prompt 'Current Optimization State' section."""


def _generate_worker_handoff(
    state: OptimizerState,
    tool_call_details: list[dict],
    failed_strategies: list[dict],
    current_wns: float | None,
) -> str:
    """Generate lean handoff for worker models (250K context)."""
    best_wns = state.timing.best_wns if state.timing.best_wns > float('-inf') else None
    best_wns_iter = state.timing.best_wns_iteration
    clock_period = state.timing.clock_period

    recent_tools = _format_recent_tools(tool_call_details, state.iteration.current)
    failed_str = _format_failed_strategies(failed_strategies)
    narrative_lines = _format_narrative_summary(state.iteration.narratives, max_entries=3)
    narrative = "\n".join(narrative_lines) if narrative_lines else "(no history)"
    goal = build_data_driven_goal(state, current_wns, tool_call_details)
    stagnation = build_stagnation_signal(state)
    stagnation_section = f"\n=== STAGNATION SIGNAL ===\n{stagnation}\n" if stagnation else ""

    wns_str = f"{current_wns:.3f}ns" if current_wns is not None else "N/A"
    best_str = f"{best_wns:.3f}ns" if best_wns is not None else "N/A"
    best_iter_str = f"iter{best_wns_iter}" if best_wns_iter is not None else "N/A"
    clock_str = f"{clock_period:.3f}ns" if clock_period is not None else "N/A"

    return f"""**ITERATION HANDOFF - Worker**

=== RECENT TRAJECTORY (last 3) ===
{narrative}

=== STATE ===
- Iter: {state.iteration.current} -> {state.iteration.current + 1} | WNS: {wns_str} | Best: {best_str} ({best_iter_str}) | Clock: {clock_str}

=== GOAL ===
{goal}

=== LAST ITERATION TOOLS ===
{recent_tools}

=== AVOID ===
{failed_str}
{stagnation_section}
Current WNS/checkpoint/clock values are in the system prompt 'Current Optimization State' section."""
