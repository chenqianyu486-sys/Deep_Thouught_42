# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Skill base classes and data structures.
"""

from abc import ABC, abstractmethod
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from skills.telemetry import (
    SkillTelemetry,
    SkillExecutionTimer,
    ExecutionStatus,
    sanitize_params_for_logging,
)


class SkillCategory(Enum):
    """Categories for classifying skills."""
    ANALYSIS = "analysis"
    OPTIMIZATION = "optimization"
    PLACEMENT = "placement"
    ROUTING = "routing"


@dataclass
class ParameterSpec:
    """Specification for a skill parameter."""
    name: str
    type: type
    description: str
    default: Any = None


@dataclass
class SkillMetadata:
    """Metadata describing a skill for discovery and introspection."""
    name: str
    description: str
    category: SkillCategory
    parameters: list[ParameterSpec] = field(default_factory=list)
    required_context: list[str] = field(default_factory=list)
    output_schema: dict = field(default_factory=dict)


@dataclass
class SkillResult:
    """Result returned from skill execution."""
    success: bool
    data: Any = None
    error: Optional[str] = None


class Skill(ABC):
    """Abstract base class for all skills.

    A skill is a self-contained unit of functionality that can be
    discovered, parameterized, and executed by an agent.
    """

    @abstractmethod
    def get_metadata(self) -> SkillMetadata:
        """Return metadata describing this skill."""
        pass

    @abstractmethod
    def execute(self, context: "SkillContext", **kwargs) -> SkillResult:
        """Execute the skill with the given context and parameters."""
        pass

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        """Validate input parameters before execution.

        Returns:
            (is_valid, error_message)
        """
        return True, ""

    def execute_with_telemetry(self, context: "SkillContext", **kwargs) -> SkillResult:
        """Execute with telemetry instrumentation.

        Records execution time, status, and parameters to SkillTelemetry.
        Override this method to add custom instrumentation logic.

        Args:
            context: SkillContext with design and tools
            **kwargs: Skill-specific parameters

        Returns:
            SkillResult from execute()
        """
        skill_name = self.get_metadata().name
        params_summary = sanitize_params_for_logging(kwargs)

        # Start heartbeat logger for long-running skills
        import threading
        import time as time_module

        _logger = logging.getLogger(__name__)
        _stop_heartbeat = threading.Event()
        _heartbeat_count = [0]
        _heartbeat_start = time_module.perf_counter()

        def _heartbeat_loop():
            while not _stop_heartbeat.is_set():
                elapsed = time_module.perf_counter() - _heartbeat_start
                _heartbeat_count[0] += 1
                _logger.info(
                    f"[SKILL_HEARTBEAT] Skill '{skill_name}' still running after {elapsed:.1f}s",
                    extra={
                        "skill_name": skill_name,
                        "heartbeat_elapsed": int(elapsed),
                        "heartbeat_count": _heartbeat_count[0]
                    }
                )
                _stop_heartbeat.wait(timeout=30.0)

        _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        _heartbeat_thread.start()

        with SkillExecutionTimer() as timer:
            result = self.execute(context, **kwargs)

        _stop_heartbeat.set()
        _heartbeat_thread.join(timeout=0.5)

        if result.success:
            status = ExecutionStatus.SUCCESS
        else:
            status = ExecutionStatus.FAILURE

        _logger.info(
            f"[SKILL_COMPLETE] '{skill_name}' completed in {timer.duration_ms:.1f}ms (heartbeats: {_heartbeat_count[0]})",
            extra={
                "skill_name": skill_name,
                "skill_duration_ms": round(timer.duration_ms, 2),
                "heartbeat_count": _heartbeat_count[0]
            }
        )

        SkillTelemetry.record_execution(
            skill_name=skill_name,
            duration_ms=timer.duration_ms,
            status=status,
            error=result.error,
            params_summary=params_summary
        )

        return result
