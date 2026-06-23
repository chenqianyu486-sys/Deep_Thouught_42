"""
Skill framework for FPGA optimization — with progressive three-layer loading.

Layers:
  1. Discovery — startup: regex scan for name→module mapping + read JSON
                 descriptors → lightweight SkillMetadataSummary (~100 tok each).
  2. Activation — on demand: ``SkillRegistry.get(name)`` triggers lazy import
                  of the specific module, firing the ``@skill`` decorator
                  and registering the full Skill instance.
  3. Execution — reuse existing ``Skill.execute_with_telemetry()`` unchanged.

No skill modules are imported at startup. All explicit ``from skills import ...``
imports in ``__init__.py`` have been removed — ``__init__.py`` only loads
framework infrastructure (base, registry, context, etc.).
"""

from skills.base import (
    Skill,
    SkillCategory,
    SkillMetadata,
    SkillMetadataSummary,
    SkillResult,
    ParameterSpec,
)
from skills.context import SkillContext
from skills.errors import SkillError, SkillErrorCode, ERROR_METADATA
from skills.registry import SkillRegistry
from skills.skill_decorator import skill
from skills.telemetry import (
    SkillTelemetry,
    SkillExecutionRecord,
    SkillMetrics,
    ExecutionStatus,
    SkillExecutionTimer,
)
from skills.idempotency import IdempotencyStore
from skills.descriptor import export_all, write_descriptor, read_descriptor
from skills.tracing import SkillTraceAttributes
from skills.validate_descriptors import validate_descriptor
from skills.strategy_plan import StrategyPlan, StrategyStep
from skills.lazy_loader import load_all_summaries, summaries_to_prompt, list_skill_names

__all__ = [
    "Skill",
    "SkillCategory",
    "SkillMetadata",
    "SkillMetadataSummary",
    "SkillResult",
    "ParameterSpec",
    "SkillError",
    "SkillErrorCode",
    "ERROR_METADATA",
    "SkillContext",
    "SkillRegistry",
    "skill",
    "SkillTelemetry",
    "SkillExecutionRecord",
    "SkillMetrics",
    "ExecutionStatus",
    "SkillExecutionTimer",
    "IdempotencyStore",
    "SkillTraceAttributes",
    "export_all",
    "write_descriptor",
    "read_descriptor",
    "validate_descriptor",
    "StrategyPlan",
    "StrategyStep",
    "load_all_summaries",
    "summaries_to_prompt",
    "list_skill_names",
]
