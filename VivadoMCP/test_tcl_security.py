"""Unit tests for TCL security primitives (tcl_security.py).

Tests the security-critical functions extracted from vivado_mcp_server.py:
  - contains_blocked_tcl_command: injection / bypass detection
  - tcl_quote: safe TCL brace quoting
  - tcl_line_is_complete: line completeness pre-check (replaces info complete)

Run:  pytest VivadoMCP/test_tcl_security.py -v
"""

import pytest

from tcl_security import (
    BLOCKED_TCL_COMMANDS,
    contains_blocked_tcl_command,
    contains_retiming_command,
    contains_equivalence_unsafe_command,
    tcl_quote,
    tcl_line_is_complete,
)


class TestBlockedCommandDetection:
    """Verify ALL common bypass forms of dangerous TCL commands are caught."""

    def test_direct_exec(self):
        assert contains_blocked_tcl_command("exec ls")

    def test_direct_source(self):
        assert contains_blocked_tcl_command("source /etc/passwd")

    def test_direct_eval(self):
        assert contains_blocked_tcl_command("eval {exec ls}")

    def test_direct_subst(self):
        assert contains_blocked_tcl_command("subst {exec ls}")

    def test_direct_load(self):
        assert contains_blocked_tcl_command("load /tmp/evil.so")

    def test_direct_open(self):
        assert contains_blocked_tcl_command("open /etc/passwd r")

    def test_direct_socket(self):
        assert contains_blocked_tcl_command("socket -server 127.0.0.1 4444")

    def test_direct_cd(self):
        assert contains_blocked_tcl_command("cd /")

    def test_direct_pwd(self):
        assert contains_blocked_tcl_command("pwd")

    def test_direct_exit(self):
        assert contains_blocked_tcl_command("exit")

    def test_semicolon_split_exec(self):
        assert contains_blocked_tcl_command("puts hi; exec ls")

    def test_semicolon_split_eval(self):
        assert contains_blocked_tcl_command("set x 1; eval {exec ls}")

    def test_multiple_semicolons(self):
        assert contains_blocked_tcl_command("puts a; puts b; exec rm -rf /")

    def test_command_subst_exec(self):
        assert contains_blocked_tcl_command("set x [exec ls]")

    def test_command_subst_eval(self):
        assert contains_blocked_tcl_command("set x [eval {exec ls}]")

    def test_nested_command_subst(self):
        assert contains_blocked_tcl_command("set y [set x [exec ls]]")

    def test_multiline_exec(self):
        assert contains_blocked_tcl_command("set x 5\nexec ls")

    def test_multiline_eval(self):
        assert contains_blocked_tcl_command("set x 5\neval {exec ls}")

    def test_case_insensitive_exec(self):
        assert contains_blocked_tcl_command("EXEC ls")

    def test_case_insensitive_eval(self):
        assert contains_blocked_tcl_command("EVAL {exec ls}")

    def test_case_insensitive_source(self):
        assert contains_blocked_tcl_command("SOURCE /tmp/x.tcl")

    def test_uplevel_direct(self):
        assert contains_blocked_tcl_command("uplevel #0 {exec ls}")

    def test_uplevel_in_command_subst(self):
        assert contains_blocked_tcl_command("set x [uplevel 0 {exec ls}]")

    def test_uplevel_semicolon(self):
        assert contains_blocked_tcl_command("puts hi; uplevel 1 {exec ls}")

    def test_uplevel_case_insensitive(self):
        assert contains_blocked_tcl_command("UPLEVEL 0 {exec ls}")


class TestSafeCommandsPass:
    """Verify legitimate Vivado TCL commands are NOT blocked."""

    def test_report_timing_summary(self):
        assert not contains_blocked_tcl_command("report_timing_summary")

    def test_get_cells(self):
        assert not contains_blocked_tcl_command("get_cells *")

    def test_set_property(self):
        assert not contains_blocked_tcl_command("set_property IS_SOFT 1 [get_pblocks pblock_0]")

    def test_place_design(self):
        assert not contains_blocked_tcl_command("place_design")

    def test_route_design(self):
        assert not contains_blocked_tcl_command("route_design")

    def test_phys_opt_design(self):
        assert not contains_blocked_tcl_command("phys_opt_design -directive Explore")

    def test_opt_design(self):
        assert not contains_blocked_tcl_command("opt_design -directive Explore -retarget")

    def test_multi_line_safe(self):
        assert not contains_blocked_tcl_command("report_timing_summary\nget_cells *")

    def test_empty_command(self):
        assert not contains_blocked_tcl_command("")

    def test_comment_line(self):
        assert not contains_blocked_tcl_command("# exec ls")

    def test_comment_after_semicolon(self):
        assert not contains_blocked_tcl_command("puts hi; # exec ls")

    def test_exec_as_substring_not_blocked(self):
        assert not contains_blocked_tcl_command("execute_pipeline")

    def test_set_variable_named_exec(self):
        assert not contains_blocked_tcl_command("set exec 1")


class TestBlockedCommandSetContents:
    """Verify the blocked-command set contains all expected entries."""

    @pytest.mark.parametrize("cmd", [
        "exec", "source", "eval", "subst", "uplevel",
        "load", "open", "socket", "cd", "pwd", "exit",
    ])
    def test_command_in_blocked_set(self, cmd):
        assert cmd in BLOCKED_TCL_COMMANDS


class TestTclQuote:
    """Verify tcl_quote produces safe TCL brace-quoted literals."""

    def test_simple_value(self):
        assert tcl_quote("Explore") == "{Explore}"

    def test_default_value(self):
        assert tcl_quote("Default") == "{Default}"

    def test_value_with_spaces(self):
        assert tcl_quote("hello world") == "{hello world}"

    def test_value_with_brackets(self):
        assert tcl_quote("get_cells [current_design]") == "{get_cells [current_design]}"

    def test_value_with_open_brace(self):
        assert tcl_quote("set {x") == "{set {x}"

    def test_empty_string(self):
        assert tcl_quote("") == "{}"

    def test_rejects_close_brace(self):
        with pytest.raises(ValueError, match="Cannot safely quote"):
            tcl_quote("evil}")

    def test_rejects_close_brace_in_middle(self):
        with pytest.raises(ValueError, match="Cannot safely quote"):
            tcl_quote("ev}il")

    def test_rejects_close_brace_only(self):
        with pytest.raises(ValueError, match="Cannot safely quote"):
            tcl_quote("}")


class TestTclLineIsComplete:
    """Verify the Python-side completeness check (C1 fix: replaces info complete)."""

    def test_simple_command(self):
        assert tcl_line_is_complete("set x 5")

    def test_empty_line(self):
        assert tcl_line_is_complete("")

    def test_braced_value_balanced(self):
        assert tcl_line_is_complete("set x {hello world}")

    def test_nested_braces_balanced(self):
        assert tcl_line_is_complete("set x {a {b} c}")

    def test_brackets_balanced(self):
        assert tcl_line_is_complete("set x [get_cells *]")

    def test_unbalanced_open_brace(self):
        assert not tcl_line_is_complete("set x {hello")

    def test_unbalanced_close_brace(self):
        assert not tcl_line_is_complete("set x hello}")

    def test_unbalanced_open_bracket(self):
        assert not tcl_line_is_complete("set x [get_cells")

    def test_unbalanced_close_bracket(self):
        assert not tcl_line_is_complete("set x get_cells]")

    def test_backslash_continuation(self):
        assert not tcl_line_is_complete("set x \\")

    def test_backslash_continuation_with_spaces(self):
        assert not tcl_line_is_complete("set x 5   \\")

    def test_c1_attack_vector_balanced_braces(self):
        line = "}; exec rm -rf /; {"
        assert tcl_line_is_complete(line)

    def test_c1_attack_vector_blocked_command(self):
        line = "}; exec rm -rf /; {"
        assert tcl_line_is_complete(line)
        assert contains_blocked_tcl_command(line)


class TestInjectionVectors:
    """End-to-end: verify the actual C1/M1 attack vectors are neutralized."""

    def test_c1_info_complete_injection(self):
        line = "}; exec rm -rf /; {"
        assert tcl_line_is_complete(line)
        assert contains_blocked_tcl_command(line)

    def test_m1_semicolon_bypass(self):
        assert contains_blocked_tcl_command("puts hello; exec rm -rf /tmp")

    def test_m1_command_subst_bypass(self):
        assert contains_blocked_tcl_command("set result [exec cat /etc/passwd]")

    def test_m1_eval_bypass(self):
        assert contains_blocked_tcl_command("eval {exec ls -la}")

    def test_m1_uplevel_bypass(self):
        assert contains_blocked_tcl_command("uplevel #0 {exec ls -la}")

    def test_multiline_injection_line2(self):
        assert contains_blocked_tcl_command("set x 5\nexec rm -rf /")

    def test_injection_in_nested_subst(self):
        assert contains_blocked_tcl_command("set x [subst {exec [eval {exec ls}]}]")

    def test_directive_injection_attempt(self):
        with pytest.raises(ValueError):
            tcl_quote("Explore}; exec ls; {Explore")

class TestRetimingBlocked:
    """Verify retiming-related TCL patterns are blocked."""

    def test_add_retime(self):
        assert contains_retiming_command("phys_opt_design -directive AddRetime")
        assert contains_blocked_tcl_command("phys_opt_design -directive AddRetime")

    def test_alternate_flow_with_retiming(self):
        assert contains_retiming_command("AlternateFlowWithRetiming")
        assert contains_blocked_tcl_command("AlternateFlowWithRetiming")

    def test_retime_flag(self):
        assert contains_retiming_command("opt_design -retime")
        assert contains_blocked_tcl_command("opt_design -retime")

    def test_interconnect_retime(self):
        assert contains_retiming_command("interconnect_retime")
        assert contains_blocked_tcl_command("interconnect_retime")

    def test_performance_retiming(self):
        assert contains_retiming_command("Performance_Retiming")
        assert contains_blocked_tcl_command("Performance_Retiming")

    def test_blocked_tcl_catches_retiming(self):
        assert contains_blocked_tcl_command("puts hi; phys_opt_design -directive AddRetime")


class TestEquivalenceUnsafeBlocked:
    """Verify equivalence-unsafe TCL patterns are blocked."""

    def test_remove_cell_bracket(self):
        assert contains_equivalence_unsafe_command("remove_cell [get_cells *]")
        assert contains_blocked_tcl_command("remove_cell [get_cells *]")

    def test_eco_remove_cell(self):
        assert contains_equivalence_unsafe_command("eco -remove_cell [get_cells u0]")
        assert contains_blocked_tcl_command("eco -remove_cell [get_cells u0]")

    def test_eco_rename_net(self):
        assert contains_equivalence_unsafe_command("eco -rename_net [get_nets n1] new_net")
        assert contains_blocked_tcl_command("eco -rename_net [get_nets n1] new_net")

    def test_write_verilog_mode_design(self):
        assert contains_equivalence_unsafe_command("write_verilog -mode design out.v")
        assert contains_blocked_tcl_command("write_verilog -mode design out.v")

    def test_blocked_tcl_catches_equiv_unsafe(self):
        assert contains_blocked_tcl_command("puts hi; remove_cell [get_cells *]")
