"""Phase handoff: structured context passing between loop phases.

Scheme B: phase isolation with structured handoff.
Each phase starts with a clean message slate. The handoff carries key
findings from completed phases to the next phase.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from optimizer.pure.tool_filter import LoopPhase

logger = logging.getLogger(__name__)

# Module-level variable to track the last design fingerprint across transitions.
# When design_fingerprint is provided and unchanged, the tool cache is preserved
# (design hasn't changed — cached results are still valid). This avoids flushing
# cached timing data during EVALUATE → CONTINUE → ANALYZE cycles when no
# design modification occurred. Callers that don't pass a fingerprint (None)
# get the original always-clear behavior for backward compatibility.
_last_design_fingerprint: str | None = None


@dataclass
class PhaseHandoff:
    """Structured context passed from a completed phase to the next phase.

    source_phase: Which phase produced this handoff (ANALYZE, SELECT_STRATEGY, EXECUTE).
    llm_summary: The LLM's own text summary at phase completion (most important).
    Structured fields: extracted from OptimizerState, not parsed from messages.
    """

    source_phase: str = ""
    timestamp: float = field(default_factory=time.time)

    llm_summary: str = ""
    wns: float | None = None
    tns: float | None = None
    failing_endpoints: int | None = None
    key_findings: dict = field(default_factory=dict)
    tools_called: list[str] = field(default_factory=list)
    message_count: int = 0
    tool_results: list[str] = field(default_factory=list)

    # P1 fix: state-awareness fields to prevent LLM blindness
    design_stage: str = ""               # "unplaced" | "placed" | "routed"
    critical_paths_count: int = 0         # number of paths in state.timing.critical_paths
    stalled_strategies: list[str] = field(default_factory=list)  # blocked this iteration

    def to_phase_context_string(self) -> str:
        """Format handoff as injectable context string.

        Produces a compact summary for the merged phase context + dashboard message.
        Returns empty string if handoff is empty (e.g. first phase in iteration).
        """
        if not self.llm_summary and self.wns is None and not self.key_findings:
            return ""
        parts = ["## Previous Phase Summary"]
        parts.append(f"Source: {self.source_phase}")
        # Surface the execution outcome prominently so a failed/restored chain
        # is not mistaken for a successful-but-ineffective run.
        outcome = self.key_findings.get("outcome") if self.key_findings else None
        if outcome:
            parts.append(f"Outcome: {outcome}")
        if self.wns is not None:
            parts.append(f"WNS: {self.wns:.3f}ns")
        if self.tns is not None:
            parts.append(f"TNS: {self.tns:.3f}ns")
        if self.failing_endpoints is not None:
            parts.append(f"Failing Endpoints: {self.failing_endpoints}")
        # P1: design state awareness
        if self.design_stage:
            parts.append(f"Design Stage: {self.design_stage}")
        if self.critical_paths_count > 0:
            parts.append(f"Critical Paths Available: {self.critical_paths_count} paths in state")
        else:
            parts.append("Critical Paths Available: NONE — parser returned 0 paths. Use vivado_extract_critical_path_cells to populate.")
        if self.stalled_strategies:
            parts.append(f"Stalled (blocked this iteration): {', '.join(self.stalled_strategies)}")
        if self.llm_summary:
            # Trim very long summaries
            summary = self.llm_summary[:600]
            parts.append(f"Summary: {summary}")
        if self.tools_called:
            tools_str = ", ".join(self.tools_called[-6:])
            parts.append(f"Tools: {tools_str}")
        if self.key_findings:
            # 'outcome' is rendered as a dedicated Outcome line above; skip here.
            items = [f"{k}={v}" for k, v in self.key_findings.items()
                     if k != "outcome"
                     and (not isinstance(v, (list, dict)) or len(str(v)) < 100)]
            if items:
                parts.append(f"Findings: {', '.join(items[:5])}")
        if self.tool_results:
            parts.append("Recent Tool Results:")
            for tr in self.tool_results[-4:]:
                parts.append(f"  {tr}")
        # Append a standard hint for transitions heading into EXECUTE phase:
        # critical path data is pre-loaded from state, no manual TCL extraction needed.
        if self.source_phase in ("SELECT_STRATEGY", "ANALYZE"):
            parts.append("")
            parts.append("NOTE: Critical path cell data is automatically injected "
                         "into strategy tools (rapidwright_execute_pblock_strategy, "
                         "rapidwright_execute_muxf_tree_reorder_strategy, etc.) "
                         "from state. No manual TCL extraction required.")
        return "\n".join(parts)


def build_phase_handoff(
    source_phase: LoopPhase,
    llm_summary: str,
    wns: float | None = None,
    tns: float | None = None,
    failing_endpoints: int | None = None,
    key_findings: dict | None = None,
    tools_called: list[str] | None = None,
    message_count: int = 0,
    tool_results: list[str] | None = None,
    design_stage: str = "",
    critical_paths_count: int = 0,
    stalled_strategies: list[str] | None = None,
) -> PhaseHandoff:
    """Build a PhaseHandoff from a completed phase."""
    return PhaseHandoff(
        source_phase=source_phase.value,
        llm_summary=llm_summary,
        wns=wns,
        tns=tns,
        failing_endpoints=failing_endpoints,
        key_findings=key_findings or {},
        tools_called=tools_called or [],
        message_count=message_count,
        tool_results=tool_results or [],
        design_stage=design_stage,
        critical_paths_count=critical_paths_count,
        stalled_strategies=stalled_strategies or [],
    )


async def transition_phase(
    deps,
    from_phase: LoopPhase,
    to_phase: LoopPhase,
    handoff: PhaseHandoff,
    tool_cache: dict | None = None,
    design_fingerprint: str | None = None,
) -> None:
    """Archive current phase messages and start a fresh message segment.

    1. Compress current working memory messages into HistoricalMemory.
    2. Start a new working memory segment for the next phase.
    3. Inject the phase context as the first user message.

    Args:
        deps: NodeDeps (needs compat and memory_manager).
        from_phase: Phase that just completed.
        to_phase: Phase that is starting.
        handoff: Structured context from the completed phase.
        tool_cache: Optional dict to clear on transition.
        design_fingerprint: Optional design-state fingerprint. When provided and
            unchanged from the last transition, tool_cache is preserved (no clear).
            When None (default), always clear for backward compatibility.
    """
    if deps.compat is None or deps.memory_manager is None:
        return

    try:
        # 1. Count messages before archival
        current_messages = deps.memory_manager._working_memory.get_all()
        current_count = len(current_messages)

        # 2. Archive current messages to HistoricalMemory if there are enough
        if current_count > 2:
            try:
                deps.memory_manager._historical_memory.add(
                    content=_format_phase_archive(from_phase, current_messages, handoff),
                    importance=0.6,
                    task_type=f"phase_{from_phase.value}",
                )
            except Exception as e:
                logger.debug(f"[phase_handoff] Archive failed (non-critical): {e}")

        # 4. Clear working memory and restore ALL system messages.
        #    Only restoring the first would lose FORMAT_GUARD / handoff_prompt /
        #    budget messages injected after the static SYSTEM_PROMPT.TXT, leaving
        #    the LLM without critical per-iteration constraints in later phases.
        system_msgs = [m for m in current_messages
                       if hasattr(m, 'role') and m.role.value == "system"]
        deps.memory_manager._working_memory.clear()
        for sm in system_msgs:
            deps.memory_manager._working_store.add(sm)

        logger.info(
            "[phase_handoff] %s -> %s: %d messages archived",
            from_phase.value, to_phase.value,
            current_count,
        )

        # 5. Conditionally clear phase-local tool cache.
        #    When design_fingerprint is provided and unchanged from the last
        #    transition, the cache is preserved (design hasn't changed, cached
        #    results are still valid). When design_fingerprint is None (caller
        #    doesn't support it yet), always clear for backward compatibility.
        if tool_cache is not None:
            global _last_design_fingerprint
            if design_fingerprint is not None and design_fingerprint == _last_design_fingerprint:
                logger.debug("[phase_handoff] Design unchanged — preserving tool cache")
            else:
                tool_cache.clear()
                _last_design_fingerprint = design_fingerprint
                logger.debug("[phase_handoff] Tool cache cleared")

    except Exception as e:
        logger.warning(f"[phase_handoff] Transition failed (non-critical): {e}")


def reset_design_fingerprint() -> None:
    """Reset the global design fingerprint tracker.

    Called on rollback and iteration start to prevent stale cache preservation
    across design-state boundaries. When the design is physically restored to a
    different checkpoint (rollback) or a new iteration begins, cached tool results
    from the previous design state may be invalid — resetting the fingerprint
    ensures the next phase transition will clear the tool cache.
    """
    global _last_design_fingerprint
    _last_design_fingerprint = None


def _format_phase_archive(
    phase: LoopPhase,
    messages: list,
    handoff: PhaseHandoff,
) -> str:
    """Create a compressed summary of phase messages for archival."""
    parts = [f"[Phase: {phase.value}]", f"Messages: {len(messages)}"]

    if handoff.llm_summary:
        parts.append(f"LLM Summary: {handoff.llm_summary[:500]}")

    if handoff.wns is not None:
        parts.append(f"WNS: {handoff.wns:.3f}ns")

    parts.append("---")
    # Last few messages as samples
    for msg in messages[-3:]:
        content = str(getattr(msg, 'content', ''))[:200]
        role = str(getattr(msg, 'role', 'unknown'))
        parts.append(f"[{role}] {content}")

    return "\n".join(parts)
