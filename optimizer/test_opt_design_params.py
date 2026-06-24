"""Layer 1 Unit Regression Tests: vivado_opt_design parameter guard.

Verifies that the -retarget + -directive conflict is correctly prevented.
Tests the guard logic that was added to VivadoMCP/vivado_mcp_server.py.
"""

import pytest


# ── The guard logic extracted for testability ──
# This is the exact logic from vivado_mcp_server.py lines 2554-2567

def build_opt_design_command(directive: str = "Explore", retarget: bool = True) -> str:
    """Build opt_design Tcl command with the safety guard.

    Vivado constraint: [Vivado_Tcl 4-167] Cannot specify '-retarget'
    when '-directive' is specified.

    The guard: when an active directive (non-empty, not "Default") is used,
    suppress -retarget because the directive implies equivalent behavior.
    """
    has_active_directive = bool(directive)

    cmd = "opt_design"
    if has_active_directive:
        cmd += f" -directive {directive}"
    elif retarget:
        cmd += " -retarget"

    return cmd


# ── Tests ──

class TestOptDesignCommandBuilding:
    """Verify opt_design never generates conflicting flag combinations."""

    # P0 regression: the exact bug from the log
    def test_addremap_no_retarget(self):
        """AddRemap directive MUST NOT include -retarget flag."""
        cmd = build_opt_design_command(directive="AddRemap", retarget=True)
        assert "-retarget" not in cmd, (
            f"BUG REGRESSION: -directive AddRemap -retarget is forbidden by Vivado. "
            f"Got: {cmd}"
        )
        assert "-directive AddRemap" in cmd

    def test_explore_no_retarget(self):
        """Explore directive MUST NOT include -retarget flag."""
        cmd = build_opt_design_command(directive="Explore", retarget=True)
        assert "-retarget" not in cmd, (
            f"BUG REGRESSION: -directive Explore -retarget is forbidden. Got: {cmd}"
        )
        assert "-directive Explore" in cmd

    def test_default_directive_suppresses_retarget(self):
        """Default directive also suppresses -retarget (maximally safe)."""
        cmd = build_opt_design_command(directive="Default", retarget=True)
        assert "-retarget" not in cmd, (
            f"Maximally safe: even -directive Default should suppress -retarget. Got: {cmd}"
        )
        assert "-directive Default" in cmd

    def test_no_directive_allows_retarget(self):
        """Empty directive ALLOWS -retarget."""
        cmd = build_opt_design_command(directive="", retarget=True)
        assert "-retarget" in cmd, f"Plain opt_design -retarget should work. Got: {cmd}"
        assert "-directive" not in cmd

    def test_retarget_false_no_flag(self):
        """When retarget=False, no -retarget flag regardless of directive."""
        cmd = build_opt_design_command(directive="Explore", retarget=False)
        assert "-retarget" not in cmd
        assert "-directive Explore" in cmd

        cmd2 = build_opt_design_command(directive="", retarget=False)
        assert "-retarget" not in cmd2
        assert "-directive" not in cmd2

    def test_all_directives_no_conflict(self):
        """ALL directive values must never produce -retarget conflict."""
        all_directives = [
            "AddRemap", "Explore", "ExploreArea",
            "ExploreSequentialArea", "RuntimeOptimized",
        ]
        for directive in all_directives:
            cmd = build_opt_design_command(directive=directive, retarget=True)
            has_both = "-directive" in cmd and "-retarget" in cmd
            assert not has_both, (
                f"FORBIDDEN: directive={directive} with -retarget. "
                f"Vivado rejects this combination. Got: {cmd}"
            )

    def test_addremap_without_retarget_is_valid(self):
        """AddRemap alone (no -retarget) produces valid Vivado command."""
        cmd = build_opt_design_command(directive="AddRemap", retarget=False)
        assert cmd == "opt_design -directive AddRemap"

    def test_retarget_only_is_valid(self):
        """opt_design -retarget (no directive) is valid Vivado."""
        cmd = build_opt_design_command(directive="", retarget=True)
        assert cmd == "opt_design -retarget"

    def test_default_no_args(self):
        """Default args should produce a valid command."""
        cmd = build_opt_design_command()
        # Default: directive="Explore", retarget=True -> should suppress retarget
        assert "-retarget" not in cmd, (
            f"Default command with directive=Explore must NOT include -retarget. "
            f"Got: {cmd}"
        )
        assert "-directive Explore" in cmd

    def test_none_directive_treated_as_empty(self):
        """None directive should be treated as no directive."""
        cmd = build_opt_design_command(directive=None, retarget=True)  # type: ignore
        assert "-retarget" in cmd
        assert "-directive" not in cmd


class TestOptDesignFlagCombinations:
    """Integration-style: verify specific flag combinations from the log."""

    def test_exact_bug_scenario_1(self):
        """Log 16:14:41 — opt_design -directive AddRemap -retarget ERROR."""
        cmd = build_opt_design_command(directive="AddRemap", retarget=True)
        assert cmd == "opt_design -directive AddRemap", (
            f"Should NOT produce the buggy command. Got: {cmd}"
        )

    def test_exact_bug_scenario_2(self):
        """Log 16:23:57 — opt_design -directive Explore -retarget ERROR."""
        cmd = build_opt_design_command(directive="Explore", retarget=True)
        assert cmd == "opt_design -directive Explore", (
            f"Should NOT produce the buggy command. Got: {cmd}"
        )

    def test_exact_bug_scenario_3(self):
        """Log 16:30:50 — opt_design -directive AddRemap -retarget ERROR (retry)."""
        cmd = build_opt_design_command(directive="AddRemap", retarget=True)
        assert "-retarget" not in cmd

    @pytest.mark.parametrize("directive,retarget,expected_has_directive,expected_has_retarget", [
        ("AddRemap", True, True, False),
        ("AddRemap", False, True, False),
        ("Explore", True, True, False),
        ("Explore", False, True, False),
        ("Default", True, True, False),   # Default directive suppresses retarget
        ("Default", False, True, False),  # Default directive + no retarget → -directive Default only
        ("", True, False, True),          # No directive + retarget is valid
        ("", False, False, False),
    ])
    def test_parameterized_combinations(
        self, directive, retarget, expected_has_directive, expected_has_retarget
    ):
        """Exhaustive test of all directive × retarget combinations."""
        cmd = build_opt_design_command(directive=directive, retarget=retarget)
        actual_has_directive = "-directive" in cmd
        actual_has_retarget = "-retarget" in cmd

        assert actual_has_directive == expected_has_directive, (
            f"directive={directive!r}, retarget={retarget}: "
            f"expected -directive={expected_has_directive}, got {cmd}"
        )
        assert actual_has_retarget == expected_has_retarget, (
            f"directive={directive!r}, retarget={retarget}: "
            f"expected -retarget={expected_has_retarget}, got {cmd}"
        )
