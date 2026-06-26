"""Save output node: write final DCP, print summary, export telemetry.

This is the terminal node of the optimizer graph.

Reference: dcp_optimizer.py optimize() exit path (~line 5476),
_save_best_checkpoint_on_timeout() (L5909-5942).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

from ..state import OptimizerState
from ..deps import NodeDeps
from ..edges import NodeName
from ..pure.tool_router import call_tool as call_tool_fn
from ..pure.timing import parse_hold_timing, parse_timing_summary
from ..pure.trajectory import format_trajectory_summary
from ..color import green

from ..pure.trajectory import format_trajectory_summary
from ..color import green

logger = logging.getLogger(__name__)

BEST_WNS_VERIFY_TOLERANCE_NS = 0.005


def _classify_design_state(status_text: str, timing_summary: str = "") -> str:
    """Classify Vivado design state from STATUS, falling back to timing summary."""
    status = (status_text or "").strip().lower()
    if "routed" in status:
        return "routed"
    if "placed" in status:
        return "placed"

    match = re.search(
        r"Design\s+State\s*:\s*([^\|\n\r\t]+)",
        timing_summary or "",
        re.IGNORECASE,
    )
    if match:
        state = match.group(1).strip().lower()
        if "routed" in state:
            return "routed"
        if "placed" in state:
            return "placed"
        if "optimized" in state:
            return "optimized"

    return "unknown"


async def _restore_best_checkpoint_for_delivery(
    state: OptimizerState, deps: NodeDeps
) -> bool:
    """Restore and verify the checkpoint that owns ``best_wns``.

    Returning False tells the caller to preserve the incrementally written
    output instead of overwriting it with the current, potentially regressed,
    Vivado design.
    """
    best_checkpoint = state.control.best_checkpoint_path
    if best_checkpoint is None:
        logger.warning(
            "[save_output] No best checkpoint is available; delivering current design"
        )
        return True
    if not best_checkpoint.exists():
        logger.error(
            f"[save_output] Best checkpoint is missing: {best_checkpoint}. "
            "Preserving the incremental output DCP."
        )
        return False

    try:
        logger.info(f"[save_output] Restoring best checkpoint: {best_checkpoint}")
        result = await call_tool_fn(
            "vivado_open_checkpoint",
            {"dcp_path": str(best_checkpoint.resolve())},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        if "error" in result.lower():
            logger.error(
                f"[save_output] Could not restore best checkpoint: {result[:300]}"
            )
            return False

        timing_report = await call_tool_fn(
            "vivado_report_timing_summary",
            {},
            deps.rapidwright_session, deps.vivado_session,
            design_size_factor=state.timing.design_size_factor,
        )
        verified_wns = parse_timing_summary(timing_report).get("wns")
        if verified_wns is None:
            logger.error(
                "[save_output] Could not verify best checkpoint WNS; "
                "preserving the incremental output DCP"
            )
            return False
        if (state.timing.best_wns != float('-inf')
                and abs(verified_wns - state.timing.best_wns)
                > BEST_WNS_VERIFY_TOLERANCE_NS):
            logger.error(
                f"[save_output] Best checkpoint WNS mismatch: "
                f"verified={verified_wns:.3f}ns, cached={state.timing.best_wns:.3f}ns. "
                "Preserving the incremental output DCP."
            )
            return False

        state.control.current_dcp_path = best_checkpoint.resolve()
        state.timing.latest_wns = verified_wns
        logger.info(
            f"[save_output] Best checkpoint verified: WNS={verified_wns:.3f}ns"
        )
        return True
    except Exception as e:
        logger.error(
            f"[save_output] Failed to restore best checkpoint: {e}. "
            "Preserving the incremental output DCP."
        )
        return False


async def _run_validation(
    golden_dcp: Path,
    revised_dcp: Path,
    num_vectors: int = 200,
) -> dict:
    """Run validate_dcps.py as a subprocess.

    Returns dict with 'passed' (bool) and 'error' (str or None).
    """
    script_path = Path(__file__).resolve().parents[2] / "validate_dcps.py"
    if not script_path.exists():
        return {"passed": False, "error": f"validate_dcps.py not found at {script_path}"}

    cmd = [
        sys.executable, "-u",
        str(script_path),
        str(golden_dcp),
        str(revised_dcp),
        "--vectors", str(num_vectors),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=7200.0)
        passed = proc.returncode == 0
        error = None if passed else (stderr.decode()[-500:] if stderr else "validation failed")
        return {"passed": passed, "error": error}
    except asyncio.TimeoutError:
        return {"passed": False, "error": "validation timed out after 7200s"}
    except Exception as e:
        return {"passed": False, "error": str(e)}


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

    logger.info(green(
        f"[save_output] Optimization complete\n"
        f"  reason={state.control.done_reason}, iterations={state.iteration.current}\n"
        f"  best_wns={best}ns, latest_wns={latest}ns\n"
        f"  cost=${state.cost.total_cost:.4f}, llm_calls={state.model.llm_call_count}, elapsed={elapsed:.1f}s"
    ))

    # Print detailed optimization trajectory before summary
    trajectory = format_trajectory_summary(state)
    print(trajectory["console_text"])

    # Print summary to stdout for user visibility (logger goes to stderr/JSON)
    elapsed_min = elapsed / 60
    print(f"\n{'='*70}")
    print(f"Optimization Summary")
    print(f"{'='*70}")
    print(f"  Reason:        {state.control.done_reason}")
    print(f"  Iterations:    {state.iteration.current}")
    print(f"  Best WNS:      {best}ns")
    print(f"  Latest WNS:    {latest}ns")
    if state.cost.total_tokens > 0:
        print(f"  Tokens:        {state.cost.total_tokens:,} (prompt={state.cost.total_prompt_tokens:,}, completion={state.cost.total_completion_tokens:,})")
        if state.cost.total_reasoning_tokens > 0:
            print(f"  Reasoning:     {state.cost.total_reasoning_tokens:,}")
        if state.cost.total_cache_read_tokens > 0:
            cache_pct = state.cost.total_cache_read_tokens * 100 // max(state.cost.total_prompt_tokens, 1)
            print(f"  Cache read:    {state.cost.total_cache_read_tokens:,} ({cache_pct}% of prompt)")
        if state.cost.total_cache_creation_tokens > 0:
            print(f"  Cache created: {state.cost.total_cache_creation_tokens:,}")
    print(f"  LLM calls:     {state.model.llm_call_count}")
    print(f"  Total cost:    ${state.cost.total_cost:.4f}")
    print(f"  Elapsed:       {elapsed:.1f}s ({elapsed_min:.1f}min)")
    print(f"{'='*70}\n")

    # The current Vivado design may have regressed after the best result was
    # recorded. Restore the checkpoint that actually owns best_wns before any
    # final checks or writes.
    delivery_ready = True
    if state.control.output_dcp and deps.vivado_session:
        delivery_ready = await _restore_best_checkpoint_for_delivery(state, deps)

    # Check hold timing on the design that will be delivered.
    if state.control.output_dcp and deps.vivado_session and delivery_ready:
        try:
            logger.info("[save_output] Checking hold timing...")
            hold_report = await call_tool_fn(
                "vivado_run_tcl",
                {"command": "report_timing_summary -delay_type min -max_paths 100"},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
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

    # Guard: check design is routed before saving DCP.
    if state.control.output_dcp and deps.vivado_session and delivery_ready:
        try:
            status_result = await call_tool_fn(
                "vivado_run_tcl",
                {"command": "get_property STATUS [current_design]"},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            design_state = _classify_design_state(status_result)
            if design_state == "unknown":
                timing_summary = await call_tool_fn(
                    "vivado_report_timing_summary",
                    {},
                    deps.rapidwright_session, deps.vivado_session,
                    design_size_factor=state.timing.design_size_factor,
                )
                design_state = _classify_design_state(status_result, timing_summary)
                if design_state != "unknown":
                    logger.info(
                        f"[save_output] Design state inferred from timing summary: {design_state}"
                    )

            needs_routing = design_state != "routed"
            if needs_routing:
                # Try restoring from best checkpoint first (fast, ~9s)
                if (state.control.best_checkpoint_path
                        and state.control.best_checkpoint_path.exists()):
                    logger.warning(
                        f"[save_output] Design not routed (state: {design_state}, "
                        f"status: {status_result.strip() or '<empty>'}). "
                        f"Restoring best checkpoint before save."
                    )
                    await call_tool_fn(
                        "vivado_open_checkpoint",
                        {"dcp_path": str(state.control.best_checkpoint_path)},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                    # Re-verify after restore
                    status_result = await call_tool_fn(
                        "vivado_run_tcl",
                        {"command": "get_property STATUS [current_design]"},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                    design_state = _classify_design_state(status_result)
                    if design_state == "unknown":
                        timing_summary = await call_tool_fn(
                            "vivado_report_timing_summary",
                            {},
                            deps.rapidwright_session, deps.vivado_session,
                            design_size_factor=state.timing.design_size_factor,
                        )
                        design_state = _classify_design_state(status_result, timing_summary)
                    if design_state == "routed":
                        logger.info("[save_output] Best checkpoint restored and routed")
                        needs_routing = False

            if needs_routing:
                if design_state != "placed":
                    logger.warning(
                        f"[save_output] Design still not placed (state: {design_state}, "
                        f"status: {status_result.strip() or '<empty>'}). "
                        f"Running emergency place+route."
                    )
                    await call_tool_fn(
                        "vivado_place_design", {"directive": "Default", "timeout": 3600},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                await call_tool_fn(
                    "vivado_route_design", {"directive": "Default", "timeout": 3600},
                    deps.rapidwright_session, deps.vivado_session,
                    design_size_factor=state.timing.design_size_factor,
                )
                logger.info("[save_output] Emergency route completed")
        except Exception as e:
            logger.warning(f"[save_output] Design state check/repair failed: {e}")

    # Write output DCP only after the best checkpoint was restored and verified.
    # On restore failure, the incrementally saved best output remains untouched.
    if state.control.output_dcp and deps.vivado_session and delivery_ready:
        try:
            logger.info(f"[save_output] Writing output DCP to {state.control.output_dcp}")
            result = await call_tool_fn(
                "vivado_write_checkpoint",
                {"dcp_path": str(state.control.output_dcp.resolve()), "force": True},
                deps.rapidwright_session, deps.vivado_session,
                design_size_factor=state.timing.design_size_factor,
            )
            if "error" in result.lower():
                logger.warning(f"[save_output] Failed to write DCP: {result}")
            else:
                logger.info(f"[save_output] Output DCP written successfully")
                # Verify design state after write — catch corrupt DCPs early
                try:
                    final_status = await call_tool_fn(
                        "vivado_run_tcl",
                        {"command": "get_property STATUS [current_design]"},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                    final_ts = await call_tool_fn(
                        "vivado_report_timing_summary",
                        {},
                        deps.rapidwright_session, deps.vivado_session,
                        design_size_factor=state.timing.design_size_factor,
                    )
                    final_state = _classify_design_state(final_status, final_ts)
                    if final_state not in ("routed",):
                        logger.warning(
                            f"[save_output] WARNING: Output DCP design state is '{final_state}' "
                            f"(not 'routed'). The DCP may be corrupt — "
                            f"validate_dcps.py will likely fail."
                        )
                except Exception as ve:
                    logger.warning(f"[save_output] Post-write verification failed: {ve}")
        except Exception as e:
            logger.warning(f"[save_output] Failed to write output DCP: {e}")

    # Validate output DCP (logic equivalence check)
    if (state.control.validation_enabled
            and state.control.output_dcp
            and state.control.input_dcp
            and state.control.output_dcp.exists()):
        logger.info("[save_output] Running DCP validation")
        print("\nRunning DCP validation...")
        validation_result = await _run_validation(
            golden_dcp=state.control.input_dcp,
            revised_dcp=state.control.output_dcp,
        )
        if validation_result["passed"]:
            print("✓ DCP validation PASSED")
            logger.info("[save_output] DCP validation passed")
        else:
            print(f"✗ DCP validation FAILED: {validation_result['error']}")
            logger.warning(
                f"[save_output] DCP validation failed: {validation_result['error']}"
            )

    # Export tracing
    if state.control.run_dir:
        try:
            trace_path = str(state.control.run_dir / "state_transitions.json")
            # Tracer export is handled by the graph builder
            logger.info(f"[save_output] Tracing exported to {trace_path}")
        except Exception as e:
            logger.warning(f"[save_output] Failed to export tracing: {e}")

    return NodeName.END
