"""Layer 1 Unit Regression Tests: critical_path parser.

Verifies the parser correctly handles real Vivado output, edge cases,
and doesn't return false "Parsed 0 paths" results.
"""

import json
import pytest
from optimizer.pure.critical_path import parse_critical_path_cells, _is_valid_cell_name, build_cell_type_chain


# ── Test fixtures: real Vivado output samples ──

SAMPLE_WORST_PATH_JSON = json.dumps([
    {
        "cells": [
            "layer0_reg/data_out_reg[41]",
            "layer0_inst/layer0_N25_inst/data_out[76]_i_19",
            "layer0_inst/layer0_N25_inst/data_out[76]_i_18",
            "layer0_inst/layer0_N25_inst/data_out_reg[76]_i_7",
            "layer0_inst/layer0_N25_inst/data_out_reg[76]_i_1",
            "layer0_inst/layer0_N25_inst/data_out[76]_i_3",
            "layer1_reg/data_out_reg[76]_rep__0",
        ],
        "slack": -0.978,
        "logic_delay": 0.440,
        "net_delay": 2.019,
        "levels": 5,
        "nodes": [
            {"kind": "cell", "name": "layer0_reg/data_out_reg[41]", "cell_type": "FDRE",
             "location": "SLICE_X38Y277", "incr_delay": 0.079, "cumul_delay": 0.079,
             "fanout": None, "net_status": ""},
            {"kind": "net", "name": "layer0_reg/data_out[41]", "cell_type": "",
             "location": "", "incr_delay": 0.287, "cumul_delay": 0.366,
             "fanout": 5, "net_status": "routed"},
            {"kind": "cell", "name": "layer0_inst/layer0_N25_inst/data_out[76]_i_19",
             "cell_type": "LUT6", "location": "SLICE_X37Y278",
             "incr_delay": 0.053, "cumul_delay": 0.419,
             "fanout": None, "net_status": ""},
        ],
        "startpoint": "layer0_reg/data_out_reg[41]/C",
        "endpoint_pin": "layer1_reg/data_out_reg[76]_rep__0/D",
    }
])

# Simulates the problematic case: full JSON with cells populated (should parse)
SAMPLE_MULTI_PATH_JSON = json.dumps([
    {
        "cells": ["layer0_reg/data_out_reg[41]", "layer0_inst/layer0_N25_inst/data_out[76]_i_19"],
        "slack": -0.978, "logic_delay": 0.44, "net_delay": 2.019, "levels": 5,
    },
    {
        "cells": ["layer1_reg/data_out_reg[16]_rep__0", "layer2_reg/data_out_reg[142]_rep"],
        "slack": -0.009, "logic_delay": 0.062, "net_delay": 0.067, "levels": 1,
    },
])

# The actual bug: empty cells arrays but valid nodes (what this run produced)
SAMPLE_EMPTY_CELLS_JSON = json.dumps([
    {
        "cells": [],
        "slack": -0.978, "logic_delay": 0.44, "net_delay": 2.019, "levels": 5,
        "nodes": [
            {"kind": "net", "name": "layer0_reg/data_out[41]", "incr_delay": 0.287, "cumul_delay": 0.366},
            {"kind": "net", "name": "layer0_inst/layer0_N25_inst/M0w[5]", "incr_delay": 0.308, "cumul_delay": 0.727},
        ],
    },
    {
        "cells": [],
        "slack": -0.009, "logic_delay": 0.062, "net_delay": 0.067, "levels": 1,
        "nodes": [
            {"kind": "net", "name": "layer1_reg/data_out[16]_rep__0", "incr_delay": 0.357, "cumul_delay": 0.357},
        ],
    },
])


# ── Tests: parse_critical_path_cells ──

class TestParseCriticalPathCells:
    """Verify the parser handles all expected input formats correctly."""

    def test_parse_real_worst_path(self):
        """The parser should extract cells from a valid JSON path dict."""
        paths = parse_critical_path_cells(SAMPLE_WORST_PATH_JSON)
        assert len(paths) == 1, f"Expected 1 path, got {len(paths)}"
        assert len(paths[0]["cells"]) >= 2, f"Expected >=2 cells, got {len(paths[0]['cells'])}"
        assert paths[0]["slack"] == -0.978
        assert paths[0]["logic_delay"] == 0.44

    def test_parse_multiple_paths(self):
        """Multiple valid paths should all be parsed."""
        paths = parse_critical_path_cells(SAMPLE_MULTI_PATH_JSON)
        assert len(paths) == 2, f"Expected 2 paths, got {len(paths)}"
        assert all(len(p["cells"]) >= 2 for p in paths)

    def test_empty_input_returns_empty(self):
        """Empty/None input should return empty list."""
        assert parse_critical_path_cells("") == []
        assert parse_critical_path_cells(None) == []

    def test_invalid_json_returns_empty(self):
        """Malformed JSON should return empty list gracefully."""
        assert parse_critical_path_cells("not valid json {{{") == []

    def test_error_dict_returns_empty(self):
        """Tool error response should return empty list."""
        result = parse_critical_path_cells(json.dumps({"error": "Vivado crashed"}))
        assert result == []

    def test_output_file_mode_returns_empty(self):
        """When tool writes to file, parser returns empty (no inline data)."""
        result = parse_critical_path_cells(json.dumps({
            "status": "success",
            "path_count": 50,
            "output_file": "/tmp/paths.json",
        }))
        assert result == []

    def test_empty_cells_arrays_returns_empty_with_warning(self):
        """When all paths have empty cells (the actual bug), returns empty.
        The diagnostic logging should fire (tested via log capture)."""
        paths = parse_critical_path_cells(SAMPLE_EMPTY_CELLS_JSON)
        assert paths == [], (
            f"Empty cells should produce 0 valid paths. "
            f"This is the bug scenario — diagnostic logging MUST fire."
        )

    def test_legacy_list_format(self):
        """Legacy format [["cell1", "cell2"], ...] should still work."""
        result = parse_critical_path_cells(json.dumps([
            ["layer0_reg/data_out_reg[41]", "layer0_inst/layer0_N25_inst/data_out[76]_i_19"],
        ]))
        assert len(result) == 1
        assert len(result[0]["cells"]) == 2

    def test_path_with_rep_suffix_cells(self):
        """Cells with _rep__0 suffixes should be parsed correctly."""
        paths = parse_critical_path_cells(json.dumps([{
            "cells": [
                "layer1_reg/data_out_reg[76]_rep__0",
                "layer0_reg/data_out_reg[41]",
            ],
            "slack": -0.5,
        }]))
        assert len(paths) == 1
        assert "layer1_reg/data_out_reg[76]_rep__0" in paths[0]["cells"]

    def test_single_cell_path_rejected(self):
        """Paths with only 1 cell should be rejected (need >=2)."""
        paths = parse_critical_path_cells(json.dumps([{
            "cells": ["layer0_reg/data_out_reg[41]"],
            "slack": -0.5,
        }]))
        assert paths == []

    def test_muxf_cells_in_path(self):
        """MUXF7/MUXF8 cells should be preserved in path output."""
        paths = parse_critical_path_cells(json.dumps([{
            "cells": [
                "layer0_inst/layer0_N25_inst/data_out_reg[76]_i_7",  # MUXF7
                "layer0_inst/layer0_N25_inst/data_out[76]_i_3",       # MUXF7
                "layer0_reg/data_out_reg[41]",
            ],
            "slack": -0.5,
        }]))
        assert len(paths) == 1
        assert len(paths[0]["cells"]) == 3


# ── Tests: _is_valid_cell_name ──

class TestBuildCellTypeChain:
    """Verify cell_type_chain uses real Vivado types when provided.

    Regression guard for run-20260710_132555: MUXF7/MUXF8 cells in logicnets
    are named *_reg[..]_i_* (matching the LUT _i_ pattern), so the name heuristic
    mislabels them as "LUT". This made the dashboard show cell_type_chain with no
    MUXF while the CELL REGISTRY listed MUXF7/MUXF8 - a contradiction that misled
    strategy selection toward MUXF strategies that all returned "no data produced".
    """

    def test_muxf_cells_mislabeled_by_heuristic(self):
        """Without real types, MUXF7 cells named *_i_* are mislabeled LUT."""
        cells = [
            "layer0_reg/data_out_reg[12]",
            "layer0_inst/layer0_N7_inst/data_out_reg[21]_i_11",  # actually MUXF7
            "layer0_inst/layer0_N7_inst/data_out[21]_i_3",        # actually MUXF7
        ]
        chain, _ = build_cell_type_chain(cells)
        assert "MUXF" not in chain  # heuristic misses them

    def test_real_types_fix_muxf_mislabel(self):
        """With real Vivado types, MUXF7 cells appear correctly in the chain."""
        cells = [
            "layer0_reg/data_out_reg[12]",
            "layer0_inst/layer0_N7_inst/data_out_reg[21]_i_11",
            "layer0_inst/layer0_N7_inst/data_out[21]_i_3",
        ]
        types = {
            "layer0_inst/layer0_N7_inst/data_out_reg[21]_i_11": "MUXF7",
            "layer0_inst/layer0_N7_inst/data_out[21]_i_3": "MUXF7",
        }
        chain, counts = build_cell_type_chain(cells, cell_types=types)
        assert chain.count("MUXF") == 2
        assert counts["MUXF"] == 2
        assert counts["FF"] == 1  # data_out_reg[12] -> FDRE -> FF

    def test_falls_back_to_heuristic_when_no_types(self):
        """cell_types=None preserves the original heuristic behavior."""
        cells = ["layer0_reg/data_out_reg[41]", "layer0_inst/layer0_N25_inst/data_out[76]_i_19"]
        chain, _ = build_cell_type_chain(cells, cell_types=None)
        assert "FF" in chain and "LUT" in chain

    def test_partial_types_use_real_where_available(self):
        """When only some cells have real types, mix real + heuristic."""
        cells = ["a/b_reg[0]", "a/c_i_5", "a/d_i_5"]
        types = {"a/c_i_5": "MUXF8"}
        chain, counts = build_cell_type_chain(cells, cell_types=types)
        assert "MUXF" in chain
        assert counts["MUXF"] == 1


class TestIsValidCellName:
    """Verify cell name validation filters correctly."""

    def test_valid_hierarchical_cell(self):
        assert _is_valid_cell_name("layer0_reg/data_out_reg[41]")
        assert _is_valid_cell_name("layer0_inst/layer0_N25_inst/data_out[76]_i_19")
        assert _is_valid_cell_name("u_core/u_alu/reg_0")

    def test_rejects_pblock_labels(self):
        assert not _is_valid_cell_name("pblock_tight")
        assert not _is_valid_cell_name("pblock_io")
        assert not _is_valid_cell_name("my_pblock_region")  # contains "pblock"

    def test_rejects_device_sites(self):
        assert not _is_valid_cell_name("SLICE_X38Y277")
        assert not _is_valid_cell_name("SLICE_X91Y106")
        assert not _is_valid_cell_name("DSP48E2_X10Y46")

    def test_rejects_non_hierarchical(self):
        assert not _is_valid_cell_name("FDRE")  # no '/'
        assert not _is_valid_cell_name("LUT6")  # no '/'

    def test_rejects_empty_or_none(self):
        assert not _is_valid_cell_name("")
        assert not _is_valid_cell_name(None)

    def test_rejects_pipe_symbol(self):
        assert not _is_valid_cell_name("///")  # no alphanumeric

    def test_accepts_bracket_indexed(self):
        assert _is_valid_cell_name("layer2_reg/data_out_reg[191]_0[30]")
        assert _is_valid_cell_name("layer1_reg/data_out_reg[9]_rep__1_0")

    def test_accepts_rep_suffix(self):
        assert _is_valid_cell_name("layer1_reg/data_out_reg[76]_rep__0")
        assert _is_valid_cell_name("layer1_reg/data_out_reg[89]_rep_0")


# ── Tests: diagnostic logging for empty cells ──

class TestEmptyCellsDiagnosticLogging:
    """Verify that the diagnostic warning fires when cells are empty."""

    def test_empty_cells_logs_warning(self, caplog):
        """When all paths have empty cells, a WARNING log should fire."""
        import logging
        caplog.set_level(logging.WARNING)

        parse_critical_path_cells(SAMPLE_EMPTY_CELLS_JSON)

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_messages) >= 1, (
            f"Expected at least 1 WARNING for empty cells, got {len(warning_messages)}. "
            f"Messages: {warning_messages}"
        )
        assert any("empty 'cells'" in msg for msg in warning_messages), (
            f"Warning should mention empty cells. Got: {warning_messages}"
        )

    def test_valid_paths_no_warning(self, caplog):
        """When paths have valid cells, no empty-cells warning should fire."""
        import logging
        caplog.set_level(logging.WARNING)

        parse_critical_path_cells(SAMPLE_WORST_PATH_JSON)

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        empty_cell_warnings = [m for m in warning_messages if "empty 'cells'" in m]
        assert len(empty_cell_warnings) == 0, (
            f"Should NOT warn about empty cells for valid paths. Got: {empty_cell_warnings}"
        )


# ── Tests: unexpected dict format ──

class TestUnexpectedDictFormat:
    """Verify the parser handles unexpected dict structures gracefully."""

    def test_unexpected_dict_keys_logs_warning(self, caplog):
        """An unexpected dict format should log a warning, not crash."""
        import logging
        caplog.set_level(logging.WARNING)

        result = parse_critical_path_cells(json.dumps({"unexpected": "format", "foo": 42}))
        assert result == []

        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Unexpected dict" in msg or "unexpected" in msg.lower() for msg in warnings), (
            f"Should warn about unexpected dict format. Got: {warnings}"
        )


# ── Tests: unplaced-report cell line regex (problem 2 regression) ──
# These patterns MUST stay in sync with the RE_CELL_LINE / RE_CELL_LINE_BARE
# definitions inside extract_critical_path_cells() in
# VivadoMCP/vivado_mcp_server.py. They guard against the unplaced-report bug
# where the Location column is empty (after place_design -unplace) and the
# cell line regexes must still match with Location optional.

import re as _re

_RE_CELL_LINE = _re.compile(r'^\s+(?:(\S+)\s+)?(\S+)\s+\(Prop_[^)]+\).*$')
_RE_CELL_LINE_BARE = _re.compile(r'^\s+(?:(\S+)\s+)?(\S+)\s+([rf])\s+(?:\S+\s+)?(\S+)')


class TestUnplacedReportCellRegex:
    """Verify cell line regexes match both placed and unplaced (empty Location) formats."""

    def test_placed_cell_line_with_location(self):
        """Placed cell line: Location + CellType + (Prop_)."""
        line = "    SLICE_X91Y106   FDRE (Prop_EFF_SLICEL_C_Q)"
        m = _RE_CELL_LINE.match(line)
        assert m is not None, "Placed cell line must match"
        assert m.group(1) == "SLICE_X91Y106"
        assert m.group(2) == "FDRE"

    def test_unplaced_cell_line_empty_location(self):
        """Unplaced cell line: empty Location column, only CellType + (Prop_).

        This is the problem-2 bug scenario - the old regex (requiring two
        \\S+ tokens before (Prop_) failed to match, yielding 0 cell nodes.
        """
        line = "                   FDRE (Prop_EFF_SLICEL_C_Q)"
        m = _RE_CELL_LINE.match(line)
        assert m is not None, "Unplaced cell line must match after the fix"
        assert m.group(1) is None, "Location group should be None when unplaced"
        assert m.group(2) == "FDRE"

    def test_placed_bare_endpoint_cell(self):
        """Placed endpoint cell: Location + CellType + r/f + pin."""
        line = "    SLICE_X64Y289        FDRE                                         r  layer1_reg/data_out_reg[76]_rep__0/D"
        m = _RE_CELL_LINE_BARE.match(line)
        assert m is not None, "Placed bare cell line must match"
        assert m.group(1) == "SLICE_X64Y289"
        assert m.group(2) == "FDRE"
        assert m.group(3) == "r"
        assert m.group(4) == "layer1_reg/data_out_reg[76]_rep__0/D"

    def test_unplaced_bare_endpoint_cell(self):
        """Unplaced endpoint cell: empty Location, only CellType + r/f + pin."""
        line = "                   FDRE                                         r  layer1_reg/data_out_reg[76]_rep__0/D"
        m = _RE_CELL_LINE_BARE.match(line)
        assert m is not None, "Unplaced bare cell line must match after the fix"
        assert m.group(1) is None, "Location group should be None when unplaced"
        assert m.group(2) == "FDRE"
        assert m.group(3) == "r"
        assert m.group(4) == "layer1_reg/data_out_reg[76]_rep__0/D"
