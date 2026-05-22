"""Pure functions for formatting optimization trajectory summaries.

Stateless — takes OptimizerState, returns formatted text and structured data.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState


def format_trajectory_summary(state: OptimizerState) -> dict:
    """Format optimization trajectory for console output and dashboard.

    Returns dict with:
        console_text: pre-formatted text block for stdout
        wns_records: per-iteration WNS changes
        tool_calls: tool call summary stats
        strategy_sequence: ordered strategy labels
        flow_control_log: flow control decision entries
        phase_history: phase transition entries
    """
    lines: list[str] = []

    # ── Header ──
    lines.append("")
    lines.append("=" * 70)
    lines.append("  Optimization Trajectory")
    lines.append("=" * 70)

    # ── 1. WNS Change Trajectory ──
    lines.append("")
    lines.append("─" * 70)
    lines.append("  1. WNS Change Trajectory (per iteration)")
    lines.append("─" * 70)

    narratives = state.iteration.narratives
    wns_records: list[dict] = []
    if narratives:
        lines.append(f"  {'Iter':>4}  {'WNS Before':>10}  {'WNS After':>10}  {'Delta':>9}  {'Strategy':<16}  {'Outcome':<12}  {'Model':<20}")
        lines.append(f"  {'─'*4}  {'─'*10}  {'─'*10}  {'─'*9}  {'─'*16}  {'─'*12}  {'─'*20}")
        for n in narratives:
            wns_before = _fmt_wns(n.get("wns_before"))
            wns_after = _fmt_wns(n.get("wns_after"))
            delta = n.get("wns_delta")
            delta_str = f"{delta:+.3f}" if isinstance(delta, (int, float)) else "--"
            strategy = str(n.get("strategy_label", ""))[:16]
            outcome = str(n.get("outcome", ""))[:12]
            model = str(n.get("model_used", ""))[:20]
            iteration = n.get("iteration", 0)
            tools = n.get("tools_used", [])
            lines.append(f"  {iteration:>4}  {wns_before:>10}  {wns_after:>10}  {delta_str:>9}  {strategy:<16}  {outcome:<12}  {model:<20}")
            wns_records.append({
                "iteration": iteration,
                "wns_before": wns_before,
                "wns_after": wns_after,
                "wns_delta": delta,
                "strategy_label": strategy,
                "outcome": outcome,
                "model_used": model,
                "tools_used": list(tools) if tools else [],
            })
    else:
        lines.append("  (no WNS records)")

    # ── 2. Tool Call Statistics ──
    lines.append("")
    lines.append("─" * 70)
    lines.append("  2. Tool Call Statistics")
    lines.append("─" * 70)

    tool_trace = state.context.tool_call_trace
    tool_calls: list[dict] = []
    if tool_trace:
        total = len(tool_trace)
        errors = sum(1 for t in tool_trace if t.status == "error")
        total_dur = sum(t.elapsed_seconds for t in tool_trace)
        lines.append(f"  Total: {total}  |  Errors: {errors}  |  Total Duration: {total_dur:.1f}s")
        lines.append("")

        counter = Counter(t.tool_name for t in tool_trace)
        top_n = 8
        lines.append(f"  Top {top_n} most-used tools:")
        for i, (name, count) in enumerate(counter.most_common(top_n), 1):
            lines.append(f"    {i:>2}. {name:<40} x{count}")
        tool_calls = [
            {
                "tool_name": t.tool_name,
                "iteration": t.iteration,
                "tool_round": t.tool_round,
                "elapsed_seconds": t.elapsed_seconds,
                "status": t.status,
                "summary": t.summary[:200] if t.summary else "",
            }
            for t in tool_trace
        ]
    else:
        lines.append("  (no tool calls)")

    # ── 3. Strategy Sequence ──
    lines.append("")
    lines.append("─" * 70)
    lines.append("  3. Strategy Sequence")
    lines.append("─" * 70)

    strategy_sequence = state.iteration.strategy_sequence
    if strategy_sequence:
        for i, s in enumerate(strategy_sequence, 1):
            lines.append(f"  {i:>3}. {s}")
    else:
        lines.append("  (no strategy sequence)")

    # ── 4. Flow Control Decisions ──
    lines.append("")
    lines.append("─" * 70)
    lines.append("  4. Flow Control Decisions")
    lines.append("─" * 70)

    flow_log = state.context.flow_control_log
    flow_records: list[dict] = []
    if flow_log:
        lines.append(f"  {'Time':<10}  {'Iter':>4}  {'Round':>5}  {'Signal':<18}  {'WNS':>8}  {'Reason'}")
        lines.append(f"  {'─'*10}  {'─'*4}  {'─'*5}  {'─'*18}  {'─'*8}  {'─'*30}")
        for f in flow_log:
            ts = datetime.fromtimestamp(f.timestamp).strftime("%H:%M:%S")
            wns = _fmt_wns(f.wns_at_decision)
            lines.append(f"  {ts:<10}  {f.iteration:>4}  {f.tool_round:>5}  {f.signal:<18}  {wns:>8}  {f.done_reason}")
            flow_records.append({
                "timestamp": f.timestamp,
                "time_str": ts,
                "iteration": f.iteration,
                "tool_round": f.tool_round,
                "signal": f.signal,
                "wns_at_decision": f.wns_at_decision,
                "done_reason": f.done_reason,
            })
    else:
        lines.append("  (no flow control decisions)")

    # ── 5. Strategy Phase Transitions (last 20) ──
    lines.append("")
    lines.append("─" * 70)
    lines.append("  5. Strategy Phase Transitions (last 20)")
    lines.append("─" * 70)

    phase_history = state.strategy.phase_history
    phase_records: list[dict] = []
    if phase_history:
        recent = phase_history[-20:]
        lines.append(f"  {'Time':<10}  {'Iter':>4}  {'Round':>5}  {'Phase':<18}  {'Strategy':<16}  {'WNS at Entry':>12}")
        lines.append(f"  {'─'*10}  {'─'*4}  {'─'*5}  {'─'*18}  {'─'*16}  {'─'*12}")
        for p in recent:
            ts = datetime.fromtimestamp(p.timestamp).strftime("%H:%M:%S")
            wns = _fmt_wns(p.wns_at_entry)
            lines.append(f"  {ts:<10}  {p.iteration:>4}  {p.tool_round:>5}  {p.phase:<18}  {p.strategy:<16}  {wns:>12}")
            phase_records.append({
                "timestamp": p.timestamp,
                "time_str": ts,
                "iteration": p.iteration,
                "tool_round": p.tool_round,
                "phase": p.phase,
                "strategy": p.strategy,
                "wns_at_entry": p.wns_at_entry,
            })
    else:
        lines.append("  (no phase transitions)")

    lines.append("")
    lines.append("=" * 70)
    lines.append("")

    return {
        "console_text": "\n".join(lines),
        "wns_records": wns_records,
        "tool_calls": tool_calls,
        "strategy_sequence": list(strategy_sequence),
        "flow_control_log": flow_records,
        "phase_history": phase_records,
    }


def _fmt_wns(val: float | None) -> str:
    """Format WNS value for display."""
    if val is None:
        return "N/A"
    return f"{val:.3f}"
