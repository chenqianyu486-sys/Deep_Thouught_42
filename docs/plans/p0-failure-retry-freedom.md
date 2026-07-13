# P0 实施计划：失败 → 看到根因 → 修正参数重试 闭环

> 对应改进报告 P0。目标：让 LLM 在策略/工具失败后，能**看到根因与修正建议**，并**用调整后的参数重试同一策略**（有上限），而非被直接冷却换策略。

## 成功标准（可验证）

1. `tool_error` / `data_quality_error` 失败的策略**不再从目录消失**，带 `detail` 与「可重试」标记显示。
2. 任意工具错误摘要含 `category` + `fix_hint` + `retryable` 三段（结构化错误信封）。
3. 同一策略连续 `tool_error` 重试 ≥ `RETRY_BUDGET=2` 次后**自动升级**为 `strategy_ineffective`（TTL=1 冷却），防止无限重试。
4. EVALUATE 阶段工具错误注入 user message（对齐 EXECUTE 的 `[EVAL]` notice）。
5. pblock / fanout 支持 `trust_llm_input: bool` 显式跳过 state 数据覆盖（cells 仍经 registry 校验）。
6. `make test-quick` + `make test-unit` 全绿；新增 `tests/test_p0_failure_retry.py` 覆盖纯函数与升级逻辑。

## 改动清单（按依赖顺序，每个独立可测）

### 改动 1 — ③A 结构化错误分类（纯函数 + 摘要注入）

**新增** `optimizer/pure/tool_error_classify.py`（纯函数，可单测）：

```python
@dataclass(frozen=True, slots=True)
class ToolErrorClass:
    category: str        # bad_cell_name|bad_directive|tcl_blocked|timeout|vivado_error|rw_error|schema_validation|partial_failure|rate_limited|unknown
    fix_hint: str        # 可执行修正建议
    retryable: bool

def classify_tool_error(tool_name: str, error: str) -> ToolErrorClass: ...
```

分类规则（字符串匹配，按优先级）：
- `"not a recognized directive"` / `"18-641"` → `bad_directive`，hint 列合法 directive，retryable=True
- `"invalid_cell_names"` / `"rejected"` / `"Cell names must be"` → `bad_cell_name`，retryable=True
- `"Application-level timeout"` → `timeout`，hint「缩小 num_paths / 避免全局 unplace / 重试」，retryable=True
- `"[BLOCKED]"` + TCL → `tcl_blocked`，retryable=True
- `"MCP tool error"` / `"Input validation error"` → `schema_validation`，retryable=True
- `"Errors:"` 多错误 → `partial_failure`，retryable=True
- `"[RATE LIMITED]"` → `rate_limited`，retryable=False（本轮）
- 默认 → `unknown`/`vivado_error`，retryable=True

**集成** `optimizer/pure/tool_summary.py`：在 `summarize_tool_result` 检测到 `status=="error"`（既有 `has_error` 分支，~line 163-166）时，调用 `classify_tool_error`，在 YAML 摘要追加：
```yaml
  error_category: bad_directive
  fix_hint: "use a supported directive (Default, Explore, ...)"
  retryable: true
```
不改动既有 cell-name/directive 富错误路径（它们已自带 hint，classify 是统一信封层）。

**验证**：纯函数单测 `test_classify_tool_error_*`（每类一条）。

---

### 改动 2 — ③B EVALUATE 工具错误反馈

`optimizer/nodes/subgraphs/phase_evaluate.py:539-543`：当前仅 `logger.warning`。改为同时注入 user message：

```python
if tool_result.error:
    _cls = classify_tool_error(tool_name, tool_result.error)
    logger.warning(f"[EVALUATE] Tool '{tool_name}' error: {tool_result.error[:200]}")
    if deps.compat is not None:
        deps.compat.add_message("user",
            f"[EVAL ERROR] {tool_name} failed (category={_cls.category}). "
            f"{_cls.fix_hint} retryable={_cls.retryable}")
```

**验证**：既有 EVALUATE 路径不破；新单测 mock tool_result.error 断言 add_message 被调用。

---

### 改动 3 — ②C tool_error 可见 + detail 上桌

**`optimizer/pure/context_snapshot.py:259-301`**：
- 移除 `_hard_exclude` 中的 `("tool_error",)`（line 261-264）。`tool_error`/`data_quality_error` 不再被静默剔除。
- 新增 `_retryable: dict[str, str]`：收集 reason ∈ {`tool_error`,`data_quality_error`,`unknown`} 且未升级（`retry_count < RETRY_BUDGET`，见改动4）的记录，value = `detail` 摘要。
- 将 `_retryable` 作为新参数 `retryable_strategies` 传给 `get_strategy_catalog`。
- `_exclude_strategies` 改为空（或仅保留未来真正不可重试的 reason）。

**`strategy_library.py:493-545` `get_strategy_catalog`**：新增 `retryable_strategies: dict[str,str] | None = None` 参数。对在 `retryable_strategies` 中的 available 策略，行尾追加 ` [RETRY: <detail 摘要>]`：
```python
if key in retryable:
    line += f" [RETRY: {retryable[key][:80]} - adjust params and retry]"
```

**`optimizer/pure/state_space.py:1034-1054` strategy_outcomes failed 段**：每条失败记录增加 `detail` 与 `retryable`/`retries_left` 字段输出。

**验证**：`tests/test_context_snapshot.py:228 test_failed_strategies_excluded_from_catalog`（用 reason="unknown" 默认值）—— 该测试 `if "strategy_catalog:" in content` 守卫，需确认仍通过；若断言触发，调整为「失败策略以 [RETRY] 显示而非消失」的新语义。新增 `test_tool_error_visible_in_catalog`。

---

### 改动 4 — ②B 重试预算 + 自动升级

**`optimizer/state.py`**：
- `FailedStrategyRecord`（line 380-388）新增 `retry_count: int = 0`。
- 新增常量 `RETRY_BUDGET = 2`（同策略 tool_error 重试上限）。
- `record_strategy_failure`（line 536-603）更新逻辑：当 `existing.reason` ∈ {`tool_error`,`data_quality_error`,`unknown`} 且新 reason 同属可重试类时：
  - `entry.retry_count += 1`
  - 若 `entry.retry_count >= RETRY_BUDGET`：**升级** reason → `strategy_ineffective`，`blocked_until_iter = _ttl_for_reason("strategy_ineffective", current)`，记日志 `[FAILED_STRATEGY] Escalated after N retries`。
  - 否则：刷新 `detail`，保持 TTL=0 可重试。
- 升级是「更严格」（TTL 0→1），不违反既有「不降级更严格分类」守卫（line 567-575）。

**`optimizer/pure/constants.py` 或 `state.py`**：导出 `RETRY_BUDGET` 供 context_snapshot 判断 `retries_left`。

**SELECT_STRATEGY 无需改动**：`_get_permanently_blocked_strategies`（phase_select_strategy.py:448-472）仅阻止 `strategy_ineffective`+`regression`；tool_error（未升级）本就不阻止，LLM 已可重选——改动3 让它**可见**即可。

**验证**：
- 新单测 `test_tool_error_escalates_after_budget`：连续 2 次 tool_error → reason 变 `strategy_ineffective`、`blocked_until_iter=current+1`。
- 既有 `test_stricter_failure_reason_not_downgraded_to_tool_error`（line 685）、`test_equally_strict_reason_refreshes_ttl`（line 706）必须仍绿（升级逻辑只在 existing 为可重试类时触发，strategy_not_applicable 不受影响）。

---

### 改动 5 — ①A trust_llm_input 显式跳过数据覆盖

**`RapidWrightMCP/rapidwright_tools.py`**：为 `execute_pblock_strategy`（line 2335）与 `execute_fanout_strategy`（line 2700）签名加 `trust_llm_input: bool = False`（函数体内不使用，仅供 MCP schema 暴露给 LLM；phase_execute 在调用前消费）。

**`skills/pblock_strategy.py`**（line 1587-1703 两处 ParameterSpec 块）与 **`skills/fanout_strategy.py`**：各加一条：
```python
ParameterSpec("trust_llm_input", bool,
              "If true, use LLM-provided critical_path_cells/nets as-is (still validated against registry for cells). Default false (use verified state data).",
              default=False),
```

**`optimizer/nodes/subgraphs/phase_execute.py`**：
- pblock cells 覆盖块（line 466-547）：`_trust = bool(tool_args.get("trust_llm_input"))`。若 `_trust` 且 LLM 提供的 cells 全部 `is_valid_cell_name` + 在 registry 中 → 使用 LLM cells，emit `[DATA INTEGRITY] LLM input trusted (trust_llm_input=True), N cells`；否则仍走 state 覆盖 + 警告「LLM cells invalid, fell back to state data」。
- fanout nets 覆盖块（line 558-597）：`_trust` 为真时使用 LLM nets（nets 无 registry 校验，仅 emit 信任通知 + 保留 tool 端 `MIN_FANOUT_TO_SPLIT` 守卫）。
- **resource_multiplier 下限 2.0x 保持不变**（这是竞赛验证的安全地板，非数据完整性问题，不可被 trust_llm_input 绕过）。
- **target_*_count 自动注入保持不变**（LLM 不传时才注入，本就是补全而非覆盖）。

**FORMAT_GUARD / skill description**：在 pblock/fanout 的描述里注明 `trust_llm_input` 用途与风险（仅在框架 state 数据陈旧/rollback 后且 LLM 有把握时使用）。

**验证**：新单测 `test_trust_llm_input_uses_llm_cells_when_valid`、`test_trust_llm_input_falls_back_when_invalid`、`test_default_uses_state_data`（覆盖 phase_execute 的纯逻辑分支，mock registry）。

---

## 测试策略

- **新增** `tests/test_p0_failure_retry.py`：
  - `classify_tool_error` 各 category（改动1）
  - tool_error 升级（改动4）
  - tool_error 可见 + [RETRY]（改动3）
  - trust_llm_input 三分支（改动5）
- **回归**：`make test-quick`（纯函数）+ `make test-unit`。重点盯：
  - `tests/test_context_engineering_fixes.py::TestCooldownLogic`（4 项，line 180-244）
  - `tests/test_context_engineering_fixes.py::test_stricter_failure_reason_not_downgraded_to_tool_error` / `test_equally_strict_reason_refreshes_ttl`（line 685-718）
  - `tests/test_context_snapshot.py::test_failed_strategies_excluded_from_catalog`（line 228，语义可能需调整）
  - `tests/test_p0_robustness_fixes.py`（directive 拒绝路径，改动1 不能破坏）
- 不跑 `make run_*`（按 memory 规约，run_* 需用户手动执行）。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 改动3 让 tool_error 可见可能让 LLM 反复重试同一坏参数 | 改动4 的 RETRY_BUDGET=2 升级兜底；升级后进 strategy_ineffective 冷却 |
| ①A trust_llm_input 绕过数据覆盖可能重现 -1.220ns 幻觉网络回归 | cells 强制 registry 校验；nets 保留 tool 端 MIN_FANOUT_TO_SPLIT 守卫；resource_multiplier 地板不放开 |
| 改动1 classify 误分类导致错误 fix_hint 误导 LLM | 默认 retryable=True、category=unknown 兜底；fix_hint 仅作建议不强制；既有富错误路径保留 |
| 升级逻辑与既有「不降级」守卫冲突 | 升级方向是 TTL 0→1（更严格），守卫允许；仅当 existing 为可重试类时触发，strategy_not_applicable 不受影响 |
| 新增 retry_count 字段未持久化/序列化 | failed_strategies 是内存态（每轮重建），无需持久化；dashboard serializer 需确认能容忍新字段（dataclass 默认值，应无碍） |

## 实施顺序

1. 改动1（③A 分类纯函数 + 摘要注入）→ `make test-quick`
2. 改动2（③B EVALUATE 反馈）→ `make test-unit`
3. 改动4（②B 升级，改 state.py）→ `make test-quick`（盯升级单测 + 既有 cooldown 测试）
4. 改动3（②C 可见，改 context_snapshot/strategy_library/state_space）→ `make test-unit`
5. 改动5（①A trust_llm_input）→ `make test-unit`
6. 全量 `make test-quick` + `make test-unit` 回归
7. 更新 README / PROJECT_TREE / architecture.md（冷却机制、错误信封、trust_llm_input 三处）
