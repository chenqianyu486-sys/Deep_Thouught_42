"""Context snapshot building pure functions.

Builds a phase-aware data dashboard injected before every LLM call.
Each phase sees only the sections relevant to its focus.
The handoff summary is merged into the same message.
"""

from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from .tool_filter import LoopPhase

if TYPE_CHECKING:
    from ..state import OptimizerState

logger = logging.getLogger(__name__)

# Header marker for the Pinned cell-registry layer (compression-resistant).
CELL_REGISTRY_MARKER = "[CELL REGISTRY]"

# Marker for per-phase FORMAT_GUARD injection (idempotency check).
FORMAT_GUARD_MARKER = "[FORMAT_GUARD:"


def extract_system_message(api_messages: list[dict]) -> tuple[str, list[dict]]:
    """Extract the first system message for the top-level API ``system`` parameter.

    Keeps prompt-caching semantics intact: the static SYSTEM_PROMPT.TXT (first
    system message) is returned as ``system_text`` for provider caching; all
    remaining messages (including subsequent system messages such as
    FORMAT_GUARD / handoff / budget) stay in ``api_clean`` as conversation
    history so they survive as context without invalidating the cache.

    Returns:
        (system_text, api_clean) — system_text is "" if no system message found.
    """
    system_text = ""
    api_clean: list[dict] = []
    for msg in api_messages:
        if msg.get("role") == "system" and not system_text:
            system_text = msg.get("content", "")
        else:
            api_clean.append(msg)
    return system_text, api_clean


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
    stale = getattr(state.timing, "critical_paths_stale", False)
    iteration = getattr(getattr(state, "iteration", None), "current", 0)
    snapshot = build_registry_snapshot_yaml(
        registry, phase=phase, stale=stale, iteration=iteration,
    )

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
    from .design_data import DesignDataManager

    space = build_state_space(state)

    # ── Persist full design data (truncation transparency) ────────
    design_data_path: str | None = None
    full_critical_paths: list | None = None
    total_failing_endpoints: int | None = None
    total_high_fanout_nets: int | None = None
    total_congestion_hotspots: int | None = None
    total_design_cell_types: int | None = None
    total_violating_modules: int | None = None

    run_dir = state.control.run_dir
    if run_dir is not None:
        try:
            ddm = DesignDataManager(run_dir)

            # Store full critical paths (untruncated), even when stale
            # so LLM can see unshown_path_stats with [stale] annotation.
            if state.timing.critical_paths:
                full_critical_paths = list(state.timing.critical_paths)

            # Compute totals for truncation transparency
            total_failing_endpoints = state.timing.latest_failing_endpoints
            if state.timing.high_fanout_nets is not None:
                total_high_fanout_nets = len(state.timing.high_fanout_nets)
            if state.timing.congestion_data is not None:
                congestion_raw = state.timing.congestion_data
                if isinstance(congestion_raw, dict):
                    total_congestion_hotspots = len(congestion_raw.get("hotspots", []))
            if state.timing.design_info is not None:
                total_design_cell_types = len(
                    state.timing.design_info.get("top_cell_types", [])
                ) if isinstance(state.timing.design_info.get("top_cell_types"), (list, dict)) else None

            # ── Compute data fingerprint for snapshot-change detection ──
            # The fingerprint captures the essential state that should trigger
            # a new snapshot when it changes. It covers: WNS, critical path
            # count, and the freshness state of all dashboard fields.
            def _compute_data_fingerprint() -> str:
                _wns = state.timing.latest_wns
                _cp_count = len(state.timing.critical_paths) if state.timing.critical_paths else 0
                _freshness_parts = sorted(
                    f"{k}={v}" for k, v in state.timing.field_freshness.items()
                )
                _wns_part = "N/A" if _wns is None else f"{_wns:.3f}"
                return f"wns={_wns_part}|cp={_cp_count}|{'|'.join(_freshness_parts)}"

            # Store full snapshot to disk when iteration changes OR when
            # the data fingerprint differs (data changed within same iteration).
            # This ensures rollback-then-refresh triggers a new snapshot even
            # when the iteration number hasn't advanced.
            current_iter = state.iteration.current
            _fp = _compute_data_fingerprint()
            _last_fp = getattr(state.context.design_data, 'last_snapshot_fingerprint', "")
            if current_iter != state.context.design_data.last_snapshot_iteration or _fp != _last_fp:
                iter_dir = ddm.store_snapshot(
                    critical_paths=full_critical_paths,
                    high_fanout_nets=state.timing.high_fanout_nets,
                    congestion_data=state.timing.congestion_data,
                    route_status=state.timing.route_status,
                    design_info=state.timing.design_info,
                    failing_endpoint_names=state.timing.failing_endpoint_names,
                    field_freshness=state.timing.field_freshness,
                    iteration=current_iter,
                    phase=phase.value if hasattr(phase, "value") else str(phase),
                    # Record when critical_paths data was actually extracted, so
                    # the LLM can tell that data from the current iteration is
                    # current even when field_freshness was conservatively set
                    # to "stale" at EXECUTE entry (pre-modification).
                    critical_paths_extraction_iter=state.timing.critical_paths_iteration,
                )
                design_data_path = iter_dir
                state.context.design_data.last_snapshot_iteration = current_iter
                state.context.design_data.design_data_path = iter_dir
                state.context.design_data.last_snapshot_fingerprint = _fp
                if current_iter not in state.context.design_data.stored_iterations:
                    state.context.design_data.stored_iterations.append(current_iter)
                logger.debug(
                    f"[DESIGN_DATA] Snapshot stored for iteration {current_iter} "
                    f"(iter_changed={current_iter != state.context.design_data.last_snapshot_iteration}, "
                    f"fp_changed={_fp != _last_fp})"
                )
            else:
                design_data_path = state.context.design_data.design_data_path
        except Exception as _dd_err:
            logger.warning(f"[DESIGN_DATA] Snapshot store failed: {_dd_err}", exc_info=True)

    # Build exclusion and blocked-strategy sets.
    # Hard-exclude: tool_error is permanent/immediate (no TTL).
    _hard_exclude: list[str] = [
        fs.strategy for fs in state.context.failed_strategies
        if fs.reason in ("tool_error",)
    ] if state.context.failed_strategies else []

    # TTL-persistent blocks: strategies with a finite cooldown that unblocks
    # after a specific number of iterations.
    #   strategy_ineffective  → TTL=1
    #   no_improvement        → TTL=3
    #   strategy_not_applicable → TTL=2
    # Each entry shows remaining iterations until re-available.
    _blocked: dict[str, str] = {}
    if state.context.failed_strategies:
        for fs in state.context.failed_strategies:
            if fs.reason in ("strategy_ineffective", "no_improvement", "strategy_not_applicable"):
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

    # P3: Suppress stale current_strategy / evaluation_result during non-execute
    # phases. state.strategy.current_strategy may still hold the previous
    # iteration's value when a new iteration enters ANALYZE/SELECT_STRATEGY,
    # misleading the LLM into thinking a strategy is already executing.
    _phase_val = phase.value if hasattr(phase, "value") else str(phase)
    _current_strategy = state.strategy.current_strategy
    _evaluation_result = state.strategy.evaluation_result
    if _phase_val not in ("EXECUTE_STRATEGY", "EVALUATE"):
        _current_strategy = ""
        _evaluation_result = ""

    snapshot = format_state_space_for_llm(
        space=space,
        phase=phase,
        handoff_summary=state.strategy.last_handoff_text,
        show_strategy_catalog=(phase == LoopPhase.SELECT_STRATEGY),
        exclude_strategies=_exclude_strategies,
        blocked_strategies=_blocked_strategies,
        iteration_narratives=state.iteration.narratives,
        tools_used=state.iteration.tools_used,
        current_strategy=_current_strategy,
        evaluation_result=_evaluation_result,
        state=state,
        design_data_path=design_data_path,
        full_critical_paths=full_critical_paths,
        total_failing_endpoints=total_failing_endpoints,
        total_high_fanout_nets=total_high_fanout_nets,
        total_congestion_hotspots=total_congestion_hotspots,
        total_design_cell_types=total_design_cell_types,
        total_violating_modules=total_violating_modules,
    )

    inject_context_snapshot_at_end(api_messages, snapshot)

    # Inject per-phase FORMAT_GUARD as a system message (idempotent via marker).
    # This ensures the guard is present in every phase with phase-specific
    # addenda, and survives phase transitions (unlike the old once-per-iteration
    # injection that was lost after the first transition_phase clear).
    _inject_phase_guard(api_messages, phase)


def _inject_phase_guard(api_messages: list[dict], phase: LoopPhase) -> None:
    """Inject or refresh the per-phase FORMAT_GUARD as a system message.

    Idempotent: removes any prior [FORMAT_GUARD:...] system message before
    inserting the fresh one, so it never accumulates across turns and always
    reflects the current phase.
    """
    from optimizer.nodes.prepare_context import build_phase_format_guard

    # Remove any existing FORMAT_GUARD system message
    for i, msg in enumerate(api_messages):
        if (msg.get("role") == "system"
                and isinstance(msg.get("content"), str)
                and FORMAT_GUARD_MARKER in msg["content"]):
            del api_messages[i]
            break

    guard_text = build_phase_format_guard(phase)
    # Insert after the first system message (static SYSTEM_PROMPT.TXT) to keep
    # prompt-caching semantics: the static prompt stays first for caching, the
    # guard follows as a second system message in the conversation.
    insert_idx = 1
    for i, msg in enumerate(api_messages):
        if msg.get("role") != "system":
            insert_idx = i
            break
    else:
        insert_idx = len(api_messages)
    api_messages.insert(insert_idx, {"role": "system", "content": guard_text})
