"""Structured tool-error classification (pure functions).

Turns an opaque tool error string into a structured envelope
``{category, fix_hint, retryable}`` so the LLM can understand WHY a tool
failed and WHAT to change before retrying - rather than only seeing a
truncated ``{"error": "..."}`` blob.

This is the classification layer of the structured error envelope (P0 ③A).
The existing rich-error paths (cell-name validation in ``entities.py``,
directive rejection in ``vivado_mcp_server.py``) already embed their own
actionable hints in the raw output; this module adds a *consistent*
category/fix_hint/retryable header on top of every error summary so the
LLM gets a uniform signal across all failure types.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolErrorClass:
    """Structured classification of a single tool failure."""

    category: str   # bad_cell_name|bad_directive|tcl_blocked|timeout|vivado_error|rw_error|schema_validation|partial_failure|rate_limited|unknown
    fix_hint: str   # actionable correction suggestion
    retryable: bool # whether the LLM should retry with adjusted params


# Default fallback: most tool errors are transient/parameter-fixable, so the
# safe default is to allow a retry (capped by RETRY_BUDGET in state.py).
_UNKNOWN = ToolErrorClass(
    category="unknown",
    fix_hint="Inspect raw_output for the failure reason and adjust parameters before retrying.",
    retryable=True,
)


def classify_tool_error(tool_name: str, error: str) -> ToolErrorClass:
    """Classify a tool error string into a structured envelope.

    Args:
        tool_name: The MCP/internal tool that failed (used for context only).
        error: The error text (raw output, ``ToolCallResult.error``, or a
            summary containing the error). Empty/None -> unknown.

    Returns:
        A ``ToolErrorClass``. Never raises.
    """
    if not error or not isinstance(error, str):
        return _UNKNOWN

    low = error.lower()

    # ── Rate limit (per-phase, not parameter-fixable this phase) ──────────
    if "[rate limited]" in low:
        return ToolErrorClass(
            category="rate_limited",
            fix_hint="Stop calling this tool this phase; reuse Dashboard/cached data or batch arguments into a single call.",
            retryable=False,
        )

    # ── Bad directive (Vivado Constraints 18-641) ─────────────────────────
    if "18-641" in error or "not a recognized directive" in low or "unrecognized directive" in low:
        return ToolErrorClass(
            category="bad_directive",
            fix_hint="Use a supported directive from PLACE_SAFE_DIRECTIVES/ROUTE_SAFE_DIRECTIVES "
                     "(e.g. Default, Explore, ExtraTimingOpt for place; Default, Explore, AggressiveExplore "
                     "for route). Do NOT unplace to retry.",
            retryable=True,
        )

    # ── Bad cell name (entities.py rich error) ────────────────────────────
    if "invalid_cell_names" in low or "\"status\": \"rejected\"" in low or "cell names must be" in low:
        return ToolErrorClass(
            category="bad_cell_name",
            fix_hint="Use canonical hierarchical cell names (containing '/') from [CELL REGISTRY]; "
                     "device sites (SLICE_X*) and bare types (LUT6, FDRE) are invalid. Re-issue with corrected names.",
            retryable=True,
        )

    # ── TCL blocked / intercepted ─────────────────────────────────────────
    if "[blocked] command contains a blocked tcl" in low:
        return ToolErrorClass(
            category="tcl_blocked",
            fix_hint="Use Vivado Tcl commands only (report_*, get_*, set_property). Avoid blocked commands.",
            retryable=True,
        )
    if "[auto-guidance]" in low:
        return ToolErrorClass(
            category="tcl_blocked",
            fix_hint="Critical-path data is already in Dashboard Module 2 and auto-injected into strategy tools; use vivado_extract_critical_path_cells instead of raw TCL.",
            retryable=True,
        )

    # ── Timeout (application-level or TCL) ────────────────────────────────
    if "application-level timeout" in low or "tcl command timed out" in low:
        return ToolErrorClass(
            category="timeout",
            fix_hint="Reduce scope (smaller num_paths, avoid global unplace, batch cells) and retry; "
                     "if Vivado hung it was auto-restarted and the DCP reopened.",
            retryable=True,
        )

    # ── MCP schema / input validation ─────────────────────────────────────
    if "mcp tool error:" in low or "input validation error" in low:
        return ToolErrorClass(
            category="schema_validation",
            fix_hint="Fix the argument type/schema per the validation error and retry.",
            retryable=True,
        )

    # ── Partial failure (physopt_and_route multiple errors) ───────────────
    if "\"errors\":" in low or "errors:" in low:
        return ToolErrorClass(
            category="partial_failure",
            fix_hint="One or more sub-steps failed; inspect raw_output errors[] and adjust per-step args before retrying.",
            retryable=True,
        )

    # ── RapidWright JSON error ────────────────────────────────────────────
    if tool_name.startswith("rapidwright_") and "\"error\"" in low:
        return ToolErrorClass(
            category="rw_error",
            fix_hint="Inspect the RapidWright error in raw_output; adjust cells/nets/directive args and retry.",
            retryable=True,
        )

    # ── Generic Vivado ^ERROR ─────────────────────────────────────────────
    if "^error:" in low or "place_design failed" in low or "route_design failed" in low or "opt_design failed" in low:
        return ToolErrorClass(
            category="vivado_error",
            fix_hint="Inspect the Vivado error code in raw_output; address the constraint/DRC issue and retry.",
            retryable=True,
        )

    return _UNKNOWN


def error_envelope_lines(tool_name: str, error: str) -> list[str]:
    """Return YAML lines for the error envelope, or [] if not classifiable.

    Convenience wrapper for ``summarize_tool_result``: emits the three
    envelope fields (category / fix_hint / retryable) as indented YAML lines.
    """
    cls = classify_tool_error(tool_name, error)
    # Always emit - even unknown gives the LLM a retryable signal.
    return [
        f"  error_category: {cls.category}",
        f'  fix_hint: "{cls.fix_hint}"',
        f"  retryable: {str(cls.retryable).lower()}",
    ]
