# PBLOCK 策略三个矛盾修复方案

## 根因总结（已对照代码核验）

| 矛盾 | 根因 | 代码位置 |
|---|---|---|
| 三（最关键）| "改进"判定有三套阈值：best_保存用 `>0`、EXECUTE 链后 verdict 用 `>0.001`、冷却/无进展用 `>0.050`。PBLOCK +0.049ns 被 best_保存却因 `0.049 ≤ 0.050` 被冷却+计无进展 | [phase_evaluate.py:40,136,289](optimizer/nodes/subgraphs/phase_evaluate.py#L40)；[phase_execute.py:1052,1411](optimizer/nodes/subgraphs/phase_execute.py#L1052) |
| 一 | 8 条链全带 `reuse:True`，但其中 5 条 route 前无布线（open_checkpoint/opt+place 后），是死配置；A.5 的 `total_nets=0` 解析 bug 恰好掩盖 | [constants.py:425,443,455,468,479,489,502,516](optimizer/pure/constants.py#L425)；[phase_execute.py:1900-1906](optimizer/nodes/subgraphs/phase_execute.py#L1900) |
| 二 | PBLOCK 链 step1 全局 `place_design -unplace` + `create_and_apply_pblock(apply_to=current_design)`（约束全部 cell）= 核弹级重做 | [constants.py:415-428](optimizer/pure/constants.py#L415)；[vivado_mcp_server.py:1583-1594](VivadoMCP/vivado_mcp_server.py#L1583) |

---

## 矛盾三：epsilon 阈值一致性（先做，低风险）

**思路 T3e**：把"正收益"和"显著收益"分开。任何 `delta > 0`（best 已更新）都不冷却、不计无进展；只有 `delta ≤ 0` 才算停滞。

**改动**：
1. [phase_evaluate.py:136](optimizer/nodes/subgraphs/phase_evaluate.py#L136) `_cool_down_current_strategy_if_stalled`：
   - `if delta > STRATEGY_IMPROVEMENT_EPSILON_NS:` → `if delta > 0:`
   - 更新函数 docstring 语义（"measured no-improvement" 改为 `delta ≤ 0`）
2. [phase_evaluate.py:288-292](optimizer/nodes/subgraphs/phase_evaluate.py#L288) 无进展计数：
   - `delta > EPSILON` → 重置为 0（显著收益）
   - `delta ≤ 0` → `+= 1`（真停滞才累计，达 3 强制 SWITCH_STRATEGY）
   - `0 < delta ≤ EPSILON` → 既不重置也不累计（边际正收益，不惩罚）
3. [test_graph.py TestStrategyCooldown](optimizer/test_graph.py#L312)：
   - 新增 `test_switch_keeps_marginal_improvement_available`：delta=0.049 → `blocked_strategies == []`（复现用户场景的回归测试）
   - 现有 delta=0.0（冷却）和 delta=0.060（不冷却）用例保持通过

**验证**：`make test-quick`（纯函数单测，我可执行）

**保留 EPSILON**：仍用于"显著收益"判定（重置无进展计数），不删除常量。

---

## 矛盾一：route reuse 死配置（中风险，当前几乎零行为变化）

**改动**：移除 5 条死配置链 route 步的 `reuse: True`（route 前无布线）：
- [constants.py:443](optimizer/pure/constants.py#L443) fanout（open_checkpoint→place→route）
- [constants.py:455](optimizer/pure/constants.py#L455) opt_design（opt→place→route）
- [constants.py:468](optimizer/pure/constants.py#L468) combinational_rebalancing
- [constants.py:479](optimizer/pure/constants.py#L479) lut_muxf_repack
- [constants.py:516](optimizer/pure/constants.py#L516) flatten_lut_cascade

**保留 `reuse: True`**：
- [constants.py:489,502](optimizer/pure/constants.py#L489) muxf_tree_reorder / physopt_strategy（phys_opt_design 保留布线，reuse 有效）
- [constants.py:425](optimizer/pure/constants.py#L425) PBLOCK（依赖矛盾二：局部 unplace 后设计保留布线，reuse 变有效）

**注**：当前因 A.5（`total_nets=0`）guard 本就回退到常规布线，移除 `reuse:True` 几乎无行为变化；这是为 A.5 修复后的正确性兜底。guard 逻辑本身不动。

**验证**：grep `reuse` 确认 SKILL_CHAIN_ACTIONS 仅剩 3 处（425/489/502）；`make test-quick`。

---

## 矛盾二：局部 unplace 改造（最大，需实测验证）

**目标流程**（标准增量 pblock 流）：
```
unplace_cells [关键cell]  →  create_and_apply_pblock(只约束关键cell)
  →  place_design(Explore, 增量布局仅关键cell)  →  route_design(reuse, 增量布线)
  →  report_timing_summary
```
只移动关键路径 cell，不破坏其他路径的布局布线。

**改动**：
1. **VivadoMCP 新增 `unplace_cells` 工具**（[vivado_mcp_server.py](VivadoMCP/vivado_mcp_server.py)）：
   - 参数 `cells: list[str]`，构造 `unplace_cells [get_cells [list <tcl_quote每个cell>]]`
   - `unplace_cells` 不在 [BLOCKED_TCL_COMMANDS](VivadoMCP/tcl_security.py#L16)，已放行
   - 加 list_tools 条目 + 错误检测（`^ERROR: \[`）
2. **`create_and_apply_pblock` 新增 `cells: list[str]` 参数**（[vivado_mcp_server.py:1518](VivadoMCP/vivado_mcp_server.py#L1518)）：
   - 若提供且非空：`add_cells_to_pblock pb [get_cells [list <tcl_quote>]]`（pblock 只约束关键 cell）
   - 若为空：回退现有 `apply_to=current_design`（全部 cell）
3. **`generate_pblock_plan` 返回 `critical_path_cells`**（[pblock_strategy.py:429-444](skills/pblock_strategy.py#L429)）：把它收到的 `critical_path_cells` 原样加入结果 dict，供链使用
4. **PBLOCK 链改写**（[constants.py:415-428](optimizer/pure/constants.py#L415)）：
   - step1：`vivado_place_design{directive:unplace}` → `vivado_unplace_cells`，`args_from_skill: {cells: critical_path_cells}`
   - step2：`create_and_apply_pblock` 增加 `args_from_skill: {cells: critical_path_cells}`
   - step3/4/5：`place_design(Explore)` / `route_design(reuse:True)` / `report_timing_summary` 不变
5. **单测**：[VivadoMCP/test_vivado_mcp.py](VivadoMCP/test_vivado_mcp.py) 加 `unplace_cells` 工具测试

**已知局限（本次不处理）**：pblock 区域仍按 LLM 提供的全设计 `target_lut_count` 计尺寸，区域偏大 → 对少量关键 cell 的软约束偏弱，聚类效果有限。**按关键 cell 资源足迹重设区域尺寸**是后续增强（需统计关键 cell 的 LUT/FF 数），不在本次范围。

**风险**：局部 unplace 后 placer 在 pblock 内可能摆不下关键 cell（软约束可溢出兜底）；增量 route 可能因关键 cell 移动产生新拥塞。需实测对比。

**验证**：
- 我可执行：`make test-quick` + `make test-unit`（含新 MCP 工具单测）
- 用户需手动执行：`make run_test_v2`（完整 P&R，对比 PBLOCK 策略 WNS 收益与耗时 vs 改前）

---

## 实施顺序

矛盾三 → 矛盾一 → 矛盾二（风险递增，每步独立可验证、可单独回滚）。

## 文档同步（完成后）

按 [CLAUDE.md](CLAUDE.md) 要求更新三文档：
- README.md：冷却阈值、PBLOCK 链、route reuse 说明
- PROJECT_TREE_AND_DATA_FLOW.md：附录 A 补充矛盾一/三修复
- architecture.md：§冷却逻辑、§SKILL_CHAIN_ACTIONS、§Layer 3 unplace 回滚（局部 unplace）

## 不在本次范围

- 附录 A.5 `route_status.total_nets=0` 解析 bug（独立 issue，影响 reuse guard 实际激活）
- 矛盾二的区域按关键 cell 尺寸重设（后续增强）