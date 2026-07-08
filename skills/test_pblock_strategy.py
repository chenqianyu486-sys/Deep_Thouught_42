#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Unit tests for pblock_strategy and smart_region_search skills.

Tests pure functions that do NOT require a RapidWright Design object:
  - _build_deficit / _build_advice_insufficient / _build_advice_sufficient
  - _validate_pblock_inputs
  - generate_pblock_plan (early-return paths only)
  - _suggest_multi_region_split
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.pblock_strategy import (
    _build_deficit,
    _build_advice_insufficient,
    _build_advice_sufficient,
    _describe_multiplier_transform,
    _should_use_whole_design_fallback,
    _validate_pblock_inputs,
    generate_pblock_plan,
    _estimate_bound_cell_resources,
)
from optimizer.pure.pblock_plan import PBLOCK_GLOBAL_MODE, PBLOCK_UNPLACE_GLOBAL, PblockExecutionPlan
from skills.smart_region_search import _suggest_multi_region_split


# ══════════════════════════════════════════════════════════════════════
# Section A: _build_deficit
# ══════════════════════════════════════════════════════════════════════

def test_build_deficit_basic():
    """DSP/BRAM deficit correctly computed (not hardcoded to 0)."""
    estimated = {"luts": 100, "ffs": 200, "dsps": 5, "brams": 2}
    result = _build_deficit(estimated, 150, 250, 10, 4)
    expected = {"luts": 50, "ffs": 50, "dsps": 5, "brams": 2}
    assert result == expected, f"Expected {expected}, got {result}"
    print(f"  PASSED: dsps={result['dsps']}, brams={result['brams']}")


def test_build_deficit_no_deficit():
    """All zeros when estimated exceeds required."""
    estimated = {"luts": 200, "ffs": 300, "dsps": 10, "brams": 5}
    result = _build_deficit(estimated, 150, 200, 5, 2)
    assert all(v == 0 for v in result.values()), f"Expected all zeros, got {result}"
    print(f"  PASSED: {result}")


def test_build_deficit_partial_deficit():
    """Only deficient resource types show positive deficit."""
    estimated = {"luts": 200, "ffs": 100, "dsps": 5, "brams": 10}
    result = _build_deficit(estimated, 150, 200, 5, 10)
    assert result["luts"] == 0 and result["ffs"] == 100
    assert result["dsps"] == 0 and result["brams"] == 0
    print(f"  PASSED: luts={result['luts']}, ffs={result['ffs']}")


def test_build_deficit_default_dsp_bram():
    """DSP/BRAM default to 0 when not specified (backward compat)."""
    estimated = {"luts": 100, "ffs": 200}
    result = _build_deficit(estimated, 150, 250)
    assert result["dsps"] == 0 and result["brams"] == 0
    print(f"  PASSED: dsps={result['dsps']}, brams={result['brams']}")


def test_build_deficit_missing_estimated_keys():
    """Graceful fallback when estimated dict lacks keys."""
    result = _build_deficit({}, 100, 200, 5, 2)
    assert result == {"luts": 100, "ffs": 200, "dsps": 5, "brams": 2}
    print(f"  PASSED: {result}")


# ══════════════════════════════════════════════════════════════════════
# Section B: _build_advice_insufficient
# ══════════════════════════════════════════════════════════════════════

def test_advice_lut_ff_deficit():
    """Basic LUT/FF deficit: cross-CLR branch and undersized pblock warning."""
    deficit = {"luts": 50, "ffs": 30, "dsps": 0, "brams": 0}
    full = {"luts": 1000, "ffs": 2000}
    result = _build_advice_insufficient(deficit, full, 500, 400, 1.0)
    # multiplier=1.0 → no multiplier advice
    # resources within device → cross-clock-region branch (no "exceed entire")
    # should end with undersized pblock warning
    text = " ".join(result)
    assert "LUTs=50" in text, "Missing LUT deficit"
    assert "FFs=30" in text, "Missing FF deficit"
    assert "cross-clock-region" in text, "Expected cross-CLR branch"
    assert "undersized pblock" in text, "Missing undersized pblock warning"
    print(f"  PASSED: {len(result)} advice lines")


def test_advice_with_multiplier():
    """Multiplier >1.0 produces reduction suggestion as first advice."""
    deficit = {"luts": 0, "ffs": 0, "dsps": 0, "brams": 0}
    full = {"luts": 1000, "ffs": 2000}
    result = _build_advice_insufficient(deficit, full, 500, 400, 1.5)
    assert result[0].startswith("Resource multiplier is 1.5x"), f"Expected multiplier advice first, got: {result[0]}"
    print(f"  PASSED: {result[0][:60]}...")


def test_advice_exceeds_device():
    """Required > full device → 'exceed entire device capacity', no cross-CLR."""
    deficit = {"luts": 50000, "ffs": 60000, "dsps": 0, "brams": 0}
    full = {"luts": 40000, "ffs": 50000}
    result = _build_advice_insufficient(deficit, full, 90000, 80000, 1.0)
    text = " ".join(result)
    assert "exceed entire device capacity" in text, "Expected device-exceed branch"
    assert "cross-clock-region" not in text, "Should NOT have cross-CLR advice"
    print(f"  PASSED: device-exceed branch taken")


def test_advice_dsp_bram_deficit():
    """DSP/BRAM deficit lines are present."""
    deficit = {"luts": 0, "ffs": 0, "dsps": 10, "brams": 4}
    full = {"luts": 1000, "ffs": 2000}
    result = _build_advice_insufficient(deficit, full, 500, 400, 1.0)
    text = " ".join(result)
    assert "DSPs=10" in text and "BRAMs=4" in text
    assert "more DSP/BRAM columns" in text
    print(f"  PASSED: DSP/BRAM deficit advice present")


def test_advice_multi_region():
    """Multi-region split advice when multi_region is non-empty."""
    deficit = {"luts": 50, "ffs": 30, "dsps": 0, "brams": 0}
    full = {"luts": 1000, "ffs": 2000}
    result = _build_advice_insufficient(deficit, full, 500, 400, 1.0,
                                        multi_region=[{"group": 1}, {"group": 2}])
    text = " ".join(result)
    assert "Multi-region split: 2 pblock groups" in text
    print(f"  PASSED: multi-region advice present")


# ══════════════════════════════════════════════════════════════════════
# Section C: _build_advice_sufficient
# ══════════════════════════════════════════════════════════════════════

def test_advice_sufficient_low_density():
    """Low density (<0.80): safe to proceed."""
    result = _build_advice_sufficient(0.50)
    assert len(result) == 1
    assert "safely proceed" in result[0]
    assert "50.0%" in result[0]
    print(f"  PASSED: low-density safe")


def test_advice_sufficient_medium_density():
    """Medium density (0.80-0.90): high-density warning, IS_SOFT noted."""
    result = _build_advice_sufficient(0.85)
    assert len(result) == 1
    assert "Density high" in result[0]
    assert "85.0%" in result[0]
    assert "safely proceed" not in result[0]
    print(f"  PASSED: medium-density warning")


def test_advice_sufficient_high_density():
    """High density (>0.90): congestion warning, NOT safe to proceed."""
    result = _build_advice_sufficient(0.95)
    assert len(result) == 1
    assert "nearly full" in result[0]
    assert "worsen timing" in result[0]
    assert "safely proceed" not in result[0]
    print(f"  PASSED: high-density congestion warning")


# ══════════════════════════════════════════════════════════════════════
# Section C2: _describe_multiplier_transform (multiplier transparency)
# ══════════════════════════════════════════════════════════════════════

def test_multiplier_transform_small_design():
    """Small design (<10% device): adaptive raised above base."""
    # 5000 LUTs / 394000 = 1.3% → small design
    reason = _describe_multiplier_transform(5000, 1000, 1.2, 1.8)
    assert "1.2→1.8" in reason
    assert "small design" in reason
    print(f"  PASSED: {reason}")


def test_multiplier_transform_large_design():
    """Large design (>30% device): adaptive lowered below base."""
    # 150000 LUTs / 394000 = 38% → large design
    reason = _describe_multiplier_transform(150000, 1000, 1.5, 1.2)
    assert "1.5→1.2" in reason
    assert "large design" in reason
    print(f"  PASSED: {reason}")


def test_multiplier_transform_unchanged():
    """Medium design: adaptive equals base, unchanged."""
    # 80000 LUTs / 394000 = 20% → medium design
    reason = _describe_multiplier_transform(80000, 1000, 1.5, 1.5)
    assert "unchanged" in reason
    assert "1.5" in reason
    print(f"  PASSED: {reason}")


# ══════════════════════════════════════════════════════════════════════
# Section D: _validate_pblock_inputs
# ══════════════════════════════════════════════════════════════════════

def test_validate_valid():
    """Valid inputs pass."""
    ok, msg = _validate_pblock_inputs(target_lut_count=5000, target_ff_count=10000)
    assert ok and msg == "", f"Expected (True, ''), got ({ok}, '{msg}')"
    print(f"  PASSED")


def test_validate_missing_lut():
    """Missing target_lut_count."""
    ok, msg = _validate_pblock_inputs(target_ff_count=10000)
    assert not ok and "target_lut_count" in msg
    print(f"  PASSED: {msg}")


def test_validate_missing_ff():
    """Missing target_ff_count."""
    ok, msg = _validate_pblock_inputs(target_lut_count=5000)
    assert not ok and "target_ff_count" in msg
    print(f"  PASSED: {msg}")


def test_validate_non_positive_lut():
    """Negative LUT value."""
    ok, msg = _validate_pblock_inputs(target_lut_count=-1, target_ff_count=10000)
    assert not ok and "positive integer" in msg
    print(f"  PASSED: {msg}")


def test_validate_non_positive_ff():
    """Zero FF value."""
    ok, msg = _validate_pblock_inputs(target_lut_count=5000, target_ff_count=0)
    assert not ok and "positive integer" in msg
    print(f"  PASSED: {msg}")


def test_validate_non_integer_type():
    """String instead of int for LUT."""
    ok, msg = _validate_pblock_inputs(target_lut_count="5000", target_ff_count=10000)
    assert not ok and "positive integer" in msg
    print(f"  PASSED: {msg}")


# ══════════════════════════════════════════════════════════════════════
# Section E: generate_pblock_plan early-return paths
# ══════════════════════════════════════════════════════════════════════

def test_generate_plan_design_none():
    """design=None → status=error."""
    result = generate_pblock_plan(None, target_lut_count=5000, target_ff_count=10000)
    assert result.get("status") == "error"
    assert "Design not loaded" in result.get("message", "")
    assert result.get("error_details") == "context.design is None"
    print(f"  PASSED: {result['status']} - {result['message'][:50]}")


def test_generate_plan_target_lut_zero():
    """target_lut_count=0 → status=skipped."""
    result = generate_pblock_plan("mock", target_lut_count=0, target_ff_count=10000)
    assert result.get("status") == "skipped"
    assert "LUT=0" in result.get("message", "")
    assert "target_resources" in result
    print(f"  PASSED: {result['status']}")


def test_generate_plan_target_lut_negative():
    """target_lut_count negative → status=skipped."""
    result = generate_pblock_plan("mock", target_lut_count=-100, target_ff_count=10000)
    assert result.get("status") == "skipped"
    assert "LUT=-100" in result.get("message", "")
    print(f"  PASSED: {result['status']}")


# ══════════════════════════════════════════════════════════════════════
# Section F: _suggest_multi_region_split
# ══════════════════════════════════════════════════════════════════════

def _make_col(col_idx, slices, dsps=5, brams=2, min_row=10, max_row=170,
              delay_heavy=False):
    return {
        "col_idx": col_idx,
        "slice_sites": slices,
        "dsp_sites": dsps,
        "bram_sites": brams,
        "min_row": min_row,
        "max_row": max_row,
        "has_delay_heavy": delay_heavy,
    }


def test_multi_region_basic():
    """Balanced synthetic columns → 2 groups with proportional targets."""
    cols = [_make_col(i, 1000) for i in range(6)]
    index = {"total_slice_sites": 6000, "total_dsps": 30, "total_brams": 12}
    result = _suggest_multi_region_split(cols, 3000, 15, 6, index)
    assert len(result) == 2, f"Expected 2 groups, got {len(result)}"
    total = result[0]["estimated_luts"] + result[1]["estimated_luts"]
    assert total == 24000, f"Estimated LUT sum should be 24000, got {total}"
    print(f"  PASSED: {len(result)} groups, {total} total LUTs")


def test_multi_region_not_enough():
    """Required >> available → empty list."""
    cols = [_make_col(i, 100) for i in range(2)]
    index = {"total_slice_sites": 200, "total_dsps": 10, "total_brams": 4}
    result = _suggest_multi_region_split(cols, 10000, 10, 4, index)
    assert result == [], f"Expected empty list, got {len(result)} groups"
    print(f"  PASSED: empty (total_slices < required * 0.5)")


def test_multi_region_few_columns():
    """Only 1 usable column (others delay-heavy) → empty list."""
    cols = [
        _make_col(0, 1000, delay_heavy=False),
        _make_col(1, 1000, delay_heavy=True),
        _make_col(2, 1000, delay_heavy=True),
    ]
    index = {"total_slice_sites": 3000, "total_dsps": 15, "total_brams": 6}
    result = _suggest_multi_region_split(cols, 500, 5, 2, index)
    assert result == [], f"Expected empty list (only 1 usable), got {len(result)} groups"
    print(f"  PASSED: empty (< 2 usable columns)")


def test_multi_region_density_aware():
    """Split point respects cumulative density, not midpoint."""
    cols = [
        _make_col(0, 4000),
        _make_col(1, 4000),
        _make_col(2, 100),
        _make_col(3, 100),
        _make_col(4, 100),
    ]
    index = {"total_slice_sites": 8300, "total_dsps": 25, "total_brams": 10}
    result = _suggest_multi_region_split(cols, 4000, 10, 5, index)
    assert len(result) == 2, f"Expected 2 groups, got {len(result)}"
    # Group A should be cols 0-1 (cumulative 8000 >= 4150)
    assert result[0]["cols"] == [0, 1], f"Group A should cover cols 0-1, got {result[0]['cols']}"
    # Group A should have ~96% of slices
    assert result[0]["estimated_luts"] > result[1]["estimated_luts"] * 5
    print(f"  PASSED: Group A cols={result[0]['cols']}, LUTs={result[0]['estimated_luts']}")


# ══════════════════════════════════════════════════════════════════════
# Section G: _estimate_bound_cell_resources (local-pblock sizing)
# ══════════════════════════════════════════════════════════════════════

class _MockCell:
    """Minimal stand-in for a RapidWright Cell exposing getType()."""
    def __init__(self, ctype: str):
        self._ctype = ctype

    def getType(self):
        return self._ctype


class _MockDesign:
    """Resolves a subset of cell names to _MockCell objects via getCell()."""
    def __init__(self, cells: dict[str, str]):
        # cells: {name: type_str}
        self._cells = cells

    def getCell(self, name):
        ctype = self._cells.get(name)
        return _MockCell(ctype) if ctype is not None else None


def test_bound_resources_lut_ff_muxf():
    """LUT6 + FDRE + MUXF7 → luts=2 (LUT6 + MUXF7), ffs=1."""
    design = _MockDesign({
        "u/lut6_a": "LUT6",
        "u/fdre_b": "FDRE",
        "u/muxf7_c": "MUXF7",
    })
    result = _estimate_bound_cell_resources(design, list(design._cells.keys()))
    assert result is not None
    assert result["luts"] == 2, f"Expected luts=2 (LUT6+MUXF7), got {result['luts']}"
    assert result["ffs"] == 1, f"Expected ffs=1, got {result['ffs']}"
    assert result["dsps"] == 0 and result["brams"] == 0
    assert result["matched"] == 3
    print(f"  PASSED: luts={result['luts']}, ffs={result['ffs']} (MUXF counted as LUT)")


def test_bound_resources_dsp_bram():
    """DSP48E2 + RAMB36 → dsps=1, brams=1."""
    design = _MockDesign({
        "u/dsp_a": "DSP48E2",
        "u/ramb_b": "RAMB36",
    })
    result = _estimate_bound_cell_resources(design, list(design._cells.keys()))
    assert result["dsps"] == 1 and result["brams"] == 1
    assert result["luts"] == 0 and result["ffs"] == 0
    print(f"  PASSED: dsps={result['dsps']}, brams={result['brams']}")


def test_bound_resources_no_match():
    """No cell resolves → None (caller falls back to whole-design sizing)."""
    design = _MockDesign({"u/real": "LUT6"})
    result = _estimate_bound_cell_resources(design, ["u/ghost1", "u/ghost2"])
    assert result is None, f"Expected None when 0 cells match, got {result}"
    print(f"  PASSED: None (0 match → fallback)")


def test_bound_resources_empty():
    """Empty cell list → None."""
    design = _MockDesign({})
    result = _estimate_bound_cell_resources(design, [])
    assert result is None
    print(f"  PASSED: None (empty input)")


def test_bound_resources_partial_match():
    """Half matched still returns a dict (matched < total, low-rate warning)."""
    design = _MockDesign({
        "u/lut_a": "LUT6",
        "u/lut_b": "LUT5",
    })
    # 4 names, only 2 resolve → matched=2, total=4 (exactly 50% → passes threshold)
    result = _estimate_bound_cell_resources(
        design, ["u/lut_a", "u/lut_b", "u/ghost1", "u/ghost2"]
    )
    assert result is not None
    assert result["matched"] == 2 and result["total"] == 4
    assert result["luts"] == 2
    print(f"  PASSED: matched={result['matched']}/{result['total']} (partial)")


# ══════════════════════════════════════════════════════════════════════
# Main orchestrator
# ══════════════════════════════════════════════════════════════════════

def test_local_pblock_fallback_triggers_for_single_column_tiny_bound_region():
    """Large designs should not keep an ultra-tiny single-column local pblock."""
    should_fallback, reason = _should_use_whole_design_fallback(
        sizing_basis="bound_cells",
        columns_used=1,
        utilization_density=0.03,
        target_lut_count=31370,
        target_ff_count=1660,
        bound_resources={"luts": 35, "ffs": 15},
    )
    assert should_fallback is True
    assert "single-column" in reason
    print(f"  PASSED: {reason}")


def test_local_pblock_fallback_does_not_trigger_for_multi_column_region():
    """A wider local pblock should keep bound-cell binding."""
    should_fallback, reason = _should_use_whole_design_fallback(
        sizing_basis="bound_cells",
        columns_used=3,
        utilization_density=0.03,
        target_lut_count=31370,
        target_ff_count=1660,
        bound_resources={"luts": 35, "ffs": 15},
    )
    assert should_fallback is False
    assert reason is None
    print("  PASSED: multi-column local pblock preserved")


def main():
    print("=" * 60)
    print("Unit Tests for pblock_strategy / smart_region_search")
    print("=" * 60)

    tests = [
        # Section A
        test_build_deficit_basic,
        test_build_deficit_no_deficit,
        test_build_deficit_partial_deficit,
        test_build_deficit_default_dsp_bram,
        test_build_deficit_missing_estimated_keys,
        # Section B
        test_advice_lut_ff_deficit,
        test_advice_with_multiplier,
        test_advice_exceeds_device,
        test_advice_dsp_bram_deficit,
        test_advice_multi_region,
        # Section C
        test_advice_sufficient_low_density,
        test_advice_sufficient_medium_density,
        test_advice_sufficient_high_density,
        # Section C2
        test_multiplier_transform_small_design,
        test_multiplier_transform_large_design,
        test_multiplier_transform_unchanged,
        test_local_pblock_fallback_triggers_for_single_column_tiny_bound_region,
        test_local_pblock_fallback_does_not_trigger_for_multi_column_region,
        # Section D
        test_validate_valid,
        test_validate_missing_lut,
        test_validate_missing_ff,
        test_validate_non_positive_lut,
        test_validate_non_positive_ff,
        test_validate_non_integer_type,
        # Section E
        test_generate_plan_design_none,
        test_generate_plan_target_lut_zero,
        test_generate_plan_target_lut_negative,
        # Section F
        test_multi_region_basic,
        test_multi_region_not_enough,
        test_multi_region_few_columns,
        test_multi_region_density_aware,
        # Section G
        test_bound_resources_lut_ff_muxf,
        test_bound_resources_dsp_bram,
        test_bound_resources_no_match,
        test_bound_resources_empty,
        test_bound_resources_partial_match,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
        print()

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    return 0 if failed == 0 else 1


def test_generate_plan_accepts_frozen_plan_without_replanning():
    """Frozen plan should round-trip through generate_pblock_plan unchanged."""
    frozen = PblockExecutionPlan(
        plan_mode=PBLOCK_GLOBAL_MODE,
        candidate_id="global_cp_center",
        pblock_name="pblock_tight",
        pblock_ranges="SLICE_X0Y0:SLICE_X54Y299",
        resource_multiplier=2.0,
        target_lut_count=12000,
        target_ff_count=24000,
        target_dsp_count=0,
        target_bram_count=0,
        bind_cells_to_pblock=False,
        unplace_mode=PBLOCK_UNPLACE_GLOBAL,
        is_soft=True,
        place_directive="Explore",
        route_directive="Explore",
        reference_col=48,
        reference_row=150,
        selection_reason="global_cp_center:smart_region_search",
        critical_path_cells_snapshot=["u0", "u1"],
        capacity_ok=True,
        estimated_resources={"luts": 14000, "ffs": 28000, "dsps": 0, "brams": 0},
        region={"col_min": 0, "col_max": 54, "row_min": 0, "row_max": 299, "columns_used": 55},
        utilization_density=0.86,
    )
    result = generate_pblock_plan(
        "mock",
        target_lut_count=12000,
        target_ff_count=24000,
        frozen_pblock_plan=frozen.to_dict(),
    )
    assert result.get("status") == "success"
    assert result.get("recommended_candidate_id") == "global_cp_center"
    assert result.get("selected_pblock_plan", {}).get("candidate_id") == "global_cp_center"
    assert result.get("candidate_plans")[0]["candidate_id"] == "global_cp_center"
    assert result.get("bind_critical_path_cells_to_pblock") is False


def test_generate_plan_rejects_invalid_frozen_plan():
    """Malformed frozen plan should fail fast."""
    result = generate_pblock_plan(
        "mock",
        target_lut_count=12000,
        target_ff_count=24000,
        frozen_pblock_plan={"candidate_id": "broken"},
    )
    assert result.get("status") == "error"
    assert "Invalid frozen_pblock_plan" in result.get("message", "")


if __name__ == "__main__":
    sys.exit(main())
