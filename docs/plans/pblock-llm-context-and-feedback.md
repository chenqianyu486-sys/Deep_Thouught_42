# PBLOCK 策略：LLM 可调参与上下文/反馈质量改进

## 背景

`dcp_optimizer_run-20260705_092133` 中 PBLOCK 完全未生效，暴露三类 LLM 侧问题：

1. **调参无效**：LLM 传 `resource_multiplier=1.2`，内部经 `compute_adaptive_resource_multiplier`（按 design size 改 1.2/1.5/1.8）+ fallback expansion 后变成 `2.0`。LLM 不知情、调参无法预测效果。
2. **上下文误导**：pblock 区域 `utilization_density=98.6%`（62740/63652），`capacity_ok=true` 且 advice 说 "You can safely proceed"。LLM 拿到虚假绿灯。
3. **反馈错误**：chain 工具 `route_design -reuse` 失败 → 回滚 → `delta=0.0` → 被当"策略无效"冷却（`Cooling down stalled strategy 'PBLOCK'`）。LLM 收到错误归因，误以为 PBLOCK 对此设计无效。

> 注：`-reuse` bug 本身已在 worktree 修复（constants.py + vivado_mcp_server.py，未提交），本次不重复，仅建议提交。

## 目标

让 LLM 能有效调参 pblock 策略，并拿到**充足、准确、客观**的上下文与反馈。不改变工具的客观判定语义（capacity_ok 仍表示"装得下"），而是补全信息、让 LLM 自主决策。

## 改动清单

### 组 A：可调参

#### A1. 新增 `max_utilization_density` 参数（LLM 可控余量）
- **位置**：`RapidWrightMCP/server.py`（schema）、`RapidWrightMCP/rapidwright_tools.py`（透传）、`skills/pblock_strategy.py`（判定）
- **默认 0.90**。pblock 选区域后若 `density > max_utilization_density`，置 `density_warning=true`，并在 advice 明确建议扩大区域或减少 target cells。
- **软约束**：不阻止执行（capacity_ok 仍 true，因为确实装得下），仅客观告知风险，由 LLM 决定是否继续。
- LLM 传 0.80 即可主动要求更低密度（用于高利用率设计避免拥塞）。

#### A2. multiplier 透明化
- **位置**：`skills/pblock_strategy.py` 返回 dict + message
- 返回 `input_multiplier`（LLM 传入的原值）、`final_multiplier`（adaptive 后的实际值）、`multiplier_transform`（变换原因，如 `"adaptive: 1.2→1.8 (small design <10% device)"` 或 `"fallback_expansion"`）。
- 现有 `resource_multiplier` 字段保留（=final），向后兼容。
- message 用 `final_multiplier` 并标注变换路径。

### 组 B：上下文质量

#### B1. 返回 dict 增加结构化字段
- **位置**：`skills/pblock_strategy.py` 返回 dict（行 429-450）
- 新增：
  - `utilization_density`: float（0.0-1.0）
  - `density_warning`: bool（`density > max_utilization_density`）
  - `capacity_basis`: `"initial_region" | "fallback_expansion"`
  - `input_multiplier` / `final_multiplier` / `multiplier_transform`
  - `region_selection_reason`: 简述（`"center_of_mass"` / `"fallback_expanded_to_capacity"`）
- 这些字段让 LLM 程序化判断，而非从 advice 文本里解析。

#### B2. advice 按 density 分级（修 `_build_advice_sufficient`）
- **位置**：`skills/pblock_strategy.py:139-144`
- 当前：`capacity_ok=true` 时无脑返回 "You can safely proceed"。
- 改为按 density 分级：
  - `density > 0.90`：警告 "Region nearly full (X%); place_design will likely congest and worsen timing. Consider higher resource_multiplier or fewer target cells."
  - `0.80 < density ≤ 0.90`：提示 "Density high (X%); IS_SOFT=1 recommended. Monitor post-place WNS closely."
  - `density ≤ 0.80`：保留 "safely proceed"。
- 需要把 `utilization_density` 传入 `_build_advice_sufficient`（当前无参）。

#### B3. message 客观化
- **位置**：`skills/pblock_strategy.py:396-405`
- message 的 `x{resource_multiplier}` 改用 `final_multiplier`，并附 `（input {input}→final {final}: {reason}）`。
- density 高时 message 末尾加 `density {x:.1%}, WARNING`。

### 组 C：反馈准确性

#### C1. chain 失败不误冷却（核心，改动最小）
- **位置**：`optimizer/nodes/subgraphs/phase_evaluate.py` `_cool_down_current_strategy_if_stalled`（行 94-167）
- **问题**：chain 失败→回滚→`delta=0.0`（非 None）→ 走 `delta<=0` 分支（行 147）冷却。skip-cooldown 只在 `delta is None`（行 124）触发，永远不命中 chain-回滚场景。且行 149-159 把 chain 失败的 `vivado_route_design`（不在 `STRATEGY_TOOL_NAMES`）当 auxiliary error，仍冷却。
- **改动**：在 `delta <= 0` 分支开头（行 147 前）加检查：
  ```python
  # Chain-tool failure caused rollback → delta=0 is a rollback artifact,
  # not a strategy verdict. The strategy never got a fair run; skip cooldown
  # so it gets a retriable tool_error treatment (matches phase_execute.py
  # chain-failure intent at line 1986).
  chain_errors = [e for e in state.iteration.tool_errors if e.get("chain")]
  if chain_errors:
      tools_str = ", ".join(e.get("tool", "?") for e in chain_errors)
      logger.info(
          f"[EVALUATE] Skipping cooldown for '{strategy}' — "
          f"chain tool(s) {tools_str} failed and design was restored to baseline "
          f"(delta={delta:+.3f}ns is rollback artifact, not strategy verdict). "
          f"Strategy remains retriable."
      )
      return False
  ```
- 与 phase_execute.py:1986 现有注释意图对齐（"chain failure = tool_error retriable, TTL=0"）。

#### C2. chain 失败保留 place 后 WNS 诊断（增强，可选）
- **位置**：`optimizer/nodes/subgraphs/phase_execute.py` `_execute_chain_actions`（行 1984 step_failed 处）
- route_design 失败回滚前，先 `report_timing_summary` 拿 place 后 WNS，记入 `tool_errors` 反馈给 LLM。
- 让 LLM 区分 "place 改善了但 route 失败" vs "place 也没改善"。
- **风险**：place 后未路由的 WNS 不准（偏乐观）。仅作诊断反馈，明确标注 "place-only WNS (unrouted, approximate)"，不作为 verdict。

#### C3. critical_path_cells 覆盖告知 LLM
- **位置**：`optimizer/nodes/subgraphs/phase_execute.py:148-149`
- 当前覆盖 LLM cells 只在 log WARNING，LLM 不知情。
- 改为同时 `add_message`：`"[DATA] Overrode your critical_path_cells with {N} verified state cells (reason: data quality guard). Cells used: [...]."`
- 让 LLM 知道输入被覆盖及原因，避免重复传无效 cells。

## 不做（边界）

- **不改 capacity_ok 语义**：保留"装得下=true"的客观事实，靠 density 字段 + 分级 advice 让 LLM 自主判断。避免工具替 LLM 决策。
- **不改 PBLOCK 强制首选规则**（phase_select_strategy.py:51）：属策略选择层，不在本次"上下文/调参"范围。建议后续单独加设计规模门控。
- **不重写 adaptive multiplier 逻辑**：仅透明化变换过程。
- **不重复修 `-reuse` bug**：worktree 已修复，本次仅建议提交。

## 验证

1. **单测**（`skills/test_pblock_strategy.py`）：
   - density 字段正确计算
   - multiplier 变换链（input/final/transform）正确返回
   - `max_utilization_density` 参数生效（传 0.80 时 density_warning 在 0.85 触发）
   - advice 分级文本正确
2. **回归测试**（`optimizer/test_graph.py`）：
   - 新增 `test_chain_failure_no_cooldown`：tool_errors 含 `chain=True` 记录 + delta=0 → 不冷却（复现日志场景）
   - 现有 delta=0（无 chain error）冷却用例保持通过
3. **端到端**（重跑 dcp_optimizer）：density 高时 LLM 收到 density_warning + 分级 advice；chain 失败时 PBLOCK 不被误冷却。

## 优先级

- **核心（必做）**：A1、A2、B1、B2、B3、C1、C3 —— 直接对应"可调参 + 上下文 + 反馈"三个关键词。
- **增强（可选）**：C2 —— 锦上添花但有风险（unrouted WNS 不准），需谨慎评估，本次暂不做。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `skills/pblock_strategy.py` | A2/B1/B2/B3：返回字段、advice 分级、message |
| `RapidWrightMCP/server.py` | A1：schema 新增 `max_utilization_density` |
| `RapidWrightMCP/rapidwright_tools.py` | A1：参数透传 |
| `optimizer/nodes/subgraphs/phase_evaluate.py` | C1：chain 失败不误冷却 |
| `optimizer/nodes/subgraphs/phase_execute.py` | C3（cells 覆盖告知）；C2 暂不做 |
| `skills/test_pblock_strategy.py` | 单测 |
| `optimizer/test_graph.py` | 回归测试 |
