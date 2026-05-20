"""ANSI color helpers for console log highlighting.

Colors are auto-disabled when stdout is not a TTY or NO_COLOR env is set.
Log files are unaffected — ANSI codes are only emitted to TTY consoles.
"""

from __future__ import annotations

import os
import sys

# Disable colors if not a TTY or NO_COLOR env set
_ENABLED = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
if os.environ.get("NO_COLOR"):
    _ENABLED = False

GREEN = "\033[32m" if _ENABLED else ""
YELLOW = "\033[33m" if _ENABLED else ""
RED = "\033[31m" if _ENABLED else ""
BOLD = "\033[1m" if _ENABLED else ""
RESET = "\033[0m" if _ENABLED else ""


def green(text: str) -> str:
    return f"{GREEN}{text}{RESET}" if _ENABLED else text


def yellow(text: str) -> str:
    return f"{YELLOW}{text}{RESET}" if _ENABLED else text


def red(text: str) -> str:
    return f"{RED}{text}{RESET}" if _ENABLED else text
