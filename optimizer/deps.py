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
    memory_manager: Any = None             # context_manager.manager.MemoryManager (message store + compression engine)
    compat: Any = None                     # context_manager.compat.DCPOptimizerCompat — message store adapter
    rapidwright_session: Any = None        # MCP ClientSession for RapidWright
    vivado_session: Any = None             # MCP ClientSession for Vivado
    tools: list = field(default_factory=list)  # Tool definitions for LLM
    event_bus: Any = None                  # EventBus
    prompt_logger: Any = None              # PromptLogger
    llm_call_logger: Any = None            # LLMCallLogger
    system_prompt: str = ""                # Formatted system prompt
    # Configuration
    model_planner: str = ""
    model_worker: str = ""
    api_key: str = ""
    reasoning_config: dict = field(default_factory=dict)  # Per-tier reasoning config: {"worker": {"enabled": bool, "max_output_tokens": int|None}, "planner": {...}}
    tracer: Any = None  # DashboardStateTracer for real-time tool event push


# Dependency health check configuration
HEALTH_CHECK_ENABLED = True
HEALTH_CHECK_INTERVAL_SECONDS = 60  # Check MCP sessions every minute

async def check_dependency_health(deps: "NodeDeps") -> dict[str, bool]:
    """Quick health check for MCP sessions and Vivado connection."""
    status = {}
    if deps.vivado:
        try:
            result = await deps.vivado.call_tool("vivado_check_design_status", {})
            status["vivado"] = "error" not in str(result).lower()
        except Exception:
            status["vivado"] = False
    if deps.rapidwright:
        try:
            status["rapidwright"] = True  # Session exists = OK
        except Exception:
            status["rapidwright"] = False
    return status

# Deps: auto-reconnect on MCP session loss
AUTO_RECONNECT_ENABLED = True
AUTO_RECONNECT_MAX_ATTEMPTS = 3

def get_active_tool_count(deps) -> int:
    """Count how many MCP tools are currently available."""
    count = 0
    if deps.vivado: count += 1
    if deps.rapidwright: count += 1
    return count

def get_session_status(deps) -> dict:
    """Check status of all MCP sessions."""
    return {"vivado": deps.vivado is not None, "rapidwright": deps.rapidwright is not None}

def get_deps_summary(deps) -> str:
    """Summary of dependency status."""
    parts = []
    if deps.vivado: parts.append("VivadoOK")
    if deps.rapidwright: parts.append("RWOK")
    return ",".join(parts) if parts else "NoMCP"

def compute_deps_startup_time(deps) -> float:
    """Estimate total startup time for dependencies."""
    return 10.0 + (5.0 if deps.rapidwright else 0) + (3.0 if deps.vivado else 0)

def compute_health_score(deps) -> float:
    """Overall dependency health score: 0=dead, 1=perfect."""
    score = 0.0
    if deps.vivado: score += 0.5
    if deps.rapidwright: score += 0.5
    return score
