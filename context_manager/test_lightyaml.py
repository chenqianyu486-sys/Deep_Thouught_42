"""
LightYAML Test Suite (pyyaml backend)

Tests are adapted for pyyaml's standard behavior:
- Block-style YAML output (e.g., `- item` instead of `[item]`)
- Standard YAML null handling (`None` is a string, not null)
- Full support for anchors, aliases, block strings, type tags
"""

import unittest
from collections import OrderedDict
import sys
import os
import json

_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lightyaml import LightYAML, YAMLParseError, YAMLUnsupportedError, YAMLEncodeError, LightYAMLError


class TestLightYAMLBasicTypes(unittest.TestCase):
    """Basic type tests."""

    def test_dump_string_simple(self):
        """Simple string."""
        self.assertEqual(LightYAML.dump("hello"), "hello\n")

    def test_dump_string_with_special_chars(self):
        """String with special characters."""
        yaml = LightYAML.dump("key: value")
        self.assertIn('"', yaml)

    def test_dump_integer(self):
        """Integer."""
        self.assertEqual(LightYAML.dump(42), "42\n")

    def test_dump_negative_integer(self):
        """Negative integer."""
        self.assertEqual(LightYAML.dump(-17), "-17\n")

    def test_dump_float(self):
        """Float."""
        self.assertEqual(LightYAML.dump(3.14), "3.14\n")

    def test_dump_boolean_true(self):
        """Boolean true."""
        self.assertEqual(LightYAML.dump(True), "true\n")

    def test_dump_boolean_false(self):
        """Boolean false."""
        self.assertEqual(LightYAML.dump(False), "false\n")

    def test_dump_null(self):
        """Null value."""
        self.assertEqual(LightYAML.dump(None), "null\n")

    def test_load_string(self):
        """Load string."""
        self.assertEqual(LightYAML.load("hello"), "hello")

    def test_load_integer(self):
        """Load integer."""
        self.assertEqual(LightYAML.load("42"), 42)

    def test_load_negative_integer(self):
        """Load negative integer."""
        self.assertEqual(LightYAML.load("-17"), -17)

    def test_load_float(self):
        """Load float."""
        self.assertAlmostEqual(LightYAML.load("3.14"), 3.14)

    def test_load_boolean_true_values(self):
        """Load boolean - true variants."""
        for val in ["true", "True", "YES", "yes", "on", "ON"]:
            self.assertIs(LightYAML.load(val), True)

    def test_load_boolean_false_values(self):
        """Load boolean - false variants."""
        for val in ["false", "False", "NO", "no", "off", "OFF"]:
            self.assertIs(LightYAML.load(val), False)

    def test_load_null_values(self):
        """Load null value variants (standard YAML)."""
        for val in ["null", "Null", "NULL", "~"]:
            self.assertIsNone(LightYAML.load(val))

    def test_load_none_as_string(self):
        """'None' is parsed as a string in standard YAML."""
        self.assertEqual(LightYAML.load("None"), "None")


class TestLightYAMLDataStructures(unittest.TestCase):
    """Data structure tests."""

    def test_dump_simple_dict(self):
        """Simple dict."""
        result = LightYAML.dump({"name": "test"})
        self.assertIn("name:", result)
        self.assertIn("test", result)

    def test_load_simple_dict(self):
        """Load simple dict."""
        result = LightYAML.load("name: test")
        self.assertEqual(result, {"name": "test"})

    def test_dump_list(self):
        """List (pyyaml block format)."""
        yaml = LightYAML.dump([1, 2, 3])
        self.assertIn("- 1", yaml)
        self.assertIn("- 2", yaml)

    def test_load_list(self):
        """Load list."""
        result = LightYAML.load("- 1\n- 2\n- 3")
        self.assertEqual(result, [1, 2, 3])

    def test_dump_nested_dict(self):
        """Nested dict."""
        data = {"outer": {"inner": "value"}}
        yaml = LightYAML.dump(data)
        self.assertIn("outer:", yaml)
        self.assertIn("inner:", yaml)

    def test_load_nested_dict(self):
        """Load nested dict."""
        yaml = """outer:
  inner: value"""
        result = LightYAML.load(yaml)
        self.assertEqual(result, {"outer": {"inner": "value"}})

    def test_dump_list_of_dicts(self):
        """List of dicts."""
        data = [{"name": "a"}, {"name": "b"}]
        yaml = LightYAML.dump(data)
        self.assertIn("- name:", yaml)

    def test_load_list_of_dicts(self):
        """Load list of dicts."""
        yaml = """- name: a
- name: b"""
        result = LightYAML.load(yaml)
        self.assertEqual(result, [{"name": "a"}, {"name": "b"}])

    def test_load_empty_dict(self):
        """Empty dict."""
        result = LightYAML.load("{}")
        self.assertEqual(result, {})

    def test_load_empty_list(self):
        """Empty list."""
        result = LightYAML.load("[]")
        self.assertEqual(result, [])


class TestLightYAMLRoundtripConsistency(unittest.TestCase):
    """Roundtrip consistency tests - data integrity after dump/load cycle."""

    def test_roundtrip_string(self):
        """String roundtrip."""
        for s in ["hello", "key: value", "with space", "中文"]:
            data = s
            _, parsed = LightYAML.roundtrip(data)
            self.assertEqual(parsed, data)

    def test_roundtrip_string_with_newline(self):
        """String with newline roundtrip."""
        data = "hello\nworld"
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(parsed, data)

    def test_roundtrip_integers(self):
        """Integer roundtrip."""
        for n in [0, 1, -1, 42, -17, 1000000]:
            _, parsed = LightYAML.roundtrip(n)
            self.assertEqual(parsed, n)

    def test_roundtrip_floats(self):
        """Float roundtrip."""
        for f in [0.0, 3.14, -2.5, 1e10, 1.5e-5]:
            _, parsed = LightYAML.roundtrip(f)
            self.assertAlmostEqual(parsed, f)

    def test_roundtrip_boolean(self):
        """Boolean roundtrip."""
        for b in [True, False]:
            _, parsed = LightYAML.roundtrip(b)
            self.assertEqual(parsed, b)

    def test_roundtrip_null(self):
        """Null roundtrip."""
        _, parsed = LightYAML.roundtrip(None)
        self.assertIsNone(parsed)

    def test_roundtrip_simple_dict(self):
        """Simple dict roundtrip."""
        data = {"name": "test", "value": 42}
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(parsed, data)

    def test_roundtrip_list(self):
        """List roundtrip."""
        data = [1, "two", 3.0, True, None]
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(parsed, data)

    def test_roundtrip_nested_structure(self):
        """Nested structure roundtrip."""
        data = {"level1": {"level2": {"level3": [1, 2, 3]}}}
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(parsed, data)

    def test_roundtrip_ordered_dict(self):
        """OrderedDict roundtrip - order preserved."""
        data = OrderedDict([("first", 1), ("second", 2), ("third", 3)])
        yaml_str, parsed = LightYAML.roundtrip(data)
        self.assertEqual(list(parsed.keys()), ["first", "second", "third"])


class TestLightYAMLComments(unittest.TestCase):
    """Comment handling tests."""

    def test_parse_inline_comment(self):
        """Inline comment is stripped."""
        yaml = """key: value  # This is a comment"""
        result = LightYAML.load(yaml)
        self.assertEqual(result, {"key": "value"})

    def test_parse_comment_only_line(self):
        """Full-line comment is ignored."""
        yaml = """# Entire line is a comment
key: value"""
        result = LightYAML.load(yaml)
        self.assertEqual(result, {"key": "value"})

    def test_parse_multiple_comments(self):
        """Multiple comments."""
        yaml = """# Comment 1
# Comment 2
data: test"""
        result = LightYAML.load(yaml)
        self.assertEqual(result, {"data": "test"})


class TestLightYAMLStandardFeatures(unittest.TestCase):
    """Tests for standard YAML features that pyyaml supports natively."""

    def test_anchor_and_alias(self):
        """Anchors and aliases are supported."""
        yaml = """timeout_val: &t 30
default_timeout: *t"""
        result = LightYAML.load(yaml)
        self.assertEqual(result["timeout_val"], 30)
        self.assertEqual(result["default_timeout"], 30)

    def test_block_literal_string(self):
        """Multi-line literal block (|) is supported."""
        yaml = """data: |
  line1
  line2"""
        result = LightYAML.load(yaml)
        self.assertIn("line1", result["data"])
        self.assertIn("line2", result["data"])

    def test_block_folded_string(self):
        """Multi-line folded block (>) is supported."""
        yaml = """data: >
  this is
  a folded
  string"""
        result = LightYAML.load(yaml)
        self.assertIn("folded string", result["data"])


class TestLightYAMLErrors(unittest.TestCase):
    """Error handling tests."""

    def test_empty_input_returns_none(self):
        """Empty input returns None."""
        self.assertIsNone(LightYAML.load(""))
        self.assertIsNone(LightYAML.load("   "))
        self.assertIsNone(LightYAML.load(None))

    def test_invalid_yaml_syntax(self):
        """Invalid syntax raises YAMLParseError."""
        with self.assertRaises(YAMLParseError):
            LightYAML.load("[1, 2")  # Unclosed list

    def test_validate_valid_yaml(self):
        """Validate valid YAML."""
        valid, error = LightYAML.validate("key: value")
        self.assertTrue(valid)
        self.assertIsNone(error)

    def test_validate_invalid_yaml(self):
        """Validate invalid YAML."""
        valid, error = LightYAML.validate("[1, 2")
        self.assertFalse(valid)
        self.assertIsNotNone(error)


class TestLightYAMLFPGASignals(unittest.TestCase):
    """FPGA signal name tests - ensuring special characters are handled correctly."""

    def test_parse_bracket_signal(self):
        """Parse bracket signal names."""
        yaml = """signals:
  clk[0]: true
  data[7:0]: 0xFF"""
        result = LightYAML.load(yaml)
        self.assertIn("signals", result)
        self.assertIn("clk[0]", result["signals"])

    def test_roundtrip_bracket_signals(self):
        """Roundtrip test for bracket signal names."""
        data = {"signals": {"clk[0]": True, "data[7:0]": 255}}
        _, parsed = LightYAML.roundtrip(data)
        self.assertIn("clk[0]", parsed["signals"])
        self.assertEqual(parsed["signals"]["clk[0]"], True)

    def test_parse_underscore_signal(self):
        """Parse underscore signal name."""
        yaml = """io_out_valid: true"""
        result = LightYAML.load(yaml)
        self.assertEqual(result["io_out_valid"], True)

    def test_parse_hyphen_signal(self):
        """Parse hyphen signal name."""
        yaml = """data-in: 100"""
        result = LightYAML.load(yaml)
        self.assertIn("data-in", result)
        self.assertEqual(result["data-in"], 100)

    def test_parse_mixed_signal_names(self):
        """Parse mixed signal names."""
        yaml = """signals:
  clk_p: true
  rst_n: false
  io_data[3:0]: 5
  data_bus[31:0]: 0xDEADBEEF"""
        result = LightYAML.load(yaml)
        signals = result["signals"]
        self.assertEqual(signals["clk_p"], True)
        self.assertEqual(signals["rst_n"], False)
        self.assertEqual(signals["io_data[3:0]"], 5)
        self.assertEqual(signals["data_bus[31:0]"], 0xDEADBEEF)


class TestLightYAMLFPGAContext(unittest.TestCase):
    """FPGA business scenario tests (roundtrip data integrity)."""

    def test_port_list(self):
        """Port list scenario."""
        data = {
            "ports": [
                OrderedDict([
                    ("name", "clk"),
                    ("direction", "input"),
                    ("width", 1),
                    ("type", "std_logic")
                ]),
                OrderedDict([
                    ("name", "data[7:0]"),
                    ("direction", "output"),
                    ("width", 8),
                    ("type", "std_logic_vector")
                ]),
                OrderedDict([
                    ("name", "valid"),
                    ("direction", "output"),
                    ("width", 1),
                    ("type", "std_logic")
                ])
            ]
        }
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(len(parsed["ports"]), 3)
        self.assertEqual(parsed["ports"][0]["name"], "clk")
        self.assertEqual(parsed["ports"][1]["name"], "data[7:0]")

    def test_timing_constraints(self):
        """Timing constraints scenario."""
        data = {
            "constraints": {
                "clock_period_ns": 10.0,
                "setup_slack_ns": 2.5,
                "hold_slack_ns": 0.8,
                "target_fmax_mhz": 100.0,
                "critical_path_ns": 7.5
            }
        }
        _, parsed = LightYAML.roundtrip(data)
        constraints = parsed["constraints"]
        self.assertAlmostEqual(constraints["clock_period_ns"], 10.0)
        self.assertAlmostEqual(constraints["target_fmax_mhz"], 100.0)

    def test_optimization_status(self):
        """Optimization status scenario."""
        data = {
            "optimization": {
                "iteration": 5,
                "strategy": "aggressive",
                "wns": -1.25,
                "tns": -15.7,
                "target_wns": 0.0,
                "best_wns": 0.5,
                "utilization": {
                    "lut": 78.5,
                    "ff": 65.2,
                    "bram": 45.0,
                    "dsp": 30.0
                },
                "blocked_strategies": ["retime", "pipeline"]
            }
        }
        _, parsed = LightYAML.roundtrip(data)
        opt = parsed["optimization"]
        self.assertEqual(opt["iteration"], 5)
        self.assertEqual(opt["strategy"], "aggressive")
        self.assertAlmostEqual(opt["wns"], -1.25)
        self.assertEqual(len(opt["blocked_strategies"]), 2)

    def test_dcp_design_state(self):
        """DCP design state scenario."""
        data = {
            "design": {
                "name": "logicnets",
                "top_module": "top",
                "device": "xc7a35t-csg324",
                "status": "placed_routed",
                "netlist_type": "post_synth"
            },
            "timing": {
                "wns": 0.5,
                "tns": 12.3,
                "fmax_mhz": 142.5,
                "slack_histogram": [
                    {"range": "-5ns to -4ns", "count": 0},
                    {"range": "-4ns to -3ns", "count": 2},
                    {"range": "-3ns to -2ns", "count": 5}
                ]
            },
            "messages": [
                {"role": "user", "content": "optimize design for fmax"},
                {"role": "assistant", "content": "running place_design"},
                {"role": "tool", "content": "WNS=0.5ns"}
            ]
        }
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(parsed["design"]["name"], "logicnets")
        self.assertEqual(len(parsed["timing"]["slack_histogram"]), 3)
        self.assertEqual(len(parsed["messages"]), 3)

    def test_message_compression_context(self):
        """Message compression context scenario."""
        data = {
            "context": {
                "current_tokens": 65000,
                "threshold_tokens": 80000,
                "hard_limit_tokens": 150000,
                "compression_ratio": 0.45,
                "preserved_messages": [
                    {"role": "system", "content": "You are FPGA optimizer"},
                    {"role": "user", "content": "high fanout nets"}
                ],
                "summarized_count": 15
            }
        }
        _, parsed = LightYAML.roundtrip(data)
        ctx = parsed["context"]
        self.assertEqual(ctx["current_tokens"], 65000)
        self.assertEqual(len(ctx["preserved_messages"]), 2)

    def test_complex_facing_signal_names(self):
        """Complex signal name test."""
        data = {
            "io_bidir[15:0]": 0x1234,
            "clk_100mhz": True,
            "rst_n": False,
            "en_1": True,
            "data[31:0]_reg": 0,
            "__private_signal__": "hidden"
        }
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(parsed["io_bidir[15:0]"], 0x1234)
        self.assertEqual(parsed["clk_100mhz"], True)
        self.assertEqual(parsed["__private_signal__"], "hidden")


class TestLightYAMLIndentation(unittest.TestCase):
    """Indentation tests."""

    def test_default_indent_2(self):
        """Default 2-space indent."""
        data = {"outer": {"inner": "value"}}
        yaml = LightYAML.dump(data)
        self.assertIn("  inner:", yaml)

    def test_indent_4(self):
        """4-space indent."""
        data = {"outer": {"inner": "value"}}
        yaml = LightYAML.dump(data, indent=4)
        self.assertIn("    inner:", yaml)

    def test_parse_various_indent_levels(self):
        """Parse various indent levels."""
        yaml = """level0:
  level1:
    level2: deep"""
        result = LightYAML.load(yaml)
        self.assertEqual(result["level0"]["level1"]["level2"], "deep")


class TestLightYAMLFlowSyntax(unittest.TestCase):
    """Flow syntax tests (loading only; dump uses block format)."""

    def test_parse_flow_sequence(self):
        """Parse flow sequence."""
        yaml = "[1, 2, 3]"
        result = LightYAML.load(yaml)
        self.assertEqual(result, [1, 2, 3])

    def test_parse_flow_mapping(self):
        """Parse flow mapping."""
        yaml = "{key: value, num: 42}"
        result = LightYAML.load(yaml)
        self.assertEqual(result["key"], "value")
        self.assertEqual(result["num"], 42)

    def test_parse_nested_flow(self):
        """Parse nested flow."""
        yaml = "{outer: {inner: value}, list: [1, 2]}"
        result = LightYAML.load(yaml)
        self.assertEqual(result["outer"]["inner"], "value")
        self.assertEqual(result["list"], [1, 2])

    def test_dump_flow_sequence(self):
        """Serialize list uses block format with pyyaml."""
        data = [1, 2, 3]
        yaml = LightYAML.dump(data)
        self.assertIn("- 1", yaml)
        self.assertIn("- 2", yaml)


class TestLightYAMLPerformance(unittest.TestCase):
    """Performance/stress tests."""

    def test_deep_nesting(self):
        """Deep nesting (20 levels)."""
        data = {"level": 0}
        for i in range(1, 20):
            data = {"level": i, "child": data}
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(parsed["level"], 19)

    def test_wide_structure(self):
        """Wide structure - many key-value pairs."""
        data = {f"key_{i}": f"value_{i}" for i in range(100)}
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(len(parsed), 100)

    def test_long_list(self):
        """Long list."""
        data = {"items": list(range(500))}
        _, parsed = LightYAML.roundtrip(data)
        self.assertEqual(len(parsed["items"]), 500)


class TestLightYAMLErrorMessages(unittest.TestCase):
    """Error message content validation tests."""

    def test_parse_error_on_invalid_syntax(self):
        """YAMLParseError for invalid syntax."""
        with self.assertRaises(YAMLParseError) as ctx:
            LightYAML.load("[1, 2")
        error_msg = str(ctx.exception).lower()
        self.assertTrue(
            "parse" in error_msg or "expect" in error_msg or "unclosed" in error_msg,
            f"Error message should describe syntax issue: {ctx.exception}"
        )

    def test_dump_set(self):
        """Set is supported via pyyaml's !!set tag."""
        yaml = LightYAML.dump({1, 2, 3})
        self.assertIn("1", yaml)
        self.assertIn("2", yaml)


class TestLightYAMLMCPToolResults(unittest.TestCase):
    """MCP tool result integration tests using real data formats."""

    def test_rapidwright_design_info(self):
        """Parse RapidWright get_design_info JSON output."""
        json_str = json.dumps({
            "status": "success",
            "design_name": "design_1_wrapper",
            "device": "xcvu9p-flgb2104-2-i",
            "part_name": "xczu9eg-ffvb2104-2-i",
            "cell_count": 48250,
            "net_count": 51300,
            "top_cell_types": {
                "LUT6": 12450,
                "LUT5": 8230,
                "FDRE": 15800,
                "DSP48E2": 48,
                "RAMB36E1": 12
            },
            "is_netlist_encrypted": False
        })
        data = json.loads(json_str)
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertEqual(parsed["design_name"], "design_1_wrapper")
        self.assertEqual(parsed["top_cell_types"]["LUT6"], 12450)
        self.assertEqual(parsed["top_cell_types"]["DSP48E2"], 48)

    def test_rapidwright_utilization_nested(self):
        """Parse RapidWright nested utilization with top_cell_types."""
        json_str = json.dumps({
            "status": "success",
            "design_name": "design_1_wrapper",
            "device": "xcvu9p-flgb2104-2-i",
            "cell_count": 48250,
            "top_cell_types": {
                "LUT6": 12450,
                "LUT5": 8230,
                "FDRE": 15800,
                "DSP48E2": 48,
                "RAMB36E1": 12
            }
        })
        data = json.loads(json_str)
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertEqual(parsed["top_cell_types"]["RAMB36E1"], 12)

    def test_rapidwright_analyze_critical_path(self):
        """Parse RapidWright analyze_critical_path_spread result."""
        data = {
            "status": "success",
            "paths_analyzed": 50,
            "max_distance_found": 45,
            "avg_max_distance": 28.5,
            "path_distances": [45, 42, 38, 35, 33, 30, 28, 26, 24, 22],
            "worst_path": {
                "path_num": 1,
                "cell_count": 15,
                "max_distance": 45,
                "start_cell": "inst_0/U0/data_path_proc/add_0",
                "end_cell": "inst_0/U0/mem_controller/data_reg"
            }
        }
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertEqual(parsed["worst_path"]["max_distance"], 45)
        self.assertEqual(parsed["worst_path"]["end_cell"], "inst_0/U0/mem_controller/data_reg")

    def test_vivado_wns_raw_string(self):
        """Parse raw Vivado get_wns output."""
        wns_value = "-0.847"
        data = {"wns": float(wns_value)}
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertAlmostEqual(parsed["wns"], -0.847)

    def test_vivado_utilization_text(self):
        """Parse Vivado report_utilization_for_pblock plain text table."""
        text_output = """=== Design Resource Utilization ===

LUTs:     28,450
FFs:      31,200
DSPs:         48
BRAMs:        24
URAMs:         0"""
        data = {"utilization_report": text_output}
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertIn("LUTs:", parsed["utilization_report"])
        self.assertIn("28,450", parsed["utilization_report"])

    def test_mcp_json_string_roundtrip(self):
        """RapidWright JSON -> parse -> YAML -> parse -> verify."""
        original = {
            "status": "success",
            "device": "xcvu9p-flgb2104-2-i",
            "cell_count": 48250,
            "net_count": 51300,
            "is_netlist_encrypted": False
        }
        json_str = json.dumps(original)
        parsed_json = json.loads(json_str)
        yaml_str = LightYAML.dump(parsed_json)
        final_parsed = LightYAML.load(yaml_str)
        self.assertEqual(final_parsed["status"], "success")
        self.assertEqual(final_parsed["device"], "xcvu9p-flgb2104-2-i")
        self.assertEqual(final_parsed["cell_count"], 48250)
        self.assertEqual(final_parsed["is_netlist_encrypted"], False)

    def test_tool_result_with_special_chars(self):
        """Signal names with brackets/underscores survive MCP roundtrip."""
        json_str = json.dumps({
            "status": "success",
            "signals": {
                "clk[0]": True,
                "data[7:0]": 255,
                "rst_n": False,
                "io_bidir[15:0]": 0x1234,
                "clk_100mhz": True,
                "data[31:0]_reg": 0
            }
        })
        data = json.loads(json_str)
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertTrue(parsed["signals"]["clk[0]"])
        self.assertEqual(parsed["signals"]["data[7:0]"], 255)
        self.assertEqual(parsed["signals"]["data[31:0]_reg"], 0)

    def test_compare_design_structure(self):
        """Parse RapidWright compare_design_structure with nested designs."""
        data = {
            "status": "success",
            "comparison_result": "PASS",
            "checks_passed": 4,
            "checks_total": 4,
            "golden_design": {
                "path": "C:/designs/golden.dcp",
                "top_module": "design_1_wrapper",
                "device": "xcvu9p-flgb2104-2-i",
                "cell_count": 48000,
                "port_count": 156
            },
            "revised_design": {
                "path": "C:/designs/optimized.dcp",
                "top_module": "design_1_wrapper",
                "device": "xcvu9p-flgb2104-2-i",
                "cell_count": 48250,
                "port_count": 156
            },
            "issues": [
                "INFO: Cell count increased from 48000 to 48250 (0.52pct increase)"
            ]
        }
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertEqual(parsed["comparison_result"], "PASS")
        self.assertEqual(parsed["golden_design"]["cell_count"], 48000)
        self.assertEqual(parsed["revised_design"]["cell_count"], 48250)


class TestLightYAMLEdgeCases(unittest.TestCase):
    """Boundary conditions and edge cases."""

    def test_empty_value(self):
        """Empty string value."""
        result = LightYAML.load('key: ""')
        self.assertEqual(result, {"key": ""})

    def test_unicode_in_keys_and_values(self):
        """Unicode characters in keys and values."""
        data = {
            "中文键": "中文值",
            "emoji": "🎉 FPGA",
            "关键": "值"
        }
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertEqual(parsed["中文键"], "中文值")
        self.assertIn("🎉", parsed["emoji"])

    def test_escape_sequences(self):
        """Escape sequence handling in quoted strings."""
        yaml = 'key: "line1\\nline2"'
        parsed = LightYAML.load(yaml)
        self.assertIn("line2", parsed["key"])
        self.assertIn("line1", parsed["key"])

    def test_backslash_in_string(self):
        """Backslash character preservation."""
        yaml = 'path: "C:\\\\Windows\\\\System32"'
        parsed = LightYAML.load(yaml)
        self.assertIn("Windows", parsed["path"])

    def test_scientific_notation(self):
        """Scientific notation parsing."""
        yaml = "value: 1.5e-10\nextra: 2.3E+5"
        parsed = LightYAML.load(yaml)
        self.assertAlmostEqual(parsed["value"], 1.5e-10)
        self.assertAlmostEqual(parsed["extra"], 2.3e5)

    def test_deeply_nested_list(self):
        """Deeply nested dict structure."""
        data = {"level": 5}
        for i in range(4, -1, -1):
            data = {"level": i, "child": data}
        yaml_str = LightYAML.dump(data)
        parsed = LightYAML.load(yaml_str)
        self.assertEqual(parsed["level"], 0)
        self.assertEqual(parsed["child"]["child"]["child"]["child"]["level"], 4)


class TestLightYAMLStructuredCompressorIntegration(unittest.TestCase):
    """Integration tests for YAMLStructuredCompressor."""

    def test_messages_to_yaml_function(self):
        """Test standalone messages_to_yaml() function."""
        from context_manager.strategies.yaml_structured_compress import messages_to_yaml, Message, MessageRole, CompressionContext

        messages = [
            Message(role=MessageRole.SYSTEM, content="You are FPGA optimizer"),
            Message(role=MessageRole.USER, content="Optimize timing for critical path"),
            Message(role=MessageRole.ASSISTANT, content="Running place_design"),
        ]
        context = CompressionContext(
            current_tokens=5000,
            threshold_tokens=80000,
            iteration=5,
            best_wns=-1.5,
            current_wns=-0.8,
            clock_period=5.0
        )
        yaml_output = messages_to_yaml(messages, context)
        self.assertIsInstance(yaml_output, str)
        self.assertIn("compression_type", yaml_output)
        self.assertIn("yaml_structured", yaml_output)

    def test_yaml_compressor_roundtrip(self):
        """Compress -> serialize -> deserialize -> verify structure."""
        from context_manager.strategies.yaml_structured_compress import YAMLStructuredCompressor, Message, MessageRole, CompressionContext

        messages = [
            Message(role=MessageRole.SYSTEM, content="You are FPGA optimizer"),
            Message(role=MessageRole.USER, content="WNS is -2.5ns"),
            Message(role=MessageRole.TOOL, content="Critical path delay: 7.2ns"),
        ]
        context = CompressionContext(
            iteration=3,
            best_wns=-2.5,
            current_wns=-1.0,
            clock_period=5.0
        )
        compressor = YAMLStructuredCompressor()
        compressed = compressor.compress(messages, context)
        self.assertGreaterEqual(len(compressed), 2)

        yaml_msg = None
        for msg in compressed:
            if msg.metadata.get('compression_type') == 'yaml_structured':
                yaml_msg = msg
                break
        self.assertIsNotNone(yaml_msg, "Should have a YAML summary message")
        self.assertIn("iteration", yaml_msg.content)

    def test_compression_context_in_yaml(self):
        """Verify CompressionContext fields appear in YAML output."""
        from context_manager.strategies.yaml_structured_compress import messages_to_yaml, Message, MessageRole, CompressionContext

        messages = [
            Message(role=MessageRole.SYSTEM, content="System prompt"),
        ]
        context = CompressionContext(
            iteration=10,
            best_wns=-0.5,
            initial_wns=-3.2,
            current_wns=-1.1,
            clock_period=5.0,
            failed_strategies=["route_design -directive Aggressive"]
        )
        yaml_output = messages_to_yaml(messages, context)
        self.assertIn("10", yaml_output)
        self.assertIn("-0.5", yaml_output)

    def test_yaml_preserves_signal_names(self):
        """Ensure clk[0], data[7:0] survive compression cycle."""
        from context_manager.strategies.yaml_structured_compress import messages_to_yaml, Message, MessageRole, CompressionContext

        messages = [
            Message(role=MessageRole.SYSTEM, content="System prompt"),
            Message(role=MessageRole.USER, content="Check signal clk[0] and data[7:0]"),
        ]
        context = CompressionContext(
            iteration=1,
            current_tokens=1000
        )
        yaml_output = messages_to_yaml(messages, context)
        self.assertIn("clk[0]", yaml_output)
        self.assertIn("data[7:0]", yaml_output)


# ============================================================================
# Test Runner
# ============================================================================

def run_tests():
    """Run all tests."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLBasicTypes))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLDataStructures))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLRoundtripConsistency))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLComments))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLStandardFeatures))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLErrors))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLFPGASignals))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLFPGAContext))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLIndentation))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLFlowSyntax))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLPerformance))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLErrorMessages))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLMCPToolResults))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLEdgeCases))
    suite.addTests(loader.loadTestsFromTestCase(TestLightYAMLStructuredCompressorIntegration))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    exit_code = run_tests()
    sys.exit(exit_code)
