# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Tracing and observability for Skill execution.

Emits OpenTelemetry-compatible span attributes as structured log events.
The existing contextvars-based trace_id system is used for context propagation.

In a production environment, replace the logging-based emission with
a real OpenTelemetry SDK (otlp exporter, etc).
"""

import logging
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class SkillTraceAttributes:
    """Span attributes per Skill Descriptor v3 tracing specification.

    These MUST be emitted on every skill invocation for observability.
    """
    skill_id: str = ""
    call_id: str = ""
    idempotency_key: str = ""
    outcome: str = ""       # success / error / timeout
    latency_ms: float = 0.0
    cache_hit: bool = False

    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "skill.id": self.skill_id,
            "skill.call_id": self.call_id,
            "skill.outcome": self.outcome,
            "skill.latency_ms": round(self.latency_ms, 2),
            "skill.cache_hit": self.cache_hit,
        }
        if self.idempotency_key:
            d["skill.idempotency_key"] = self.idempotency_key
        d.update(self.extra)
        return d

    def emit(self) -> None:
        """Emit the trace attributes as a structured log event.

        In production, this would also push to OpenTelemetry spans.
        """
        logger.info(
            f"[SKILL_TRACE] skill_id={self.skill_id} call_id={self.call_id} "
            f"outcome={self.outcome} latency_ms={self.latency_ms:.1f}",
            extra={"skill_trace": self.to_dict()},
        )

    @classmethod
    def from_execution(cls, skill_id: str, call_id: str = "",
                       idempotency_key: str = "", outcome: str = "success",
                       latency_ms: float = 0.0, cache_hit: bool = False,
                       **extra) -> "SkillTraceAttributes":
        return cls(
            skill_id=skill_id,
            call_id=call_id,
            idempotency_key=idempotency_key,
            outcome=outcome,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            extra=extra,
        )
