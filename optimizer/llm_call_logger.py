"""Dedicated logger for LLM call history with state snapshots.

Records every LLM call to a JSONL file with full request/response data
and a snapshot of the optimizer state at the time of the call.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _extract_snapshot(state) -> dict:
    """Extract key state fields from OptimizerState into a flat dict."""
    # Display strategy: fall back to the most recent phase_history entry when
    # current_strategy is empty (e.g. during ANALYZE after a CONTINUE/ROLLBACK
    # clear). This keeps the log header informative instead of showing a bare
    # "Strategy: " for every ANALYZE call.
    display_strategy = state.strategy.current_strategy
    if not display_strategy and state.strategy.phase_history:
        display_strategy = state.strategy.phase_history[-1].strategy or ""
    return {
        # Timing
        "latest_wns": state.timing.latest_wns,
        "baseline_wns": state.timing.baseline_wns,
        "wns_freshness": state.timing.field_freshness.get("timing_summary", ""),
        "best_wns": state.timing.best_wns,
        "initial_wns": state.timing.initial_wns,
        "latest_tns": state.timing.latest_tns,
        "failing_endpoints": state.timing.latest_failing_endpoints,
        "clock_period": state.timing.clock_period,
        "best_wns_iteration": state.timing.best_wns_iteration,
        # Iteration
        "iteration": state.iteration.current,
        "tool_round": state.iteration.tool_round,
        "max_iterations": state.iteration.max_iterations,
        "global_no_improvement": state.iteration.global_no_improvement,
        "strategy_sequence": list(state.iteration.strategy_sequence),
        # Strategy
        "current_strategy": state.strategy.current_strategy,
        "display_strategy": display_strategy,
        "current_phase": state.strategy.current_phase,
        "evaluation_result": state.strategy.evaluation_result,
        "evaluation_wns_delta": state.strategy.evaluation_wns_delta,
        # Model
        "current_model": state.model.current_model,
        "planner_model": state.model.planner_model,
        "worker_model": state.model.worker_model,
        "llm_call_count": state.model.llm_call_count,
        # Cost
        "total_cost": state.cost.total_cost,
        "total_prompt_tokens": state.cost.total_prompt_tokens,
        "total_completion_tokens": state.cost.total_completion_tokens,
        "total_tokens": state.cost.total_tokens,
        "total_reasoning_tokens": state.cost.total_reasoning_tokens,
        "total_cache_read_tokens": state.cost.total_cache_read_tokens,
        "total_cache_creation_tokens": state.cost.total_cache_creation_tokens,
        # Control
        "is_done": state.control.is_done,
        "done_reason": state.control.done_reason,
        "run_dir": str(state.control.run_dir) if state.control.run_dir else "",
    }


def _serialize_messages(messages: list[dict]) -> list[dict]:
    """Truncate long content fields in messages to keep JSONL lines manageable."""
    serialized = []
    for msg in messages:
        item = {"role": msg.get("role", "")}
        content = msg.get("content", "")
        if content and len(content) > 10000:
            item["content"] = content[:5000] + f"\n... [TRUNCATED {len(content)} chars] ...\n" + content[-5000:]
        else:
            item["content"] = content
        if msg.get("tool_calls"):
            item["tool_calls"] = _serialize_tool_calls(msg["tool_calls"])
        if msg.get("tool_call_id"):
            item["tool_call_id"] = msg["tool_call_id"]
        if msg.get("name"):
            item["name"] = msg["name"]
        serialized.append(item)
    return serialized


def _serialize_tool_calls(tool_calls) -> list[dict]:
    """Convert tool_calls from API response to serializable dicts."""
    result = []
    for tc in tool_calls:
        func_name = tc.function.name if tc.function else ""
        func_args = tc.function.arguments if tc.function else ""
        if len(func_args) > 5000:
            func_args = func_args[:2500] + f"\n... [TRUNCATED {len(func_args)} chars] ...\n" + func_args[-2500:]
        result.append({
            "id": tc.id,
            "type": tc.type,
            "function": {"name": func_name, "arguments": func_args},
        })
    return result


def _serialize_tools(tools: list) -> list[str]:
    """Extract tool names from tool definition list."""
    names = []
    for t in tools:
        if hasattr(t, "function"):
            names.append(t.function.name)
        elif isinstance(t, dict):
            names.append(t.get("function", {}).get("name", ""))
    return names


def _format_readable(entry: dict) -> str:
    """Format a log entry as human-readable text."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"LLM Call #{entry['call_id']}  |  {entry['phase']}  |  Iteration {entry['iteration']}")
    # WNS line: show freshness tag + baseline so stale/regressed WNS is obvious
    # at a glance (a bare stale number previously misled readers into thinking
    # the LLM was acting on current data). Strategy falls back to last active.
    wns_val = entry.get('latest_wns')
    wns_str = f"{wns_val:.3f}" if wns_val is not None else "N/A"
    freshness = entry.get('wns_freshness') or ""
    fresh_tag = f" [{freshness}]" if freshness else ""
    baseline_val = entry.get('baseline_wns')
    baseline_str = f"  baseline={baseline_val:.3f}" if baseline_val is not None else ""
    strategy_str = entry.get('display_strategy') or entry.get('current_strategy') or ""
    lines.append(
        f"Model: {entry['model']}  |  WNS: {wns_str}{fresh_tag}{baseline_str}  |  Strategy: {strategy_str}"
    )
    lines.append("-" * 80)
    if entry.get("error"):
        lines.append(f"ERROR: {entry['error'][:500]}")
    else:
        content = entry.get("response_content", "") or ""
        lines.append(f"Response ({len(content)} chars):")
        lines.append(content[:2000])
        if len(content) > 2000:
            lines.append(f"... [TRUNCATED, total {len(content)} chars]")
        tc = entry.get("response_tool_calls", [])
        if tc:
            for t in tc:
                func_name = t.get("function", {}).get("name", "")
                lines.append(f"  -> Tool call: {func_name}")
                func_args = t.get("function", {}).get("arguments", "")
                if func_args:
                    try:
                        args_obj = json.loads(func_args)
                        formatted_args = json.dumps(args_obj, indent=2, ensure_ascii=False)
                        if len(formatted_args) > 1500:
                            formatted_args = formatted_args[:1500] + f"\n       ... [TRUNCATED, total {len(formatted_args)} chars]"
                        for arg_line in formatted_args.split("\n"):
                            lines.append(f"       {arg_line}")
                    except (json.JSONDecodeError, ValueError):
                        truncated = func_args[:500]
                        lines.append(f"       Raw: {truncated}")
                        if len(func_args) > 500:
                            lines.append(f"       ... [TRUNCATED, total {len(func_args)} chars]")
        usage = entry.get("usage", {})
        if usage:
            lines.append(f"Tokens: {usage.get('total_tokens', 0)}  Cost: ${usage.get('cost', 0):.6f}")
    lines.append("=" * 80)
    lines.append("")
    return "\n".join(lines)


class LLMCallLogger:
    """Logs every LLM call with full state snapshot.

    Writes two files to the run directory:
      - llm_response.jsonl  — one JSON object per line (parsable)
      - llm_response.log    — human-readable formatted text
    """

    def __init__(self):
        self._call_count = 0
        self._jsonl_fh: Any = None
        self._readable_fh: Any = None
        self._log_dir = ""
        self._tracer: Any = None  # Optional DashboardStateTracer for real-time push

    def setup(self, log_dir: str) -> None:
        """Open file handles in the given log directory."""
        if not log_dir:
            logger.warning("[LLMCallLogger] No log_dir provided, logging disabled")
            return
        self._log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self._jsonl_fh = open(
            os.path.join(log_dir, "llm_response.jsonl"),
            "a", encoding="utf-8",
        )
        self._readable_fh = open(
            os.path.join(log_dir, "llm_response.log"),
            "a", encoding="utf-8",
        )
        logger.info(f"[LLMCallLogger] Logging to {log_dir}")

    def set_tracer(self, tracer: Any) -> None:
        """Attach a DashboardStateTracer for real-time WebSocket push."""
        self._tracer = tracer

    def log_call(
        self,
        state,
        *,
        model: str,
        messages: list,
        tools: list,
        response=None,
        error: Optional[str] = None,
        phase: str = "",
    ) -> None:
        """Record one LLM call with its response and current state snapshot."""
        if self._jsonl_fh is None:
            return

        self._call_count += 1
        call_id = self._call_count

        # Extract response details
        response_content = None
        response_tool_calls = None
        finish_reason = None
        prompt_tokens = completion_tokens = total_tokens = reasoning_tokens = 0
        cost_val = 0.0
        usage = None  # bound only when response has usage; referenced in entry dict below

        if response and not error:
            try:
                choice = response.choices[0] if response.choices else None
                if choice:
                    msg = choice.message
                    response_content = msg.content if msg else None
                    response_tool_calls = _serialize_tool_calls(msg.tool_calls) if msg and msg.tool_calls else None
                    finish_reason = choice.finish_reason
                usage = getattr(response, "usage", None)
                if usage:
                    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                    total_tokens = getattr(usage, "total_tokens", 0) or 0
                    details = getattr(usage, "completion_tokens_details", None)
                    if details:
                        reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0
                    cost_val = float(getattr(usage, "cost", 0.0) or 0.0)
            except Exception as e:
                logger.debug(f"[LLMCallLogger] Response extraction failed: {e}")

        state_snapshot = _extract_snapshot(state)

        entry = {
            "timestamp": time.time(),
            "call_id": call_id,
            "phase": phase,
            "graph_node": "llm_tool_loop",
            # Request
            "model": model,
            "provider": "openrouter",
            "messages": _serialize_messages(messages),
            "tool_names": _serialize_tools(tools),
            # Response
            "response_content": response_content,
            "response_tool_calls": response_tool_calls,
            "finish_reason": finish_reason,
            "error": error,
            # Usage (includes prompt caching metrics from OpenRouter)
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cost": cost_val,
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None) if usage else None,
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) if usage else None,
            },
        }
        entry.update(state_snapshot)

        # Write JSONL
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str)
            self._jsonl_fh.write(line + "\n")
            self._jsonl_fh.flush()
        except Exception as e:
            logger.error(f"[LLMCallLogger] JSONL write failed: {e}")

        # Write human-readable log
        if self._readable_fh:
            try:
                self._readable_fh.write(_format_readable(entry))
                self._readable_fh.flush()
            except Exception as e:
                logger.error(f"[LLMCallLogger] Readable log write failed: {e}")

        # Push real-time update to dashboard tracer (if available)
        if self._tracer is not None and hasattr(self._tracer, "push_llm_event"):
            try:
                self._tracer.push_llm_event(
                    state,
                    phase=phase,
                    model=model,
                    iteration=state_snapshot.get("iteration", 0),
                    user_prompt=response_content or error or "",
                    assistant_response=response_content or "",
                    latest_wns=state_snapshot.get("latest_wns"),
                    total_cost=state_snapshot.get("total_cost", 0.0),
                    call_id=call_id,
                )
            except Exception as e:
                logger.debug(f"[LLMCallLogger] Tracer push failed: {e}")

    def close(self) -> None:
        """Close file handles."""
        if self._jsonl_fh:
            self._jsonl_fh.close()
            self._jsonl_fh = None
        if self._readable_fh:
            self._readable_fh.close()
            self._readable_fh = None
