# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Skill framework for FPGA optimization.

This module provides a standardized skill mechanism for defining,
registering, and invoking skills within the optimization system.
"""

from skills.base import (
    Skill,
    SkillCategory,
    SkillMetadata,
    SkillResult,
    ParameterSpec,
)
from skills.context import SkillContext
from skills.registry import SkillRegistry
from skills.skill_decorator import skill

# Import submodules to trigger @skill decorators
from skills import net_detour_optimization

__all__ = [
    "Skill",
    "SkillCategory",
    "SkillMetadata",
    "SkillResult",
    "ParameterSpec",
    "SkillContext",
    "SkillRegistry",
    "skill",
]
