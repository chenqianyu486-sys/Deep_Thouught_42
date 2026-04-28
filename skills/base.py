# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Skill base classes and data structures.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


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
