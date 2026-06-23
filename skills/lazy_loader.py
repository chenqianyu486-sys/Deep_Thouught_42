"""
Progressive three-layer skill loader.

Layers:
  1. Discovery  — regex scan skills/*.py for name→module mapping + read
                  descriptors/*.json into SkillMetadataSummary (~100 tok each).
  2. Activation — dynamic import of the specific module, triggering @skill
                  decorator and SkillRegistry registration.
  3. Execution  — reuse existing Skill.execute_with_telemetry() unchanged.
"""

import importlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from skills.base import SkillMetadataSummary

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent
_DESCRIPTORS_DIR = _SKILLS_DIR / "descriptors"

_RE_SKILL_NAME = re.compile(r'@skill\(\s*\n\s*name="([^"]+)"')
_RE_SKILL_IMPORT = re.compile(r'from skills\.skill_decorator import')

_name_to_module: dict[str, str] | None = None
_activated: set[str] = set()


# ── Layer 1: Discovery (name → module index) ────────────────────


def _ensure_index() -> dict[str, str]:
    global _name_to_module
    if _name_to_module is not None:
        return _name_to_module
    _name_to_module = {}
    for py_file in sorted(_SKILLS_DIR.glob("*.py")):
        stem = py_file.stem
        if stem.startswith("test_") or stem.startswith("_"):
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
            if not _RE_SKILL_IMPORT.search(text):
                continue
            for match in _RE_SKILL_NAME.finditer(text):
                _name_to_module[match.group(1)] = stem
        except OSError as exc:
            logger.warning("Failed to read %s: %s", py_file.name, exc)
    logger.debug("Skill index built: %d skills in %d files",
                 len(_name_to_module), len({v for v in _name_to_module.values()}))
    return _name_to_module


def list_skill_names() -> list[str]:
    return sorted(_ensure_index())


def find_module(name: str) -> Optional[str]:
    """Return the module stem (no .py) that contains skill *name*."""
    return _ensure_index().get(name)


# ── Layer 2: Activation (lazy import) ──────────────────────────


def activate(name: str) -> bool:
    """Import the module containing skill *name* so its @skill decorator fires.

    Returns True if the module was newly imported, False if already loaded
    or if the skill cannot be found.
    """
    if name in _activated:
        return False
    mod_stem = find_module(name)
    if mod_stem is None:
        logger.error("No module found for skill '%s'", name)
        return False
    try:
        importlib.import_module(f"skills.{mod_stem}")
        _activated.add(name)
        logger.info("Activated skill '%s' from skills.%s", name, mod_stem)
        return True
    except ImportError as exc:
        logger.error("Failed to import skills.%s: %s", mod_stem, exc)
        return False


def is_activated(name: str) -> bool:
    return name in _activated


def deactivate_all():
    """For testing only — clear activation cache."""
    global _name_to_module
    _name_to_module = None
    _activated.clear()


# ── Layer 1: Discovery (summaries from descriptors) ────────────


def load_all_summaries() -> list[SkillMetadataSummary]:
    """Read all descriptor JSON files and return lightweight summaries.

    This is a startup-only operation — no skill modules are imported.
    """
    if not _DESCRIPTORS_DIR.is_dir():
        logger.debug("Descriptors directory not found: %s", _DESCRIPTORS_DIR)
        return []
    summaries: list[SkillMetadataSummary] = []
    for json_file in sorted(_DESCRIPTORS_DIR.glob("*.json")):
        try:
            with open(json_file, encoding="utf-8") as f:
                descriptor = json.load(f)
            summaries.append(SkillMetadataSummary.from_descriptor(descriptor))
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Skipping invalid descriptor %s: %s", json_file.name, exc)
    return summaries


def summaries_to_prompt(summaries: list[SkillMetadataSummary]) -> str:
    """Format summaries into a compact prompt block for the agent."""
    if not summaries:
        return ""
    lines = [
        "# Available Skills",
        "",
        "Activate a skill by calling `SkillRegistry.get(name)` when its description matches the task.",
        "",
    ]
    for s in summaries:
        lines.append(s.to_prompt_block())
    lines.append("")
    return "\n".join(lines)
