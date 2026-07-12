"""Tests for optimizer.pure.json_repair (Improvement 1: JSON repair/tolerance)."""
from __future__ import annotations

import logging

from optimizer.pure.json_repair import parse_tool_arguments


class TestParseToolArguments:
    def test_valid_passthrough(self):
        args = parse_tool_arguments('{"a": 1, "b": "x"}', "t")
        assert args == {"a": 1, "b": "x"}

    def test_empty_or_none(self):
        assert parse_tool_arguments(None, "t") == {}
        assert parse_tool_arguments("", "t") == {}

    def test_escaped_quote_repair(self):
        """Observed failure (run-20260711_230953, call 40): mid-stream stray \\\"."""
        malformed = (
            '{"critical_paths": [{"cells": ['
            '{"name": "layer1_reg/data_out_reg[44]", "delay": 0.05}, '
            '{\\"name\\": \\"layer0_inst/data_out[1]\\", \\"delay\\": 0.35}'
            "]}]}"
        )
        args = parse_tool_arguments(malformed, "replicate_critical_cells")
        assert args["critical_paths"][0]["cells"][1] == {
            "name": "layer0_inst/data_out[1]",
            "delay": 0.35,
        }

    def test_trailing_comma_repair(self):
        malformed = '{"cells": ["a/b", "c/d",], "n": 3,}'
        args = parse_tool_arguments(malformed, "t")
        assert args == {"cells": ["a/b", "c/d"], "n": 3}

    def test_balanced_extraction_drops_trailing_garbage(self):
        malformed = '{"name": "a/b"} extra, non-json tail }}}'
        args = parse_tool_arguments(malformed, "t")
        assert args == {"name": "a/b"}

    def test_unrecoverable_returns_empty(self):
        # No braces and not JSON at all.
        assert parse_tool_arguments("not json at all", "t") == {}

    def test_non_dict_top_level_returns_empty(self):
        # Tool args must be objects; a bare array is not usable args.
        assert parse_tool_arguments('["a", "b"]', "t") == {}

    def test_repair_is_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="optimizer.pure.json_repair"):
            # Fully escaped quotes -> invalid JSON; \" -> " repair recovers it.
            parse_tool_arguments('{\\"name\\": \\"a/b\\"}', "mytool")
        assert any("[JSON_REPAIR]" in r.message for r in caplog.records)

    def test_failure_is_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="optimizer.pure.json_repair"):
            parse_tool_arguments("not json", "mytool")
        msgs = [r.message for r in caplog.records]
        assert any("failed to parse" in m and "mytool" in m for m in msgs)
