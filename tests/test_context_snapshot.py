"""Tests for optimizer/pure/context_snapshot.py — inject functions + dashboard builder."""
from __future__ import annotations

import pytest

from optimizer.state import OptimizerState
from optimizer.pure.context_snapshot import (
    inject_context_snapshot,
    inject_context_snapshot_at_end,
    inject_merged_dashboard,
)
from optimizer.pure.tool_filter import LoopPhase


# ── inject_context_snapshot_at_end: Message list operations ─────────────

class TestInjectAtEnd:
    def test_appends_to_empty_list(self):
        messages = []
        inject_context_snapshot_at_end(messages, "---snapshot---")
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "---snapshot---"

    def test_replaces_existing_dashboard(self):
        messages = [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "[ANALYZE — Context & Dashboard]\nsome old data\n--- End Dashboard ---"},
            {"role": "assistant", "content": "ok"},
        ]
        inject_context_snapshot_at_end(messages, "[ANALYZE — Context & Dashboard]\nfresh data\n--- End Dashboard ---")
        # Old dashboard removed, new one at end
        assert len(messages) == 3
        assert messages[-1]["content"].startswith("[ANALYZE — Context & Dashboard]")
        assert "fresh data" in messages[-1]["content"]

    def test_preserves_non_dashboard_user_messages(self):
        messages = [
            {"role": "user", "content": "normal question"},
            {"role": "assistant", "content": "normal answer"},
        ]
        inject_context_snapshot_at_end(messages, "dashboard data")
        assert messages[0]["content"] == "normal question"
        assert messages[-1]["content"] == "dashboard data"
        assert len(messages) == 3

    def test_idempotent_double_call(self):
        """Two consecutive calls should still produce exactly one dashboard."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        # First call: inject a proper dashboard
        inject_context_snapshot_at_end(
            messages,
            "[ANALYZE — Context & Dashboard]\ndata v1\n--- End Dashboard ---",
        )
        # Second call: should replace the existing one (content starts with known header)
        inject_context_snapshot_at_end(
            messages,
            "[ANALYZE — Context & Dashboard]\ndata v2\n--- End Dashboard ---",
        )
        dashboards = [m for m in messages if "--- End Dashboard ---" in m.get("content", "")]
        assert len(dashboards) == 1
        assert "data v2" in dashboards[0]["content"]

    def test_all_header_markers_recognized(self):
        """Each of the 4 phase headers should be recognized and replaced."""
        for header in [
            "[ANALYZE — Context & Dashboard]",
            "[SELECT_STRATEGY — Context & Dashboard]",
            "[EXECUTE — Context & Dashboard]",
            "[EVALUATE — Context & Dashboard]",
        ]:
            messages = [
                {"role": "user", "content": header + "\nold"},
                {"role": "user", "content": "keep me"},
            ]
            inject_context_snapshot_at_end(messages, "[NEW — Context & Dashboard]\nfresh")
            assert len(messages) == 2, f"Failed for header: {header}"
            assert messages[-1]["content"] == "[NEW — Context & Dashboard]\nfresh"
            assert messages[0]["content"] == "keep me"


# ── inject_context_snapshot (V1 compatibility) ─────────────────────────

class TestInjectV1:
    def test_inserts_after_system_messages(self):
        messages = [
            {"role": "system", "content": "sys1"},
            {"role": "system", "content": "sys2"},
            {"role": "user", "content": "hello"},
        ]
        inject_context_snapshot(messages, "snapshot_data")
        # After all system messages, before first user
        assert len(messages) == 4
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == "snapshot_data"

    def test_no_system_message_goes_to_front(self):
        messages = [{"role": "user", "content": "hello"}]
        inject_context_snapshot(messages, "snapshot_data")
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "snapshot_data"

    def test_replaces_old_dashboard(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[ANALYZE — Context & Dashboard]\nold"},
            {"role": "user", "content": "keep"},
        ]
        inject_context_snapshot(messages, "new_snapshot")
        assert len(messages) == 3  # old removed, new inserted
        assert messages[0]["role"] == "system"
        snapshot_idx = 1
        assert messages[snapshot_idx]["content"] == "new_snapshot"
        assert messages[2]["content"] == "keep"


# ── inject_merged_dashboard: Integration entry point ────────────────────

class TestInjectMergedDashboard:
    def test_injects_dashboard_as_last_message(self):
        """For each phase, the dashboard is the last user message."""
        for phase in LoopPhase:
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "previous context"},
            ]
            state = OptimizerState()
            state.timing.latest_wns = -0.5
            inject_merged_dashboard(messages, state, phase)
            # inject_merged_dashboard now also injects a FORMAT_GUARD system
            # message, so the count is 4: sys, guard, previous, dashboard.
            last = messages[-1]
            assert last["role"] == "user"
            phase_label = phase.value.upper()
            assert f"[{phase_label} — Context & Dashboard]" in last["content"]
            assert "--- End Dashboard ---" in last["content"]

    def test_dashboard_content_updates_with_state(self):
        """Changing state fields should be reflected in the dashboard."""
        messages = [{"role": "system", "content": "sys"}]
        state = OptimizerState()
        state.timing.latest_wns = -0.5
        inject_merged_dashboard(messages, state, LoopPhase.ANALYZE)
        assert "wns_setup: -0.500" in messages[-1]["content"]

        # Change state and re-inject
        state.timing.latest_wns = 0.0
        inject_merged_dashboard(messages, state, LoopPhase.ANALYZE)
        assert "wns_setup: 0.000" in messages[-1]["content"]
        assert "wns_setup: -0.500" not in messages[-1]["content"]

    def test_handoff_text_merged(self):
        messages = [{"role": "system", "content": "sys"}]
        state = OptimizerState()
        state.strategy.last_handoff_text = "[phase handoff] prev_strategy: PBLOCK"
        inject_merged_dashboard(messages, state, LoopPhase.ANALYZE)
        assert "prev_strategy: PBLOCK" in messages[-1]["content"]

    def test_strategy_catalog_only_in_select_strategy(self):
        for phase in LoopPhase:
            messages = [{"role": "system", "content": "sys"}]
            state = OptimizerState()
            state.strategy.current_strategy = "PhysOpt"
            inject_merged_dashboard(messages, state, phase)
            content = messages[-1]["content"]
            if phase == LoopPhase.SELECT_STRATEGY:
                assert "strategy_catalog:" in content
            else:
                assert "strategy_catalog:" not in content


# ── Edge cases ──────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_wns_none_does_not_crash(self):
        messages = [{"role": "system", "content": "sys"}]
        state = OptimizerState()
        state.timing.latest_wns = None
        inject_merged_dashboard(messages, state, LoopPhase.ANALYZE)
        assert "N/A" in messages[-1]["content"]

    def test_empty_critical_paths_still_produces_output(self):
        messages = [{"role": "system", "content": "sys"}]
        state = OptimizerState()
        state.timing.critical_paths = []
        state.timing.latest_wns = -0.1
        inject_merged_dashboard(messages, state, LoopPhase.ANALYZE)
        last = messages[-1]["content"]
        assert "wns_setup: -0.100" in last
        assert "--- End Dashboard ---" in last

    def test_clock_period_zero_no_crash(self):
        messages = [{"role": "system", "content": "sys"}]
        state = OptimizerState()
        state.timing.clock_period = 0
        inject_merged_dashboard(messages, state, LoopPhase.ANALYZE)
        assert messages[-1]["content"]

    def test_multiple_iterations_no_accumulation(self):
        """Simulate 5 consecutive LLM calls — only 1 dashboard should exist."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "start"},
        ]
        state = OptimizerState()
        state.timing.latest_wns = -0.5

        for wns in [-0.5, -0.4, -0.3, -0.2, -0.1]:
            state.timing.latest_wns = wns
            inject_merged_dashboard(messages, state, LoopPhase.ANALYZE)

        dashboards = [m for m in messages if "--- End Dashboard ---" in m.get("content", "")]
        assert len(dashboards) == 1
        assert f"wns_setup: -0.100" in dashboards[0]["content"]

    def test_empty_api_messages(self):
        state = OptimizerState()
        state.timing.latest_wns = -0.5
        inject_merged_dashboard([], state, LoopPhase.ANALYZE)
        # Should not crash; check via direct call
        messages = []
        inject_context_snapshot_at_end(messages, "test")
        assert len(messages) == 1

    def test_failed_strategy_shown_with_marker_not_excluded(self):
        """P0 ②C / P2 ②D: failed strategies are NOT excluded from the catalog -
        they appear in the available list with a marker ([RETRY]/[PRIOR FAIL]/
        [BLOCKED]) so the LLM can see what failed and retry/avoid accordingly.
        A default 'unknown' reason is retriable -> [RETRY]."""
        messages = [{"role": "system", "content": "sys"}]
        state = OptimizerState()
        from optimizer.state import FailedStrategyRecord
        state.context.failed_strategies = [
            FailedStrategyRecord(strategy="PBLOCK", reason="unknown"),
        ]
        inject_merged_dashboard(messages, state, LoopPhase.SELECT_STRATEGY)
        content = messages[-1]["content"]
        assert "strategy_catalog:" in content
        # PBLOCK stays in the available list with a [RETRY] marker (not excluded)
        assert "PBLOCK-Based Re-placement" in content
        assert "RETRY:" in content
