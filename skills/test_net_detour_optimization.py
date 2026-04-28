#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Unit tests for net_detour_optimization skill.
Tests the _group_pins_by_cell boundary conditions.
"""

import sys
import os

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.net_detour_optimization import _group_pins_by_cell


def test_basic_path():
    """Test basic pin path through LUTs."""
    pin_paths = ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]
    result = _group_pins_by_cell(pin_paths)

    print(f"Test: basic_path")
    print(f"  Input: {pin_paths}")
    print(f"  Result: {result}")

    # Should identify lut1 and lut2 as data path cells
    assert len(result) >= 2, f"Expected at least 2 cells, got {len(result)}"
    print("  PASSED")


def test_source_ff():
    """Test source FF handling (first pin is FF output)."""
    pin_paths = ["src_ff/Q", "lut1/I2", "lut1/O"]
    result = _group_pins_by_cell(pin_paths)

    print(f"Test: source_ff")
    print(f"  Input: {pin_paths}")
    print(f"  Result: {result}")

    # First cell should have None as in_pin (source FF output)
    assert len(result) >= 1, "Expected at least 1 cell"
    first_cell_in, first_cell_out, first_cell_name = result[0]
    print(f"  First cell: ({first_cell_in}, {first_cell_out}, {first_cell_name})")
    assert first_cell_in is None, f"Expected None for source FF in_pin, got {first_cell_in}"
    print("  PASSED")


def test_sink_ff():
    """Test sink FF handling (last pin is FF input)."""
    pin_paths = ["lut1/O", "dst_ff/D"]
    result = _group_pins_by_cell(pin_paths)

    print(f"Test: sink_ff")
    print(f"  Input: {pin_paths}")
    print(f"  Result: {result}")

    # Last cell should have None as out_pin (sink FF input)
    assert len(result) >= 1, "Expected at least 1 cell"
    last_cell_in, last_cell_out, last_cell_name = result[-1]
    print(f"  Last cell: ({last_cell_in}, {last_cell_out}, {last_cell_name})")
    assert last_cell_out is None, f"Expected None for sink FF out_pin, got {last_cell_out}"
    print("  PASSED")


def test_cross_cell_jump():
    """Test cross-cell jump detection."""
    pin_paths = ["lut1/O", "lut2/I0", "lut2/O"]
    result = _group_pins_by_cell(pin_paths)

    print(f"Test: cross_cell_jump")
    print(f"  Input: {pin_paths}")
    print(f"  Result: {result}")

    # Should identify lut2 as the cell (lut1/O is input to lut2)
    assert len(result) >= 1, "Expected at least 1 cell"
    # The first entry should be lut2 with in_pin=lut2/I0
    lut2_entry = None
    for entry in result:
        if entry[2] == "lut2":
            lut2_entry = entry
            break
    assert lut2_entry is not None, "Expected lut2 in result"
    print(f"  lut2 entry: {lut2_entry}")
    print("  PASSED")


def test_empty_path():
    """Test empty path handling."""
    result = _group_pins_by_cell([])
    print(f"Test: empty_path")
    print(f"  Input: []")
    print(f"  Result: {result}")
    assert result == [], f"Expected empty list, got {result}"
    print("  PASSED")


def test_single_pin():
    """Test single pin handling."""
    result = _group_pins_by_cell(["single_pin"])
    print(f"Test: single_pin")
    print(f"  Input: ['single_pin']")
    print(f"  Result: {result}")
    assert result == [], f"Expected empty list for single pin, got {result}"
    print("  PASSED")


def test_two_pins_same_cell():
    """Test two pins from same cell."""
    pin_paths = ["lut1/I2", "lut1/O"]
    result = _group_pins_by_cell(pin_paths)

    print(f"Test: two_pins_same_cell")
    print(f"  Input: {pin_paths}")
    print(f"  Result: {result}")

    assert len(result) >= 1, "Expected at least 1 cell"
    print("  PASSED")


def test_multiple_cross_cells():
    """Test path with multiple cells."""
    pin_paths = [
        "ff_src/Q",
        "lut0/I0", "lut0/O",
        "lut1/I0", "lut1/O",
        "lut2/I0", "lut2/O",
        "ff_dst/D"
    ]
    result = _group_pins_by_cell(pin_paths)

    print(f"Test: multiple_cross_cells")
    print(f"  Input: {pin_paths}")
    print(f"  Result: {result}")

    # Should identify lut0, lut1, lut2
    cell_names = [entry[2] for entry in result]
    print(f"  Cell names: {cell_names}")
    assert "lut0" in cell_names, "Expected lut0 in result"
    assert "lut1" in cell_names, "Expected lut1 in result"
    assert "lut2" in cell_names, "Expected lut2 in result"
    print("  PASSED")


def main():
    """Run all tests."""
    print("=" * 60)
    print("Unit Tests for net_detour_optimization skill")
    print("=" * 60)

    tests = [
        test_basic_path,
        test_source_ff,
        test_sink_ff,
        test_cross_cell_jump,
        test_empty_path,
        test_single_pin,
        test_two_pins_same_cell,
        test_multiple_cross_cells,
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


if __name__ == "__main__":
    sys.exit(main())