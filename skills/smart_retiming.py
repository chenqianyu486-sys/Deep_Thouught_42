"""Smart Retiming Optimizer — verified incremental pipeline register insertion.

Wraps the existing analyze_register_retiming + ECOTools FF insertion with:
  - Candidate scoring, filtering, sorting, dedup
  - Incremental (one-at-a-time) FF insertion
  - RapidWright report_timing estimation after each insertion (~2.5 s)
  - Auto-rollback of degradations
  - Detailed per-candidate report

Pure RapidWright domain — Vivado steps returned as post_actions.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill
from skills.errors import SkillErrorCode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helper functions  (no side effects, no state)
# ---------------------------------------------------------------------------

def _score_and_filter_candidates(
    candidates: list[dict],
    min_chain_depth: int = 2,
    wns_threshold: float = -0.3,
    max_fanout: int = 50,
) -> list[dict]:
    """Score, filter, sort, and deduplicate retiming candidates.

    Filter rules:
      - combinational_depth >= min_chain_depth
      - slack < wns_threshold  (only paths worse than threshold)
      - insertion_net_fanout <= max_fanout
      - branched == False  (single-fanout insertion points only)

    Score:  chain_depth * |slack| / ln(fanout + 1)

    Dedup:  same insertion_net → keep highest score.
    """
    scored: list[dict] = []

    for c in candidates:
        chain_depth = c.get("combinational_depth", 0)
        slack = c.get("slack", 0.0)
        fanout = c.get("insertion_net_fanout", 1)
        branched = c.get("branched", False)

        # Filter
        if chain_depth < min_chain_depth:
            continue
        if slack >= wns_threshold:
            continue
        if fanout > max_fanout:
            continue
        if branched:
            continue

        # Score (geometric mean of depth impact and fanout penalty)
        # Guard: if fanout == 0, ln(1) = 0 → skip (degenerate case)
        if fanout <= 0:
            continue
        score = chain_depth * abs(slack) / math.log(fanout + 1)
        scored.append({**c, "_score": round(score, 3)})

    # Sort descending by score
    scored.sort(key=lambda c: c["_score"], reverse=True)

    # Dedup by insertion_net — keep highest score
    seen_nets: set[str] = set()
    deduped: list[dict] = []
    for c in scored:
        net = c.get("insertion_net", "")
        if net and net in seen_nets:
            continue
        seen_nets.add(net)
        deduped.append(c)

    return deduped


# ---------------------------------------------------------------------------
# FF insertion helper (reuses existing register_retiming_strategy internals)
# ---------------------------------------------------------------------------

def _insert_single_ff(design, candidate: dict) -> dict:
    """Insert a single pipeline FF for one retiming candidate.

    Reuses the ECOTools-based insertion logic from register_retiming_strategy:
      _find_available_slice → createAndPlaceInlineCellOnInputPin → connect control nets.

    Returns:
        {"success": True, "new_ff_name": ..., "site": ..., "bel": ...}
        or {"success": False, "error": "reason"}
    """
    from com.xilinx.rapidwright.eco import ECOTools
    from com.xilinx.rapidwright.design import Unisim

    # Import internal helpers from the existing retiming module
    from skills.register_retiming_strategy import (
        _find_available_slice,
        _get_ff_control_nets,
    )

    _ff_type_to_unisim = {
        "FDRE": Unisim.FDRE,
        "FDSE": Unisim.FDSE,
        "FDCE": Unisim.FDCE,
        "FDPE": Unisim.FDPE,
    }

    dest_ff = candidate.get("destination_ff", "")
    insertion_ref_cell = candidate.get("insertion_ref_cell", "")
    ff_type_str = candidate.get("destination_ff_type", "FDRE")

    # Resolve the destination FF cell
    dest_cell = design.getCell(dest_ff)
    if dest_cell is None:
        return {"success": False, "error": f"Destination FF '{dest_ff}' not found"}

    # Get hierarchical port for the D pin
    ehci = dest_cell.getEDIFHierCellInst()
    if ehci is None:
        return {"success": False, "error": "Cannot get hierarchical cell instance"}

    d_port_inst = ehci.getPortInst("D")
    if d_port_inst is None:
        return {"success": False, "error": "Cannot find D port on destination FF"}

    # Find a placement reference site
    ref_site = None
    if insertion_ref_cell:
        ref_cell = design.getCell(insertion_ref_cell)
        if ref_cell is not None and ref_cell.isPlaced():
            ref_site = ref_cell.getSite()
    if ref_site is None and dest_cell.isPlaced():
        ref_site = dest_cell.getSite()
    if ref_site is None:
        return {"success": False, "error": "No reference site available"}

    # Spiral search for an empty SLICE
    new_site, bel_name = _find_available_slice(design, ref_site)
    if new_site is None:
        return {"success": False, "error": "No available SLICE near insertion point"}

    bel = new_site.getBEL(bel_name)
    unisim_type = _ff_type_to_unisim.get(ff_type_str.upper(), Unisim.FDRE)

    # Core FF insertion
    new_cell = ECOTools.createAndPlaceInlineCellOnInputPin(
        design, d_port_inst, unisim_type, new_site, bel, "D", "Q"
    )
    if new_cell is None:
        return {"success": False, "error": "createAndPlaceInlineCellOnInputPin returned None"}

    new_ff_name = str(new_cell.getName())

    # Connect control signals
    ctrl_nets = _get_ff_control_nets(design, dest_ff)
    ctrl_status = {"clk": False, "ce": False, "rst": False}

    if ctrl_nets["clk_net"]:
        try:
            clk_net = design.getNet(ctrl_nets["clk_net"])
            if clk_net is not None:
                ECOTools.connectNet(design, new_cell, "C", clk_net)
                ctrl_status["clk"] = True
        except Exception:
            pass

    if ctrl_nets["ce_net"]:
        try:
            ce_net = design.getNet(ctrl_nets["ce_net"])
            if ce_net is not None:
                ECOTools.connectNet(design, new_cell, "CE", ce_net)
                ctrl_status["ce"] = True
        except Exception:
            pass

    if ctrl_nets["rst_net"]:
        try:
            rst_net = design.getNet(ctrl_nets["rst_net"])
            if rst_net is not None:
                ECOTools.connectNet(design, new_cell, "R", rst_net)
                ctrl_status["rst"] = True
        except Exception:
            pass

    return {
        "success": True,
        "new_ff_name": new_ff_name,
        "site": str(new_site),
        "bel": bel_name,
        "control_signals": ctrl_status,
        "ff_type": ff_type_str,
    }


# ---------------------------------------------------------------------------
# Timing estimation helper
# ---------------------------------------------------------------------------

def _estimate_wns(design) -> tuple[float | None, str | None]:
    """Estimate WNS using RapidWright TimingGraph (~2 % error, ~2.5 s).

    Must be called while _current_design == design (the normal case when
    invoked from the rapidwright_tools wrapper).

    Returns (wns_ns, error_message).  wns_ns is None on failure.
    """
    try:
        from RapidWrightMCP.rapidwright_tools import report_timing

        result = report_timing()
        if "error" in result:
            return None, result.get("error")
        wns = result.get("wns_ns", None)
        return wns, None
    except ImportError:
        logger.warning("Cannot import report_timing — RapidWright timing unavailable")
        return None, "report_timing unavailable"
    except Exception as e:
        logger.warning(f"Timing estimation failed: {e}")
        return None, str(e)


# ---------------------------------------------------------------------------
# Skill class
# ---------------------------------------------------------------------------

@skill(
    name="smart_retiming",
    namespace="optimization",
    version="1.0.0",
    display_name="Smart Retiming Optimizer",
    description=(
        "Iteratively insert pipeline registers on deep combinational chains, "
        "verify each insertion with RapidWright timing estimation, and "
        "auto-rollback on degradation. Returns final checkpoint + post_actions "
        "for Vivado sign-off. "
        "MUTATING. Side effects: new FF cells added, net topology changed, "
        "checkpoint files written. "
        "Trigger: WNS stuck, critical paths have deep combinational chains "
        "(>2 LUTs) between pipeline registers."
    ),
    category=SkillCategory.OPTIMIZATION,
    idempotency="non-idempotent",
    side_effects=["cell_creation", "net_topology", "checkpoint_file"],
    timeout_ms=300_000,
    parameters=[
        ParameterSpec("critical_paths", list,
            "Critical path pin data from vivado_extract_critical_path_pins. "
            "Each dict must contain: path_index, pins (list of pin strings), "
            "slack (float). See analyze_register_retiming for expected format."),
        ParameterSpec("max_ops", int,
            "Maximum FF insertions to perform. Hard cap of 10 per call to "
            "limit impact scope. Default: 5.",
            default=5),
        ParameterSpec("min_chain_depth", int,
            "Minimum LUT chain depth to consider. Chains shorter than this "
            "are skipped. Default: 2.",
            default=2),
        ParameterSpec("wns_threshold", float,
            "Only process paths with slack worse than this value (ns). "
            "Default: -0.3.",
            default=-0.3),
        ParameterSpec("verify_each", bool,
            "If True, run RapidWright report_timing after each FF insertion "
            "to estimate WNS impact (~2.5s). Default: True.",
            default=True),
        ParameterSpec("auto_rollback", bool,
            "If True and verify_each is True, restore the pre-insertion "
            "checkpoint when estimated WNS degrades >0.001ns. Default: True.",
            default=True),
        ParameterSpec("temp_dir", str,
            "Directory for intermediate checkpoints. Default: 'temp'.",
            default="temp"),
        ParameterSpec("checkpoint_prefix", str,
            "Filename prefix for saved checkpoints. "
            "Default: 'smart_retime'.",
            default="smart_retime"),
        ParameterSpec("max_fanout_for_insertion", int,
            "Skip insertion points with net fanout exceeding this value. "
            "Default: 50.",
            default=50),
    ],
    required_context=["design"],
    error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND",
                 "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"],
)
class SmartRetimingSkill(Skill):
    """Smart retiming with incremental verification and auto-rollback."""

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        critical_paths = kwargs.get("critical_paths")
        if not critical_paths:
            return False, "critical_paths is required and must be non-empty"
        if not isinstance(critical_paths, list):
            return False, "critical_paths must be a list of path dicts"

        max_ops = kwargs.get("max_ops", 5)
        if not isinstance(max_ops, int) or max_ops < 1 or max_ops > 10:
            return False, "max_ops must be an integer between 1 and 10"

        return True, ""

    def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        design = context.design
        if design is None:
            return SkillResult(success=False, error="Design not loaded",
                               error_code=SkillErrorCode.INVALID_PARAMETER)

        critical_paths = kwargs.get("critical_paths", [])
        max_ops = min(int(kwargs.get("max_ops", 5)), 10)
        min_chain_depth = int(kwargs.get("min_chain_depth", 2))
        wns_threshold = float(kwargs.get("wns_threshold", -0.3))
        verify_each = bool(kwargs.get("verify_each", True))
        auto_rollback = bool(kwargs.get("auto_rollback", True))
        temp_dir = str(kwargs.get("temp_dir", "temp"))
        ckpt_prefix = str(kwargs.get("checkpoint_prefix", "smart_retime"))
        max_fanout = int(kwargs.get("max_fanout_for_insertion", 50))

        os.makedirs(temp_dir, exist_ok=True)

        # ---- Phase 1: PRE-FLIGHT ----
        logger.info("[smart_retiming] Phase 1: PRE-FLIGHT")
        pre_ckpt = os.path.join(temp_dir, f"{ckpt_prefix}_pre_retime.dcp")
        try:
            design.writeCheckpoint(pre_ckpt)
            logger.info(f"Pre-retime checkpoint: {pre_ckpt}")
        except Exception as e:
            return SkillResult(success=False,
                               error=f"Failed to save pre-retime checkpoint: {e}",
                               error_code=SkillErrorCode.TEMPORARILY_UNAVAILABLE)

        baseline_wns, timing_err = _estimate_wns(design)
        # Fallback: use cached WNS from Vivado if RapidWright estimation fails
        if baseline_wns is None:
            cached_wns = kwargs.get("cached_wns")
            if cached_wns is not None:
                baseline_wns = float(cached_wns)
                logger.info(f"Baseline WNS (Vivado fallback): {baseline_wns}")
            else:
                logger.info(f"Baseline WNS (RapidWright): None — {timing_err}")
        else:
            logger.info(f"Baseline WNS (RapidWright): {baseline_wns}")

        # ---- Phase 2: ANALYZE & SCORE ----
        logger.info("[smart_retiming] Phase 2: ANALYZE & SCORE")
        from skills.register_retiming_strategy import analyze_register_retiming

        try:
            analysis = analyze_register_retiming(
                design,
                critical_paths=critical_paths,
                delay_threshold=0.5,
                min_chain_depth=min_chain_depth,
            )
        except Exception as e:
            return SkillResult(
                success=False,
                error=f"analyze_register_retiming failed: {e}",
                error_code=SkillErrorCode.INVALID_PARAMETER,
            )
        if isinstance(analysis, dict) and "error" in analysis:
            return SkillResult(success=False,
                               error=f"Analysis failed: {analysis['error']}",
                               error_code=SkillErrorCode.TEMPORARILY_UNAVAILABLE)

        raw_candidates = analysis.get("candidates", []) if isinstance(analysis, dict) else []
        if not raw_candidates:
            return SkillResult(success=True, data={
                "baseline_wns": baseline_wns,
                "candidates_total": 0,
                "inserted": 0,
                "rolled_back": 0,
                "skipped": 0,
                "message": "No retiming candidates found",
                "final_checkpoint_path": pre_ckpt,
                "post_actions": ["vivado_open_checkpoint", "vivado_route_design",
                                 "vivado_report_timing_summary"],
                "per_candidate": [],
            })

        scored = _score_and_filter_candidates(
            raw_candidates,
            min_chain_depth=min_chain_depth,
            wns_threshold=wns_threshold,
            max_fanout=max_fanout,
        )
        logger.info(f"Candidates: {len(raw_candidates)} raw → "
                    f"{len(scored)} scored (min_depth={min_chain_depth}, "
                    f"wns_threshold={wns_threshold}, max_fanout={max_fanout})")

        # ---- Phase 3: INCREMENTAL EXECUTE ----
        logger.info(f"[smart_retiming] Phase 3: INCREMENTAL EXECUTE (max_ops={max_ops})")
        per_candidate: list[dict] = []
        inserted = 0
        rolled_back = 0

        for idx, candidate in enumerate(scored):
            if inserted >= max_ops:
                break

            # Save pre-insertion checkpoint
            pre_ckpt_i = os.path.join(temp_dir, f"{ckpt_prefix}_{idx}_pre.dcp")
            try:
                design.writeCheckpoint(pre_ckpt_i)
            except Exception:
                pass  # non-fatal

            # Insert
            start = time.time()
            ins_result = _insert_single_ff(design, candidate)
            elapsed = time.time() - start

            entry = {
                "path_index": candidate.get("path_index", idx),
                "source_ff": candidate.get("source_ff", ""),
                "destination_ff": candidate.get("destination_ff", ""),
                "chain_depth": candidate.get("combinational_depth", 0),
                "score": candidate.get("_score", 0),
                "fanout": candidate.get("insertion_net_fanout", 0),
                "insertion_elapsed_s": round(elapsed, 2),
            }

            if not ins_result.get("success"):
                entry["status"] = "skipped"
                entry["error"] = ins_result.get("error", "insertion failed")
                per_candidate.append(entry)
                continue

            entry["new_ff_name"] = ins_result.get("new_ff_name", "")
            entry["site"] = ins_result.get("site", "")

            # Save post-insertion checkpoint
            post_ckpt_i = os.path.join(temp_dir, f"{ckpt_prefix}_{idx}_post.dcp")
            try:
                design.writeCheckpoint(post_ckpt_i)
            except Exception:
                pass

            # Verify
            if verify_each:
                est_wns, _ = _estimate_wns(design)
                entry["estimated_wns"] = est_wns
                if baseline_wns is not None and est_wns is not None:
                    delta = est_wns - baseline_wns
                    entry["estimated_wns_delta"] = round(delta, 4)
                    if delta < -0.001 and auto_rollback:
                        # Degradation → rollback
                        try:
                            from com.xilinx.rapidwright.design import Design
                            restored = Design.readCheckpoint(pre_ckpt_i)
                            # Mutate context design in-place (RapidWright)
                            # The design reference itself is replaced
                            entry["status"] = "rolled_back"
                            entry["rollback_reason"] = (
                                f"WNS degraded by {delta:.4f}ns "
                                f"({baseline_wns:.4f} → {est_wns:.4f})"
                            )
                            rolled_back += 1
                            per_candidate.append(entry)
                            # Restore context.design to the pre-insertion state
                            # We can't replace context.design, but we can
                            # re-read the checkpoint into the same variable
                            # via the global _current_design in rapidwright_tools
                            import RapidWrightMCP.rapidwright_tools as rwt
                            rwt._current_design = restored
                            # Also try to update context.design for consistency
                            try:
                                object.__setattr__(context, 'design', restored)
                            except Exception:
                                pass  # context.design will be stale, but rwt._current_design is canonical
                            # Also update the local design reference used below
                            design = restored
                            # Also update context.design (mutate the ref)
                            # context is a frozen dataclass, so we work via
                            # rwt._current_design which is the real singleton
                            logger.info(
                                f"Rolled back candidate {idx}: {entry['rollback_reason']}"
                            )
                            continue
                        except Exception as e:
                            logger.warning(f"Rollback failed for candidate {idx}: {e}")
                            entry["status"] = "inserted"
                            entry["rollback_failed"] = str(e)
                    else:
                        entry["status"] = "inserted"
                else:
                    entry["status"] = "inserted"
            else:
                entry["status"] = "inserted"

            inserted += 1
            per_candidate.append(entry)

        # ---- Phase 4: FINAL CHECKPOINT ----
        logger.info("[smart_retiming] Phase 4: FINAL CHECKPOINT")
        final_ckpt = os.path.join(temp_dir, f"{ckpt_prefix}_final.dcp")
        try:
            design.writeCheckpoint(final_ckpt)
            logger.info(f"Final checkpoint: {final_ckpt}")
        except Exception as e:
            return SkillResult(success=False,
                               error=f"Failed to write final checkpoint: {e}",
                               error_code=SkillErrorCode.TEMPORARILY_UNAVAILABLE)

        # ---- Phase 5: REPORT ----
        logger.info("[smart_retiming] Phase 5: REPORT")
        final_wns, _ = _estimate_wns(design) if verify_each else (None, None)

        skipped = len(scored) - inserted - rolled_back
        wns_delta = None
        if baseline_wns is not None and final_wns is not None:
            wns_delta = round(final_wns - baseline_wns, 4)

        summary = {
            "baseline_wns": baseline_wns,
            "final_estimated_wns": final_wns,
            "wns_delta": wns_delta,
            "candidates_raw": len(raw_candidates),
            "candidates_scored": len(scored),
            "inserted": inserted,
            "rolled_back": rolled_back,
            "skipped": skipped,
            "max_ops": max_ops,
            "verify_each": verify_each,
            "auto_rollback": auto_rollback,
        }

        return SkillResult(success=True, data={
            **summary,
            "final_checkpoint_path": final_ckpt,
            "pre_retime_checkpoint_path": pre_ckpt,
            "post_actions": [
                f"vivado_open_checkpoint(dcp_path='{final_ckpt}')",
                "vivado_route_design",
                "vivado_report_timing_summary",
                "rapidwright_compare_design_structure(golden_dcp, revised_dcp)",
            ],
            "per_candidate": per_candidate,
        })
