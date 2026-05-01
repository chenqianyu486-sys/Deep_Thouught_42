# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Descriptor Validation Suite.

Implements validation checks from the Skill Descriptor v3 specification
Section 8: Descriptor Validation Suite.

Each check returns a list of violation strings. An empty list means PASS.

Usage:
    python -m skills.validate_descriptors
    python skills/validate_descriptors.py
"""

import sys
import os

# Ensure project root is on sys.path when run as __main__
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_script_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from skills.registry import SkillRegistry


def _collect_violations(checks: list[tuple[str, bool]], label: str) -> list[str]:
    """Collect violation messages from a list of (message, passed) tuples."""
    return [f"  FAIL: {msg}" for msg, ok in checks if not ok]


def _check_object(violations: list[str], path: str, obj: dict):
    """Recursively assert additionalProperties: false on every nested object."""
    if not isinstance(obj, dict):
        return
    if "additionalProperties" in obj and obj["additionalProperties"] is not False:
        violations.append(
            f"  FAIL [{path}]: additionalProperties must be false"
        )
    for key, val in obj.items():
        if isinstance(val, dict):
            _check_object(violations, f"{path}.{key}", val)
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, dict):
                    _check_object(violations, f"{path}.{key}[{i}]", item)


def validate_descriptor(meta) -> list[str]:
    """Validate a SkillMetadata against all specification checks.

    Returns:
        List of violation strings. Empty list = descriptor passes all checks.
    """
    violations: list[str] = []

    if not meta.id:
        violations.append("  FAIL: missing id")
    elif "@" not in meta.id:
        violations.append(f"  FAIL: id '{meta.id}' missing version (@)")

    if not meta.description:
        violations.append("  FAIL: missing description")

    if meta.idempotency not in ("safe", "idempotent", "non-idempotent"):
        violations.append(f"  FAIL: invalid idempotency '{meta.idempotency}'")

    if not isinstance(meta.side_effects, list):
        violations.append("  FAIL: side_effects must be a list")

    if meta.timeout_ms <= 0:
        violations.append(f"  FAIL: timeout_ms must be positive, got {meta.timeout_ms}")

    if not meta.error_codes:
        violations.append("  FAIL: error_codes is empty")
    else:
        required_errors = {"INVALID_PARAMETER", "TEMPORARILY_UNAVAILABLE", "SKILL_TIMEOUT"}
        declared = set(meta.error_codes)
        missing = required_errors - declared
        if missing:
            violations.append(
                f"  FAIL: missing required error codes: {sorted(missing)}"
            )

    for param in meta.parameters:
        if not param.description:
            violations.append(f"  FAIL: parameter '{param.name}' missing description")

    # Schema-level checks on to_json_schema output
    schema = meta.to_json_schema()
    if schema.get("additionalProperties") is not False:
        violations.append("  FAIL [parameters]: additionalProperties must be false")

    props = schema.get("properties", {})
    for pname, pval in props.items():
        if not pval.get("description"):
            violations.append(f"  FAIL [parameters.{pname}]: missing description")

    return violations


def run_all() -> int:
    """Validate all registered skills and print results.

    Returns:
        0 if all pass, 1 if any violations found.
    """
    skills_list = SkillRegistry.list_all()
    if not skills_list:
        print("No skills registered.")
        return 1

    print("=" * 60)
    print("Descriptor Validation Suite")
    print("=" * 60)

    all_ok = True
    for meta in skills_list:
        print(f"\nSkill: {meta.id}")
        try:
            violations = validate_descriptor(meta)
        except Exception as e:
            print(f"  ERROR: validation crashed: {e}")
            violations = [f"  ERROR: {e}"]

        if violations:
            all_ok = False
            for v in violations:
                print(v)
        else:
            print("  PASS")

    print("\n" + "=" * 60)
    if all_ok:
        print("Result: ALL PASSED")
        return 0
    else:
        print("Result: VIOLATIONS FOUND")
        return 1


if __name__ == "__main__":
    sys.exit(run_all())
