# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Skill telemetry for observability.

Provides logging, metrics, and execution tracking for skills.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ExecutionStatus(Enum):
    """Skill execution status."""
    SUCCESS = "success"
    FAILURE = "failure"
    VALIDATION_ERROR = "validation_error"
    SKIPPED = "skipped"


@dataclass
class SkillExecutionRecord:
    """Record of a single skill execution."""
    skill_name: str
    timestamp: datetime
    duration_ms: float
    status: ExecutionStatus
    error: Optional[str] = None
    error_code: str = ""  # Canonical error code from SkillErrorCode
    params_summary: str = ""  # Sanitized params for logging
    wns: Optional[float] = None  # WNS value at time of execution (optimizer context)
    iteration: int = 0  # Optimizer iteration number
    extra: dict = field(default_factory=dict)  # Extensible metadata

    def to_dict(self) -> dict:
        d = {
            "skill_name": self.skill_name,
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status.value,
            "error": self.error,
            "error_code": self.error_code,
            "params_summary": self.params_summary,
            "wns": self.wns,
            "iteration": self.iteration,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class SkillMetrics:
    """Aggregated metrics for a skill."""
    skill_name: str
    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    validation_error_count: int = 0
    skipped_count: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: float = float('inf')
    max_duration_ms: float = 0.0
    last_execution: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_code: str = ""

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.success_count / self.total_calls

    @property
    def avg_duration_ms(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_duration_ms / self.total_calls

    def record(self, record: SkillExecutionRecord) -> None:
        """Update metrics with a new execution record."""
        self.total_calls += 1
        self.total_duration_ms += record.duration_ms
        self.min_duration_ms = min(self.min_duration_ms, record.duration_ms)
        self.max_duration_ms = max(self.max_duration_ms, record.duration_ms)
        self.last_execution = record.timestamp

        if record.status == ExecutionStatus.SUCCESS:
            self.success_count += 1
        elif record.status == ExecutionStatus.FAILURE:
            self.failure_count += 1
            self.last_error = record.error
            if record.error_code:
                self.last_error_code = record.error_code
        elif record.status == ExecutionStatus.VALIDATION_ERROR:
            self.validation_error_count += 1
        elif record.status == ExecutionStatus.SKIPPED:
            self.skipped_count += 1

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "total_calls": self.total_calls,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "validation_error_count": self.validation_error_count,
            "skipped_count": self.skipped_count,
            "success_rate": round(self.success_rate, 4),
            "total_duration_ms": round(self.total_duration_ms, 2),
            "avg_duration_ms": round(self.avg_duration_ms, 2),
            "min_duration_ms": round(self.min_duration_ms, 2) if self.min_duration_ms != float('inf') else 0,
            "max_duration_ms": round(self.max_duration_ms, 2),
            "last_execution": self.last_execution.isoformat() if self.last_execution else None,
            "last_error": self.last_error,
            "last_error_code": self.last_error_code
        }


class SkillTelemetry:
    """Centralized telemetry collection for skills.

    Records execution logs, metrics, and provides querying capabilities.
    """
    _records: list[SkillExecutionRecord] = []
    _metrics: dict[str, SkillMetrics] = {}
    _max_records: int = 1000  # Keep last 1000 records

    @classmethod
    def reset(cls) -> None:
        """Reset all telemetry data. For testing."""
        cls._records.clear()
        cls._metrics.clear()

    @classmethod
    def record_execution(
        cls,
        skill_name: str,
        duration_ms: float,
        status: ExecutionStatus,
        error: Optional[str] = None,
        error_code: str = "",
        params_summary: str = ""
    ) -> SkillExecutionRecord:
        """Record a skill execution.

        Args:
            skill_name: Name of the skill
            duration_ms: Execution duration in milliseconds
            status: Execution status
            error: Error message if failed
            error_code: Canonical error code from SkillErrorCode
            params_summary: Sanitized parameter summary for logging

        Returns:
            The created execution record
        """
        record = SkillExecutionRecord(
            skill_name=skill_name,
            timestamp=datetime.now(),
            duration_ms=duration_ms,
            status=status,
            error=error,
            error_code=error_code,
            params_summary=params_summary
        )

        # Store record (with limit)
        cls._records.append(record)
        if len(cls._records) > cls._max_records:
            cls._records.pop(0)

        # Update metrics
        if skill_name not in cls._metrics:
            cls._metrics[skill_name] = SkillMetrics(skill_name=skill_name)
        cls._metrics[skill_name].record(record)

        # Log the execution
        cls._log_execution(record)

        return record

    @classmethod
    def _log_execution(cls, record: SkillExecutionRecord) -> None:
        """Log skill execution."""
        if record.status == ExecutionStatus.SUCCESS:
            logger.debug(
                f"[Skill] {record.skill_name} executed successfully "
                f"in {record.duration_ms:.2f}ms"
            )
        elif record.status == ExecutionStatus.FAILURE:
            logger.warning(
                f"[Skill] {record.skill_name} failed after {record.duration_ms:.2f}ms: {record.error}"
            )
        elif record.status == ExecutionStatus.VALIDATION_ERROR:
            logger.info(
                f"[Skill] {record.skill_name} validation error: {record.error}"
            )
        elif record.status == ExecutionStatus.SKIPPED:
            logger.info(
                f"[Skill] {record.skill_name} skipped: {record.error}"
            )

    @classmethod
    def get_metrics(cls, skill_name: str) -> Optional[dict]:
        """Get metrics for a specific skill.

        Args:
            skill_name: Name of the skill

        Returns:
            Metrics dict or None if skill not found
        """
        if skill_name not in cls._metrics:
            return None
        return cls._metrics[skill_name].to_dict()

    @classmethod
    def get_all_metrics(cls) -> dict[str, dict]:
        """Get metrics for all skills.

        Returns:
            Dict mapping skill names to their metrics
        """
        return {name: m.to_dict() for name, m in cls._metrics.items()}

    @classmethod
    def get_recent_executions(cls, limit: int = 10, skill_name: Optional[str] = None) -> list[dict]:
        """Get recent execution records.

        Args:
            limit: Maximum number of records to return
            skill_name: Optional filter by skill name

        Returns:
            List of execution record dicts, newest first
        """
        records = cls._records
        if skill_name:
            records = [r for r in records if r.skill_name == skill_name]
        return [r.to_dict() for r in records[-limit:]][::-1]

    @classmethod
    def get_execution_summary(cls) -> dict:
        """Get a summary of all executions.

        Returns:
            Summary dict with totals and recent status
        """
        total_calls = sum(m.total_calls for m in cls._metrics.values())
        total_failures = sum(m.failure_count for m in cls._metrics.values())
        total_success = sum(m.success_count for m in cls._metrics.values())

        return {
            "total_calls": total_calls,
            "total_success": total_success,
            "total_failures": total_failures,
            "skills_tracked": len(cls._metrics),
            "recent_executions": len(cls._records)
        }


    @classmethod
    def export_to_json(cls, filepath: Optional[str] = None) -> dict:
        """Export all telemetry to a JSON-serializable dict.

        Args:
            filepath: Optional file path to write JSON output.

        Returns:
            Dict with summary, metrics, and recent executions.
        """
        import json
        data = {
            "export_timestamp": datetime.now().isoformat(),
            "summary": cls.get_execution_summary(),
            "metrics": cls.get_all_metrics(),
            "recent_executions": [r.to_dict() for r in cls._records],
        }
        if filepath:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2, default=str)
        return data

    @classmethod
    def clear_older_than(cls, hours: float = 24) -> int:
        """Remove records older than the specified number of hours.

        Args:
            hours: Age threshold in hours.

        Returns:
            Number of records removed.
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        before = len(cls._records)
        cls._records = [r for r in cls._records if r.timestamp >= cutoff]
        # Rebuild metrics from remaining records
        cls._metrics.clear()
        for r in cls._records:
            if r.skill_name not in cls._metrics:
                cls._metrics[r.skill_name] = SkillMetrics(skill_name=r.skill_name)
            cls._metrics[r.skill_name].record(r)
        return before - len(cls._records)


def sanitize_params_for_logging(params: dict, max_length: int = 100) -> str:
    """Sanitize parameters for logging (remove sensitive data).

    Args:
        params: Parameter dict
        max_length: Maximum length per value

    Returns:
        Sanitized summary string
    """
    if not params:
        return "{}"

    parts = []
    for key, value in params.items():
        # Truncate long values
        str_value = str(value)
        if len(str_value) > max_length:
            str_value = str_value[:max_length] + "..."
        # Simple sanitization - just truncate, don't remove fields
        parts.append(f"{key}={str_value}")

    return "{" + ", ".join(parts) + "}"


class SkillExecutionTimer:
    """Context manager for timing skill execution."""

    def __init__(self):
        self.start_time: float = 0
        self.duration_ms: float = 0

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.duration_ms = (time.perf_counter() - self.start_time) * 1000
        return False
