"""Shared phase and outer-loop exit policy contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhaseExitContract:
    should_exit: bool = False
    event: str | None = None
    flow_signal: str | None = None
    done_reason: str | None = None
    record_reason: str | None = None
    set_is_done: bool = False


def build_phase_exit_contract(
    *,
    round_count: int | None = None,
    max_rounds: int | None = None,
    start_time: float | None = None,
    wall_clock_timeout: float | None = None,
    now: float | None = None,
    user_exit_requested: bool = False,
    total_cost: float | None = None,
    cost_hard_limit: float | None = None,
    consecutive_empty_responses: int | None = None,
    empty_response_limit: int | None = None,
    no_progress_count: int | None = None,
    no_progress_limit: int | None = None,
) -> PhaseExitContract:
    """Return the first matching exit contract for a phase or outer loop."""
    if (
        round_count is not None
        and max_rounds is not None
        and round_count > max_rounds
    ):
        return PhaseExitContract(
            should_exit=True,
            event="max_rounds",
            flow_signal="SYSTEM_EXIT",
            record_reason="max_rounds",
        )

    if (
        start_time is not None
        and wall_clock_timeout is not None
        and now is not None
        and now - start_time > wall_clock_timeout
    ):
        return PhaseExitContract(
            should_exit=True,
            event="wall_clock_timeout",
            flow_signal="SYSTEM_EXIT",
            done_reason="wall_clock_timeout",
            record_reason="wall_clock_timeout",
            set_is_done=True,
        )

    if user_exit_requested:
        return PhaseExitContract(
            should_exit=True,
            event="user_requested",
            flow_signal="SYSTEM_EXIT",
            record_reason="user_requested",
        )

    if (
        total_cost is not None
        and cost_hard_limit is not None
        and total_cost >= cost_hard_limit
    ):
        return PhaseExitContract(
            should_exit=True,
            event="cost_limit",
            flow_signal="SYSTEM_EXIT",
            done_reason="cost_limit",
            record_reason="cost_limit",
            set_is_done=True,
        )

    if (
        consecutive_empty_responses is not None
        and empty_response_limit is not None
        and consecutive_empty_responses >= empty_response_limit
    ):
        return PhaseExitContract(
            should_exit=True,
            event="empty_responses",
            flow_signal="SYSTEM_EXIT",
            record_reason="empty_responses",
        )

    if (
        no_progress_count is not None
        and no_progress_limit is not None
        and no_progress_count >= no_progress_limit
    ):
        return PhaseExitContract(
            should_exit=True,
            event="no_progress",
            flow_signal="SYSTEM_EXIT",
            record_reason="no_progress",
        )

    return PhaseExitContract()
