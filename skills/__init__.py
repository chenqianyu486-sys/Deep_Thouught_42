# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Skill framework for FPGA optimization.

This module provides a standardized skill mechanism for defining,
registering, and invoking skills within the optimization system.

The framework follows the Skill Descriptor v3 specification:
    - Strongly-typed parameter contracts via JSON Schema
    - Standardized error envelopes for Agent-safe error handling
    - Idempotency and concurrency guards
    - Tracing and observability attributes
    - Machine-readable JSON descriptor files for each skill
"""

from skills.base import (
    Skill,
    SkillCategory,
    SkillMetadata,
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

# Import submodules to trigger @skill decorators
from skills import net_detour_optimization
from skills import smart_region_search
from skills import pblock_strategy
from skills import physopt_strategy
from skills import fanout_strategy

__all__ = [
    "Skill",
    "SkillCategory",
    "SkillMetadata",
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
]
