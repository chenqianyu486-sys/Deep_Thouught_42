# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
SkillContext for dependency injection.
"""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SkillContext:
    """Dependency injection container for skill execution.

    Avoids global state by passing design/tool access at invocation time.
    """
    design: Any = None
    device: Any = None
    initialized: bool = False
    tools: dict[str, Callable] = field(default_factory=dict)
    trace_id: str = ""
