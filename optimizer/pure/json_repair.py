"""Lightweight JSON repair for LLM tool-call arguments.

LLMs occasionally emit malformed JSON in long tool-call argument strings
(observed in run-20260711_230953, replicate_critical_cells call 40: stray
escaped quotes ``\\"`` appearing mid-stream). The previous fallback discarded
the whole payload (``args={}``), which made the tool run with empty arguments
and triggered a wasteful "missing required property" validation error that had
to be fed back to the LLM for a retry on the next round.

This module attempts conservative repairs before giving up. Each repaired
candidate is re-validated with ``json.loads``; a repair is only accepted if it
parses to a dict. Downstream JSON-schema + cell-registry validation still
catches any residual corruption, so repairing is strictly safer than the
``{}`` fallback (it gives the tool a chance and keeps the arguments visible in
logs).

Domain assumption: tool-call arguments in this project are cell names, numbers,
directives, and paths. They do not contain legitimately escaped quotes, so
unescaping ``\\"`` -> ``"`` is safe here.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Matches a comma immediately before a closing } or ] (possibly with whitespace).
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _try_loads(s: str) -> Optional[dict]:
    """Parse ``s`` as JSON; return the dict only if it parses to a dict."""
    try:
        value = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _first_balanced_object(s: str) -> Optional[str]:
    """Return the first brace-balanced ``{...}`` region of ``s``.

    Drops any trailing garbage after the first balanced object. String-aware so
    braces inside JSON string literals are not counted.
    """
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return None


def _strip_trailing_commas(s: str) -> str:
    return _TRAILING_COMMA_RE.sub(r"\1", s)


def parse_tool_arguments(arguments: Optional[str], tool_name: str = "") -> dict:
    """Parse LLM tool-call arguments with conservative repair on failure.

    Returns ``{}`` for empty/None input or unrecoverable malformed JSON. On
    successful repair or final failure, emits a ``[JSON_REPAIR]`` warning so the
    event is observable (never silent).
    """
    if not arguments:
        return {}

    direct = _try_loads(arguments)
    if direct is not None:
        return direct

    # Conservative repair candidates, tried in order; first that re-parses wins.
    unescaped = arguments.replace('\\"', '"')
    decommaed = _strip_trailing_commas(arguments)
    balanced = _first_balanced_object(arguments)
    candidates = [
        unescaped,
        decommaed,
    ]
    if balanced is not None and balanced != arguments:
        candidates.append(balanced)
        candidates.append(balanced.replace('\\"', '"'))
        candidates.append(_strip_trailing_commas(balanced))

    for cand in candidates:
        repaired = _try_loads(cand)
        if repaired is not None:
            logger.warning(
                "[JSON_REPAIR] recovered %s arguments via repair (len=%d)",
                tool_name or "<unknown>",
                len(arguments),
            )
            return repaired

    logger.warning(
        "[JSON_REPAIR] failed to parse %s arguments (len=%d); falling back to {}",
        tool_name or "<unknown>",
        len(arguments),
    )
    return {}
