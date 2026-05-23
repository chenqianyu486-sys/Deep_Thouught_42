"""Tests for smart_retiming skill — pure functions and mock-based integration.

Run:  python3 skills/test_smart_retiming.py
"""

from __future__ import annotations

import math
import sys
import os
from unittest.mock import MagicMock, patch

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.smart_retiming import (
    _score_and_filter_candidates,
    SmartRetimingSkill,
)


# ============================================================================
# Test harness (same pattern as test_pblock_strategy.py)
# ============================================================================

TESTS = []
PASSED = 0
FAILED = 0


def test(name: str):
    """Decorator to register test functions."""
    def decorator(fn):
        TESTS.append((name, fn))
        return fn
    return decorator


def run():
    global PASSED, FAILED
    PASSED = FAILED = 0
    print(f"\n{'=' * 60}")
    print("smart_retiming Tests")
    print(f"{'=' * 60}")
    for name, fn in TESTS:
        try:
            fn()
            PASSED += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            FAILED += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            FAILED += 1
            print(f"  ERROR {name}: {e}")
            import traceback
            traceback.print_exc()
    print(f"\n{'=' * 60}")
    print(f"Results: {PASSED} passed, {FAILED} failed, {PASSED + FAILED} total")
    print(f"{'=' * 60}")
    return FAILED == 0


# ============================================================================
# Helper factories
# ============================================================================

def _candidate(**overrides) -> dict:
    """Build a retiming candidate dict with sensible defaults."""
    return {
        "path_index": 0,
        "source_ff": "reg_a/Q",
        "destination_ff": "reg_b/D",
        "combinational_depth": 4,
        "slack": -0.5,
        "insertion_net": "lut5/O",
        "insertion_net_fanout": 1,
        "branched": False,
        "insertion_ref_cell": "lut5",
        "destination_ff_type": "FDRE",
        **overrides,
    }


# ============================================================================
# Section A: _score_and_filter_candidates()
# ============================================================================

@test("A1: empty candidates returns empty list")
def test_a1_empty():
    result = _score_and_filter_candidates([], min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert result == [], f"Expected [], got {result}"


@test("A2: all filtered out (branched + insufficient depth + good slack)")
def test_a2_all_filtered():
    candidates = [
        _candidate(path_index=0, branched=True, combinational_depth=4, slack=-0.5),
        _candidate(path_index=1, combinational_depth=1, slack=-0.5),
        _candidate(path_index=2, slack=-0.2),  # better than threshold
    ]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert result == [], f"All should be filtered, got {len(result)}"


@test("A3: single candidate passes — correct score")
def test_a3_single():
    candidates = [_candidate(combinational_depth=4, slack=-0.5, insertion_net_fanout=1)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert len(result) == 1, f"Expected 1, got {len(result)}"
    expected = round(4 * 0.5 / math.log(2), 3)
    assert result[0]["_score"] == expected, f"Score {result[0]['_score']} != {expected}"


@test("A4: branched=True excluded")
def test_a4_branched():
    candidates = [
        _candidate(path_index=0, branched=True, combinational_depth=5, slack=-0.9),
        _candidate(path_index=1, branched=False, combinational_depth=3, slack=-0.5),
    ]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert len(result) == 1, f"branched should be excluded, got {len(result)}"
    assert result[0]["path_index"] == 1


@test("A5: combinational_depth below threshold filtered")
def test_a5_depth():
    candidates = [_candidate(combinational_depth=1)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert result == [], f"depth=1 should be filtered"


@test("A6: slack equal to threshold is filtered (strict <)")
def test_a6_slack_equal():
    candidates = [_candidate(slack=-0.3)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert result == [], f"slack=-0.3 should be filtered (strict < threshold)"


@test("A7: slack just below threshold passes")
def test_a7_slack_below():
    candidates = [_candidate(slack=-0.301)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert len(result) == 1, f"slack=-0.301 should pass"


@test("A8: fanout at max boundary passes (<=)")
def test_a8_fanout_equal_max():
    candidates = [_candidate(insertion_net_fanout=50)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert len(result) == 1, f"fanout=50 (equal to max) should pass"


@test("A9: fanout above max filtered")
def test_a9_fanout_above():
    candidates = [_candidate(insertion_net_fanout=51)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert result == [], f"fanout=51 should be filtered"


@test("A10: score formula verification (known values)")
def test_a10_score_formula():
    c = _candidate(combinational_depth=3, slack=-0.6, insertion_net_fanout=1)
    result = _score_and_filter_candidates([c], min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    expected = round(3 * 0.6 / math.log(2), 3)  # = 2.597...
    assert result[0]["_score"] == expected, f"{result[0]['_score']} != {expected}"


@test("A11: high fanout reduces score")
def test_a11_fanout_penalty():
    c_low = _candidate(path_index=0, insertion_net="a", insertion_net_fanout=1)
    c_high = _candidate(path_index=1, insertion_net="b", insertion_net_fanout=100)
    result = _score_and_filter_candidates([c_low, c_high], min_chain_depth=2, wns_threshold=-0.3, max_fanout=200)
    scores = {r["path_index"]: r["_score"] for r in result}
    assert scores[0] > scores[1], f"Low fanout score {scores[0]} should exceed high fanout score {scores[1]}"


@test("A12: dedup by insertion_net — higher score wins")
def test_a12_dedup():
    candidates = [
        _candidate(path_index=0, insertion_net="net_x", combinational_depth=4, slack=-0.5),
        _candidate(path_index=1, insertion_net="net_x", combinational_depth=6, slack=-0.8),
    ]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert len(result) == 1, f"Dedup should leave 1, got {len(result)}"
    assert result[0]["path_index"] == 1  # higher score


@test("A13: empty insertion_net not deduped")
def test_a13_empty_net():
    candidates = [
        _candidate(path_index=0, insertion_net=""),
        _candidate(path_index=1, insertion_net=""),
    ]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert len(result) == 2, f"Empty-net candidates should both survive, got {len(result)}"


@test("A14: sorted descending by score")
def test_a14_sort():
    candidates = [
        _candidate(path_index=0, insertion_net="a", combinational_depth=2, slack=-0.3),
        _candidate(path_index=1, insertion_net="b", combinational_depth=5, slack=-0.9),
        _candidate(path_index=2, insertion_net="c", combinational_depth=3, slack=-0.5),
    ]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    scores = [r["_score"] for r in result]
    assert scores == sorted(scores, reverse=True), f"Not sorted: {scores}"


@test("A15: missing keys use defaults")
def test_a15_missing_keys():
    c = {"path_index": 99,
         "source_ff": "a/Q",
         "destination_ff": "b/D",
         "slack": -0.5,
         "insertion_net": "x/O"}
    # missing: combinational_depth, insertion_net_fanout, branched
    result = _score_and_filter_candidates([c], min_chain_depth=0, wns_threshold=-0.3, max_fanout=50)
    assert len(result) == 1
    assert result[0]["path_index"] == 99


@test("A16: fanout=0 (div-by-zero guard — log(1)=0)")
def test_a16_fanout_zero():
    # If fanout key is present and set to 0, log(fanout+1)=log(1)=0 → div by zero.
    # This is a known edge; the function should handle it gracefully.
    c = _candidate(insertion_net_fanout=0)
    try:
        result = _score_and_filter_candidates([c], min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
        # If we get here, it didn't crash — fanout=0 should produce some behavior
        assert True
    except ZeroDivisionError:
        assert False, "fanout=0 caused ZeroDivisionError — should guard or document"


@test("A17: positive slack filtered")
def test_a17_positive_slack():
    candidates = [_candidate(slack=0.5)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert result == [], f"positive slack should be filtered"


@test("A18: many candidates — performance acceptable")
def test_a18_many():
    candidates = [_candidate(path_index=i, insertion_net=f"net_{i}",
                              combinational_depth=3 + (i % 3), slack=-0.4 - (i % 5) * 0.1)
                  for i in range(1000)]
    result = _score_and_filter_candidates(candidates, min_chain_depth=2, wns_threshold=-0.3, max_fanout=50)
    assert len(result) > 0
    assert len(result) <= 1000


# ============================================================================
# Section B: validate_inputs()
# ============================================================================

@test("B1: valid inputs pass")
def test_b1_valid():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths=[{"path": 1}], max_ops=5)
    assert ok, f"Should pass: {msg}"


@test("B2: critical_paths=None fails")
def test_b2_none():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths=None)
    assert not ok, f"None should fail"


@test("B3: critical_paths=[] fails")
def test_b3_empty():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths=[])
    assert not ok, f"Empty list should fail"


@test("B4: critical_paths=string fails")
def test_b4_string():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths="not_a_list")
    assert not ok, f"String should fail"
    assert "list" in msg.lower()


@test("B5: max_ops=0 fails")
def test_b5_max_ops_zero():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths=[{}], max_ops=0)
    assert not ok, f"max_ops=0 should fail"


@test("B6: max_ops=11 fails")
def test_b6_max_ops_11():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths=[{}], max_ops=11)
    assert not ok, f"max_ops=11 should fail"


@test("B7: max_ops='5' (string) fails")
def test_b7_max_ops_string():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths=[{}], max_ops="5")
    assert not ok, f"max_ops='5' should fail (must be int)"


@test("B8: max_ops=10 (boundary) passes")
def test_b8_max_ops_10():
    skill = SmartRetimingSkill()
    ok, msg = skill.validate_inputs(critical_paths=[{}], max_ops=10)
    assert ok, f"max_ops=10 should pass: {msg}"


# ============================================================================
# Section C: execute() control flow (mock-based)
# ============================================================================

@test("C1: design=None returns error")
def test_c1_design_none():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = None
    result = skill.execute(ctx, critical_paths=[{}])
    assert result.success is False
    assert "not loaded" in result.error.lower()


@test("C2: analysis returns empty candidates — early return")
def test_c2_no_candidates():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = MagicMock()  # non-None

    with patch("skills.smart_retiming._estimate_wns", return_value=(-0.5, None)), \
         patch("skills.register_retiming_strategy.analyze_register_retiming",
               return_value={"candidates": [], "summary": "no paths found"}):

        result = skill.execute(ctx, critical_paths=[{}])
        assert result.success is True
        data = result.data
        assert data.get("candidates_total", data.get("inserted", -1)) == 0
        assert data["inserted"] == 0
        assert data["rolled_back"] == 0


@test("C3: max_ops caps insertions (more candidates than max_ops)")
def test_c3_max_ops_cap():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = MagicMock()

    candidates = [_candidate(path_index=i, insertion_net=f"net_{i}",
                              combinational_depth=3, slack=-0.5)
                  for i in range(10)]

    with patch("skills.smart_retiming._estimate_wns", return_value=(-0.5, None)), \
         patch("skills.register_retiming_strategy.analyze_register_retiming",
               return_value={"candidates": candidates, "summary": "ok"}), \
         patch("skills.smart_retiming._insert_single_ff",
               return_value={"success": True, "new_ff_name": "new_ff",
                             "site": "SLICE_X0Y0", "bel": "AFF",
                             "control_signals": {}, "ff_type": "FDRE"}):

        result = skill.execute(ctx, critical_paths=[{}], max_ops=3,
                               verify_each=False)
        assert result.success is True
        assert result.data["inserted"] == 3, f"Should cap at 3, got {result.data['inserted']}"
        assert result.data["max_ops"] == 3


@test("C4: verify_each=False skips timing estimation")
def test_c4_no_verify():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = MagicMock()

    candidates = [_candidate(path_index=0, insertion_net="a", combinational_depth=3, slack=-0.5)]

    with patch("skills.smart_retiming._estimate_wns",
               return_value=(-0.5, None)) as mock_est, \
         patch("skills.register_retiming_strategy.analyze_register_retiming",
               return_value={"candidates": candidates, "summary": "ok"}), \
         patch("skills.smart_retiming._insert_single_ff",
               return_value={"success": True, "new_ff_name": "new_ff",
                             "site": "SLICE", "bel": "AFF",
                             "control_signals": {}, "ff_type": "FDRE"}):

        result = skill.execute(ctx, critical_paths=[{}], verify_each=False, max_ops=1)
        assert result.success is True
        # _estimate_wns: baseline (Phase 1) only — Phase 5 skips when verify_each=False
        assert mock_est.call_count == 1, f"Expected 1 call (baseline), got {mock_est.call_count}"
        assert result.data["inserted"] == 1
        for c in result.data["per_candidate"]:
            assert "estimated_wns" not in c, f"verify_each=False should not have estimated_wns per candidate"


@test("C5: degradation auto-rollback")
def test_c5_rollback():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = MagicMock()

    candidates = [_candidate(path_index=0, insertion_net="a", combinational_depth=3, slack=-0.5)]

    # Baseline = -0.5, after insertion = -0.6 (degradation of 0.1ns)
    wns_values = iter([(-0.5, None),  # baseline
                       (-0.6, None)])  # after insertion (degraded)

    def mock_estimate(design):
        try:
            return next(wns_values)
        except StopIteration:
            return (-0.6, None)

    # Mock the com.xilinx.rapidwright.design module chain so Design.readCheckpoint works
    fake_design_mod = MagicMock()
    fake_design_mod.Design.readCheckpoint.return_value = MagicMock()
    sys.modules["com"] = MagicMock()
    sys.modules["com.xilinx"] = MagicMock()
    sys.modules["com.xilinx.rapidwright"] = MagicMock()
    sys.modules["com.xilinx.rapidwright.design"] = fake_design_mod
    # Also mock rapidwright_tools module for _current_design
    fake_rwt = MagicMock()
    fake_rwt._current_design = MagicMock()
    sys.modules["RapidWrightMCP"] = MagicMock()
    sys.modules["RapidWrightMCP.rapidwright_tools"] = fake_rwt

    try:
        with patch("skills.smart_retiming._estimate_wns", side_effect=mock_estimate), \
             patch("skills.register_retiming_strategy.analyze_register_retiming",
                   return_value={"candidates": candidates, "summary": "ok"}), \
             patch("skills.smart_retiming._insert_single_ff",
                   return_value={"success": True, "new_ff_name": "new_ff",
                                 "site": "SLICE", "bel": "AFF",
                                 "control_signals": {}, "ff_type": "FDRE"}):

            result = skill.execute(ctx, critical_paths=[{}], verify_each=True,
                                   auto_rollback=True, max_ops=1)
            assert result.success is True
            per = result.data["per_candidate"]
            assert per[0]["status"] == "rolled_back", f"Expected rolled_back, got {per[0]['status']}"
            assert result.data["rolled_back"] == 1
            assert result.data["inserted"] == 0
    finally:
        for m in ["com", "com.xilinx", "com.xilinx.rapidwright",
                  "com.xilinx.rapidwright.design", "RapidWrightMCP",
                  "RapidWrightMCP.rapidwright_tools"]:
            sys.modules.pop(m, None)


@test("C6: degradation without auto_rollback — candidate marked inserted")
def test_c6_no_rollback():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = MagicMock()

    candidates = [_candidate(path_index=0, insertion_net="a", combinational_depth=3, slack=-0.5)]

    wns_values = iter([(-0.5, None), (-0.6, None)])

    def mock_estimate(design):
        try:
            return next(wns_values)
        except StopIteration:
            return (-0.6, None)

    with patch("skills.smart_retiming._estimate_wns", side_effect=mock_estimate), \
         patch("skills.register_retiming_strategy.analyze_register_retiming",
               return_value={"candidates": candidates, "summary": "ok"}), \
         patch("skills.smart_retiming._insert_single_ff",
               return_value={"success": True, "new_ff_name": "new_ff",
                             "site": "SLICE", "bel": "AFF",
                             "control_signals": {}, "ff_type": "FDRE"}):

        result = skill.execute(ctx, critical_paths=[{}], verify_each=True,
                               auto_rollback=False, max_ops=1)
        assert result.success is True
        per = result.data["per_candidate"]
        assert per[0]["status"] == "inserted", \
            f"Without rollback, degradation should still mark inserted, got {per[0]['status']}"
        assert result.data["rolled_back"] == 0


@test("C7: analysis returns error dict")
def test_c7_analysis_error():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = MagicMock()

    with patch("skills.smart_retiming._estimate_wns", return_value=(-0.5, None)), \
         patch("skills.register_retiming_strategy.analyze_register_retiming",
               return_value={"error": "RapidWright not initialized"}):

        result = skill.execute(ctx, critical_paths=[{}])
        assert result.success is False
        assert "analysis failed" in result.error.lower() or "Analysis failed" in result.error


@test("C8: final checkpoint write failure")
def test_c8_checkpoint_fail():
    skill = SmartRetimingSkill()
    ctx = MagicMock()
    ctx.design = MagicMock()

    # Make writeCheckpoint raise on the FINAL write (the 3rd call or later)
    call_count = [0]
    def write_ckpt(path):
        call_count[0] += 1
        # Let pre-retime and pre/post insertion checkpoints succeed
        # Fail on final checkpoint (usually the first after all insertions)
        if call_count[0] >= 3:
            raise OSError("Disk full")

    ctx.design.writeCheckpoint = write_ckpt

    candidates = [_candidate(path_index=0, insertion_net="a", combinational_depth=3, slack=-0.5)]

    with patch("skills.smart_retiming._estimate_wns", return_value=(-0.5, None)), \
         patch("skills.register_retiming_strategy.analyze_register_retiming",
               return_value={"candidates": candidates, "summary": "ok"}), \
         patch("skills.smart_retiming._insert_single_ff",
               return_value={"success": True, "new_ff_name": "new_ff",
                             "site": "SLICE", "bel": "AFF",
                             "control_signals": {}, "ff_type": "FDRE"}):

        result = skill.execute(ctx, critical_paths=[{}], verify_each=False, max_ops=1)
        assert result.success is False
        assert "checkpoint" in result.error.lower()


# ============================================================================
# Section D: _estimate_wns() mock-based
# ============================================================================

@test("D1: report_timing returns error → (None, error_msg)")
def test_d1_report_timing_error():
    from skills.smart_retiming import _estimate_wns

    fake = MagicMock()
    fake.report_timing = MagicMock(return_value={"error": "Not initialized"})
    sys.modules["RapidWrightMCP"] = MagicMock()
    sys.modules["RapidWrightMCP.rapidwright_tools"] = fake

    wns, err = _estimate_wns(MagicMock())
    assert wns is None
    assert "Not initialized" in err

    del sys.modules["RapidWrightMCP"]
    del sys.modules["RapidWrightMCP.rapidwright_tools"]


@test("D2: report_timing returns valid WNS")
def test_d2_report_timing_ok():
    from skills.smart_retiming import _estimate_wns

    fake = MagicMock()
    fake.report_timing = MagicMock(return_value={"wns_ns": -0.123, "max_delay_ps": 1234})
    sys.modules["RapidWrightMCP"] = MagicMock()
    sys.modules["RapidWrightMCP.rapidwright_tools"] = fake

    wns, err = _estimate_wns(MagicMock())
    assert wns == -0.123
    assert err is None

    del sys.modules["RapidWrightMCP"]
    del sys.modules["RapidWrightMCP.rapidwright_tools"]


@test("D3: report_timing import error")
def test_d3_import_error():
    from skills.smart_retiming import _estimate_wns

    fake = MagicMock()
    fake.report_timing = MagicMock(side_effect=ImportError("No module"))
    sys.modules["RapidWrightMCP"] = MagicMock()
    sys.modules["RapidWrightMCP.rapidwright_tools"] = fake

    wns, err = _estimate_wns(MagicMock())
    assert wns is None
    assert err is not None
    assert "unavailable" in (err or "").lower()

    del sys.modules["RapidWrightMCP"]
    del sys.modules["RapidWrightMCP.rapidwright_tools"]


# ============================================================================
# Entry point
# ============================================================================

if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
