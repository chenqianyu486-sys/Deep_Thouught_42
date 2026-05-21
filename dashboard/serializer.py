"""Serialize OptimizerState to JSON-safe dict for the web dashboard."""

from __future__ import annotations

import dataclasses
import json
import math
import time
from pathlib import Path


def _make_json_safe(obj):
    """Recursively convert non-JSON-serializable values."""
    if isinstance(obj, dict):
        return {
            str(k): _make_json_safe(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_make_json_safe(v) for v in obj)
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None
        return obj
    if isinstance(obj, Path):
        return str(obj)
    return obj


def serialize_state(state) -> dict:
    """Convert OptimizerState to a JSON-serializable dict.

    Handles float('-inf'), set, Path, tuple keys, etc.
    """
    raw = dataclasses.asdict(state)
    return _make_json_safe(raw)
