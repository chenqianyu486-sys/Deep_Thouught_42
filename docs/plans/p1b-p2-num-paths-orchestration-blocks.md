# ①B + P2 实施计划：num_paths 链参数 + 自由编排模式 + 统一 block 语义

> 用户指令：①补做 ①B（num_paths 链参数）；②进入 P2（自由编排模式 / 统一 block 语义）。

## 成功标准（可验证）

1. **①B**：opt_design 族的 `vivado_extract_critical_path_cells` 链步 `num_paths` 由 LLM 经 skill 参数控制（默认 10）；未传时回退 10。
2. **①C 自由编排**：`strategy_name` enum 新增 `CUSTOM`；选 CUSTOM 时 EXECUTE 不收窄到单主工具，开放完整 `EXECUTE_CORE_TOOLS`（机制已存在，正式化为 enum 值 + 文档）。
3. **②D 统一 block 语义**：消除「软阻止」矛盾--`[BLOCKED]` 仅表示真阻断（regression + 结构性 strategy_ineffective）；`no_improvement`/`strategy_not_applicable` 改为 `[PRIOR FAIL]` 软标记（可选，带上下文）；regression 加入 `[BLOCKED]` 显示（与其硬阻断一致）。
4. `make test-quick` + `make test-unit` 全绿；新增 `tests/test_p2_orchestration_blocks.py`。

---

## 改动 9 - ①B num_paths 链参数（MCP 边界 + 重复链同步）

### skill wrapper（`RapidWrightMCP/rapidwright_tools.py`）
- `execute_opt_design_strategy`(2493)、`execute_combinational_rebalancing_strategy`(2544)、`execute_lut_muxf_repack_strategy`(2595) 签名各加 `num_paths: int = 10`。
- 三者 return 行相同（`return _attach_chain_directives(_strategy_plan_to_dict(plan), place_directive, route_directive)`），用 replace_all 改为：
  ```python
  _r = _attach_chain_directives(_strategy_plan_to_dict(plan), place_directive, route_directive)
  _r["extract_num_paths"] = num_paths
  return _r
  ```
  num_paths 不传给 `skill.execute_with_telemetry`（它是链后提取步参数，非 skill 分析参数）。

### inputSchema（`RapidWrightMCP/server.py`）
- opt_design(783)、combinational(810)、lut_muxf(871) 三处 inputSchema.properties 加：
  ```python
  "num_paths": {"type": "integer", "description": "Number of critical paths to extract after the auto-chain (vivado_extract_critical_path_cells). Default 10. Increase for complex multi-path analysis.", "default": 10}
  ```

### 链步 args_from_skill（`optimizer/pure/tool_chain_policy.py` + `optimizer/pure/constants.py` 双份，遵循 [[project_duplicated_chain_policy]]）
- opt_design/combinational/lut_muxf 三链的 `vivado_extract_critical_path_cells` 步（`{"tool": "vivado_extract_critical_path_cells", "args": {"num_paths": 10}}`，三处相同）改为加 `"args_from_skill": {"num_paths": "extract_num_paths"}`。replace_all 各 3 处。
- `resolve_chain_step_arguments` 已处理 `args_from_skill`（skill result 的 `extract_num_paths` 覆盖硬编码 10）。无需改 resolver。

### 验证
- 新单测：`test_num_paths_flows_from_skill_to_chain`（构造 skill_result_data={"extract_num_paths": 20}，断言 resolve_chain_step_arguments 返回 args["num_paths"]==20）；`test_num_paths_default_10_when_absent`（skill_result 无 extract_num_paths -> 保留硬编码 10）。
- 既有 `TestSkillChainActions`（仅断言 pblock）+ sync 测试（两份链相等）不破。

---

## 改动 10 - ①C 自由编排模式（CUSTOM）

### 机制（已存在，正式化）
`filter_tools_for_phase`（tool_filter.py:229-233）：EXECUTE 阶段若 `get_strategy_primary_tool(strategy)` 返回 None（策略不在 STRATEGY_MAP），则不收窄，保留完整 `PHASE_TOOLS[EXECUTE]`（含所有策略主工具 + Vivado place/route/opt/phys_opt + 分析工具）。`test_unknown_execute_strategy_keeps_phase_toolset`（test_pure.py:185）已验证此路径。

### enum（`dcp_optimizer.py:410-415`）
- `strategy_name` enum 加 `"CUSTOM"`。

### 文档（catalog / FORMAT_GUARD）
- `strategy_library.py` `STRATEGIES` 加一条 CUSTOM 条目（trigger: "No predetermined chain fits; LLM orchestrates tools directly"），或独立在 catalog 末尾说明。
- FORMAT_GUARD（EXECUTE 阶段）补一段：`CUSTOM` 模式下不触发 auto-chain，LLM 直接调用 Vivado/RapidWright 工具自由编排；等价性依赖工具级守卫（retiming 黑名单、directive 白名单）+ DCP 验证；完成后 `flow_control=EXEC_DONE`。

### 行为确认（无需改代码）
- PBLOCK 首迭代 override（phase_select_strategy.py:72）是软提示，LLM 的 `report_step_state(strategy_name="CUSTOM")` 在 line 210 覆盖之 -> CUSTOM 可选。
- EXECUTE 循环：CUSTOM 无主工具 -> 不触发 pblock/fanout override（按 tool_name 匹配，CUSTOM 下 LLM 调原始工具不命中）-> 不触发 auto-chain（无 primary tool）-> LLM 直接编排，`EXEC_DONE` 退出。
- 失败归因：CUSTOM 失败记 `state.strategy.current_strategy="CUSTOM"`（策略级）。

### 验证
- 新单测：`test_custom_strategy_keeps_broad_toolset`（filter_tools_for_phase(EXECUTE, strategy="CUSTOM") 不收窄，包含 vivado_place_design + 多个 strategy 工具）。
- `test_unknown_execute_strategy_keeps_phase_toolset` 已覆盖等价机制。

---

## 改动 11 - ②D 统一 block 语义（catalog 四态）

### 现状矛盾
- `no_improvement`/`strategy_not_applicable`：catalog 标 `[BLOCKED]` 但 SELECT 不硬阻断（软，可选）-- 矛盾。
- `regression`：SELECT 硬阻断但 catalog 不标 `[BLOCKED]`（出现在 available 但选时被拒）-- 矛盾。

### 统一方案（`optimizer/pure/context_snapshot.py`）
四态（在 P1 三态基础上拆分）：
- `_retryable`：tool_error/data_quality/unknown 未升级 -> `[RETRY: ...]`（不变）。
- `_blocked`（真阻断，TTL 内不可选）：**regression + strategy_ineffective**（param_signature==""）-> `[BLOCKED: ...]`。**新增 regression**。
- `_combo_cooled`：param_signature!="" strategy_ineffective -> `[COMBO COOLED]`（不变）。
- `_soft_failed`（新）：**no_improvement + strategy_not_applicable**（param_signature==""）-> available 行 `[PRIOR FAIL: <reason> - selectable, unblocks in N iter]`。从 `_blocked` 移出。

### `get_strategy_catalog`（strategy_library.py）
- 新增 `soft_failed_strategies: dict[str, str]` 参数。
- available 行标记优先级：`combo_cooled > soft_failed > retryable`（互斥，一个策略同一轮命中其一）。

### `format_state_space_for_llm`（state_space.py）
- 新增 `soft_failed_strategies` 参数，透传给 `_get_catalog`。

### SELECT 守卫不变
`_get_permanently_blocked_strategies` 仍只硬阻断 strategy_ineffective + regression（param_signature==""）。no_improvement/strategy_not_applicable 保持可选（软）。regression 现也在 catalog 显示 `[BLOCKED]`（与其硬阻断一致）。

### 验证
- 新单测：`test_no_improvement_shown_as_prior_fail_not_blocked`、`test_regression_shown_as_blocked`、`test_soft_failed_selectable`（不在 _get_permanently_blocked_strategies）。
- 既有 `test_escalated_shown_as_blocked`（strategy_ineffective，仍在 _blocked）✓；`test_p27_catalog_marks_physopt_blocked`（physopt WNS-ineffective 走单独路径，不受影响）✓。

---

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| ①B 两份链（constants.py/tool_chain_policy.py）不同步 | replace_all 两份相同改动；sync 测试（test_pure.py:1221）守护 |
| ①C CUSTOM 绕过 auto-chain 等价性保障 | 工具级守卫（retiming 黑名单/directive 白名单）仍在；DCP 验证每 5 迭代兜底；FORMAT_GUARD 明示风险 |
| ①C CUSTOM 下 LLM 调策略 wrapper 触发链 | 可接受（LLM 显式选择）；失败归因到 CUSTOM 是 minor 不一致，不阻断 |
| ②D no_improvention 软化致 LLM 反复试无收益策略 | 软标记明示 "selectable, unblocks in N iter"；MAX_STRATEGY_CYCLES=5 + global_no_improvement=3 兜底 |
| ②D 四态增加 catalog 复杂度 | 优先级互斥（一策略一轮一标记）；纯渲染层改动，SELECT 语义不变 |

## 实施顺序

1. 改动 9（①B num_paths）-> `make test-quick`（盯 sync + 链结构测试）
2. 改动 10（①C CUSTOM enum + 文档）-> `make test-unit`
3. 改动 11（②D 四态 catalog）-> `make test-quick`（盯 p2/p0 block 测试）
4. 新增 `tests/test_p2_orchestration_blocks.py` + 全量回归
5. 更新 README / PROJECT_TREE / architecture.md（num_paths、CUSTOM、四态 block）

## 范围说明
①B 机械（~4 文件 + 双份链）；①C 轻量（enum + 文档，机制已存在）；②D 纯渲染层四态（~3 文件）。三者独立可测，按序落地。