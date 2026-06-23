"""
SkillRegistry for progressive skill discovery and lazy activation.

Three-layer integration:
  - Layer 1 (Discovery):  skill module index (regex) + descriptor summaries
  - Layer 2 (Activation):  `get()` auto-triggers lazy import of the module
  - Layer 3 (Execution):   `get()` returns the registered Skill instance
"""

import logging
from typing import Optional

from skills.base import Skill, SkillMetadata, SkillCategory, SkillMetadataSummary
from skills import lazy_loader

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Global registry for skill discovery, lazy activation, and invocation.

    Callers of ``get(name)`` are transparently served: if the skill's module
    hasn't been imported yet, it is imported on demand.
    """
    _skills: dict[str, Skill] = {}

    @classmethod
    def register(cls, skill: Skill) -> None:
        meta = skill.get_metadata()
        if meta.name in cls._skills:
            raise ValueError(f"Skill '{meta.name}' already registered")
        cls._skills[meta.name] = skill
        logger.debug("Registered skill '%s' (%s)", meta.name, meta.id)

    @classmethod
    def get(cls, name: str) -> Optional[Skill]:
        skill = cls._skills.get(name)
        if skill is not None:
            return skill
        if lazy_loader.activate(name):
            return cls._skills.get(name)
        return None

    @classmethod
    def is_registered(cls, name: str) -> bool:
        return name in cls._skills

    @classmethod
    def list_all(cls) -> list[SkillMetadata]:
        return [s.get_metadata() for s in cls._skills.values()]

    @classmethod
    def list_by_category(cls, category: SkillCategory) -> list[SkillMetadata]:
        return [s.get_metadata() for s in cls._skills.values()
                if s.get_metadata().category == category]

    # ── Discovery layer ────────────────────────────────────────

    @classmethod
    def discover_all(cls) -> list[SkillMetadataSummary]:
        """Return lightweight summaries for *all* known skills (no imports)."""
        return lazy_loader.load_all_summaries()

    @classmethod
    def discover_by_category(cls, category: str) -> list[SkillMetadataSummary]:
        cat_upper = category.upper()
        return [s for s in cls.discover_all()
                if s.category.upper() == cat_upper]

    @classmethod
    def discovery_prompt(cls) -> str:
        """Format the full discovery index as a compact prompt block."""
        summaries = cls.discover_all()
        return lazy_loader.summaries_to_prompt(summaries)

    @classmethod
    def clear(cls) -> None:
        cls._skills.clear()
        lazy_loader.deactivate_all()
