#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Integration tests for the skill framework.
Tests SkillRegistry, Skill classes, and @skill decorator.

Note: Skills are registered at import time via @skill decorator.
Tests should work with already-registered skills, not clear the registry.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import SkillRegistry, SkillContext, SkillCategory
from skills.base import Skill, SkillMetadata, SkillResult, ParameterSpec, SkillCategory as BaseSkillCategory
from skills.skill_decorator import skill


# Use a unique name for test skill to avoid collision
@skill(
    name="test_mock_skill",
    description="A mock skill for testing",
    category=BaseSkillCategory.ANALYSIS,
    parameters=[
        ParameterSpec("required_param", str, "A required parameter")
    ],
    required_context=["design"]
)
class MockSkillForTest(Skill):
    """Mock skill for testing."""

    def __init__(self):
        self.execute_called = False
        self.execute_args = None

    def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        self.execute_called = True
        self.execute_args = kwargs
        return SkillResult(success=True, data={"mock": "result"})

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "required_param" not in kwargs:
            return False, "required_param is required"
        return True, ""


def test_skill_registry_get():
    """Test skill retrieval."""
    skill = SkillRegistry.get("test_mock_skill")
    assert skill is not None, "Skill should be retrievable"
    print("  PASSED: get")


def test_skill_registry_get_registered_skill():
    """Test retrieving an already-registered skill."""
    # Skills are registered at import time
    skill = SkillRegistry.get("net_detour")
    assert skill is not None, "net_detour should be registered"
    skill2 = SkillRegistry.get("optimize_cell")
    assert skill2 is not None, "optimize_cell should be registered"
    print("  PASSED: get registered skills")


def test_skill_registry_duplicate_registration():
    """Test that duplicate registration raises ValueError."""
    # Get the already-registered test skill
    skill = SkillRegistry.get("test_mock_skill")

    try:
        SkillRegistry.register(skill)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "already registered" in str(e)
        print("  PASSED: duplicate registration raises error")


def test_skill_registry_list_all():
    """Test listing all registered skills."""
    all_skills = SkillRegistry.list_all()
    assert len(all_skills) >= 3, f"Expected at least 3 skills, got {len(all_skills)}"
    print(f"  PASSED: list all ({len(all_skills)} skills)")


def test_skill_registry_list_by_category():
    """Test filtering skills by category."""
    filtered = SkillRegistry.list_by_category(SkillCategory.ANALYSIS)
    # Should have at least test_mock_skill and net_detour
    assert len(filtered) >= 2, f"Expected at least 2 analysis skills, got {len(filtered)}"
    print(f"  PASSED: list by category ({len(filtered)} analysis skills)")


def test_skill_decorator_registration():
    """Test that @skill decorator registers the skill automatically."""
    skills = SkillRegistry.list_all()
    skill_names = [s.name for s in skills]

    assert "net_detour" in skill_names, "AnalyzeNetDetourSkill should be registered"
    assert "optimize_cell" in skill_names, "OptimizeCellPlacementSkill should be registered"
    assert "test_mock_skill" in skill_names, "MockSkillForTest should be registered"
    print("  PASSED: @skill decorator registers automatically")


def test_skill_metadata_from_decorator():
    """Test that metadata from decorator is correctly attached."""
    skill = SkillRegistry.get("net_detour")
    assert skill is not None

    meta = skill.get_metadata()
    assert meta.name == "net_detour"
    assert meta.category == SkillCategory.ANALYSIS
    assert len(meta.parameters) == 2
    assert "design" in meta.required_context
    print("  PASSED: metadata from decorator")


def test_skill_context():
    """Test SkillContext dataclass."""
    context = SkillContext(
        design="mock_design",
        device="mock_device",
        initialized=True,
        tools={"tool1": lambda x: x},
        trace_id="test-123"
    )

    assert context.design == "mock_design"
    assert context.device == "mock_device"
    assert context.initialized is True
    assert "tool1" in context.tools
    assert context.trace_id == "test-123"
    print("  PASSED: SkillContext")


def test_skill_execute():
    """Test skill execution."""
    skill = SkillRegistry.get("net_detour")
    assert skill is not None

    # execute with None design should return error result
    context = SkillContext(design=None, initialized=True)
    result = skill.execute(context, pin_paths=[], detour_threshold=2.0)

    assert result is not None
    assert isinstance(result, SkillResult)
    # With None design, should return empty results (not crash)
    print("  PASSED: skill execute")


def test_skill_validate_inputs():
    """Test skill input validation."""
    skill = SkillRegistry.get("net_detour")
    assert skill is not None

    # Valid inputs
    valid, msg = skill.validate_inputs(pin_paths=["a", "b"], detour_threshold=2.0)
    assert valid is True, f"Should be valid: {msg}"

    # Invalid: missing pin_paths
    valid, msg = skill.validate_inputs()
    assert valid is False, "Should be invalid without pin_paths"
    assert "required" in msg.lower()

    # Invalid: too few pin_paths
    valid, msg = skill.validate_inputs(pin_paths=["only_one"])
    assert valid is False, "Should be invalid with single pin_paths"

    print("  PASSED: skill validate_inputs")


def test_skill_result_dataclass():
    """Test SkillResult dataclass."""
    result1 = SkillResult(success=True, data={"key": "value"})
    assert result1.success is True
    assert result1.data == {"key": "value"}
    assert result1.error is None

    result2 = SkillResult(success=False, error="Something went wrong")
    assert result2.success is False
    assert result2.error == "Something went wrong"
    print("  PASSED: SkillResult dataclass")


def test_skill_metadata_dataclass():
    """Test SkillMetadata dataclass."""
    meta = SkillMetadata(
        name="test_skill",
        description="A test skill",
        category=SkillCategory.OPTIMIZATION,
        parameters=[
            ParameterSpec("param1", str, "A parameter", default="value")
        ],
        required_context=["design"],
        output_schema={"type": "object"}
    )

    assert meta.name == "test_skill"
    assert meta.category == SkillCategory.OPTIMIZATION
    assert len(meta.parameters) == 1
    assert meta.parameters[0].name == "param1"
    assert "design" in meta.required_context
    print("  PASSED: SkillMetadata dataclass")


def test_parameter_spec():
    """Test ParameterSpec dataclass."""
    param = ParameterSpec(
        name="test_param",
        type=int,
        description="A test parameter",
        default=42
    )

    assert param.name == "test_param"
    assert param.type == int
    assert param.default == 42
    print("  PASSED: ParameterSpec")


def test_skill_category_enum():
    """Test SkillCategory enum."""
    assert SkillCategory.ANALYSIS.value == "analysis"
    assert SkillCategory.OPTIMIZATION.value == "optimization"
    assert SkillCategory.PLACEMENT.value == "placement"
    assert SkillCategory.ROUTING.value == "routing"
    print("  PASSED: SkillCategory enum")


def test_skill_default_get_metadata():
    """Test that Skill base class provides a default get_metadata()."""
    skill = SkillRegistry.get("test_mock_skill")
    assert skill is not None

    # The default get_metadata() should return metadata injected by @skill
    meta = skill.get_metadata()
    assert meta.name == "test_mock_skill"
    assert meta.description == "A mock skill for testing"
    assert meta.category == SkillCategory.ANALYSIS
    assert len(meta.parameters) == 1
    assert meta.parameters[0].name == "required_param"
    print("  PASSED: default get_metadata")


def test_telemetry_record_execution():
    """Test SkillTelemetry recording."""
    from skills import SkillTelemetry, ExecutionStatus
    SkillTelemetry.reset()

    # Record a mock execution
    record = SkillTelemetry.record_execution(
        skill_name="test_skill",
        duration_ms=100.5,
        status=ExecutionStatus.SUCCESS,
        params_summary="{param1=value1}"
    )

    assert record.skill_name == "test_skill"
    assert record.duration_ms == 100.5
    assert record.status == ExecutionStatus.SUCCESS
    print("  PASSED: telemetry record execution")


def test_telemetry_metrics():
    """Test SkillTelemetry metrics aggregation."""
    from skills import SkillTelemetry, ExecutionStatus
    SkillTelemetry.reset()

    # Record multiple executions
    SkillTelemetry.record_execution("test_skill", 100.0, ExecutionStatus.SUCCESS)
    SkillTelemetry.record_execution("test_skill", 200.0, ExecutionStatus.SUCCESS)
    SkillTelemetry.record_execution("test_skill", 150.0, ExecutionStatus.FAILURE, error="test error")

    metrics = SkillTelemetry.get_metrics("test_skill")
    assert metrics is not None
    assert metrics["total_calls"] == 3
    assert metrics["success_count"] == 2
    assert metrics["failure_count"] == 1
    assert metrics["avg_duration_ms"] == 150.0
    assert metrics["last_error"] == "test error"
    print("  PASSED: telemetry metrics")


def test_telemetry_execute_with_telemetry():
    """Test execute_with_telemetry on a skill."""
    from skills import SkillTelemetry
    SkillTelemetry.reset()

    skill = SkillRegistry.get("net_detour")
    assert skill is not None

    context = SkillContext(design=None, initialized=True)
    result = skill.execute_with_telemetry(context, pin_paths=["a", "b"], detour_threshold=2.0)

    assert result is not None

    metrics = SkillTelemetry.get_metrics("net_detour")
    assert metrics is not None
    assert metrics["total_calls"] == 1
    print("  PASSED: execute_with_telemetry")


def test_telemetry_get_all_metrics():
    """Test getting all metrics."""
    from skills import SkillTelemetry, ExecutionStatus
    SkillTelemetry.reset()

    # Record something first
    SkillTelemetry.record_execution("net_detour", 50.0, ExecutionStatus.SUCCESS)

    all_metrics = SkillTelemetry.get_all_metrics()
    assert "net_detour" in all_metrics
    print("  PASSED: get all metrics")


def test_telemetry_get_recent_executions():
    """Test getting recent executions."""
    from skills import SkillTelemetry, ExecutionStatus
    SkillTelemetry.reset()

    # Record multiple
    SkillTelemetry.record_execution("test_skill", 10.0, ExecutionStatus.SUCCESS)
    SkillTelemetry.record_execution("test_skill", 20.0, ExecutionStatus.SUCCESS)

    recent = SkillTelemetry.get_recent_executions(limit=5)
    # Most recent is first (T2=20ms before T1=10ms)
    assert len(recent) == 2
    assert recent[0]["duration_ms"] == 20.0
    assert recent[1]["duration_ms"] == 10.0
    print("  PASSED: get recent executions")


def test_telemetry_execution_summary():
    """Test execution summary."""
    from skills import SkillTelemetry, ExecutionStatus
    SkillTelemetry.reset()

    SkillTelemetry.record_execution("skill1", 100.0, ExecutionStatus.SUCCESS)
    SkillTelemetry.record_execution("skill2", 100.0, ExecutionStatus.FAILURE, error="err")

    summary = SkillTelemetry.get_execution_summary()
    assert summary["total_calls"] == 2
    assert summary["total_success"] == 1
    assert summary["total_failures"] == 1
    assert summary["skills_tracked"] == 2
    print("  PASSED: execution summary")


def test_skill_descriptor_id_format():
    """Test that skill IDs follow {namespace}.{name}@version format."""
    for meta in SkillRegistry.list_all():
        if meta.name == "test_mock_skill":
            assert meta.id == "analysis.test_mock_skill@1.0.0"
            continue
        assert "@" in meta.id, f"ID '{meta.id}' missing version"
        parts = meta.id.split("@")
        assert len(parts) == 2
        assert parts[1].count(".") == 2  # MAJOR.MINOR.PATCH
    print("  PASSED: descriptor ID format")


def test_skill_to_json_schema():
    """Test that skills can generate valid JSON Schema for parameters."""
    for meta in SkillRegistry.list_all():
        schema = meta.to_json_schema()
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False
        if meta.parameters:
            assert "properties" in schema
            assert len(schema["properties"]) == len(meta.parameters)
    print("  PASSED: to_json_schema")


def test_skill_descriptor_generation():
    """Test that skills can generate the full descriptor dict."""
    for meta in SkillRegistry.list_all():
        desc = meta.to_descriptor()
        assert "$schema" in desc
        assert desc["specVersion"] == "3.0"
        assert desc["id"] == meta.id
        assert desc["displayName"]
        assert desc["description"]
        assert desc["idempotency"] in ("safe", "idempotent", "non-idempotent")
        assert isinstance(desc["sideEffects"], list)
        assert "defaultMs" in desc["timeout"]
        assert "maxMs" in desc["timeout"]
        assert desc["authentication"]["type"] == "none"
        assert desc["parameters"]["type"] == "object"
        assert isinstance(desc["errors"], list)
    print("  PASSED: descriptor generation")


def test_skill_error_code():
    """Test SkillError dataclass and error code metadata."""
    from skills.errors import SkillError, SkillErrorCode, ERROR_METADATA

    # Basic error creation
    err = SkillError.from_code(SkillErrorCode.INVALID_PARAMETER, message="test error")
    assert err.code == "INVALID_PARAMETER"
    assert not err.recoverable

    # Recoverable error
    err2 = SkillError.from_code(SkillErrorCode.TEMPORARILY_UNAVAILABLE)
    assert err2.recoverable

    # to_dict format matches spec
    d = err.to_dict()
    assert d["code"] == "INVALID_PARAMETER"
    assert d["message"] == "test error"

    # Error metadata is exhaustive
    expected_codes = {
        "INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "PERMISSION_DENIED",
        "QUOTA_EXCEEDED", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT",
        "CONCURRENT_MODIFICATION",
    }
    assert set(ERROR_METADATA.keys()) == expected_codes
    print("  PASSED: error code contract")


def test_skill_idempotency_store():
    """Test IdempotencyStore basic operations."""
    from skills.idempotency import IdempotencyStore
    IdempotencyStore.reset()

    assert not IdempotencyStore.is_duplicate("key1")
    IdempotencyStore.store("key1", {"result": "data"})
    assert IdempotencyStore.is_duplicate("key1")
    assert IdempotencyStore.get_result("key1") == {"result": "data"}
    print("  PASSED: idempotency store")


def test_skill_inflight_guard():
    """Test concurrent mutation guard."""
    from skills.idempotency import IdempotencyStore
    IdempotencyStore.reset()

    acquired = IdempotencyStore.set_inflight("resource_1", "key-001")
    assert acquired, "Should acquire lock"

    not_acquired = IdempotencyStore.set_inflight("resource_1", "key-002")
    assert not not_acquired, "Should NOT acquire already-locked resource"

    assert IdempotencyStore.has_inflight("resource_1")
    assert IdempotencyStore.get_inflight_key("resource_1") == "key-001"

    IdempotencyStore.clear_inflight("resource_1")
    assert not IdempotencyStore.has_inflight("resource_1")
    print("  PASSED: inflight guard")


def test_skill_trace_attributes():
    """Test SkillTraceAttributes emission."""
    from skills.tracing import SkillTraceAttributes

    attrs = SkillTraceAttributes.from_execution(
        skill_id="analysis.net_detour@1.0.0",
        call_id="call_001",
        outcome="success",
        latency_ms=42.5,
    )
    d = attrs.to_dict()
    assert d["skill.id"] == "analysis.net_detour@1.0.0"
    assert d["skill.call_id"] == "call_001"
    assert d["skill.outcome"] == "success"
    assert d["skill.latency_ms"] == 42.5
    assert d["skill.cache_hit"] is False
    print("  PASSED: trace attributes")


def main():
    """Run all tests."""
    print("=" * 60)
    print("Skill Framework Integration Tests")
    print("=" * 60)

    tests = [
        test_skill_registry_get,
        test_skill_registry_get_registered_skill,
        test_skill_registry_duplicate_registration,
        test_skill_registry_list_all,
        test_skill_registry_list_by_category,
        test_skill_decorator_registration,
        test_skill_metadata_from_decorator,
        test_skill_context,
        test_skill_execute,
        test_skill_validate_inputs,
        test_skill_result_dataclass,
        test_skill_metadata_dataclass,
        test_parameter_spec,
        test_skill_category_enum,
        test_skill_default_get_metadata,
        test_telemetry_record_execution,
        test_telemetry_metrics,
        test_telemetry_execute_with_telemetry,
        test_telemetry_get_all_metrics,
        test_telemetry_get_recent_executions,
        test_telemetry_execution_summary,
        test_skill_descriptor_id_format,
        test_skill_to_json_schema,
        test_skill_descriptor_generation,
        test_skill_error_code,
        test_skill_idempotency_store,
        test_skill_inflight_guard,
        test_skill_trace_attributes,
    ]

    passed = 0
    failed = 0

    for test in tests:
        print(f"\nTest: {test.__name__}")
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
