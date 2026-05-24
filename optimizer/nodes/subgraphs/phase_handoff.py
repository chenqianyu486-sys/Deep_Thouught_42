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

    def to_phase_context_string(self) -> str:
        """Format handoff as injectable context string.

        Produces a compact summary for the merged phase context + dashboard message.
        Returns empty string if handoff is empty (e.g. first phase in iteration).
        """
        if not self.llm_summary and self.wns is None and not self.key_findings:
            return ""
        parts = ["## Previous Phase Summary"]
        parts.append(f"Source: {self.source_phase}")
        if self.wns is not None:
            parts.append(f"WNS: {self.wns:.3f}ns")
        if self.tns is not None:
            parts.append(f"TNS: {self.tns:.3f}ns")
        if self.failing_endpoints is not None:
            parts.append(f"Failing Endpoints: {self.failing_endpoints}")
        if self.llm_summary:
            # Trim very long summaries
            summary = self.llm_summary[:600]
            parts.append(f"Summary: {summary}")
        if self.tools_called:
            tools_str = ", ".join(self.tools_called[-6:])
            parts.append(f"Tools: {tools_str}")
        if self.key_findings:
            items = [f"{k}={v}" for k, v in self.key_findings.items()
                     if not isinstance(v, (list, dict)) or len(str(v)) < 100]
            if items:
                parts.append(f"Findings: {', '.join(items[:5])}")
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
    )


async def transition_phase(
    deps,
    from_phase: LoopPhase,
    to_phase: LoopPhase,
    handoff: PhaseHandoff,
    tool_cache: dict | None = None,
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

        # 3. Get the system message (first message) to preserve
        system_msg = None
        for m in current_messages:
            if hasattr(m, 'role') and m.role.value == "system":
                system_msg = m
                break

        # 4. Clear working memory and restore system message
        deps.memory_manager._working_memory.clear()
        if system_msg is not None:
            deps.memory_manager._working_store.add(system_msg)

        logger.info(
            "[phase_handoff] %s -> %s: %d messages archived",
            from_phase.value, to_phase.value,
            current_count,
        )

        # 5. Clear phase-local tool cache
        if tool_cache is not None:
            tool_cache.clear()
            logger.debug("[phase_handoff] Tool cache cleared")

    except Exception as e:
        logger.warning(f"[phase_handoff] Transition failed (non-critical): {e}")


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
