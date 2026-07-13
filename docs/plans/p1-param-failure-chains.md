# P1 实施计划：参数化失败记录 + 链参数可调 + 跨轮次失败记忆

> 对应改进报告 P1。核心是 ②A：把失败冷却从「按策略名」细化到「按策略+参数组合」，让 OptDesign 用 directive A 失败后不阻塞用 directive B 重试。

## 成功标准（可验证）

1. **②A**：`FailedStrategyRecord` 新增 `param_signature`；`record_strategy_failure` 按 `(strategy, param_signature)` 去重。directive 类策略的 tool_error 重试预算按组合独立计算。
2. **②A**：升级后的组合（tool_error→strategy_ineffective）**不在 SELECT_STRATEGY 阻断整策略**，而是在 EXECUTE 由组合守卫拦截重试（emit `[COMBO COOLED]`）；结构性/regression 失败仍按策略级阻断。
3. **②A**：catalog 三态展示——`[RETRY]`（可重试组合）/ `[BLOCKED]`（策略级）/ `[COMBO COOLED]`（升级组合，策略仍可选其他组合）。
4. **③C**：`PhaseHandoff` 携带 `recent_failures`，跨阶段（EXECUTE→EVALUATE→SELECT）注入最近工具错误摘要。
5. **①B**：`resolve_chain_step_arguments` 的三层回退从「仅 directive」泛化到任意链参数；`num_paths` 作为首个示例对 opt_design 族链可调。
6. `make test-quick` + `make test-unit` 全绿；新增 `tests/test_p1_param_failure.py`。

---

## 改动 6 - ②A 参数化失败记录（per-combo，核心）

### 数据结构（state.py）
- `FailedStrategyRecord` 新增 `param_signature: str = ""`（P0 已有 `retry_count`）。
- `StrategyState`（state.py:490）新增 `current_param_signature: str = ""`，EXECUTE 入口写入，EVALUATE/iteration_end 读取。

### 新增纯函数 `compute_param_signature(strategy, tool_args) -> str`
放 `optimizer/pure/execute_contracts.py`（与链解析同模块）。逻辑：
- 从 `tool_args` 收集 key ∈ {`directive`, `place_directive`, `route_directive`, `resource_multiplier`} 的非空值；`resource_multiplier` 量化到 1 位小数避免浮点噪声。
- 排序后格式化 `key=val|key=val`；无任何 directive 参数（如 Fanout/NetSwap/CellReplication）返回 `""`（→ 策略级，行为同今日）。
- 纯函数，单测覆盖。

### `record_strategy_failure`（state.py:536）
- 新增可选参数 `param_signature: str = ""`。
- 去重 key 从 `f.strategy == strategy` 改为 `f.strategy == strategy and f.param_signature == param_signature`。同一策略允许按组合多条记录。
- P0 的升级逻辑（retry_count→strategy_ineffective）天然按组合独立（因去重键含 param_signature）。

### EXECUTE 入口捕获 + 组合守卫（phase_execute.py）
- 在策略工具 MCP 调用前（~line 918，override 块之后、`call_tool_structured_fn` 之前）：
  - 若 `tool_name` 是策略主工具（`get_strategy_primary_tool` 反查或 `tool_name.startswith("rapidwright_execute_")` 且在 STRATEGY_MAP），计算 `_sig = compute_param_signature(state.strategy.current_strategy, tool_args)`，存 `state.strategy.current_param_signature = _sig`。
  - **组合守卫**：查 `failed_strategies` 是否存在 `(strategy==current, param_signature==_sig, reason=="strategy_ineffective", blocked_until_iter>current, param_signature!="")`。命中则**跳过 MCP 调用**，emit `[COMBO COOLED] strategy+combo(directive X) is in cooldown, unblocks in N iter - select the same strategy with a DIFFERENT directive combo`，`continue`（不消耗 MCP，但计一轮）。
- 向后兼容：`_sig==""` 时守卫不触发（策略级失败仍由 SELECT_STRATEGY 处理）。

### SELECT_STRATEGY 阻断收窄（phase_select_strategy.py:448 `_get_permanently_blocked_strategies`）
- 仅阻断 `param_signature == ""` 且 reason ∈ {strategy_ineffective, regression} 且 TTL 未过的记录。
- 升级组合（param_signature != ""）不在此阻断——改由 EXECUTE 组合守卫拦截。

### 调用点传参（param_signature 透传）
- **组合级**（传 `state.strategy.current_param_signature`）：`iteration_end.py:183`（tool_error）、`phase_execute.py:647`（data_quality_error）。
- **策略级**（传 `""`，默认）：`phase_execute.py:1190/1208/1272/2328`、`phase_evaluate.py:275`(regression)/`760`(no_improvement)、`iteration_end.py:330`。
- 既有「不降级更严格分类」守卫：升级方向（TTL 0→1）仍成立；新增维度 param_signature 不影响（同组合内才比较）。

### Catalog 三态（context_snapshot.py + strategy_library.py）
- `_retryable`：reason ∈ {tool_error, data_quality_error, unknown} 且未升级（retry_count < RETRY_BUDGET）→ available 行尾 `[RETRY: <detail> - N left]`（P0 已有，保持）。
- `_blocked`：param_signature=="" 且 TTL 活跃 → `[BLOCKED: ...]`（P0 已有，收窄到 param_signature==""）。
- 新增 `_combo_cooled`：param_signature!="" 且 reason==strategy_ineffective 且 TTL 活跃 → available 行尾 `[COMBO COOLED: <combo> - try other combos, unblocks in N iter]`。
- `get_strategy_catalog` 新增 `combo_cooled_strategies: dict[str,str]` 参数，在 available 行追加标记（与 `[RETRY]` 同处，互斥——一个策略同一轮只会命中其一）。

### state_space.py strategy_outcomes
- 失败表每条加 `param_signature` 字段输出（已有 detail/retries）。

### 验证
- 新单测：`test_param_signature_distinguishes_combos`（OptDesign directive A 升级后不阻塞 directive B）、`test_combo_guard_blocks_exhausted_combo`（EXECUTE 守卫命中）、`test_structural_failure_still_blocks_strategy`（regression 仍策略级阻断）、`test_compute_param_signature`（各类策略）。
- 既有 `test_stricter_failure_reason_not_downgraded`、`TestRetryBudgetEscalation` 必须仍绿（param_signature 默认 ""，去重键扩展向后兼容）。

---

## 改动 7 - ③C 跨轮次失败记忆（小）

### PhaseHandoff（phase_handoff.py）
- `PhaseHandoff` 新增 `recent_failures: list[str] = field(default_factory=list)`。
- `build_phase_handoff` 新增 `recent_failures: list[str] | None = None` 参数。
- `to_phase_context_string` 渲染：`if self.recent_failures: parts.append("Recent Failures:"); for f in recent_failures[-3:]: parts.append(f"  {f}")`。

### 调用点（phase_execute.py:1490、phase_select_strategy.py:338）
- 构造 `recent_failures = [f"{e['tool']}: {e['result'][:120]}" for e in state.iteration.tool_errors[-3:]]` 传入。
- iteration_start 已在迭代边界清空 `tool_errors`，故跨迭代靠 `failed_strategies`（P0 ②C 已上 detail）；③C 补的是迭代内跨阶段的即时记忆。

### 验证
- 新单测：`test_handoff_carries_recent_failures`（构造 tool_errors，断言 to_phase_context_string 含 "Recent Failures"）。

---

## 改动 8 - ①B 链参数可调（scoped：num_paths 示例）

### 泛化三层回退（execute_contracts.py:334 `resolve_chain_step_arguments`）
- 现状：三层仅作用于 `directive`（LLM 显式 > `STRATEGY_DEFAULT_DIRECTIVES` > 硬编码）。
- 改造：新增 `STRATEGY_DEFAULT_CHAIN_ARGS: dict[str, dict[str, Any]]`（tool_chain_policy.py + constants.py 双份，遵循 [[project_duplicated_chain_policy]]）。对 `args_from_skill` 映射的非 directive key，若 skill 未提供，查 `STRATEGY_DEFAULT_CHAIN_ARGS[tool_name]` 取默认。
- 首个示例：opt_design 族链的 `vivado_extract_critical_path_cells` 步 `num_paths`（现状硬编码 10）。

### MCP 边界（rapidwright_tools.py + server.py）
- `execute_opt_design_strategy`/`execute_combinational_rebalancing_strategy`/`execute_lut_muxf_repack_strategy` 签名 + inputSchema 加 `num_paths: int = 10`（仅 opt_design 族）。
- 经 `_attach_chain_directives` 同款 helper 把 `num_paths` 附到 skill result；chain 步 `args_from_skill` 映射 `num_paths`。
- phase_execute 在调用前 `pop("num_paths")` 不影响——num_paths 是给 chain 用的，skill wrapper 接收后附到 result，chain 步消费。**注意**：与 trust_llm_input 不同，num_paths 需要流到 skill result 再到 chain，故 skill wrapper 需接收并附到 result（不 pop）。

### 验证
- 新单测：`test_resolve_chain_args_num_paths_default`、`test_num_paths_from_skill_overrides_default`。

---

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| ②A 去重键扩展破坏既有「同策略单记录」假设 | param_signature 默认 ""，所有策略级失败记录仍归一条；既有测试（TestRetryBudgetEscalation 等）守护 |
| 组合守卫误拦截合法重试 | 守卫仅对 param_signature!="" 且 reason==strategy_ineffective 且 TTL 活跃命中；directive 类策略未升级前不触发 |
| param_signature 计算不稳定（浮点/顺序） | resource_multiplier 量化 1 位；key 排序；纯函数单测锁定 |
| SELECT_STRATEGY 阻断收窄导致策略被反复试 | 结构性/regression 仍策略级阻断；组合守卫拦截已升级组合；MAX_STRATEGY_CYCLES=5 兜底 |
| ①B num_paths 流经 MCP 边界引入新 schema 字段 | 仅 opt_design 族；默认 10 与现状一致；既有链测试守护 |
| 三份 duplicated policy（constants.py / tool_chain_policy.py）漂移 | ①B 改 STRATEGY_DEFAULT_CHAIN_ARGS 必须同步两份（[[project_duplicated_chain_policy]]） |

## 实施顺序

1. 改动 7（③C，最小，先热身）-> `make test-unit`
2. 改动 6（②A）：state.py 字段+去重 → compute_param_signature 纯函数 → EXECUTE 捕获+守卫 → SELECT 收窄 → 调用点传参 → catalog 三态 → state_space -> 每步 `make test-quick`
3. 改动 8（①B）：泛化 resolve → STRATEGY_DEFAULT_CHAIN_ARGS 双份 → num_paths MCP 边界 -> `make test-unit`
4. 全量 `make test-quick` + `make test-unit` 回归
5. 更新 README / PROJECT_TREE / architecture.md（per-combo 冷却、组合守卫、③C handoff、①B 链参数）

## 范围说明
②A 是 P1 主体（~10 文件，含阻断语义变更）；③C 小（~3 文件）；①B scoped 到 num_paths 示例（~4 文件，含 MCP 边界）。若需缩减，建议先做 ②A+③C（失败重试核心），①B 可独立后置。