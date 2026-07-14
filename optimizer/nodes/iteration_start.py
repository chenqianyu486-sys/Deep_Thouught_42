"""Iteration start node: begin a new optimization iteration.

Increments iteration counter, checks exit conditions (user quit,
wall-clock timeout, max iterations), snapshots WNS for rollback.

Reference: dcp_optimizer.py optimize() loop start (~line 5195)
"""

from __future__ import annotations

import logging
import shutil
import time

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..color import green
from optimizer.pure.tool_router import call_tool as call_tool_fn
from .subgraphs.phase_handoff import reset_design_fingerprint
from .subgraphs.phase_execute import sync_rapidwright_baseline

logger = logging.getLogger(__name__)


async def iteration_start_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Begin a new optimization iteration.

    Actions:
        1. Increment iteration counter
        2. Reset per-iteration tool errors
        3. Check exit conditions (user quit, wall-clock, max iterations)
        4. Snapshot WNS state for rollback

    Note: Node return values are not used for routing — graph edges decide.
    The static edge from iteration_start always routes to select_model.
    Early-exit flags (is_done) are picked up by check_exit on the next pass.

    Returns:
        Next node name (deterministic: select_model).
    """
    # Check max iterations BEFORE incrementing (fix C-1: off-by-one)
    if state.iteration.current >= state.iteration.max_iterations:
        logger.info(
            f"[iteration_start] Max iterations reached: "
            f"{state.iteration.current} >= {state.iteration.max_iterations}"
        )
        state.control.is_done = True
        state.control.done_reason = "max_iterations_reached"
        return NodeName.SAVE_OUTPUT

    # Check wall-clock timeout BEFORE incrementing
    if state.control.start_time is not None:
        elapsed = time.time() - state.control.start_time
        if elapsed > state.control.wall_clock_timeout:
            logger.warning(
                f"[iteration_start] Wall-clock timeout: "
                f"{elapsed:.0f}s > {state.control.wall_clock_timeout:.0f}s"
            )
            state.control.is_done = True
            state.control.done_reason = "wall_clock_timeout"
            return NodeName.SAVE_OUTPUT

    # Increment iteration
    state.iteration.current += 1

    # ── 迭代边界消息清理（容错设计） ──
    # 此块与 transition_phase() 的 archive + clear + restore system 模式一致。
    # 失败时不中断迭代流程（wrap 在 try/except 中，仅 warning log）。
    if deps.memory_manager is not None:
        try:
            current_msgs = deps.memory_manager.get_context()
            system_msgs = [
                m for m in current_msgs
                if hasattr(m, 'role') and m.role.value == "system"
            ]
            non_system_msgs = [
                m for m in current_msgs
                if not (hasattr(m, 'role') and m.role.value == "system")
            ]
            if non_system_msgs:
                archive_content = _format_iteration_archive(
                    state.iteration.current, current_msgs, state
                )
                deps.memory_manager._historical_memory.add(
                    content=archive_content,
                    importance=0.5,
                    task_type="iteration_archive",
                )
                deps.memory_manager._working_memory.clear()
                for sm in system_msgs:
                    deps.memory_manager._working_store.add(sm)
                logger.info(
                    "[iteration_start] Cleared %d non-system messages from iteration %d",
                    len(non_system_msgs), state.iteration.current - 1,
                )
        except Exception as e:
            logger.warning("[iteration_start] Iteration cleanup skipped: %s", e)

    state.iteration.tool_errors.clear()
    state.iteration.tools_used.clear()
    state.iteration.blocked_strategies.clear()
    state.iteration.tool_round = 0
    state.model.current_task_type = ""

    # Clear cross-iteration caches: previous iteration's tool results and
    # raw outputs are invalid for the new iteration's different design state.
    state.context.tool_cache.clear()
    state.context.raw_tool_outputs.clear()
    # Reset the per-iteration no-progress counter. It previously persisted
    # across iterations (never reset here), so a stall streak from iteration N
    # carried into N+1 and grew unboundedly (3->4->...->11), forcing a
    # SWITCH_STRATEGY on every evaluation without an escalation path. Cross-
    # iteration plateau detection is handled separately by global_no_improvement
    # + GLOBAL_NO_IMPROVEMENT_LIMIT in check_exit; this counter only gates
    # within-iteration strategy switching.
    state.context.consecutive_no_progress = 0
    state.context.pending_pblock_plan = None
    state.context.pending_pblock_candidates.clear()
    state.context.attempted_pblock_candidate_ids.clear()
    # Reset phase-handoff design fingerprint so the next phase transition
    # clears any remaining cached tool results from the prior iteration.
    reset_design_fingerprint()
    logger.info(f"[iteration_start] Tool cache and raw outputs cleared for iteration {state.iteration.current}")

    iter_num = state.iteration.current
    logger.info(green(
        f"[iteration_start] === Iteration {iter_num} === "
        f"(best_wns={state.timing.best_wns:.3f}ns, "
        f"cost=${state.cost.total_cost:.4f})"
    ))

    # Snapshot WNS/TNS for rollback (store prev_best_*)
    state.timing.prev_best_wns = state.timing.best_wns
    state.timing.prev_best_tns = state.timing.best_wns_tns

    # Save iteration start checkpoint for rollback baseline.
    # _ensure_iteration_start_checkpoint (in EXECUTE) reuses this.
    # When best_checkpoint.dcp already exists, copy it directly (saves ~5s
    # per iteration by avoiding a full Vivado serialization of unchanged memory).
    try:
        iter_ckpt = state.control.run_dir / f"iteration_{state.iteration.current}_start.dcp"
        if not iter_ckpt.exists():
            best = state.control.best_checkpoint_path
            if best is not None and best.exists():
                shutil.copy2(str(best), str(iter_ckpt))
                logger.info(
                    f"[iteration_start] Copied iteration {state.iteration.current} start DCP "
                    f"from best_checkpoint (saved ~5s)")
            else:
                await call_tool_fn(
                    "vivado_write_checkpoint",
                    {"dcp_path": str(iter_ckpt.resolve()), "force": True},
                    deps.rapidwright_session,
                    deps.vivado_session,
                    design_size_factor=state.timing.design_size_factor,
                )
                logger.info(
                    f"[iteration_start] Saved iteration {state.iteration.current} start DCP "
                    f"via Vivado (no best_checkpoint to copy)")
            state.control.iteration_checkpoints.append((state.iteration.current, iter_ckpt))
    except Exception as e:
        logger.warning(f"[iteration_start] Failed to save iteration checkpoint: {e}")

    # P0: sync RapidWright to the iteration baseline (best_checkpoint). RW's
    # _current_design drifts after a netlist-mutating strategy; without this the
    # new iteration's ANALYZE + RW strategies run on a stale/drifted netlist.
    best = state.control.best_checkpoint_path
    if best is not None and best.exists():
        try:
            await sync_rapidwright_baseline(state, deps, best)
        except Exception as e:
            logger.warning(f"[iteration_start] RapidWright baseline sync failed: {e}")

    return NodeName.SELECT_MODEL


def _format_iteration_archive(
    iteration: int,
    messages: list,
    state,
) -> str:
    """迭代归档摘要。所有字段用 getattr 安全访问，不假设任何可选字段存在。

    截断规则：
    - 每条消息采样超 300 字符 -> " [TRUNCATED]"
    """
    prev_iter = iteration - 1
    wns_str = (
        f"{state.timing.latest_wns:.3f}ns"
        if state.timing.latest_wns is not None else "N/A"
    )
    best_str = (
        f"{state.timing.best_wns:.3f}ns"
        if state.timing.best_wns not in (None, float("-inf")) else "N/A"
    )

    parts = [
        f"[Iteration {prev_iter} Archive]",
        f"  messages={len(messages)}",
        f"  best_wns={best_str}",
        f"  final_wns={wns_str}",
        f"  strategy={state.strategy.current_strategy or 'N/A'}",
    ]
    if state.control.done_reason:
        parts.append(f"  exit_reason={state.control.done_reason}")

    # 采样尾部 non-system 消息
    non_system = [
        m for m in messages
        if not (hasattr(m, "role") and m.role.value == "system")
    ]
    if non_system:
        parts.append(f"--- Last {min(3, len(non_system))} messages ---")
        for msg in non_system[-3:]:
            role = str(getattr(msg, "role", "?"))
            content = str(getattr(msg, "content", ""))
            if len(content) > 300:
                content = content[:300] + " [TRUNCATED]"
            parts.append(f"  [{role}] {content}")

    return "\n".join(parts)
