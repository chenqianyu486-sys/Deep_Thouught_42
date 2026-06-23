"""Shared pytest fixtures and configuration for the FPL26 optimization contest."""

from __future__ import annotations

from typing import Any

import pytest

from optimizer.state import (
    CriticalPathEntry,
    OptimizerState,
    StateSpace,
)
from optimizer.pure.state_space import build_state_space


# ── OptimizerState fixtures ─────────────────────────────────────────────


@pytest.fixture
def empty_state() -> OptimizerState:
    """An OptimizerState with all default values."""
    return OptimizerState()


@pytest.fixture
def basic_state() -> OptimizerState:
    """An OptimizerState with basic timing data populated."""
    state = OptimizerState()
    state.timing.latest_wns = -0.5
    state.timing.latest_tns = -12.34
    state.timing.clock_period = 5.0
    state.iteration.current = 1
    return state


@pytest.fixture
def sample_state_space(basic_state: OptimizerState) -> StateSpace:
    """A realistic StateSpace for format/output tests."""
    basic_state.timing.critical_paths = [
        CriticalPathEntry(
            cells=["u_core/u_alu/reg_0"],
            slack=-0.523, logic_delay=0.4, net_delay=0.6, levels=12,
        ),
    ]
    basic_state.timing.congestion_data = {"global_score": 0.65}
    basic_state.timing.resource_utilization = {"LUT": 20000, "FF": 10000}
    basic_state.timing.device_capacity = {"LUT": 50000, "FF": 20000}
    basic_state.strategy.evaluation_wns_delta = 0.077
    basic_state.strategy.evaluation_result = "IMPROVED"
    basic_state.strategy.current_strategy = "PhysOpt"
    return build_state_space(basic_state)


# ── Helpers ─────────────────────────────────────────────────────────────


@pytest.fixture
def make_critical_paths() -> Any:
    """Factory fixture: returns a function that creates N CriticalPathEntries."""

    def _make(n: int, slack: float = -0.5) -> list[CriticalPathEntry]:
        return [
            CriticalPathEntry(
                cells=[f"u_cell_{i}"],
                path_length=1, slack=slack,
                logic_delay=0.3, net_delay=0.2, levels=5,
            )
            for i in range(n)
        ]

    return _make
