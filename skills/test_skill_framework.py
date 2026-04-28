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

    def get_metadata(self) -> SkillMetadata:
        return self._skill_metadata

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
    skill = SkillRegistry.get("analyze_net_detour")
    assert skill is not None, "analyze_net_detour should be registered"
    skill2 = SkillRegistry.get("optimize_cell_placement")
    assert skill2 is not None, "optimize_cell_placement should be registered"
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
    # Should have at least test_mock_skill and analyze_net_detour
    assert len(filtered) >= 2, f"Expected at least 2 analysis skills, got {len(filtered)}"
    print(f"  PASSED: list by category ({len(filtered)} analysis skills)")


def test_skill_decorator_registration():
    """Test that @skill decorator registers the skill automatically."""
    skills = SkillRegistry.list_all()
    skill_names = [s.name for s in skills]

    assert "analyze_net_detour" in skill_names, "AnalyzeNetDetourSkill should be registered"
    assert "optimize_cell_placement" in skill_names, "OptimizeCellPlacementSkill should be registered"
    assert "test_mock_skill" in skill_names, "MockSkillForTest should be registered"
    print("  PASSED: @skill decorator registers automatically")


def test_skill_metadata_from_decorator():
    """Test that metadata from decorator is correctly attached."""
    skill = SkillRegistry.get("analyze_net_detour")
    assert skill is not None

    meta = skill.get_metadata()
    assert meta.name == "analyze_net_detour"
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
    skill = SkillRegistry.get("analyze_net_detour")
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
    skill = SkillRegistry.get("analyze_net_detour")
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
