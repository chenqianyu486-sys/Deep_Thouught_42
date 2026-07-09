"""Layer 1 Unit Regression Tests: handoff trajectory attribution.

Verifies _format_trajectory_brief attributes an iteration's net WNS gain to
the strategies that actually produced it (via optimization_history), not to
the last-selected (possibly ineffective) strategy (via narrative label).
This is the problem-1 fix.
"""

import pytest
from optimizer.pure.handoff import _format_trajectory_brief
from optimizer.state import OptimizationAppliedRecord


def _narrative(iteration, strategy_label, wns_before, wns_after, outcome):
    """Build a narrative dict matching build_iteration_narrative output."""
    return {
        "iteration": iteration,
        "strategy_label": strategy_label,
        "wns_before": wns_before,
        "wns_after": wns_after,
        "wns_delta": (wns_after - wns_before) if wns_before is not None and wns_after is not None else None,
        "outcome": outcome,
    }


def _rec(strategy, wns_before, wns_after, iteration):
    """Build an OptimizationAppliedRecord (only added when best WNS improved)."""
    return OptimizationAppliedRecord(
        strategy=strategy,
        wns_before=wns_before,
        wns_after=wns_after,
        iteration=iteration,
    )


class TestTrajectoryAttribution:
    """Verify per-strategy attribution via optimization_history."""

    def test_effective_strategies_attributed_not_last_selected(self):
        """Iteration gain must be split across effective strategies, not the
        last-selected (ineffective) strategy.

        Scenario: iter 1 ran PBLOCK(+0.436) then PhysOpt(+0.077), both
        effective; LUTMUXFRepack was selected last but produced no gain.
        narrative.strategy_label = "LUTMUXFRepack" (the bug source).
        """
        narratives = [_narrative(1, "LUTMUXFRepack", -0.978, -0.465, "improved")]
        history = [
            _rec("PBLOCK", -0.978, -0.542, 1),   # +0.436
            _rec("PhysOpt", -0.542, -0.465, 1),  # +0.077
        ]
        out = _format_trajectory_brief(narratives, history, max_entries=10)
        assert "PBLOCK(+0.436)" in out
        assert "PhysOpt(+0.077)" in out
        # The ineffective last-selected strategy must NOT be credited.
        assert "LUTMUXFRepack" not in out
        # Iteration-level WNS span is still shown.
        assert "WNS -0.978->-0.465" in out
        assert "improved" in out

    def test_no_effective_strategy_falls_back_to_narrative(self):
        """An iteration where no strategy improved best WNS (so no
        optimization_history record) falls back to the narrative label to
        preserve "tried X, no gain" visibility."""
        narratives = [_narrative(2, "LUTMUXFRepack", -0.465, -0.465, "unchanged")]
        out = _format_trajectory_brief(narratives, [], max_entries=10)
        assert "LUTMUXFRepack" in out
        assert "WNS -0.465->-0.465" in out
        assert "unchanged" in out

    def test_mixed_iterations(self):
        """Iter with effective strategies uses per-strategy; iter without
        falls back to narrative."""
        narratives = [
            _narrative(1, "LUTMUXFRepack", -0.978, -0.465, "improved"),
            _narrative(2, "LUTMUXFRepack", -0.465, -0.465, "unchanged"),
        ]
        history = [
            _rec("PBLOCK", -0.978, -0.542, 1),
            _rec("PhysOpt", -0.542, -0.465, 1),
        ]
        out = _format_trajectory_brief(narratives, history, max_entries=10)
        lines = out.splitlines()
        # Iter 1: per-strategy attribution, no LUTMUXFRepack.
        assert any("Iter 1:" in ln and "PBLOCK(+0.436)" in ln and "PhysOpt(+0.077)" in ln for ln in lines)
        assert not any("Iter 1:" in ln and "LUTMUXFRepack" in ln for ln in lines)
        # Iter 2: fallback narrative label.
        assert any("Iter 2:" in ln and "LUTMUXFRepack" in ln for ln in lines)

    def test_empty_narratives_returns_no_history(self):
        assert _format_trajectory_brief([], None) == "(no history)"

    def test_no_optimization_history_falls_back_for_all(self):
        """When optimization_history is None, every iteration falls back to
        the narrative label (legacy behavior)."""
        narratives = [_narrative(1, "LUTMUXFRepack", -0.978, -0.465, "improved")]
        out = _format_trajectory_brief(narratives, None, max_entries=10)
        assert "LUTMUXFRepack" in out
        # No per-strategy parts rendered.
        assert "(" not in out.split("Iter 1:")[1].split("|")[0]
