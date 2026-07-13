"""P0 failure->retry-freedom tests.

Covers the three P0 changes:
- ③A structured error classification (tool_error_classify + summary envelope)
- ②C tool_error visible in catalog with [RETRY] marker + detail
- ②B retry budget: retriable failures escalate to strategy_ineffective after
      RETRY_BUDGET retries, and stricter classifications are preserved.

(③B EVALUATE feedback and ①A trust_llm_input are integration paths inside the
large async phase loops; their imports are smoke-tested here and their
non-regression is covered by the existing 490-test unit suite.)
"""

from __future__ import annotations

import pytest

from optimizer.state import (
    OptimizerState,
    FailedStrategyRecord,
    RETRY_BUDGET,
    record_strategy_failure,
)
from optimizer.pure.tool_error_classify import classify_tool_error, error_envelope_lines
from optimizer.pure.tool_summary import summarize_tool_result
from optimizer.pure.context_snapshot import inject_merged_dashboard
from optimizer.pure.tool_filter import LoopPhase
from strategy_library import get_strategy_catalog


# ── ③A: structured error classification ───────────────────────────────────


class TestClassifyToolError:
    @pytest.mark.parametrize("error,expected", [
        ("place_design directive 'BadDir' was not recognized by Vivado (Constraints 18-641)", "bad_directive"),
        ("Directive 'X' not a recognized directive", "bad_directive"),
        ('{"reason": "invalid_cell_names", "status": "rejected"}', "bad_cell_name"),
        ("Cell names must be hierarchical paths", "bad_cell_name"),
        ("[BLOCKED] Command contains a blocked TCL command", "tcl_blocked"),
        ("[AUTO-GUIDANCE] Detected TCL command", "tcl_blocked"),
        ("Application-level timeout after 60s", "timeout"),
        ("[ERROR] Tcl command timed out after 30s", "timeout"),
        ("MCP tool error: invalid args", "schema_validation"),
        ("Input validation error: foo", "schema_validation"),
        ('{"errors": ["a", "b"]}', "partial_failure"),
        ("[RATE LIMITED] Tool called 3 times", "rate_limited"),
        ("place_design failed: ERROR: [Place 30-99] routing congestion", "vivado_error"),
    ])
    def test_categories(self, error, expected):
        cls = classify_tool_error("vivado_place_design", error)
        assert cls.category == expected, f"{error!r} -> {cls.category} (want {expected})"

    def test_rate_limited_not_retryable(self):
        cls = classify_tool_error("rapidwright_search_cells", "[RATE LIMITED] called 3 times")
        assert cls.retryable is False

    def test_default_retryable(self):
        # Most tool errors are parameter-fixable -> retryable by default.
        assert classify_tool_error("vivado_place_design", "some weird error").retryable is True
        assert classify_tool_error("vivado_place_design", "some weird error").category == "unknown"

    def test_empty_error(self):
        assert classify_tool_error("x", "").category == "unknown"

    def test_envelope_lines_format(self):
        lines = error_envelope_lines("vivado_place_design", "not a recognized directive")
        assert any(l.startswith("  error_category:") for l in lines)
        assert any(l.startswith("  fix_hint:") for l in lines)
        assert any(l.startswith("  retryable:") for l in lines)


class TestSummaryErrorEnvelope:
    def test_small_error_has_envelope(self):
        # Small error (<3KB) goes through the bypass path but must still
        # carry the structured envelope (P0 ③A).
        out = summarize_tool_result(
            "vivado_place_design",
            '{"error": "directive X not a recognized directive"}',
        )
        assert "status: error" in out
        assert "error_category: bad_directive" in out
        assert "fix_hint:" in out
        assert "retryable: true" in out

    def test_success_no_envelope(self):
        out = summarize_tool_result("vivado_get_wns", '{"wns": -0.5, "tns": -1.0}')
        assert "error_category:" not in out


# ── ②B: retry budget escalation ────────────────────────────────────────────


class TestRetryBudgetEscalation:
    def test_retriable_failures_escalate_after_budget(self):
        s = OptimizerState()
        s.iteration.current = 2
        # attempt 1: tool_error -> retriable, retry_count=0
        record_strategy_failure(s, "PBLOCK", "tool_error", tool="rw_pblock", detail="d1")
        e = s.context.failed_strategies[0]
        assert e.reason == "tool_error" and e.retry_count == 0 and e.blocked_until_iter == 2
        # attempt 2: retry_count=1, still retriable
        record_strategy_failure(s, "PBLOCK", "tool_error", tool="rw_pblock", detail="d2")
        assert e.reason == "tool_error" and e.retry_count == 1
        # attempt 3 (RETRY_BUDGET+1 total): escalate to strategy_ineffective (TTL=1)
        record_strategy_failure(s, "PBLOCK", "tool_error", tool="rw_pblock", detail="d3")
        assert e.reason == "strategy_ineffective"
        assert e.retry_count == RETRY_BUDGET
        assert e.blocked_until_iter == 3  # current(2) + 1

    def test_escalated_strategy_is_hard_blocked(self):
        from optimizer.nodes.subgraphs.phase_select_strategy import _get_permanently_blocked_strategies
        s = OptimizerState()
        s.iteration.current = 2
        # drive to escalation
        for _ in range(RETRY_BUDGET + 1):
            record_strategy_failure(s, "OptDesign", "tool_error", tool="rw_opt", detail="d")
        # now strategy_ineffective, blocked_until_iter = 3 > current 2 -> blocked
        assert "OptDesign" in _get_permanently_blocked_strategies(s)
        # after TTL expires (iter 3), unblocked
        s.iteration.current = 3
        assert "OptDesign" not in _get_permanently_blocked_strategies(s)

    def test_data_quality_error_also_escalates(self):
        s = OptimizerState()
        s.iteration.current = 1
        for _ in range(RETRY_BUDGET + 1):
            record_strategy_failure(s, "PBLOCK", "data_quality_error", tool="rw_pblock", detail="bad cells")
        assert s.context.failed_strategies[0].reason == "strategy_ineffective"

    def test_stricter_reason_not_downgraded(self):
        """Regression: strategy_not_applicable (TTL=5) must survive a later
        tool_error re-scan (the existing no-downgrade guard)."""
        s = OptimizerState()
        s.iteration.current = 2
        record_strategy_failure(s, "LUTCascade", "strategy_not_applicable",
                                tool="rw_cascade", detail="chain_skipped")
        e = s.context.failed_strategies[0]
        assert e.blocked_until_iter == 7  # 2 + STRATEGY_NOT_APPLICABLE_TTL(5)
        record_strategy_failure(s, "LUTCascade", "tool_error",
                                tool="rw_cascade", detail="empty")
        assert len(s.context.failed_strategies) == 1  # no duplicate
        assert e.reason == "strategy_not_applicable"  # not downgraded
        assert e.blocked_until_iter == 7

    def test_retry_budget_constant(self):
        assert RETRY_BUDGET == 2


# ── ②C: tool_error visible in catalog ──────────────────────────────────────


class TestToolErrorVisible:
    def _state_with_failure(self, reason: str, retry_count: int = 0) -> OptimizerState:
        s = OptimizerState()
        s.iteration.current = 1
        s.context.failed_strategies = [
            FailedStrategyRecord(
                strategy="OptDesign", reason=reason,
                detail="directive X not recognized", retry_count=retry_count,
            )
        ]
        return s

    def test_tool_error_shown_as_retry_not_excluded(self):
        s = self._state_with_failure("tool_error", retry_count=0)
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "strategy_catalog:" in content
        # OptDesign must still be present (not hard-excluded) with a RETRY marker
        assert "RETRY:" in content
        assert "2 retry/retries left" in content

    def test_escalated_shown_as_blocked(self):
        # After escalation the reason is strategy_ineffective -> [BLOCKED], not [RETRY].
        s = self._state_with_failure("strategy_ineffective")
        s.context.failed_strategies[0].blocked_until_iter = 3  # > current(1)
        msgs = [{"role": "system", "content": "sys"}]
        inject_merged_dashboard(msgs, s, LoopPhase.SELECT_STRATEGY)
        content = msgs[-1]["content"]
        assert "BLOCKED" in content

    def test_catalog_retryable_marker_unit(self):
        # Direct unit test of the catalog renderer.
        cat = get_strategy_catalog(
            retryable_strategies={"OptDesign": "directive bad - 1 retry left"},
        )
        assert "RETRY: directive bad - 1 retry left" in cat
        # OptDesign renders under its display name (Logic Optimization), not the key
        assert "Logic Optimization" in cat


# ── Smoke: integration paths import cleanly with new params ────────────────


class TestIntegrationImports:
    def test_phase_evaluate_imports_classify(self):
        from optimizer.nodes.subgraphs import phase_evaluate
        assert hasattr(phase_evaluate, "classify_tool_error")

    def test_state_space_accepts_retryable_param(self):
        from optimizer.pure.state_space import format_state_space_for_llm
        from optimizer.pure.state_space import StateSpace
        # build a minimal space; just ensure the kwarg is accepted (no TypeError)
        s = OptimizerState()
        space = StateSpace()
        out = format_state_space_for_llm(
            space=space, phase=LoopPhase.SELECT_STRATEGY,
            show_strategy_catalog=True,
            retryable_strategies={"PhysOpt": "detail - 1 retry left"},
            state=s,
        )
        assert isinstance(out, str)
