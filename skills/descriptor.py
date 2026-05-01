# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
JSON Descriptor export for Skills.

Generates Skill Descriptor v3 JSON files from registered Skill instances.
Descriptors are written to the ``descriptors/`` subdirectory as
``{namespace}.{name}@version.json``.

The @skill decorator calls ``write_descriptor()`` automatically at registration time.
Use ``export_all()`` to re-export descriptors for all registered skills.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Directory where descriptor JSON files are written
DESCRIPTORS_DIR = Path(__file__).parent / "descriptors"


def _descriptor_path(meta) -> Path:
    """Compute the filesystem path for a skill's descriptor JSON."""
    safe_name = meta.id.replace("@", "-at-").replace("/", "_")
    DESCRIPTORS_DIR.mkdir(parents=True, exist_ok=True)
    return DESCRIPTORS_DIR / f"{safe_name}.json"


def write_descriptor(skill) -> Optional[Path]:
    """Generate and write the JSON descriptor for a skill instance.

    Args:
        skill: A registered Skill instance.

    Returns:
        Path to the written descriptor file, or None on failure.
    """
    try:
        meta = skill.get_metadata()
        descriptor = meta.to_descriptor()
        path = _descriptor_path(meta)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(descriptor, f, indent=2, ensure_ascii=False)
        logger.debug(f"Descriptor written: {path}")
        return path
    except Exception as e:
        logger.warning(f"Failed to write descriptor for {skill}: {e}")
        return None


def export_all() -> list[Path]:
    """Export descriptors for all currently registered skills.

    Returns:
        List of paths to written descriptor files.
    """
    from skills.registry import SkillRegistry

    paths: list[Path] = []
    for skill_meta in SkillRegistry.list_all():
        skill = SkillRegistry.get(skill_meta.name)
        if skill:
            path = write_descriptor(skill)
            if path:
                paths.append(path)
    return paths


def read_descriptor(skill_id: str) -> Optional[dict]:
    """Read a descriptor JSON file by skill ID.

    Args:
        skill_id: Fully-qualified skill ID (e.g., "analysis.net_detour@1.0.0").

    Returns:
        Descriptor dict, or None if not found.
    """
    safe_name = skill_id.replace("@", "-at-").replace("/", "_")
    path = DESCRIPTORS_DIR / f"{safe_name}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read descriptor {path}: {e}")
        return None
