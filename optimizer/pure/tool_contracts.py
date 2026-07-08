"""Structured tool-call boundary contracts.

This module turns raw tool text into a single parsed envelope so phase code
does not need to repeatedly ``json.loads()`` and infer error state ad hoc.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .tool_runtime_policy import _MCP_ERROR_PATTERNS


def is_mcp_error_response(text: str) -> bool:
    """Return True when an MCP response string encodes a recoverable error."""
    if not text:
        return False
    return any(pattern in text for pattern in _MCP_ERROR_PATTERNS)


def coerce_payload_dict(raw_result: object) -> dict[str, Any] | None:
    """Best-effort convert a raw tool result into a JSON dict payload."""
    if isinstance(raw_result, dict):
        return raw_result
    if not isinstance(raw_result, str) or not raw_result:
        return None
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """Single parsed representation of a tool response."""

    tool_name: str
    raw_text: str
    payload: dict[str, Any] | None
    error: str | None
    status: str | None
    is_mcp_error: bool

    @property
    def ok(self) -> bool:
        return self.error is None and not self.is_mcp_error


def build_tool_call_result(tool_name: str, raw_text: str) -> ToolCallResult:
    """Parse raw tool text into a structured envelope."""
    payload = coerce_payload_dict(raw_text)
    error = None
    status = None

    if payload is not None:
        raw_error = payload.get("error")
        if isinstance(raw_error, str) and raw_error.strip():
            error = raw_error
        raw_status = payload.get("status")
        if isinstance(raw_status, str) and raw_status.strip():
            status = raw_status

    mcp_error = is_mcp_error_response(raw_text)
    if error is None and mcp_error:
        error = raw_text

    return ToolCallResult(
        tool_name=tool_name,
        raw_text=raw_text,
        payload=payload,
        error=error,
        status=status,
        is_mcp_error=mcp_error,
    )
