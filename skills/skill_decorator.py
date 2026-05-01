# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
@skill decorator for automatic skill registration and descriptor export.

Usage:
    @skill(
        name="net_detour",
        namespace="analysis",
        version="1.0.0",
        display_name="Analyze Net Detour",
        description="Analyze detour ratios for cells on critical paths. READ-ONLY.",
        category=SkillCategory.ANALYSIS,
        idempotency="safe",
        side_effects=[],
        timeout_ms=30000,
        parameters=[ParameterSpec("pin_paths", list, "Pin path list from Vivado.")],
        required_context=["design"],
        error_codes=["INVALID_PARAMETER", "RESOURCE_NOT_FOUND", "SKILL_TIMEOUT"],
    )
    class MySkill(Skill):
        ...
"""

from typing import Callable, Optional

from skills.base import SkillCategory, ParameterSpec
from skills.registry import SkillRegistry


def skill(
    name: str,
    description: str = "",
    category: SkillCategory = SkillCategory.ANALYSIS,
    parameters: Optional[list[ParameterSpec]] = None,
    required_context: Optional[list[str]] = None,
    output_schema: Optional[dict] = None,
    # New fields per Skill Descriptor v3 spec
    version: str = "1.0.0",
    namespace: str = "",
    display_name: str = "",
    idempotency: str = "safe",
    side_effects: Optional[list[str]] = None,
    timeout_ms: int = 30000,
    error_codes: Optional[list[str]] = None,
) -> Callable:
    """Decorator to register a Skill class with full descriptor metadata.

    Args:
        name: Short skill name (e.g., "net_detour").
        description: Full description including READ-ONLY/MUTATING declaration,
                     preconditions, and trigger conditions.
        category: SkillCategory for filtering.
        parameters: List of ParameterSpec defining the call signature.
        required_context: Context fields required (e.g., ["design"]).
        output_schema: JSON Schema Draft 2020-12 for return value.

        version: Semantic version (MAJOR.MINOR.PATCH).
        namespace: Domain grouping (e.g., "analysis", "placement").
                   Falls back to category.value if empty.
        display_name: Human-readable name. Falls back to name if empty.
        idempotency: One of "safe", "idempotent", "non-idempotent".
        side_effects: Declared side effects (e.g., ["cell_placement"]).
                      Empty list means pure/read-only.
        timeout_ms: Default execution timeout in milliseconds.
        error_codes: List of declared error codes. Defaults to
                     ["INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"].

    Returns:
        Class decorator that attaches _skill_metadata and registers the skill.
    """
    def decorator(cls) -> type:
        # Import here to avoid circular dependency
        from skills.base import SkillMetadata

        resolved_ns = namespace or category.value
        auto_id = f"{resolved_ns}.{name}@{version}"
        resolved_display = display_name or name.replace("_", " ").title()

        # Attach metadata to the class for later use by Skill.get_metadata()
        cls._skill_metadata = SkillMetadata(
            name=name,
            id=auto_id,
            version=version,
            namespace=resolved_ns,
            display_name=resolved_display,
            description=description,
            category=category,
            spec_version="3.0",
            idempotency=idempotency,
            side_effects=side_effects or [],
            timeout_ms=timeout_ms,
            parameters=parameters or [],
            required_context=required_context or [],
            output_schema=output_schema or {},
            authentication={"type": "none"},
            error_codes=error_codes or [
                "INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT",
            ],
        )

        # Register the skill instance
        instance = cls()
        SkillRegistry.register(instance)

        # Export JSON descriptor
        try:
            from skills.descriptor import write_descriptor
            write_descriptor(instance)
        except Exception:
            pass  # Descriptor export is best-effort at import time

        return cls
    return decorator
