"""Context snapshot building pure functions.

Builds a phase-aware data dashboard injected before every LLM call.
Each phase sees only the sections relevant to its focus.
The handoff summary is merged into the same message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .tool_filter import LoopPhase

if TYPE_CHECKING:
    from ..state import OptimizerState

# Header marker for the Pinned cell-registry layer (compression-resistant).
CELL_REGISTRY_MARKER = "[CELL REGISTRY]"


def inject_pinned_cell_registry(
    api_messages: list[dict],
    state: "OptimizerState",
) -> None:
    """Inject the canonical cell-name registry as an independent user message
    right after the system message(s).

    This is the Pinned context layer (Layer 2): rebuilt every turn from
    state.entity_registry, never enters MessageStore, and survives
    compression. The LLM reads canonical cell names from here instead of
    reconstructing them from (compressed) tool outputs — eliminating the
    "memory reconstruction" failure mode at the EXECUTE boundary.

    Idempotent: any prior [CELL REGISTRY] message is removed before the
    fresh one is inserted, so it never accumulates across turns.
    """
    from .entities import build_registry_snapshot_yaml

    # Remove any existing pinned registry message
    for i, msg in enumerate(api_messages):
        if (msg.get("role") == "user"
                and isinstance(msg.get("content"), str)
                and msg["content"].lstrip().startswith(CELL_REGISTRY_MARKER)):
            del api_messages[i]
            break

    # Only inject when the registry has content OR we are past init_analysis
    # (so the LLM sees the placeholder + guidance even before cells load).
    registry = getattr(state, "entity_registry", None)
    if registry is None:
        return

    phase = getattr(state.strategy, "current_phase", "") or ""
    snapshot = build_registry_snapshot_yaml(registry, phase=phase)

    # Insert right after the last system message (Pinned layer position).
    insert_idx = 0
    for i, msg in enumerate(api_messages):
        if msg.get("role") != "system":
            insert_idx = i
            break
    else:
        insert_idx = len(api_messages)
    api_messages.insert(insert_idx, {"role": "user", "content": snapshot})


def inject_context_snapshot(api_messages: list[dict], snapshot_yaml: str) -> None:
    """Inject or update the context snapshot as the FIRST user message.

    Scans for an existing snapshot (by header marker), removes it to
    prevent accumulation, then inserts a fresh snapshot after all
    system messages. (V1 compatibility mode.)
    """
    header_markers = (
        "[ANALYZE — Context & Dashboard]",
        "[SELECT_STRATEGY — Context & Dashboard]",
        "[EXECUTE — Context & Dashboard]",
        "[EVALUATE — Context & Dashboard]",
    )
    for i, msg in enumerate(api_messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            if any(msg["content"].startswith(m) for m in header_markers):
                del api_messages[i]
                break
    insert_idx = 0
    for i, msg in enumerate(api_messages):
        if msg.get("role") != "system":
            insert_idx = i
            break
    else:
        insert_idx = len(api_messages)
    api_messages.insert(insert_idx, {"role": "user", "content": snapshot_yaml})


def inject_context_snapshot_at_end(api_messages: list[dict], snapshot_yaml: str) -> None:
    """Inject or update a dashboard message as the LAST user message.

    Finds and replaces any existing dashboard message (by header marker),
    then appends the new one at the end.
    """
    # Remove any existing merged dashboard message
    header_markers = (
        "[ANALYZE — Context & Dashboard]",
        "[SELECT_STRATEGY — Context & Dashboard]",
        "[EXECUTE — Context & Dashboard]",
        "[EVALUATE — Context & Dashboard]",
    )
    for i, msg in enumerate(api_messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            if any(msg["content"].startswith(m) for m in header_markers):
                del api_messages[i]
                break

    # Append at end for maximum attention weight
    api_messages.append({"role": "user", "content": snapshot_yaml})


def inject_merged_dashboard(
    api_messages: list,
    state: "OptimizerState",
    phase: LoopPhase,
) -> None:
    """Build and inject merged handoff + dashboard as the last user message.

    Uses the canonical 6-module StateSpace representation via
    build_state_space() + format_state_space_for_llm().

    Called by each phase's _call_phase_llm() before every LLM call.
    The handoff summary (from previous phase) is merged into the same message
    so it gets maximum attention weight at the end of the conversation.
    """
    from .state_space import build_state_space, format_state_space_for_llm

    space = build_state_space(state)

    # Build exclusion and blocked-strategy sets.
    # Reasons that are always retriable → hard-exclude from catalog entirely.
    _hard_exclude: list[str] = [
        fs.strategy for fs in state.context.failed_strategies
        if fs.reason in ("strategy_not_applicable", "tool_error", "no_improvement")
    ] if state.context.failed_strategies else []

    # Reasons that merit a [BLOCKED] placeholder so the LLM understands why.
    _blocked: dict[str, str] = {}

    # TTL-persistent blocks (strategy_ineffective, unblocks after N iterations).
    if state.context.failed_strategies:
        for fs in state.context.failed_strategies:
            if fs.reason == "strategy_ineffective":
                remaining = max(0, fs.blocked_until_iter - state.iteration.current)
                if remaining > 0:
                    _blocked[fs.strategy] = f"unblocks in {remaining} iter"

    # Per-iteration cooldown strategies.
    if state.iteration.blocked_strategies:
        for s in state.iteration.blocked_strategies:
            _blocked[s] = "cooldown (explored this iteration)"

    # Prepare catalog parameters.
    _exclude_strategies = _hard_exclude or None
    _blocked_strategies = _blocked or None

    snapshot = format_state_space_for_llm(
        space=space,
        phase=phase,
        handoff_summary=state.strategy.last_handoff_text,
        show_strategy_catalog=(phase == LoopPhase.SELECT_STRATEGY),
        exclude_strategies=_exclude_strategies,
        blocked_strategies=_blocked_strategies,
        iteration_narratives=state.iteration.narratives,
        tools_used=state.iteration.tools_used,
        current_strategy=state.strategy.current_strategy,
        evaluation_result=state.strategy.evaluation_result,
        state=state,
    )

    inject_context_snapshot_at_end(api_messages, snapshot)
