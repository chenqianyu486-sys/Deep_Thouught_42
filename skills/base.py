# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Skill base classes and data structures.

This module defines the core abstractions for the Skills framework:
SkillCategory, ParameterSpec, SkillMetadata, SkillResult, SkillError,
and the abstract Skill base class with telemetry support.
"""

from abc import ABC, abstractmethod
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from skills.errors import SkillError, SkillErrorCode, ERROR_METADATA
from skills.telemetry import (
    SkillTelemetry,
    SkillExecutionTimer,
    ExecutionStatus,
    sanitize_params_for_logging,
)


# ── Category ────────────────────────────────────────────────────


class SkillCategory(Enum):
    """Categories for classifying skills."""
    ANALYSIS = "analysis"
    OPTIMIZATION = "optimization"
    PLACEMENT = "placement"
    ROUTING = "routing"
    SEARCH = "search"


# ── Parameter Spec ──────────────────────────────────────────────

_TYPE_TO_JSON: dict = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _python_type_to_json_schema(tp: type) -> str:
    """Map a Python type annotation to a JSON Schema type string."""
    # Handle Optional[X] / Union[X, None]
    origin = getattr(tp, "__origin__", None)
    args = getattr(tp, "__args__", ())
    if origin is type(Optional[str]) and type(None) in args:
        for a in args:
            if a is not type(None):
                return _python_type_to_json_schema(a)
    return _TYPE_TO_JSON.get(tp, "string")


@dataclass
class ParameterSpec:
    """Specification for a skill parameter.

    name: Parameter identifier matching the @skill decorator and execute() kwargs.
    type: Python type annotation (int, str, float, list, etc.).
    description: Semantic description including canonicalization rules.
    default: Default value. Omitted (None) means required.
    """
    name: str
    type: type
    description: str
    default: Any = None

    def to_json_schema_property(self) -> dict:
        """Convert this ParameterSpec to a JSON Schema Draft 2020-12 property dict."""
        prop: dict = {"type": _python_type_to_json_schema(self.type)}
        prop["description"] = self.description
        if self.default is not None:
            prop["default"] = self.default
        if self.type is str:
            prop["minLength"] = 1
        elif self.type is int or self.type is float:
            if self.default is None:
                pass  # No bound inference without domain knowledge
        return prop

    def is_required(self) -> bool:
        return self.default is None


# ── Metadata ────────────────────────────────────────────────────


@dataclass
class SkillMetadata:
    """Metadata describing a skill for discovery, introspection, and descriptor export.

    All fields map to the Skill Descriptor v3 specification.
    """
    # Identity
    name: str
    id: str = ""                  # Auto-generated: {namespace}.{name}@{version}
    version: str = "1.0.0"        # Semantic version (MAJOR.MINOR.PATCH)
    namespace: str = ""            # e.g. "analysis", "placement"
    display_name: str = ""         # Human-readable name

    # Description
    description: str = ""
    category: SkillCategory = SkillCategory.ANALYSIS
    spec_version: str = "3.0"

    # Contract
    idempotency: str = "safe"     # safe | idempotent | non-idempotent
    side_effects: list[str] = field(default_factory=list)
    timeout_ms: int = 30000

    # Parameters / output
    parameters: list[ParameterSpec] = field(default_factory=list)
    required_context: list[str] = field(default_factory=list)
    output_schema: dict = field(default_factory=dict)

    # Auth & errors
    authentication: dict = field(default_factory=lambda: {"type": "none"})
    error_codes: list[str] = field(default_factory=lambda: [
        "INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT",
    ])

    def __post_init__(self):
        if not self.id:
            self.id = self._generate_id()
        if not self.display_name:
            self.display_name = self.name.replace("_", " ").title()

    def _generate_id(self) -> str:
        ns = self.namespace or self.category.value
        return f"{ns}.{self.name}@{self.version}"

    def to_json_schema(self) -> dict:
        """Generate a JSON Schema Draft 2020-12 for parameters."""
        required = [p.name for p in self.parameters if p.is_required()]
        props = {}
        for p in self.parameters:
            props[p.name] = p.to_json_schema_property()
        schema: dict = {
            "type": "object",
            "additionalProperties": False,
            "properties": props,
        }
        if required:
            schema["required"] = required
        return schema

    def to_descriptor(self) -> dict:
        """Export this metadata as a full Skill Descriptor v3 dict.

        The output conforms to the canonical Skill Descriptor JSON schema.
        """
        from skills.errors import ERROR_METADATA as _err_meta

        def _error_entry(code: str) -> dict:
            meta = _err_meta.get(code, {})
            return {"code": code, "recoverable": meta.get("recoverable", False)}

        return {
            "$schema": "https://spec.example.com/skill-descriptor-v3.json",
            "specVersion": self.spec_version,
            "id": self.id,
            "displayName": self.display_name,
            "description": self.description,
            "idempotency": self.idempotency,
            "sideEffects": self.side_effects,
            "timeout": {
                "defaultMs": self.timeout_ms,
                "maxMs": self.timeout_ms * 2,
            },
            "authentication": self.authentication,
            "parameters": self.to_json_schema(),
            "returns": self.output_schema or {
                "type": "object",
                "additionalProperties": False,
            },
            "errors": [_error_entry(c) for c in self.error_codes],
        }


# ── Result ──────────────────────────────────────────────────────


@dataclass
class SkillResult:
    """Result returned from skill execution.

    success: Whether execution completed without error.
    data: Payload on success (Any JSON-serializable value).
    error: Human-readable error message on failure.
    error_code: Canonical error code from SkillErrorCode on failure.
    """
    success: bool
    data: Any = None
    error: Optional[str] = None
    error_code: str = ""


# ── Skill Base Class ────────────────────────────────────────────


class Skill(ABC):
    """Abstract base class for all skills.

    A skill is a self-contained unit of functionality that can be
    discovered, parameterized, and executed by an agent.
    """

    def get_metadata(self) -> SkillMetadata:
        """Return metadata describing this skill.

        Default implementation returns metadata injected by the @skill decorator.
        Subclasses only need to override if custom metadata logic is required.
        """
        if not hasattr(self, '_skill_metadata'):
            raise NotImplementedError(
                f"Skill '{type(self).__name__}' has no _skill_metadata. "
                "Use the @skill decorator or override get_metadata()."
            )
        return self._skill_metadata

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
        Handles idempotency checks, tracing, and error envelope generation.

        Args:
            context: SkillContext with design and tools
            **kwargs: Skill-specific parameters

        Returns:
            SkillResult from execute()
        """
        meta = self.get_metadata()
        skill_name = meta.name
        skill_id = meta.id
        call_id = context.call_id or ""
        idempotency_key = context.idempotency_key or ""
        params_summary = sanitize_params_for_logging(kwargs)

        # ── Idempotency check ──────────────────────────────────
        if idempotency_key and meta.idempotency in ("idempotent", "non-idempotent"):
            from skills.idempotency import IdempotencyStore
            if meta.idempotency == "idempotent" and IdempotencyStore.is_duplicate(idempotency_key):
                cached = IdempotencyStore.get_result(idempotency_key)
                if cached:
                    return SkillResult(success=True, data=cached)

        # ── Heartbeat ──────────────────────────────────────────
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
                        "skill_id": skill_id,
                        "call_id": call_id,
                        "heartbeat_elapsed": int(elapsed),
                        "heartbeat_count": _heartbeat_count[0],
                    }
                )
                _stop_heartbeat.wait(timeout=30.0)

        _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        _heartbeat_thread.start()

        # ── Execute with timeout enforcement ──────────────────
        import concurrent.futures as _cfutures

        _timeout_seconds = meta.timeout_ms / 1000.0
        _timed_out = False

        with SkillExecutionTimer() as timer:
            _executor = _cfutures.ThreadPoolExecutor(max_workers=1)
            try:
                _future = _executor.submit(self.execute, context, **kwargs)
                result = _future.result(timeout=_timeout_seconds)
            except _cfutures.TimeoutError:
                _timed_out = True
                result = SkillResult(
                    success=False,
                    data=None,
                    error=f"Skill '{skill_name}' timed out after {meta.timeout_ms}ms",
                    error_code=SkillErrorCode.SKILL_TIMEOUT,
                )
            finally:
                _executor.shutdown(wait=False)

        _stop_heartbeat.set()
        _heartbeat_thread.join(timeout=0.5)

        if _timed_out:
            _latency_ms = timer.duration_ms
            _logger.error(
                f"[SKILL_TIMEOUT] '{skill_name}' timed out after {_latency_ms:.0f}ms "
                f"(limit: {meta.timeout_ms}ms, heartbeats: {_heartbeat_count[0]})",
                extra={
                    "skill_name": skill_name,
                    "skill_id": skill_id,
                    "call_id": call_id,
                    "skill_duration_ms": round(_latency_ms, 2),
                    "heartbeat_count": _heartbeat_count[0],
                    "outcome": "timeout",
                    "timeout_ms": meta.timeout_ms,
                }
            )
            SkillTelemetry.record_execution(
                skill_name=skill_name,
                duration_ms=_latency_ms,
                status=ExecutionStatus.FAILURE,
                error=result.error,
                error_code=SkillErrorCode.SKILL_TIMEOUT,
                params_summary=params_summary,
            )
            return result

        latency_ms = timer.duration_ms
        outcome = "success" if result.success else "error"

        _logger.info(
            f"[SKILL_COMPLETE] '{skill_name}' completed in {latency_ms:.1f}ms "
            f"(heartbeats: {_heartbeat_count[0]})",
            extra={
                "skill_name": skill_name,
                "skill_id": skill_id,
                "call_id": call_id,
                "skill_duration_ms": round(latency_ms, 2),
                "heartbeat_count": _heartbeat_count[0],
                "outcome": outcome,
            }
        )

        # ── Record telemetry ───────────────────────────────────
        if result.success:
            status = ExecutionStatus.SUCCESS
        elif result.error_code:
            status = ExecutionStatus.FAILURE
        else:
            status = ExecutionStatus.FAILURE

        SkillTelemetry.record_execution(
            skill_name=skill_name,
            duration_ms=latency_ms,
            status=status,
            error=result.error,
            error_code=result.error_code,
            params_summary=params_summary,
        )

        # ── Cache idempotent result ────────────────────────────
        if idempotency_key and meta.idempotency == "idempotent" and result.success:
            from skills.idempotency import IdempotencyStore
            IdempotencyStore.store(idempotency_key, result.data)

        return result
