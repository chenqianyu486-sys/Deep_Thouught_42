"""Entity registry and cell-name validation pure functions.

Single source of truth for hierarchical cell instance name validation and
the canonical cell-name registry (SSOT + Pinned context layer).

This module is shared by:
- critical_path.parse_critical_path_cells  (ingress validation)
- tool_router.call_tool                    (LLM->tool boundary validation)
- state_space / context_snapshot           (Pinned layer rendering)
- phase_execute auto-inject                (registry-filtered injection)

All functions are pure: they take an EntityRegistry (or None) and return
results without side effects on MCP sessions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import OptimizerState

logger = logging.getLogger(__name__)

# Patterns that look like device site/coordinate names (NOT valid cell instances)
_DEVICE_SITE_PATTERNS = re.compile(
    r'^(SLICE_X\d+Y\d+'
    r'|DSP\d*_X\d+Y\d+'
    r'|RAMB\d*_X\d+Y\d+'
    r'|URAM\d*_X\d+Y\d+'
    r'|BUFGCE_X\d+Y\d+'
    r'|MMCM\d*_X\d+Y\d+'
    r'|PLL\d*_X\d+Y\d+'
    r'|X\d+Y\d+'
    r')$', re.IGNORECASE,
)

# Tool parameters that carry hierarchical cell instance names and must be
# validated at the LLM->tool boundary. Maps param name -> whether it is a
# list-of-paths (each element is itself a list of cell names) vs list-of-cells.
CELL_PARAM_SPECS: dict[str, bool] = {
    "cell_names": False,             # list[str] of cell names
    "critical_path_cells": False,    # list[str] of cell names
    "critical_paths": True,          # list[list[str]] of paths
    "hierarchical_input_pins": False,  # list[str] of pin names (hierarchical, validated loosely)
}

# Tools that accept hierarchical cell names and should be guarded by the router.
CELL_NAME_TOOLS: frozenset[str] = frozenset({
    "rapidwright_optimize_cell_placement",
    "rapidwright_analyze_critical_path_spread",
    "rapidwright_optimize_lut_input_cone",
    "rapidwright_execute_pblock_strategy",
    "rapidwright_analyze_pblock_region",
    "rapidwright_flatten_lut_cascade",
    "rapidwright_execute_combinational_rebalancing_strategy",
    "rapidwright_execute_lut_muxf_repack_strategy",
    "rapidwright_execute_muxf_tree_reorder_strategy",
})


def is_valid_cell_name(name: str) -> bool:
    """Check if a string looks like a valid hierarchical cell instance name.

    Valid cell names are hierarchical paths with at least one '/' separator
    and alphanumeric leaf names. Rejects pblock labels, device site
    coordinates, and other non-cell strings.

    This is the canonical implementation shared across the codebase.
    critical_path.py re-exports it as _is_valid_cell_name for backward compat.

    Examples of valid names:
      - layer0_reg/data_out_reg[41]
      - layer0_inst/layer0_N25_inst/data_out[76]_i_19
      - u_core/u_alu/reg_0

    Examples of invalid names:
      - pblock_tight          (PBLOCK constraint name)
      - pblock_io             (PBLOCK constraint name)
      - SLICE_X56Y0           (device site coordinate)
      - DSP48E2_X8Y0          (device site)
      - LUT6                  (bare type name, not hierarchical)
    """
    if not name or not isinstance(name, str):
        return False

    name_lower = name.lower()
    if name_lower.startswith("pblock") or "pblock" in name_lower:
        return False

    leaf = name.split("/")[-1] if "/" in name else name
    if _DEVICE_SITE_PATTERNS.match(leaf):
        return False

    if "/" not in name:
        return False

    for segment in name.split("/"):
        if not segment:
            return False
        if not re.search(r'[a-zA-Z0-9]', segment):
            return False

    return True


def is_valid_pin_name(name: str) -> bool:
    """Loosely validate a hierarchical pin name.

    Pin names are hierarchical (contain '/') and typically end with a pin
    suffix, e.g. 'module/inst/pin'. We require at least one '/' and reject
    device sites and pblock labels, but allow a broader character set than
    cell names since pins may include dots/special chars.
    """
    if not name or not isinstance(name, str):
        return False
    name_lower = name.lower()
    if "pblock" in name_lower:
        return False
    leaf = name.split("/")[-1] if "/" in name else name
    if _DEVICE_SITE_PATTERNS.match(leaf):
        return False
    if "/" not in name:
        return False
    for segment in name.split("/"):
        if not segment:
            return False
        if not re.search(r'[a-zA-Z0-9]', segment):
            return False
    return True


@dataclass
class CellRef:
    """A single registered cell with lightweight metadata."""
    canonical_name: str
    cell_type: str = ""
    location: str = ""
    source_path_idx: int = -1       # which critical path it came from (-1 = search)
    last_seen_iter: int = 0


@dataclass
class EntityRegistry:
    """Canonical, compression-resistant cell-name registry (SSOT).

    Lives on OptimizerState. Rebuilt/pinned into LLM context each turn
    (never enters MessageStore), so cell names survive compression.
    """
    cells: dict[str, CellRef] = field(default_factory=dict)
    by_module: dict[str, set[str]] = field(default_factory=dict)
    snapshot_version: int = 0       # incremented after design modification

    # ── Mutation API ──────────────────────────────────────────────

    def register_cell(
        self,
        name: str,
        cell_type: str = "",
        location: str = "",
        source_path_idx: int = -1,
        iteration: int = 0,
    ) -> None:
        """Register a single canonical cell name (idempotent)."""
        if not is_valid_cell_name(name):
            return
        existing = self.cells.get(name)
        if existing is not None:
            # Update metadata if richer
            if cell_type and not existing.cell_type:
                existing.cell_type = cell_type
            if location and not existing.location:
                existing.location = location
            existing.last_seen_iter = max(existing.last_seen_iter, iteration)
            return
        self.cells[name] = CellRef(
            canonical_name=name,
            cell_type=cell_type,
            location=location,
            source_path_idx=source_path_idx,
            last_seen_iter=iteration,
        )
        # Index by module (second path segment, matching state_space convention)
        parts = name.split("/")
        if len(parts) >= 2:
            module = parts[1]
            self.by_module.setdefault(module, set()).add(name)

    def register_cells_from_paths(
        self,
        paths: list[list[str]],
        iteration: int = 0,
    ) -> int:
        """Register all valid cell names from a list of critical paths.

    Args:
        paths: list of paths, each a list of cell names.
        iteration: iteration when these were observed.

    Returns:
        Number of newly registered cells.
    """
        before = len(self.cells)
        for path_idx, cells in enumerate(paths):
            if not cells:
                continue
            for cell_name in cells:
                self.register_cell(
                    cell_name,
                    source_path_idx=path_idx,
                    iteration=iteration,
                )
        return len(self.cells) - before

    def register_cells_from_entries(self, entries: list, iteration: int = 0) -> int:
        """Register cells from CriticalPathEntry objects (typed).

    Accepts the CriticalPathEntry dataclass from state.py. Uses node
    metadata (cell_type/location) when available for richer registry.
    """
        before = len(self.cells)
        for path_idx, entry in enumerate(entries):
            cells = getattr(entry, "cells", None) or []
            nodes = getattr(entry, "nodes", None) or []
            # Build name -> node metadata lookup
            node_meta = {}
            if nodes:
                for n in nodes:
                    nname = getattr(n, "name", "")
                    if nname:
                        node_meta[nname] = (
                            getattr(n, "cell_type", "") or "",
                            getattr(n, "location", "") or "",
                        )
            for cell_name in cells:
                ctype, loc = node_meta.get(cell_name, ("", ""))
                self.register_cell(
                    cell_name,
                    cell_type=ctype,
                    location=loc,
                    source_path_idx=path_idx,
                    iteration=iteration,
                )
        return len(self.cells) - before

    def mark_stale(self) -> None:
        """Increment snapshot_version after a design modification.

    This signals that previously registered cells may no longer exist
    (cells can be merged/split/renamed by opt_design/phys_opt). The LLM
    Pinned layer will display the new version so the LLM knows to re-fetch.
    """
        self.snapshot_version += 1

    def clear(self) -> None:
        self.cells.clear()
        self.by_module.clear()

    # ── Query API ─────────────────────────────────────────────────

    def contains(self, name: str) -> bool:
        return name in self.cells

    def top_n_cells(self, n: int = 30) -> list[str]:
        """Return up to n canonical cell names, prioritized by recency.

    Cells seen most recently (highest last_seen_iter) come first, breaking
    ties by source_path_idx (earlier critical paths = more important).
    """
        items = sorted(
            self.cells.items(),
            key=lambda kv: (-kv[1].last_seen_iter, kv[1].source_path_idx),
        )
        return [name for name, _ in items[:n]]

    def suggest(self, query: str, limit: int = 5) -> list[str]:
        """Return up to `limit` canonical names similar to `query`.

    Similarity is measured by shared path-segment overlap (leaf or parent
    module). This powers the rich-error feedback's "suggested names".
    """
        if not self.cells or not query:
            return []
        q_leaf = query.split("/")[-1] if "/" in query else query
        q_parts = set(query.split("/"))
        scored: list[tuple[int, str]] = []
        for name, ref in self.cells.items():
            score = 0
            leaf = name.split("/")[-1]
            if leaf == q_leaf:
                score += 10
            elif leaf.startswith(q_leaf) or q_leaf.startswith(leaf):
                score += 6
            parts = set(name.split("/"))
            shared = len(q_parts & parts)
            score += shared
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [name for _, name in scored[:limit]]


# ── Validation result ─────────────────────────────────────────────


@dataclass
class CellValidationResult:
    """Result of validating a list of cell names against the registry."""
    accepted: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)   # format-valid, not in registry
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    all_invalid: bool = False

    def to_rich_error(self, tool_name: str, registry: EntityRegistry | None) -> str:
        """Build a structured rejection/feedback message for the LLM."""
        import json as _json
        invalid_names = [name for name, _ in self.rejected]
        reasons = [{"name": name, "reason": reason} for name, reason in self.rejected]
        suggestions: list[str] = []
        if registry is not None and invalid_names:
            for bad in invalid_names[:3]:
                suggestions.extend(registry.suggest(bad, limit=3))
            # Dedupe, preserve order
            seen = set()
            suggestions = [s for s in suggestions if not (s in seen or seen.add(s))][:6]
            # Fallback: if no similar names found (e.g. device-site input
            # shares no segments with any cell), offer the most recently
            # seen canonical names so the LLM has valid targets to use.
            if not suggestions:
                suggestions = registry.top_n_cells(6)
        payload = {
            "tool": tool_name,
            "status": "rejected" if self.all_invalid else "partial",
            "reason": "invalid_cell_names",
            "invalid_names": invalid_names,
            "rejection_reasons": reasons,
            "unverified_names": self.unverified,
            "suggested_canonical_names": suggestions,
            "guidance": (
                "Cell names must be hierarchical paths (contain '/'), e.g. "
                "'u_core/u_alu/lut1'. Device sites (SLICE_X*, DSP*_X*) and "
                "bare type names (LUT6, FDRE) are NOT valid. Use names from "
                "the [CELL REGISTRY] section in your context. Re-issue the "
                "call with corrected names."
            ),
        }
        return _json.dumps(payload, ensure_ascii=False)


def classify_cell_name(name: str) -> str:
    """Classify a single cell-name string.

    Returns one of:
      - 'valid'         : format-valid hierarchical cell name
      - 'device_site'   : looks like a device site coordinate
      - 'pblock_label'  : contains 'pblock'
      - 'bare_type'     : no '/' separator (bare type or instance name)
      - 'empty'         : empty/None
    """
    if not name or not isinstance(name, str):
        return "empty"
    name_lower = name.lower()
    if "pblock" in name_lower:
        return "pblock_label"
    leaf = name.split("/")[-1] if "/" in name else name
    if _DEVICE_SITE_PATTERNS.match(leaf):
        return "device_site"
    if "/" not in name:
        return "bare_type"
    return "valid"


def validate_cell_list(
    names: list[str],
    registry: EntityRegistry | None,
    *,
    allow_unverified: bool = True,
) -> CellValidationResult:
    """Validate a flat list of cell names against the registry.

    Partial-pass + warn policy (confirmed decision):
      - format-valid + in registry      -> accepted
      - format-valid + not in registry  -> unverified (kept, allowed)
      - format-invalid                  -> rejected (stripped)
      - all invalid (no accepted/unverified) -> all_invalid=True

    Args:
        names: list of cell-name strings to validate.
        registry: EntityRegistry to check membership against (None = skip
            membership check, treat all format-valid as unverified).
        allow_unverified: when False, unverified names are also rejected
            (strict mode — not used in default policy).
    """
    result = CellValidationResult()
    for name in names:
        if not is_valid_cell_name(name):
            reason = classify_cell_name(name)
            result.rejected.append((name, reason))
            continue
        if registry is not None and registry.contains(name):
            result.accepted.append(name)
        else:
            if allow_unverified:
                result.unverified.append(name)
            else:
                result.rejected.append((name, "not_in_registry"))
    has_usable = bool(result.accepted) or bool(result.unverified)
    result.all_invalid = not has_usable and bool(names)
    return result


def validate_pin_list(
    names: list[str],
    registry: EntityRegistry | None,
) -> CellValidationResult:
    """Validate hierarchical pin names (looser than cell names)."""
    result = CellValidationResult()
    for name in names:
        if not is_valid_pin_name(name):
            reason = classify_cell_name(name)
            if reason == "valid":
                reason = "invalid_pin_format"
            result.rejected.append((name, reason))
            continue
        # Pin membership not tracked in cell registry; treat as unverified
        result.unverified.append(name)
    has_usable = bool(result.accepted) or bool(result.unverified)
    result.all_invalid = not has_usable and bool(names)
    return result


def validate_and_sanitize_cell_args(
    tool_name: str,
    arguments: dict,
    registry: EntityRegistry | None,
    *,
    allow_unverified: bool = True,
) -> tuple[dict, str | None]:
    """Validate & sanitize cell-name arguments at the LLM->tool boundary.

    Implements the partial-pass+warn policy. Returns:
      (sanitized_arguments, error_message_or_None)

    - When error_message is None: arguments are sanitized and safe to pass
      to the MCP tool (invalid names stripped; unverified kept).
    - When error_message is not None: ALL provided cell names were invalid.
      The caller should return error_message to the LLM WITHOUT calling MCP.
    """
    if tool_name not in CELL_NAME_TOOLS:
        return arguments, None

    sanitized = dict(arguments)
    accumulated_error: str | None = None

    for param, is_paths in CELL_PARAM_SPECS.items():
        if param not in sanitized:
            continue
        value = sanitized[param]
        if value is None:
            continue

        if param == "hierarchical_input_pins":
            names_list = value if isinstance(value, list) else []
            res = validate_pin_list(names_list, registry)
            kept = res.accepted + res.unverified
            if res.all_invalid and names_list:
                accumulated_error = res.to_rich_error(tool_name, registry)
                # Remove the param so we don't pass garbage downstream
                del sanitized[param]
                continue
            sanitized[param] = kept
            continue

        if is_paths:
            # critical_paths: list[list[str]]
            paths_val = value if isinstance(value, list) else []
            kept_paths: list[list[str]] = []
            total_rejected = 0
            total_names = 0
            for path in paths_val:
                if not isinstance(path, list):
                    continue
                total_names += len(path)
                res = validate_cell_list(path, registry, allow_unverified=allow_unverified)
                total_rejected += len(res.rejected)
                kept = res.accepted + (res.unverified if allow_unverified else [])
                if kept:
                    kept_paths.append(kept)
            if total_names > 0 and total_rejected == total_names:
                # every single name was invalid
                all_invalid_res = CellValidationResult(
                    rejected=[(n, classify_cell_name(n)) for path in paths_val if isinstance(path, list) for n in path if not is_valid_cell_name(n)],
                    all_invalid=True,
                )
                accumulated_error = all_invalid_res.to_rich_error(tool_name, registry)
                del sanitized[param]
                continue
            sanitized[param] = kept_paths
        else:
            # flat list[str]
            names_list = value if isinstance(value, list) else []
            res = validate_cell_list(names_list, registry, allow_unverified=allow_unverified)
            if res.all_invalid and names_list:
                accumulated_error = res.to_rich_error(tool_name, registry)
                del sanitized[param]
                continue
            sanitized[param] = res.accepted + (res.unverified if allow_unverified else [])

    return sanitized, accumulated_error


def extract_registry_cells_for_inject(
    registry: EntityRegistry,
    critical_paths_entries: list,
    *,
    max_cells: int = 50,
    max_paths: int = 10,
) -> list[str]:
    """Extract a deduplicated, registry-validated cell list for auto-inject.

    Prefers cells from the top critical paths (highest priority), then
    backfills from the registry's recently-seen cells. All returned names
    pass is_valid_cell_name and are canonical (in registry).

    This unifies the auto-inject strategy across all cell-name tools:
    pblock, lut_cascade, combinational strategies, and optimize_cell_placement.
    """
    cells: list[str] = []
    seen: set[str] = set()
    # 1) Critical path cells first (the actual timing hotspots)
    for entry in critical_paths_entries[:max_paths]:
        ec = getattr(entry, "cells", None) or []
        for cell_name in ec:
            if cell_name in seen:
                continue
            if not is_valid_cell_name(cell_name):
                continue
            seen.add(cell_name)
            cells.append(cell_name)
            if len(cells) >= max_cells:
                return cells
    # 2) Backfill from registry (search_cells-discovered cells not on paths)
    if len(cells) < max_cells:
        for name in registry.top_n_cells(max_cells * 2):
            if name in seen:
                continue
            seen.add(name)
            cells.append(name)
            if len(cells) >= max_cells:
                break
    return cells


def sync_search_cells_result(
    registry: EntityRegistry,
    raw_result: str,
    iteration: int = 0,
) -> int:
    """Register canonical cell names from a rapidwright_search_cells result.

    The tool returns {"cells": [{"name", "type", "placement"}, ...]}.
    Names are hierarchical (from RapidWright getName()) and pass validation.
    """
    import json as _json
    try:
        data = _json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except (ValueError, TypeError):
        return 0
    if not isinstance(data, dict):
        return 0
    cells = data.get("cells")
    if not isinstance(cells, list):
        return 0
    before = len(registry.cells)
    for c in cells:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or ""
        ctype = c.get("type") or ""
        placement = c.get("placement") or ""
        loc = placement if placement and placement != "unplaced" else ""
        registry.register_cell(name, cell_type=ctype, location=loc, iteration=iteration)
    return len(registry.cells) - before


def build_registry_snapshot_yaml(
    registry: EntityRegistry,
    *,
    phase: str = "",
    max_cells: int = 40,
    max_modules: int = 8,
) -> str:
    """Render the registry as a compact YAML block for the Pinned context layer.

    This is the compression-resistant snapshot injected as an independent
    user message right after the system message every turn. The LLM reads
    canonical cell names from here instead of reconstructing them from
    (compressed) tool outputs.
    """
    if not registry.cells:
        return (
            "[CELL REGISTRY]\n"
            "# No canonical cell names registered yet. Cell names will appear\n"
            "# here after vivado_extract_critical_path_cells or rapidwright_search_cells\n"
            "# is called. When calling cell-targeting tools, use hierarchical names\n"
            "# (containing '/') from this section.\n"
        )
    lines = ["[CELL REGISTRY]"]
    lines.append(f"# snapshot_version: {registry.snapshot_version}  # phase={phase or 'N/A'}")
    lines.append(f"# total_registered: {len(registry.cells)} cells across {len(registry.by_module)} modules")
    lines.append("canonical_cells:")

    top = registry.top_n_cells(max_cells)
    for name in top:
        ref = registry.cells[name]
        meta = []
        if ref.cell_type:
            meta.append(f"type={ref.cell_type}")
        if ref.location:
            meta.append(f"loc={ref.location}")
        meta_str = f"  # {' '.join(meta)}" if meta else ""
        lines.append(f"  - {name}{meta_str}")
    if len(registry.cells) > max_cells:
        lines.append(f"  # ... {len(registry.cells) - max_cells} more (use rapidwright_search_cells to query)")

    # Module index
    if registry.by_module:
        lines.append("module_index:")
        sorted_mods = sorted(
            registry.by_module.items(),
            key=lambda kv: -len(kv[1]),
        )[:max_modules]
        for mod, cell_set in sorted_mods:
            lines.append(f"  {mod}: {len(cell_set)} cells")
    lines.append(
        "# NOTE: Use these exact names when calling cell-targeting tools.\n"
        "# Device sites (SLICE_X*, DSP*_X*) and bare type names are NOT valid.\n"
    )
    return "\n".join(lines)
