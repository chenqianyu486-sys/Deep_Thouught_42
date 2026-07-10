"""Unit tests for the routed-state / data-integrity guards added for
run-20260710_002051 (Bug #1 unplaced-best corruption, Bug #2 NoneType crash,
Bug #3 pin_paths array coerce).

Run: python3 -m pytest optimizer/test_routed_guards.py -v
"""

from __future__ import annotations

import pytest

from optimizer.state import OptimizerState
from optimizer.pure.tool_router import _coerce_array_arguments
from optimizer.pure.state_space import _build_netlist_quality


class TestArrayCoerce:
    """Bug #3: LLMs sometimes serialize array args as comma-strings."""

    def test_pin_paths_string_is_split_into_list(self):
        args = {"pin_paths": "a/Q, b/O, c/I"}
        _coerce_array_arguments(args)
        assert args["pin_paths"] == ["a/Q", "b/O", "c/I"]

    def test_empty_string_becomes_empty_list(self):
        args = {"pin_paths": "   "}
        _coerce_array_arguments(args)
        assert args["pin_paths"] == []

    def test_existing_list_is_left_untouched(self):
        # Framework-injected values are already lists and must not be mutated.
        original = [["a/Q", "b/O"], ["c/I"]]
        args = {"critical_paths": original}
        _coerce_array_arguments(args)
        assert args["critical_paths"] is original

    def test_non_array_string_param_is_left_untouched(self):
        # Only known array params are coerced; a directive string must persist.
        args = {"directive": "Explore", "pin_paths": "a/Q,b/O"}
        _coerce_array_arguments(args)
        assert args["directive"] == "Explore"
        assert args["pin_paths"] == ["a/Q", "b/O"]

    def test_missing_keys_are_no_op(self):
        args = {"unrelated": 42}
        _coerce_array_arguments(args)
        assert args == {"unrelated": 42}


class TestNetlistQualityNoneGuard:
    """Bug #2: rollback set high_fanout_nets=None, crashing _build_netlist_quality."""

    def test_none_high_fanout_nets_does_not_raise(self):
        state = OptimizerState()
        state.timing.high_fanout_nets = None
        # Previously: TypeError: 'NoneType' object is not iterable.
        result = _build_netlist_quality(state)
        assert result.high_fanout_nets == []

    def test_empty_list_high_fanout_nets_does_not_raise(self):
        state = OptimizerState()
        state.timing.high_fanout_nets = []
        result = _build_netlist_quality(state)
        assert result.high_fanout_nets == []

    def test_populated_high_fanout_nets_still_parsed(self):
        state = OptimizerState()
        state.timing.high_fanout_nets = [
            {"net_name": "clk", "fanout": 64},
            ["rst", 200],
        ]
        result = _build_netlist_quality(state)
        assert len(result.high_fanout_nets) == 2
        assert result.high_fanout_nets[0].net_name == "clk"
        assert result.high_fanout_nets[0].fanout_count == 64
        assert result.high_fanout_nets[1].net_name == "rst"
