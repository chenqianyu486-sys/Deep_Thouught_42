# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
SkillRegistry for skill discovery and invocation.
"""

from typing import Optional

from skills.base import Skill, SkillMetadata, SkillCategory


class SkillRegistry:
    """Global registry for skill discovery and invocation.

    Supports registration via @skill decorator and retrieval by name.
    """
    _skills: dict[str, Skill] = {}

    @classmethod
    def register(cls, skill: Skill) -> None:
        """Register a skill instance.

        Args:
            skill: Skill instance to register

        Raises:
            ValueError: If skill name already registered
        """
        meta = skill.get_metadata()
        if meta.name in cls._skills:
            raise ValueError(f"Skill '{meta.name}' already registered")
        cls._skills[meta.name] = skill

    @classmethod
    def get(cls, name: str) -> Optional[Skill]:
        """Get a skill by name.

        Args:
            name: Skill name

        Returns:
            Skill instance or None if not found
        """
        return cls._skills.get(name)

    @classmethod
    def list_all(cls) -> list[SkillMetadata]:
        """List metadata for all registered skills.

        Returns:
            List of SkillMetadata for all registered skills
        """
        return [s.get_metadata() for s in cls._skills.values()]

    @classmethod
    def list_by_category(cls, category: SkillCategory) -> list[SkillMetadata]:
        """List skills filtered by category.

        Args:
            category: Category to filter by

        Returns:
            List of SkillMetadata matching the category
        """
        return [s.get_metadata() for s in cls._skills.values()
                if s.get_metadata().category == category]

    @classmethod
    def clear(cls) -> None:
        """Clear all registered skills. For testing purposes."""
        cls._skills.clear()
