"""Regression tests for Priority-1 fixes from run-20260713_073429.

Two blocking defects prevented timing convergence on the vexriscv design:

P1-1: CellReplication schema rejected top-level cell names.
      RapidWrightMCP/server.py required cell names to match `^.+/.+$`
      (MUST contain '/'), but critical paths legitimately include top-level
      pipeline registers (e.g. 'execute_to_memory_REGFILE_WRITE_DATA_reg[9]',
      'decode_to_execute_INSTRUCTION_reg[7]') - exactly the high-fanout driver
      cells replication should target. derive_cells_rich injects them from
      path nodes; the schema rejected them, failing the whole strategy.

P1-2: parse_high_fanout_nets returned [] for the JSON-wrapped runtime input.
      vivado get_critical_high_fanout_nets returns
      json.dumps({"raw_output": <report>, ...}, indent=2); the report's
      newlines are JSON-escaped, so split('\n') on the wrapper never saw the
      data rows. state.high_fanout_nets stayed empty, the Fanout strategy
      fell back to an LLM-hallucinated net name (dropped the
      'IBusCachedPlugin_cache/' prefix), and the tool returned
      "Net not found in design".
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from optimizer.pure.timing import parse_high_fanout_nets  # noqa: E402


# ── P1-2: parse_high_fanout_nets JSON-wrapper unwrapping ─────────────────


class TestParseHighFanoutNetsJsonWrapper:
    """The parser must handle the JSON-wrapped form the vivado tool returns."""

    _REPORT = (
        "=== High Fanout Nets in Critical Paths (Parent Net Names) ===\n"
        "Analyzed 50 worst timing paths\n"
        "Found 7 high fanout nets:\n\n"
        " Paths    Fanout  Parent Net Name\n"
        "------  --------  --------------------------------------------------\n"
        "     3        82  IBusCachedPlugin_cache/IBusCachedPlugin_iBusRsp_stages_1_output_m2sPipe_ready\n"
        "     2       134  IBusCachedPlugin_cache/CsrPlugin_mepc_reg[31]_2[0]\n"
        "=== End ===\n"
    )

    def test_parses_json_wrapped_raw_output(self):
        """Runtime input is json.dumps({"raw_output": report, ...}, indent=2).

        Before the fix this returned [] (escaped newlines not split), leaving
        state.high_fanout_nets empty and forcing the LLM-hallucinated net name.
        """
        wrapper = {
            "num_paths_analyzed": 50,
            "min_fanout_threshold": 50,
            "clock_nets_excluded": True,
            "raw_output": self._REPORT,
        }
        runtime_input = json.dumps(wrapper, indent=2)
        nets = parse_high_fanout_nets(runtime_input)
        assert nets == [
            ("IBusCachedPlugin_cache/IBusCachedPlugin_iBusRsp_stages_1_output_m2sPipe_ready", 82, 3),
            ("IBusCachedPlugin_cache/CsrPlugin_mepc_reg[31]_2[0]", 134, 2),
        ]

    def test_plain_text_still_parses(self):
        """Direct callers (and the pre-existing 2026-07-12 test) pass plain
        text - the JSON-unwrap must not break that path."""
        nets = parse_high_fanout_nets(self._REPORT)
        assert len(nets) == 2
        assert nets[0][0] == "IBusCachedPlugin_cache/IBusCachedPlugin_iBusRsp_stages_1_output_m2sPipe_ready"

    def test_returns_empty_for_no_section(self):
        assert parse_high_fanout_nets("No high fanout nets found.") == []

    def test_json_wrapper_without_raw_output_falls_back(self):
        """A JSON object without raw_output must not crash; fall back to
        parsing the wrapper text itself (which yields no nets)."""
        runtime_input = json.dumps({"num_paths_analyzed": 50}, indent=2)
        assert parse_high_fanout_nets(runtime_input) == []


# ── P1-1: replicate_critical_cells schema accepts top-level cell names ────

# RapidWrightMCP lives outside the optimizer package; put it on sys.path so
# the tool schema can be imported for unit testing (mirrors the VivadoMCP
# import pattern in test_p0_robustness_fixes.py).
_RW_MCP_DIR = Path(__file__).resolve().parent.parent / "RapidWrightMCP"
if str(_RW_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_RW_MCP_DIR))


@pytest.fixture(scope="module")
def replicate_schema():
    """Load the replicate_critical_cells inputSchema from the live MCP tool
    definition. Skips if RapidWrightMCP/server cannot be imported."""
    try:
        import server as rw_server  # type: ignore
    except Exception as e:  # pragma: no cover - env-dependent
        pytest.skip(f"RapidWrightMCP server not importable: {e}")

    try:
        tools = asyncio.run(rw_server.list_tools())
    except Exception as e:  # pragma: no cover - env-dependent
        pytest.skip(f"list_tools() failed: {e}")

    tool = next((t for t in tools if t.name == "replicate_critical_cells"), None)
    assert tool is not None, "replicate_critical_cells tool not found"
    return tool.inputSchema


class TestReplicateCriticalCellsSchema:
    """The schema must accept top-level (non-hierarchical) cell names."""

    def _name_field(self, schema):
        return schema["properties"]["critical_paths"]["items"]["properties"][
            "cells"
        ]["items"]["properties"]["name"]

    def test_name_field_has_no_slash_pattern(self, replicate_schema):
        """The `^.+/.+$` pattern is what rejected top-level names; it must be
        gone (or relaxed to accept names without '/')."""
        name_field = self._name_field(replicate_schema)
        pattern = name_field.get("pattern")
        # A top-level name with no '/' must satisfy whatever pattern remains.
        import re
        if pattern:
            assert re.match(pattern, "execute_to_memory_REGFILE_WRITE_DATA_reg[9]"), (
                f"name pattern {pattern!r} still rejects top-level cell names"
            )

    def test_accepts_top_level_cell_name(self, replicate_schema):
        """A critical_paths payload whose cell name has no '/' must validate.

        Before the fix jsonschema raised: '... does not match ^.+/.+$'.
        """
        jsonschema = pytest.importorskip("jsonschema")
        payload = {
            "critical_paths": [
                {
                    "cells": [
                        {"name": "execute_to_memory_REGFILE_WRITE_DATA_reg[9]", "delay": 0.0},
                        {"name": "decode_to_execute_INSTRUCTION_reg[7]", "delay": 0.079},
                    ]
                }
            ]
        }
        # Must not raise.
        jsonschema.validate(payload, replicate_schema)

    def test_accepts_hierarchical_cell_name(self, replicate_schema):
        """Regression guard: hierarchical names (with '/') still validate."""
        jsonschema = pytest.importorskip("jsonschema")
        payload = {
            "critical_paths": [
                {
                    "cells": [
                        {"name": "dataCache_1/ways_0_tags_reg_i_57", "delay": 0.05},
                    ]
                }
            ]
        }
        jsonschema.validate(payload, replicate_schema)


# ── P1-1 (follow-up): other cell-name tools also accept top-level names ────
#
# The same `^.+/.+$` pattern that blocked CellReplication was present in 7 more
# cell-name fields. Top-level pipeline registers (e.g. the vexriscv design has
# 1395/3351 top-level cells with no '/') are valid inputs to all of them; the
# skills resolve names via design.getCell() which matches by exact name
# regardless of hierarchy. optimize_lut_input_cone is EXCLUDED - its field is
# PIN names (cell/pin), which always contain '/', so the pattern is correct.

_TOP_LEVEL = "decode_to_execute_INSTRUCTION_reg[7]"
_HIER = "dataCache_1/ways_0_tags_reg_i_57"


@pytest.fixture(scope="module")
def all_tools():
    """Load every RapidWright MCP tool definition. Skips if unimportable."""
    try:
        import server as rw_server  # type: ignore
    except Exception as e:  # pragma: no cover - env-dependent
        pytest.skip(f"RapidWrightMCP server not importable: {e}")
    try:
        tools = asyncio.run(rw_server.list_tools())
    except Exception as e:  # pragma: no cover - env-dependent
        pytest.skip(f"list_tools() failed: {e}")
    return {t.name: t for t in tools}


# (tool_name, minimal payload with a TOP-LEVEL cell name in the cell-name field)
# Note: MCP tool names omit the "rapidwright_" prefix the optimizer adds when
# routing calls; the schema is defined on the bare name in RapidWrightMCP/server.
_CELL_NAME_TOOLS = [
    ("optimize_cell_placement", {"cell_names": [_TOP_LEVEL]}),
    ("analyze_pblock_region", {
        "critical_path_cells": [_TOP_LEVEL],
        "target_lut_count": 1, "target_ff_count": 1,
        "target_dsp_count": 0, "target_bram_count": 0,
    }),
    ("execute_combinational_rebalancing_strategy",
     {"critical_paths": [[_TOP_LEVEL]]}),
    ("execute_lut_muxf_repack_strategy",
     {"critical_paths": [[_TOP_LEVEL]]}),
    ("execute_muxf_tree_reorder_strategy",
     {"critical_paths": [[_TOP_LEVEL]]}),
    ("flatten_lut_cascade", {"critical_paths": [[_TOP_LEVEL]]}),
    ("optimize_pin_swapping",
     {"critical_paths": [{"cells": [_TOP_LEVEL]}]}),
]


@pytest.mark.parametrize("tool_name, payload", _CELL_NAME_TOOLS)
def test_cell_name_tool_accepts_top_level_name(all_tools, tool_name, payload):
    """Each cell-name tool must accept a top-level (no '/') cell name.

    Before the fix every one of these rejected top-level names with
    '... does not match ^.+/.+$', silently failing the strategy if the LLM
    or injection layer ever supplied one.
    """
    jsonschema = pytest.importorskip("jsonschema")
    tool = all_tools[tool_name]
    jsonschema.validate(payload, tool.inputSchema)  # must not raise


@pytest.mark.parametrize("tool_name, payload", _CELL_NAME_TOOLS)
def test_cell_name_tool_accepts_hierarchical_name(all_tools, tool_name, payload):
    """Regression guard: hierarchical names (with '/') still validate."""
    jsonschema = pytest.importorskip("jsonschema")
    # Swap the top-level name for a hierarchical one in the same payload shape.
    hier_payload = json.loads(json.dumps(payload).replace(_TOP_LEVEL, _HIER))
    tool = all_tools[tool_name]
    jsonschema.validate(hier_payload, tool.inputSchema)


def test_lut_input_cone_still_requires_slash_for_pins(all_tools):
    """optimize_lut_input_cone takes PIN names (cell/pin), not cell names.
    Pin names always contain '/' (the pin separator), so the pattern is
    correct and must remain - a bare pin like 'I0' is invalid."""
    jsonschema = pytest.importorskip("jsonschema")
    tool = all_tools["optimize_lut_input_cone"]
    # Bare pin (no '/') must be rejected.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"hierarchical_input_pins": ["I0"]}, tool.inputSchema)
    # A top-level cell's pin (cell/pin, has '/') must be accepted.
    jsonschema.validate(
        {"hierarchical_input_pins": [f"{_TOP_LEVEL}/I0"]}, tool.inputSchema,
    )
