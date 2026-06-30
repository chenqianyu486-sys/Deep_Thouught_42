"""TCL security primitives for the Vivado MCP server.

Extracted from vivado_mcp_server.py to enable independent unit testing of the
security-critical functions (blocked-command detection, safe quoting, line
completeness check) without pulling in pexpect / mcp / Vivado dependencies.

These functions are pure and have no side effects.
"""

import re

# TCL commands that can execute arbitrary code or perform I/O - blocked in
# LLM-facing TCL execution. `source`/`eval`/`subst`/`uplevel` can bypass the
# `exec` guard (e.g. `eval {exec ls}`, `uplevel #0 {exec ls}`); `load` loads
# shared libraries; `open`/`socket` do I/O.
BLOCKED_TCL_COMMANDS: frozenset[str] = frozenset({
    "exec", "source", "eval", "subst", "uplevel", "load", "open", "socket",
    "cd", "pwd", "exit",
})

# Splits a TCL script into statements: newline / semicolon / open-bracket
# (open-bracket starts command substitution, e.g. `set x [exec ls]`).
TCL_COMMAND_SPLIT_RE = re.compile(r'[\n;\[]')


def contains_blocked_tcl_command(command: str) -> bool:
    """Detect blocked TCL commands anywhere in the script.

    Catches all common bypass forms of the old startswith("exec ") guard:
      - line-start:        exec ls
      - semicolon-split:   puts hi; exec ls
      - command subst:     set x [exec ls]
      - multi-line:        set x 5\nexec ls
      - uplevel bypass:    uplevel #0 {exec ls}
    """
    for stmt in TCL_COMMAND_SPLIT_RE.split(command):
        stmt = stmt.strip()
        if not stmt or stmt.startswith("#"):
            continue
        first_token = stmt.split(None, 1)[0].lower()
        if first_token in BLOCKED_TCL_COMMANDS:
            return True
    return False


def tcl_quote(value: str) -> str:
    """Safely wrap a value as a TCL brace-quoted literal.

    Brace quoting treats the content as a literal string. A value containing
    a close-brace cannot be safely represented this way, so we raise rather
    than risk breaking the brace balance (which would enable injection).
    """
    if '}' in value:
        raise ValueError(f"Cannot safely quote value containing close-brace: {value!r}")
    return "{" + value + "}"


def tcl_line_is_complete(line: str) -> bool:
    """Lightweight Python-side check: is this single line a complete TCL command?

    Replaces the previous info-complete call which was vulnerable to injection
    via crafted lines like }; exec rm -rf /; {. Checks brace and bracket
    balance plus absence of trailing-backslash continuation. Full TCL syntax
    validation is deferred to actual execution.
    """
    if line.rstrip().endswith('\\'):
        return False
    if line.count('{') != line.count('}'):
        return False
    if line.count('[') != line.count(']'):
        return False
    return True
