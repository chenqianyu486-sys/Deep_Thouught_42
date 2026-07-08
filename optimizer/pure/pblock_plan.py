"""Pure PBLOCK planning contracts and deterministic selection helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


PBLOCK_LOCAL_MODE = "local_bound_cells"
PBLOCK_GLOBAL_MODE = "global_replacement"
PBLOCK_UNPLACE_LOCAL = "local_cells"
PBLOCK_UNPLACE_GLOBAL = "global"
PBLOCK_EXECUTE_DEFAULT_RESOURCE_MULTIPLIER = 2.0

LOCAL_PLACE_ONLY_THRESHOLD = 0.03
GLOBAL_PLACE_ONLY_THRESHOLD = 0.10


@dataclass
class PblockExecutionPlan:
    """Frozen PBLOCK execution plan shared across phases."""

    plan_mode: str
    candidate_id: str
    pblock_name: str
    pblock_ranges: str
    resource_multiplier: float
    target_lut_count: int
    target_ff_count: int
    target_dsp_count: int
    target_bram_count: int
    bind_cells_to_pblock: bool
    unplace_mode: str
    is_soft: bool
    place_directive: str
    route_directive: str
    reference_col: int | None = None
    reference_row: int | None = None
    selection_reason: str = ""
    fallback_reason: str | None = None
    critical_path_cells_snapshot: list[str] = field(default_factory=list)
    capacity_ok: bool = True
    estimated_resources: dict[str, int] = field(default_factory=dict)
    region: dict[str, Any] = field(default_factory=dict)
    utilization_density: float | None = None
    bound_resources: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def coerce_pblock_plan(data: Any) -> PblockExecutionPlan | None:
    """Best-effort conversion from dict payloads into the typed plan."""
    if isinstance(data, PblockExecutionPlan):
        return data
    if not isinstance(data, dict):
        return None
    required = (
        "plan_mode",
        "candidate_id",
        "pblock_name",
        "pblock_ranges",
        "resource_multiplier",
        "target_lut_count",
        "target_ff_count",
        "target_dsp_count",
        "target_bram_count",
        "bind_cells_to_pblock",
        "unplace_mode",
        "is_soft",
        "place_directive",
        "route_directive",
    )
    if any(key not in data for key in required):
        return None
    return PblockExecutionPlan(
        plan_mode=str(data["plan_mode"]),
        candidate_id=str(data["candidate_id"]),
        pblock_name=str(data["pblock_name"]),
        pblock_ranges=str(data["pblock_ranges"]),
        resource_multiplier=float(data["resource_multiplier"]),
        target_lut_count=int(data["target_lut_count"]),
        target_ff_count=int(data["target_ff_count"]),
        target_dsp_count=int(data.get("target_dsp_count", 0)),
        target_bram_count=int(data.get("target_bram_count", 0)),
        bind_cells_to_pblock=bool(data["bind_cells_to_pblock"]),
        unplace_mode=str(data["unplace_mode"]),
        is_soft=bool(data["is_soft"]),
        place_directive=str(data["place_directive"]),
        route_directive=str(data["route_directive"]),
        reference_col=_coerce_optional_int(data.get("reference_col")),
        reference_row=_coerce_optional_int(data.get("reference_row")),
        selection_reason=str(data.get("selection_reason", "")),
        fallback_reason=(
            None if data.get("fallback_reason") in (None, "") else str(data["fallback_reason"])
        ),
        critical_path_cells_snapshot=list(data.get("critical_path_cells_snapshot") or []),
        capacity_ok=bool(data.get("capacity_ok", True)),
        estimated_resources=dict(data.get("estimated_resources") or {}),
        region=dict(data.get("region") or {}),
        utilization_density=_coerce_optional_float(data.get("utilization_density")),
        bound_resources=dict(data.get("bound_resources") or {}),
    )


def coerce_pblock_plans(items: Iterable[Any] | None) -> list[PblockExecutionPlan]:
    plans: list[PblockExecutionPlan] = []
    for item in items or []:
        plan = coerce_pblock_plan(item)
        if plan is not None:
            plans.append(plan)
    return plans


def find_plan_by_candidate_id(
    plans: Iterable[PblockExecutionPlan | dict[str, Any]],
    candidate_id: str | None,
) -> PblockExecutionPlan | None:
    if not candidate_id:
        return None
    for item in plans:
        plan = coerce_pblock_plan(item)
        if plan is not None and plan.candidate_id == candidate_id:
            return plan
    return None


def extract_selected_plan_from_payload(payload: Any) -> PblockExecutionPlan | None:
    """Resolve the selected/recommended plan from a PBLOCK skill payload."""
    if not isinstance(payload, dict):
        return None
    for key in ("selected_pblock_plan", "frozen_pblock_plan"):
        plan = coerce_pblock_plan(payload.get(key))
        if plan is not None:
            return plan
    recommended_id = payload.get("recommended_candidate_id")
    candidate_plans = payload.get("candidate_plans")
    return find_plan_by_candidate_id(candidate_plans or [], recommended_id)


def is_non_degenerate_local_plan(plan: PblockExecutionPlan | dict[str, Any] | None) -> bool:
    typed = coerce_pblock_plan(plan)
    if typed is None or typed.plan_mode != PBLOCK_LOCAL_MODE:
        return False
    columns_used = int(typed.region.get("columns_used") or typed.region.get("col_count") or 0)
    if columns_used < 2:
        return False
    bound_luts = int(typed.bound_resources.get("luts", 0))
    bound_ffs = int(typed.bound_resources.get("ffs", 0))
    if bound_luts < max(20, int(typed.target_lut_count * 0.05)):
        return False
    if bound_ffs < max(10, int(typed.target_ff_count * 0.10)):
        return False
    if typed.utilization_density is not None and typed.utilization_density < 0.10:
        return False
    return typed.capacity_ok


def recommend_pblock_plan(
    candidate_plans: Iterable[PblockExecutionPlan | dict[str, Any]],
    *,
    critical_path_reference: tuple[int | None, int | None] | None = None,
) -> tuple[PblockExecutionPlan | None, list[PblockExecutionPlan]]:
    """Choose the recommended frozen plan using deterministic policy."""
    plans = coerce_pblock_plans(candidate_plans)
    if not plans:
        return None, []

    local_plans = [plan for plan in plans if plan.plan_mode == PBLOCK_LOCAL_MODE]
    global_plans = [plan for plan in plans if plan.plan_mode == PBLOCK_GLOBAL_MODE]

    ordered_globals = sorted(
        global_plans,
        key=lambda plan: _global_plan_rank(plan, critical_path_reference),
    )

    if local_plans and is_non_degenerate_local_plan(local_plans[0]):
        ordered = [local_plans[0], *ordered_globals]
        return ordered[0], ordered

    ordered = [*ordered_globals, *local_plans]
    if ordered:
        return ordered[0], ordered
    return None, []


def order_candidate_execution_plans(
    candidate_plans: Iterable[PblockExecutionPlan | dict[str, Any]],
    *,
    recommended_candidate_id: str | None = None,
    attempted_candidate_ids: Iterable[str] | None = None,
) -> list[PblockExecutionPlan]:
    """Return execution order: recommended first, then remaining untried candidates."""
    plans = coerce_pblock_plans(candidate_plans)
    attempted = set(attempted_candidate_ids or [])
    recommended = find_plan_by_candidate_id(plans, recommended_candidate_id)
    ordered: list[PblockExecutionPlan] = []
    if recommended is not None and recommended.candidate_id not in attempted:
        ordered.append(recommended)
    for plan in plans:
        if plan.candidate_id in attempted:
            continue
        if recommended is not None and plan.candidate_id == recommended.candidate_id:
            continue
        ordered.append(plan)
    return ordered


def get_place_only_screening_threshold(plan_mode: str) -> float:
    if plan_mode == PBLOCK_GLOBAL_MODE:
        return GLOBAL_PLACE_ONLY_THRESHOLD
    return LOCAL_PLACE_ONLY_THRESHOLD


def should_route_pblock_after_place(
    plan: PblockExecutionPlan | dict[str, Any] | None,
    place_only_delta: float | None,
    *,
    threshold: float | None = None,
) -> bool:
    """Return whether a PBLOCK candidate should continue from place to route.

    The hard threshold keeps the planned screening behavior. For global
    replacement, however, a neutral place-only WNS is still worth routing when
    the region has capacity and is not near-full; the historical strong
    baseline gained after route, while place-only timing often stayed flat.
    """
    typed = coerce_pblock_plan(plan)
    if typed is None or place_only_delta is None:
        return True

    required = (
        get_place_only_screening_threshold(typed.plan_mode)
        if threshold is None else float(threshold)
    )
    if place_only_delta >= required:
        return True

    if (
        typed.plan_mode == PBLOCK_GLOBAL_MODE
        and place_only_delta >= 0.0
        and typed.capacity_ok
        and _global_density_bucket(typed) <= 1
    ):
        return True

    return False


def should_keep_strategy_unblocked(best_routed_delta: float | None, epsilon: float = 0.001) -> bool:
    return best_routed_delta is not None and best_routed_delta > epsilon


def plan_requires_execution_rebuild(
    plan: PblockExecutionPlan | dict[str, Any] | None,
    *,
    execute_resource_multiplier: float = PBLOCK_EXECUTE_DEFAULT_RESOURCE_MULTIPLIER,
) -> bool:
    """Return True when a frozen plan is too weak for execute-time PBLOCK use."""
    typed = coerce_pblock_plan(plan)
    if typed is None:
        return True
    if not typed.pblock_ranges:
        return True
    if typed.plan_mode != PBLOCK_GLOBAL_MODE:
        return False
    return typed.resource_multiplier < execute_resource_multiplier


def _global_plan_rank(
    plan: PblockExecutionPlan,
    critical_path_reference: tuple[int | None, int | None] | None,
) -> tuple[int, int, int, int, int]:
    estimated = plan.estimated_resources
    target_sum = (
        int(plan.target_lut_count)
        + int(plan.target_ff_count)
        + int(plan.target_dsp_count)
        + int(plan.target_bram_count)
    )
    estimate_sum = (
        int(estimated.get("luts", 0))
        + int(estimated.get("ffs", 0))
        + int(estimated.get("dsps", 0))
        + int(estimated.get("brams", 0))
    )
    surplus = max(0, estimate_sum - target_sum)
    columns_used = int(plan.region.get("columns_used") or 0)
    density_bucket = _global_density_bucket(plan)
    distance = _distance_from_reference(plan, critical_path_reference)
    return (0 if plan.capacity_ok else 1, density_bucket, surplus, columns_used, distance)


def _global_density_bucket(plan: PblockExecutionPlan) -> int:
    """Rank loose replacement windows first; near-full regions congest.

    The strong-baseline winner (run-20260706_165117, SLICE_X0Y0:X54Y299)
    ran at ~0.24 target/capacity: wide enough for the placer to solve
    timing, narrow enough to force compaction. Density above ~0.85 produced
    congestion warnings and flat place-only timing; density below ~0.10
    approaches a whole-device no-op.
    """
    density = _coerce_optional_float(plan.utilization_density)
    if density is None:
        return 1
    if 0.15 <= density <= 0.60:
        return 0
    if 0.10 <= density <= 0.85:
        return 1
    return 2


def _distance_from_reference(
    plan: PblockExecutionPlan,
    critical_path_reference: tuple[int | None, int | None] | None,
) -> int:
    if not critical_path_reference:
        return 0
    ref_col, ref_row = critical_path_reference
    if ref_col is None and ref_row is None:
        return 0
    plan_col = _coerce_optional_int(plan.region.get("center_col")) or plan.reference_col
    plan_row = _coerce_optional_int(plan.region.get("center_row")) or plan.reference_row
    distance = 0
    if ref_col is not None and plan_col is not None:
        distance += abs(int(plan_col) - int(ref_col))
    if ref_row is not None and plan_row is not None:
        distance += abs(int(plan_row) - int(ref_row))
    return distance


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _coerce_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None
