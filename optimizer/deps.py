"""Node dependency container.

External dependencies (MCP sessions, MemoryManager, OpenAI client)
are injected via NodeDeps, not stored in state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NodeDeps:
    """External dependencies injected into nodes. Not part of state."""
    openai_client: Any = None              # AsyncOpenAI instance
    memory_manager: Any = None             # context_manager.manager.MemoryManager
    compat: Any = None                     # context_manager.compat.DCPOptimizerCompat
    rapidwright_session: Any = None        # MCP ClientSession for RapidWright
    vivado_session: Any = None             # MCP ClientSession for Vivado
    tools: list = field(default_factory=list)  # Tool definitions for LLM
    event_bus: Any = None                  # EventBus
    prompt_logger: Any = None              # PromptLogger
    system_prompt: str = ""                # Formatted system prompt
    # Configuration
    model_planner: str = ""
    model_worker: str = ""
    api_key: str = ""
    reasoning_config: dict = field(default_factory=dict)  # Per-tier reasoning config: {"worker": {"enabled": bool, "max_output_tokens": int|None}, "planner": {...}}
    tracer: Any = None  # DashboardStateTracer for real-time tool event push
