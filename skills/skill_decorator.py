# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
@skill decorator for automatic skill registration.
"""

from typing import Callable

from skills.base import SkillCategory, ParameterSpec
from skills.registry import SkillRegistry


def skill(
    name: str,
    description: str,
    category: SkillCategory,
    parameters: list[ParameterSpec] = None,
    required_context: list[str] = None,
    output_schema: dict = None
) -> Callable:
    """Decorator to register a Skill class.

    Args:
        name: Skill name for discovery
        description: Human-readable description
        category: Skill category for filtering
        parameters: List of parameter specifications
        required_context: List of required context fields (e.g., ["design"])
        output_schema: JSON schema for output

    Usage:
        @skill(
            name="analyze_net_detour",
            description="Analyze detour ratios for critical path cells.",
            category=SkillCategory.ANALYSIS,
            parameters=[ParameterSpec("pin_paths", list[str], "...")],
            required_context=["design"]
        )
        class AnalyzeNetDetourSkill(Skill):
            ...
    """
    def decorator(cls) -> type:
        # Import here to avoid circular dependency
        from skills.base import SkillMetadata

        # Attach metadata to the class for later use
        cls._skill_metadata = SkillMetadata(
            name=name,
            description=description,
            category=category,
            parameters=parameters or [],
            required_context=required_context or [],
            output_schema=output_schema or {}
        )

        # Register the skill instance
        SkillRegistry.register(cls())

        return cls
    return decorator
