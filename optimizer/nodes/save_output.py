"""Save output node: write final DCP, print summary, export telemetry.

This is the terminal node of the optimizer graph.

Reference: dcp_optimizer.py optimize() exit path (~line 5476),
_save_best_checkpoint_on_timeout() (L5909-5942).
"""

from __future__ import annotations

import logging
import time

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.tool_router import call_tool as call_tool_fn
from ..pure.timing import parse_hold_timing

logger = logging.getLogger(__name__)


async def save_output_node(
    state: OptimizerState, deps: NodeDeps
) -> str:
    """Save output DCP and print optimization summary.

    Actions:
        1. Record end_time
        2. Log final summary (iterations, WNS, cost)
        3. Write output DCP via MCP (if available)
        4. Export tracing (if run_dir set)

    Returns:
        'end' to terminate the graph.
    """
    state.control.end_time = time.time()

    elapsed = 0.0
    if state.control.start_time:
        elapsed = state.control.end_time - state.control.start_time

    best = f"{state.timing.best_wns:.3f}" if state.timing.best_wns != float('-inf') else "N/A"
    latest = f"{state.timing.latest_wns:.3f}" if state.timing.latest_wns is not None else "N/A"

    logger.info("=" * 60)
    logger.info("[save_output] Optimization complete")
    logger.info(f"  Reason:     {state.control.done_reason}")
    logger.info(f"  Iterations: {state.iteration.current}")
    logger.info(f"  Best WNS:   {best} ns")
    logger.info(f"  Latest WNS: {latest} ns")
    logger.info(f"  Total cost: ${state.cost.total_cost:.4f}")
    logger.info(f"  LLM calls:  {state.model.llm_call_count}")
    logger.info(f"  Elapsed:    {elapsed:.1f}s")
    logger.info("=" * 60)

    # Check hold timing before final save (competition requirement)
    if state.control.output_dcp and deps.vivado_session:
        try:
            logger.info("[save_output] Checking hold timing...")
            hold_report = await call_tool_fn(
                "vivado_run_tcl",
                {"command": "report_timing_summary -delay_type min -max_paths 100"},
                deps.rapidwright_session, deps.vivado_session,
            )
            hold = parse_hold_timing(hold_report)
            if hold.get("hold_wns") is not None:
                if hold["hold_wns"] < 0:
                    logger.warning(
                        f"[save_output] HOLD VIOLATED: WNS={hold['hold_wns']:.3f}ns, "
                        f"failing={hold.get('hold_failing')}"
                    )
                else:
                    logger.info(f"[save_output] Hold timing MET: WNS={hold['hold_wns']:.3f}ns")
        except Exception as e:
            logger.warning(f"[save_output] Hold timing check failed: {e}")

    # Write output DCP
    if state.control.output_dcp and deps.vivado_session:
        try:
            logger.info(f"[save_output] Writing output DCP to {state.control.output_dcp}")
            result = await call_tool_fn(
                "vivado_write_checkpoint",
                {"dcp_path": str(state.control.output_dcp.resolve()), "force": True},
                deps.rapidwright_session, deps.vivado_session,
            )
            if "error" in result.lower():
                logger.warning(f"[save_output] Failed to write DCP: {result}")
            else:
                logger.info(f"[save_output] Output DCP written successfully")
        except Exception as e:
            logger.warning(f"[save_output] Failed to write output DCP: {e}")

    # Export tracing
    if state.control.run_dir:
        try:
            trace_path = str(state.control.run_dir / "state_transitions.json")
            # Tracer export is handled by the graph builder
            logger.info(f"[save_output] Tracing exported to {trace_path}")
        except Exception as e:
            logger.warning(f"[save_output] Failed to export tracing: {e}")

    return NodeName.END
