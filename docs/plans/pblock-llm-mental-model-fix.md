# P1 修复计划：消除 LLM PBLOCK 心智模型错配

## 目标

两个 LLM 可见的文本文件仍然将 PBLOCK 描述为全局操作（"unplace everything, pblock whole design"），而这与 2026 年 07 月 04 日链式调用重构后的实际行为相悖，即只对关键路径单元进行局部 unplace 和局部 pblock。修复是纯粹的文档/字符串更新——不涉及逻辑变更。

## P1 已由 P0 处理（无需重复操作）

- ✅ `ParameterSpec` 文档已更新（`critical_path_cells` 现在写着 "BIND to the pblock"，而不仅仅是 "centering"）
- ✅ `next_steps` 文本已更新（`unplace_cells(cells=critical_path_cells)` 而非 `place_design -unplace`）
- ✅ `is_soft_recommended` / `sizing_basis` / `bound_resources` / `bound_cell_count` 现在已返回

## 剩余变更

### 1. `strategy_library.py` 第 40-52 行 — PBLOCK 策略顺序

LLM 在 `strategy_catalog` 中看到的具体顺序如下（通过 `get_strategy_catalog()` → 状态空间仪表盘 → `SELECT_STRATEGY` 阶段注入）：

当前（已过时）：
- 第 46 行：`"step": "place_design -unplace"`（全局 unplace）
- `create_and_apply_pblock` 参数中缺少 `cells=critical_path_cells`
- 没有任何关于绑定单元大小调整或硬 pblock 的说明

更新的顺序：
```python
"sequence": [
    {"step": "report_utilization_for_pblock", "platform": "Vivado", "params": None,
     "note": "Get current resource counts (used for adaptive multiplier classification only)"},
    {"step": "analyze_pblock_region / execute_pblock_strategy", "platform": "RapidWright",
     "params": {"critical_path_cells": "bound cells from critical paths", "resource_multiplier": "default 1.2x"},
     "note": "Region is sized for the BOUND cells (critical_path_cells x multiplier), NOT the whole design. is_soft follows true bound-cell density → low density → IS_SOFT=0 (hard pblock, real constraint)."},
    {"step": "unplace_cells(cells=critical_path_cells)", "platform": "Vivado",
     "note": "AUTO-CHAINED: local unplace of bound cells only. Rest of design stays placed/routed → incremental P&R."},
    {"step": "create_and_apply_pblock", "platform": "Vivado",
     "params": {"ranges": "pblock_ranges from skill", "cells": "critical_path_cells", "is_soft": "from is_soft_recommended"},
     "note": "AUTO-CHAINED: binds ONLY the critical_path_cells to the pblock region (local pblock)."},
    {"step": "place_design", "platform": "Vivado", "params": {"directive": "Explore"},
     "note": "AUTO-CHAINED: re-place the unplaced bound cells within the pblock. Other cells stay placed."},
    {"step": "route_design", "platform": "Vivado", "params": {"directive": "NoTimingRelaxation"},
     "note": "AUTO-CHAINED: re-route. Vivado auto-reuses prior routing for unchanged nets."},
    {"step": "report_timing_summary", "platform": "Vivado", "params": None,
     "note": "AUTO-CHAINED: verify WNS after PBLOCK."},
],
```

### 2. `prepare_context.py` 第 135-148 行 — FORMAT_GUARD PBLOCK 部分

文本 "it therefore tears down and rebuilds the existing place/route" 明确描述了全局卸载的行为——这是一个事实错误。同样，"place_design -unplace → create_and_apply_pblock → place_design → route_design" 遗漏了关键的 `cells=critical_path_cells` 且缺少了 `create_and_apply_pblock`。

新文本：
```
PBLOCK AUTO-CHAIN BEHAVIOR (LOCAL pblock on critical path cells):
  rapidwright_execute_pblock_strategy auto-chains: unplace_cells(cells=critical_path_cells) →
  create_and_apply_pblock(cells=critical_path_cells, is_soft=is_soft_recommended) →
  place_design → route_design → report_timing_summary.
  KEY: Only the critical_path_cells (~50 cells from the Dashboard) are unplaced and
  bound to the pblock. The remaining 99%+ of the design stays placed/routed — this is
  INCREMENTAL P&R, not a full tear-down. Vivado auto-reuses prior routing.
  
  The pblock region is SIZED for the bound cells (bound cell resources × resource_multiplier),
  NOT the whole design. is_soft follows the BOUND cells' true density — when only a few
  cells are bound, density is low and IS_SOFT=0 (hard pblock).
  
  Very small WNS improvements (e.g., <0.05ns) may be P&R random noise rather than pblock
  effect. If PBLOCK yields delta ≈ 0, do NOT re-select it; switch strategies.

PBLOCK MANDATORY VIVADO FLOW:
  The RapidWright PBLOCK tool only plans the pblock region — it does NOT modify the
  design. The auto-chain handles all Vivado steps for you; do NOT call Vivado tools
  manually during PBLOCK execution. See the result's sizing_basis / bound_resources /
  bound_cell_count fields to understand region sizing.
```

### 3. 测试（无需变更）

- `tests/test_context_engineering_fixes.py`：检查的是 "PBLOCK AUTO-CHAIN" 标题行（未变更），而非正文内容
- `optimizer/test_pure.py`：测试的是来自 `constants.py` 的链定义，而非这些用户可见的字符串

## 验证

1. `python3 skills/test_pblock_strategy.py` — 仍然通过
2. `python3 -m pytest tests/test_context_engineering_fixes.py -q` — 仍然通过（检查 "PBLOCK AUTO-CHAIN" 的包含/排除）
3. `python3 -m pytest optimizer/test_pure.py -q` — 仍然通过（126 个）
4. `python3 skills/validate_descriptors.py` — 仍然通过（所有通过）