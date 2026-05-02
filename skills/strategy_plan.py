# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Shared data structures for strategy skill execution plans.

Each strategy skill returns a StrategyPlan containing an ordered list of
StrategyStep entries that the caller can execute. Steps already performed
by the skill (RapidWright operations) are marked executed=True; remaining
steps (typically Vivado TCL commands) are marked executed=False.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StrategyStep:
    """A single step in a strategy execution plan."""
    step_name: str                     # e.g. "place_design", "optimize_fanout_batch"
    platform: str                      # "Vivado" or "RapidWright"
    params: Optional[dict] = None      # Parameters for the step
    description: str = ""              # Human-readable description
    executed: bool = False             # True if already performed by the skill
    expected_duration_seconds: int = 0 # Estimated runtime hint


@dataclass
class StrategyPlan:
    """Execution plan returned by a strategy skill."""
    strategy_name: str                              # "PBLOCK", "PhysOpt", "Fanout"
    status: str                                     # "ready" | "skipped" | "error"
    message: str                                    # Human-readable summary
    preconditions_satisfied: bool                   # Whether strategy is applicable
    analysis_summary: dict = field(default_factory=dict)  # Key analysis findings
    steps: list[StrategyStep] = field(default_factory=list)  # Ordered execution plan
    error_details: Optional[str] = None             # Error info if status == "error"
