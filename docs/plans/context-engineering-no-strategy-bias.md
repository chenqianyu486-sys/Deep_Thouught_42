# 上下文工程改进：去除策略强制/暗示，提升 CUSTOM 自由编排

## 目标（用户四点诉求）
1. 不再鼓励 LLM 依赖 skill
2. 策略无效时 LLM 可手动编排工具（CUSTOM 一等选项 + 失效回退）
3. 不强制或暗示策略选择（移除 PBLOCK 硬覆盖；**保留** deepen/hint 机制——用户确认）
4. LLM 依据客观数据自行决策（剥离主观策略先验，保留客观工具事实）

## 用户澄清结论
- PBLOCK 迭代1硬覆盖 → **完全移除**
- `_auto_deepen_hint` 与 `next_strategy_hint` → **两者保留**（deepen 鼓励深耕有效策略；hint 是 LLM 自身选择）
- CUSTOM 定位 → **一等对等选项 + 失效回退**

---

## 改动清单

### 1. 移除迭代1 PBLOCK 硬覆盖（逻辑，唯一非提示词改动）
文件：[phase_select_strategy.py:89-107](optimizer/nodes/subgraphs/phase_select_strategy.py#L89-L107)
- 删除 `_pblock_blocked` / `_strategy_was_already_selected` 判定块及 `state.strategy.current_strategy = "PBLOCK"` 覆盖分支与三条 `Override` 日志
- **保留** `_get_permanently_blocked_strategies` 等 TTL 阻断（失败策略冷却，非强制选择）
- 已确认无测试断言该覆盖行为（`test_context_engineering_fixes.py` 等仅手动 set `current_strategy="PBLOCK"` 做夹具），不破坏现有测试
- 验证：`make test-quick`

### 2. SYSTEM_PROMPT.TXT 剥离主观策略先验
- `strategy_lifecycle.phases` SELECT_STRATEGY 行：`Choose ONE strategy` → 增补 `or CUSTOM to orchestrate tools directly`
- `strategies.available`：新增 `CUSTOM` 条目（Free Orchestration - 直接调用任意 EXECUTE 工具，无 auto-chain）
- `PERFORMANCE OBSERVATIONS`（L113-123）：**删除** PBLOCK/PhysOpt/Fanout 三条主观成功率先验；**保留** Fast timing / Cost 两条客观工具事实
- `NOTE` 段（L120-123）：改写为中性——catalog 列出可用策略 + CUSTOM，Dashboard 提供客观数据，依据数据决策（不再以 skill_guidance 为「authoritative」）

### 3. SELECT_STRATEGY FORMAT_GUARD 提升 CUSTOM（[prepare_context.py:169-179](optimizer/nodes/prepare_context.py#L169-L179)）
- 改写 addendum：将 CUSTOM 作为与各策略并列的一等选项；说明 catalog 策略走 auto-chain、CUSTOM = 直接调用任意 EXECUTE 工具自由编排；明确「当目录策略无效/不适配时，选 CUSTOM 手动编排」
- 策略-工具映射表保留为中性参考（去掉「for choosing」倾向性框定）
- 保留 deepen/hint 相关说明不动（用户确认保留）

### 4. EXECUTE FORMAT_GUARD 中性化 + CUSTOM 说明（[prepare_context.py:181-280](optimizer/nodes/prepare_context.py#L181-L280)）
- 在 EXECUTE addendum 开头补充说明：非 CUSTOM 策略仅暴露主工具（auto-chain 接管后续 P&R/timing）；CUSTOM 暴露全部 EXECUTE 工具供自由编排；策略无效时可 SWITCH_STRATEGY 回 SELECT 选 CUSTOM
- 保留 PBLOCK auto-chain / directive tuning 等事实说明（让 LLM 理解系统行为，非「鼓励」）
- 不改动 flow_control 语义段、AUTO-INJECTED DATA 段（这些是数据完整性事实）

### 5. _append_skill_guidance 中性化（[state_space.py:1415-1462](optimizer/pure/state_space.py#L1415-L1462)）
- `"  avoid: vivado_run_tcl - use the tool above instead."` → 改为中性信息：说明调用 skill 工具时 auto_chain 会执行哪些步骤（信息性），去掉「avoid」强制倾向
  - tcl 偏好已由 SYSTEM_PROMPT automation 规则客观覆盖，此处不再重复强制
- 可选增强：CUSTOM（`_STRATEGY_MAP.get` 返回 None 分支）补充一行「无 auto-chain，直接调用任意 EXECUTE 工具自由编排，完成后 signal EXEC_DONE」

### 6. 文档同步（CLAUDE.md ALWAYS 规则）
- `architecture.md`：新增小节记录设计哲学转变（移除 PBLOCK 强制首选项、CUSTOM 一等化、剥离主观先验、**保留** deepen/hint 的理由）
- `README.md` / `PROJECT_TREE_AND_DATA_FLOW.md`：视必要更新策略选择相关描述（如「迭代1强制 PBLOCK」表述）

---

## 不改动（用户确认保留 / 客观数据）
- `_auto_deepen_hint`（[STRATEGY HINT - DEEPEN]）：鼓励 LLM 深耕有效策略
- `next_strategy_hint`（[STRATEGY HINT]）：LLM 自身跨阶段意图
- `compute_strategy_prior`：本运行客观历史 WNS delta 排名（正是「客观数据」）
- skill 工具与 auto-chain 功能本身（仅改提示词倾向，不删功能；等价性仍由工具级守卫 + DCP 验证兜底）
- CUSTOM 功能链路（enum/catalog/EXECUTE 不收窄——已完整，仅需提示词提升）

## 验证
1. `make test-quick`（纯函数单测）+ `make test-unit`：确保无回归
2. 重点检查 `tests/test_p2_orchestration_blocks.py`（CUSTOM / deepen / hint 测试）全通过
3. grep 确认无残留 `forcing PBLOCK` / `highest observed success rate` / `avoid: vivado_run_tcl` 主观/强制文案
4. 运行时验证（用户手动 `make run_optimizer`）：对照迭代1不再自动选 PBLOCK、SELECT 阶段可见 CUSTOM 一等说明