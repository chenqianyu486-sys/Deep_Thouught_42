# FPL26 优化竞赛 - 项目结构与数据流

## 1. 项目结构

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # 主Agent: LLM编排、模型选择、压缩触发
├── config_loader.py              # 模型配置加载器（单例）
├── model_config.yaml             # 模型层级与fallback配置
├── validate_dcps.py              # DCP等价性验证器
├── SYSTEM_PROMPT.TXT             # 系统提示词
├── requirements.txt
├── context_manager/              # 内存管理模块
│   ├── manager.py                # MemoryManager - 中心编排，单次_compress()触发
│   ├── estimator.py              # TokenEstimator (tiktoken)
│   ├── events.py                 # EventBus - 订阅/取消订阅
│   ├── lightyaml.py              # YAML解析器
│   ├── interfaces.py             # 核心数据类
│   ├── agent_context.py          # AgentContextManager - 多Agent分支
│   └── strategies/
│       ├── yaml_structured_compress.py  # YAML压缩基类 + 时序报告智能截断 + 过时时序报告替换
│       ├── planner_compress.py         # PlannerCompressor: 100K token_budget, preserve_turns=60, preserve_role_turns=6
│       └── worker_compress.py          # WorkerCompressor: 35K token_budget, preserve_turns=40, preserve_role_turns=6
├── RapidWrightMCP/               # RapidWright MCP服务器
│   ├── rapidwright_tools.py      # 工具函数实现
│   ├── server.py                 # MCP服务器入口
│   └── test_server.py            # 服务器测试
├── VivadoMCP/                    # Vivado MCP服务器
├── skills/                       # Skill框架（Skill Descriptor v3 规范实现）
│   ├── __init__.py                  # 导出所有公共符号
│   ├── base.py                      # Skill基类、SkillMetadata、SkillResult、ParameterSpec
│   ├── context.py                   # SkillContext依赖注入（design, call_id, idempotency_key）
│   ├── registry.py                  # SkillRegistry注册发现
│   ├── skill_decorator.py           # @skill装饰器（增强版：namespace/version/idempotency）
│   ├── telemetry.py                 # 可观测性：执行记录、指标聚合、error_code追踪
│   ├── errors.py                    # 错误契约：SkillErrorCode, ERROR_METADATA, SkillError信封
│   ├── idempotency.py               # 幂等性存储 + 并发变异保护（423 Locked）
│   ├── tracing.py                   # 追踪属性：SkillTraceAttributes（OTel兼容）
│   ├── descriptor.py                # JSON描述符生成/导出
│   ├── validate_descriptors.py      # CI验证套件（Schema/Enum/Description检查）
│   ├── net_detour_optimization.py   # Skill类 + 纯函数：绕路比率分析 + 重心放置优化
│   ├── smart_region_search.py       # Skill类 + 纯函数：智能PBlock区域搜索
│   ├── descriptors/                 # 自动生成的JSON描述符文件
│   ├── test_net_detour_optimization.py  # 单元测试（_group_pins_by_cell）
│   └── test_skill_framework.py      # 28项集成测试（注册/执行/遥测/错误/幂等/追踪）
```

## 2. 核心数据流

### 2.1 消息流程

```
add_message(role, content)
         ↓
WorkingMemory.add_message()  # 无自动压缩
         ↓
DCPOptimizer._compress_context()  ← 单次触发点（LLM调用前）
         ↓
MemoryManager._compress("yaml_structured", context, model_tier)
         ↓
YAMLStructuredCompressor:
    - 正常模式: preserve_turns=40/min_importance=0.15/preserve_role_turns=6 (worker), preserve_turns=60/min_importance=0.1/preserve_role_turns=6 (planner)
    - 激进模式(hard_limit触发): preserve_turns=25(worker)/40(planner), min_importance=0.35(worker)/0.25(planner)
    - system消息始终保护
    - preserve_role_turns=6: 最近6条消息保留原始API role（user/assistant/tool），不塞进YAML
    - 两轮预算分配: 60%高重要性 + 40%中等重要性
    - preserve_turns预留预算: ~1500 tokens/turn, 最多10K
    - 工具调用保留参数（最多5个）
    - 时序报告智能截断（5项改进：动态预算/阈值过滤/起终点成对/时钟域分组/回退保护）
    - 过时时序报告替换：迭代 < current_iteration-1 的长时序报告 → `[Outdated timing report from iteration N]`（节省 token）
    - WNS状态注入时机: API调用时（不在working memory）
```

### 2.2 顺序压缩流程

```
1. 分离system消息（受保护）
2. HistoricalMemory.add(summary, importance=0.8)  ← 先归档
3. WorkingMemory.clear()                        ← 再清空
4. 添加system + YAML摘要（旧消息）              ← YAML压缩
5. 添加最近 preserve_role_turns=6 条消息        ← 保留原始 role（user/assistant/tool）
```

### 2.3 WNS/TNS状态注入（防压缩丢失）

```
API调用前 → _inject_wns_state_to_system_prompt()
    - 更新wns_ns、clock_period_ns
    - 注入 current_tns、failing_endpoints（2026-05-01 新增）
    - 追加"Current Optimization State:"块（含 WNS/TNS/failing_endpoints/best_checkpoint/next_model）
    → 模型始终看到当前状态，不依赖working memory
```

### 2.4 关键信息保护

| 类型 | 存储位置 | 保护机制 |
|------|----------|----------|
| System消息 | Working memory（受保护） | 压缩前分离，始终前置 |
| WNS状态 | MemoryManager（独立于WM） | API调用时注入 |
| TNS/Failing Endpoints | DCPOptimizer.latest_tns/latest_failing_endpoints | API调用时随 WNS 一同注入 |
| Tool调用摘要 | MemoryManager._tool_call_details | 独立存储 |
| 失败策略 | CompressionContext | 存入YAML输出 |
| 最近N轮消息 | Working memory（role保留） | preserve_role_turns=6, 保持 user/assistant/tool 原始role不压缩进YAML |

### 2.5 模型选择

```
PLANNER: openrouter/owl-alpha (1M context, 复杂推理)
WORKER: tencent/hy3-preview:free (250K context, 快速执行)
- 429降级: 按层级fallback列表，轮询+耗尽追踪
- 迭代边界切换: 模型切换在迭代结束保存检查点后，下一迭代开始时发生
- 交接提示词: 新模型收到包含最优状态、下一步目标的上下文
```

### 2.5.1 模型选择维度（`_select_model()`）

评分系统（6维度，加权得分高的模型胜出，margin=2防止震荡）：

| 维度 | 条件 | Planner得分 | Worker得分 |
|------|------|-----------|-----------|
| 1. 上下文复杂度 | >=6 | +2 | - |
| 2. 历史能力 | >=70%成功率 | - | +2 |
| 3. 历史能力 | <30%成功率 | +2 | - |
| 4. 连续失败 | >=2次 | +4 | - |
| 5. 连续成功 | >=3次 | - | +1 |
| 6. 全局无改善 | >=2.5次 | - | +1 |
| 7. 上下文容量 | >=60% worker限制 | +2 | - |
| 8. WNS状态 | 严重倒退(>-2.0ns) | +3 | - |


### 2.6 Skill 机制

```
skills/
├── Skill (base.py)                 # 抽象基类 + 默认 get_metadata()
├── SkillMetadata                   # 元数据（Skill Descriptor v3 规范）
│   ├── id                          # 全限定名: {namespace}.{name}@{version}
│   ├── idempotency / side_effects  # 契约声明
│   ├── error_codes                 # 可声明错误码
│   └── to_descriptor() / to_json_schema()
├── SkillResult + SkillError        # 结构化执行结果 + 错误信封
├── SkillContext                    # 依赖注入：design, call_id, idempotency_key
├── SkillRegistry                   # 注册/发现：register(), get(), list_all()
├── @skill decorator                # 增强版：支持 namespace/version/idempotency 等
│
├── errors.py                       # 错误契约：SkillErrorCode, ERROR_METADATA
├── idempotency.py                  # 幂等性存储 + 并发变异保护
├── tracing.py                      # 追踪属性：SkillTraceAttributes
├── descriptor.py                   # JSON 描述符生成/导出 → skills/descriptors/*.json
├── validate_descriptors.py         # CI 验证套件（Schema/Enum/Description 检查）
│
├── telemetry.py                    # SkillTelemetry + SkillExecutionTimer
├── net_detour_optimization.py      # Skill类 + 纯函数
├── smart_region_search.py          # Skill类 + 纯函数
├── descriptors/                    # 自动生成的 JSON 描述符文件
└── test_skill_framework.py         # 28 项测试（含编排/执行/遥测/错误/幂等）

已注册 Skills:
├── analysis.net_detour@1.0.0           # 分析关键路径网络的绕路比率
├── placement.optimize_cell@1.0.0       # 基于重心优化单元布局（non-idempotent）
└── placement.smart_region@1.0.0        # 智能 PBlock 区域搜索

调用链:
Agent → MCP Tool → rapidwright_tools.py wrapper → SkillRegistry.get()
         ↓
   SkillContext(design, call_id, idempotency_key)
         ↓
   Skill.execute_with_telemetry(context, **kwargs)
     ├── 幂等性检查（idempotent/non-idempotent）
     ├── Heartbeat daemon（30秒间隔）
     ├── self.execute(context, **kwargs)
     ├── 追踪属性发射（SkillTraceAttributes）
     ├── SkillTelemetry.record_execution(duration_ms, status, error_code)
     └── 返回 SkillResult(success, data, error, error_code)

JSON 描述符示例（skills/descriptors/analysis.net_detour-at-1.0.0.json）：
├── $schema / specVersion / id / displayName
├── idempotency: "safe" | sideEffects: []
├── timeout: { defaultMs: 30000, maxMs: 60000 }
├── authentication: { type: "none" }
├── parameters: type=object, additionalProperties=false
│   ├── pin_paths: { type: array, description, required }
│   └── detour_threshold: { type: number, default: 2.0 }
├── returns: { type: object, additionalProperties: false }
└── errors: [{ code, recoverable }, ...]
```

## 3. 事件系统

```python
EventBus (events.py)
├── subscribe(event_type, handler) → token
├── unsubscribe_by_token(token)
├── emit(event)

EventTypes: CONTEXT_COMPRESSED, LAYER_PROMOTED, BRANCH_CREATED, BRANCH_MERGED
```

## 4. 配置（model_config.yaml）

```yaml
# Worker: 速度优化, 250K max
worker:
  soft_threshold: 40K, hard_limit: 200K
  token_budget: 35K, preserve_turns: 40/25(激进), min_importance: 0.15/0.35(激进)
  preserve_role_turns: 6, max_chars_multiplier: 1.0 (正常) / 0.5 (激进)
  fallback_models: ["deepseek/deepseek-v4-flash", "stepfun/step-3.5-flash"]

# Planner: 推理优化, 1M max
planner:
  soft_threshold: 120K, hard_limit: 300K
  token_budget: 100K, preserve_turns: 60/40(激进), min_importance: 0.1/0.25(激进)
  preserve_role_turns: 6, max_chars_multiplier: 1.0 (正常) / 0.5 (激进)
  fallback_models: ["xiaomi/mimo-v2.5-pro"]
```

## 5. 迭代控制

```python
MAX_TOOL_ROUNDS_PER_ITERATION = 50
GLOBAL_NO_IMPROVEMENT_LIMIT = 3
WNS_TARGET_THRESHOLD = 0.0  # 0.0ns = 时序收敛

迭代流程:
1. get_completion() → LLM tool-calling 循环
2. checkpoint 保存 + get_wns 确认 WNS → 更新 best_wns/latest_tns/latest_failing_endpoints
3. [FIX] 计算 wns_improved → _on_iteration_end() → _prev_best_wns (在 checkpoint 确认后)
4. 中间验证 (每 N 迭代)
5. 下一迭代

继续条件: iteration<50 AND WNS<0 AND global_no_improvement<3 AND tool_rounds<=22
         AND checkpoint保存成功 AND get_wns返回有效值

WNS回归处理: WNS<0且差于best时自动回滚
完成判定: 使用latest_wns（当前），非best_wns（历史）
```

### 5.1 迭代边界模型切换

**机制**:
- `_on_iteration_end()` 时调用 `_select_model()` 决定下一迭代使用的模型
- 预定的模型存入 `self._next_iteration_model`
- 下一迭代 `get_completion()` 开头直接使用预定模型，不再重新选择
- 交接提示词（`_iteration_handoff_prompt`）在迭代结束时生成，模型分层专属

**交接提示词（上下文工程优化）**:
- `_generate_iteration_handoff_prompt()` → 分发器，根据 next_tier 调用对应生成器
- `_generate_planner_handoff()`: Planner (1M context) → 完整迭代轨迹 + 策略分析 + 数据驱动目标 + 退出原因 + 续接指令（~500-800 tokens）
- `_generate_worker_handoff()`: Worker (250K context) → 最近3迭代浓缩 + 续接指令 + 退出标签（~200-300 tokens）
- **Planner handoff 结构**:
  - `=== EXIT REASON ===`: 上一轮退出原因（Tool Round Limit / Premature DONE / Cost Limit）+ 当前 WNS
  - `=== CONTINUATION DIRECTIVE ===`: 显式续接指令（"不要从头重启，从中断处继续"）+ 中断策略详情
  - `=== ITERATION TRAJECTORY ===`: 完整迭代轨迹
  - `=== NEXT OPTIMIZATION GOAL ===`: 含 continuation 前缀的数据驱动目标
- **Worker handoff 结构**:
  - `=== CONTINUATION ===`: 单行续接指令 + Exit 标签
  - `=== RECENT TRAJECTORY (last 3) ===`: 最近 3 次迭代浓缩
- 策略中断检测: `_detect_unfinished_strategy()` 分析上一轮 tool call，判断策略是否被 tool_round_limit / premature DONE 中断（检查最后 2 步是否有 report_timing_summary），信息注入 handoff 和 goal 前缀
- 首次迭代上下文: 无 handoff 时自动注入 `**FIRST ITERATION** - Begin with initial design analysis...` 提示模型先分析再优化
- Handoff 注入方式: 插入为独立 system message（index=1），而非 prepend 到 user message
- 渐进式叙事: `_iteration_narratives[]` 记录每个迭代的结构化摘要（iteration, model, wns_delta, strategy_label, outcome），最多 20 条
- 工具效果标注: `_build_tool_effect_summary()` → 工具名 + WNS 变化量（最近 8 条）
- 失败策略标注: `_build_failed_strategy_summary()` → 策略名 + 失败迭代号 + 当时 WNS（最近 5 条）
- 数据驱动目标: `_build_data_driven_goal()` 基于 WNS 轨迹和策略效果生成目标，若检测到策略中断则在目标前拼接 `[CONTINUATION]` 前缀
- WNS 状态单点注入: `_inject_wns_state_to_system_prompt()` 为唯一真源，handoff 引用之（消除字段重复）
- `next_model` 注入到 "Current Optimization State" section
- SYSTEM_PROMPT.TXT 新增 `iteration_handoff` 指导节，解释 handoff section 含义及续接规则

**限制迭代内切换**:
- 只有首次迭代或 fallback 场景才允许迭代内模型重新选择
- 预定模型场景下，迭代内任务类别变化不会触发模型切换

### 5.2 WNS解析修复（Bug Fix）

**问题**: `report_timing_summary` 在 `phys_opt_design` 后执行时，输出缓冲区包含前一个命令的残留内容（许可证消息、命令回显等），导致 `parse_timing_summary_static()` 无法找到 `WNS(ns) TNS(ns)` 头，行返回 `None`。

**修复**: `parse_timing_summary_static()` 增强了跳过非时序行的逻辑：
- 跳过许可证消息 (`Attempting to get a license`, `Got license`)
- 跳过 info/warning/error 消息 (`INFO:`, `WARNING:`, `ERROR:`, `Common 17-`)
- 跳过命令回显 (`Command:`, `phys_opt_design`, `place_design`, `route_design`, `report_`)
- 在整个输出中搜索时序头，而非假设在开头

### 5.3 flow_control=DONE 信号处理（Bug Fix）

**问题**: LLM 返回 `action: DONE` 或 `flow_control: DONE` 时，系统只解析 `tool_calls:` 字段，忽略 YAML 中的 `action`/`flow_control` 字段。导致系统继续在同一迭代内循环调用 LLM，产生垃圾输出。

**语义澄清**:
- `flow_control: DONE` ≠ 退出信号
- `flow_control: DONE` = 当前迭代分析完成，需要进入下一迭代继续优化
- 真正退出条件 = 达到目标 fmax（WNS >= 0）

**修复**: `get_completion()` 中新增 `_parse_action_from_yaml()` 解析 `action`/`flow_control`：
- 解析方式与 `_parse_yaml_tool_calls()` 相同（block 分割 + yaml.safe_load）
- 检测到 `flow_control == "DONE"` 或 `action == "DONE"` 时：
  - 目标 fmax 已达成（WNS >= 0）→ `is_done=True`，退出优化
  - 目标未达成 → 设置 `_end_iteration_on_return=True`，退出 `get_completion()`，主循环进入下一迭代
- 不再在同一迭代内空转调用 LLM

**关键行为变化**:
| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| LLM 返回 `flow_control: DONE`，WNS=-0.538 | 继续在同一迭代内空转，产生垃圾输出 | 正确识别为迭代完成，进入下一迭代 |
| LLM 返回 `flow_control: DONE`，WNS>=0 | 继续空转 | 正确退出优化 |
| LLM 返回无 tool_calls，无 DONE 信号 | continue 回循环 | 沿用原有逻辑 |

### 5.4 flow_control=DONE 优化补丁（2026-05-01）

**问题 1: WNS 改善判定时序错误**
- `_on_iteration_end()` 和 `_prev_best_wns` 在 checkpoint/get_wns 之前执行，导致 `global_no_improvement` 可能使用 stale WNS 错误递增
- **修复**: 将 `_on_iteration_end()` 和 `_prev_best_wns` 移到 checkpoint/get_wns 成功之后，确保 counter、model selection、handoff prompt 都使用确认后的 WNS

**问题 2: get_completion() break→None 返回**
- `user_requested`、`tool_round_limit`、`cost_limit` 等路径使用 `break` 退出 while 循环，导致 `get_completion()` 隐式返回 None
- `optimize()` 将 None 统一当 `tool_round_limit` 处理，`cost_limit` 的真实原因丢失
- **修复**: 所有 `break` 改为 `return content, is_done`，确保退出原因正确传递
- `_is_done_reason` 覆盖添加 `if self._is_done_reason is None:` guard

**问题 4: LLM 过早声明 DONE**
- System Prompt 定义 `DONE: "Optimization complete"` 但系统实际将 WNS<0 时的 DONE 诠释为"迭代结束"——语义不匹配
- TNS 和 failing_endpoints 未注入 Context，LLM 缺少对问题规模的全局感知
- **修复**:
  - `SYSTEM_PROMPT.TXT`: DONE 语义收紧为 `WNS >= 0 achieved`；新增 `flow_control_rules` 显式规则；新增 `strategy_exhausted` 示例（用 SWITCH_STRATEGY 替代 DONE）
  - `_inject_wns_state_to_system_prompt()`: 注入 `current_tns` 和 `failing_endpoints` 到 Current Optimization State
  - 新增 `latest_tns`、`latest_failing_endpoints` 实时追踪（auto-eval + report_timing_summary）
  - `optimize()`: 检测到 `flow_control_done_next_iteration` 时注入 corrective feedback user message，告知下一 LLM "上轮过早 DONE，优化未完成"

**新增状态变量**:
- `latest_tns: Optional[float]` — 最新 TNS（从 timing report / auto-eval 获取）
- `latest_failing_endpoints: Optional[int]` — 最新失败端点计数

## 6. 429降级机制

```
1. 保存rate_limited_model（修复日志错误）
2. 标记为耗尽
3. 尝试下一fallback（轮询）
4. 成功后清last_exception，更新model_worker
5. 全耗尽则切换到model_planner
6. 切换时清空双方耗尽集合

_select_model()检查: worker在耗尽列表则强制planner
```

## 7. 控制台退出

```
_user_exit_requested: threading.Event   # 同步退出标志
_async_exit_requested: asyncio.Event    # 异步退出标志（与async代码兼容）
检查点: optimize()循环开始、get_completion()工具轮次间、LLM调用返回后
输入"quit"请求优雅退出
响应延迟: LLM调用完成后立即检查（最多等待LLM调用完成）
```

## 8. 退出原因

| 原因 | 描述 |
|------|------|
| `cost_limit` | 达到$1.00硬限制 |
| `wns_target_met` | WNS>=0.0（时序收敛） |
| `max_iterations_reached` | 3次迭代无改进 |
| `tool_round_limit` | 22轮工具调用达限 |
| `user_requested` | 用户输入quit |
| `flow_control_done_next_iteration` | LLM返回flow_control=DONE但目标未达成，进入下一迭代 |

## 11. DCP验证

```
每5次迭代: 中间checkpoint验证（500向量）
完成时: 完整验证（Phase1+Phase2, 10000向量）
触发条件: validation_enabled AND intermediate_dcp存在 AND 非完成态 AND iteration%5==0
```

## 10. 心跳日志系统

```
tool call 入口:
├── DCPOptimizer.call_tool()           # 主要入口，MCP工具调用
├── FPGAOptimizerTest.call_vivado_tool()    # 测试模式Vivado工具
└── FPGAOptimizerTest.call_rapidwright_tool() # 测试模式RapidWright工具

心跳机制 (_start_tool_heartbeat):
- 每60秒打印 [HEARTBEAT #{n}] Tool '{name}' still running after {elapsed}s
- 日志包含 extra 字段: tool_name, heartbeat_elapsed, heartbeat_count
- 工具完成时打印 [TOOL_COMPLETE] '{name}' completed in {elapsed}s (heartbeats: {n})
- 超时/异常时正确取消心跳任务

所有tool call路径统一心跳日志，无遗漏
```

## 12. 重要常量

```python
WORKER_HARD_LIMIT = 200K, WORKER_TOKEN_BUDGET = 35K
PLANNER_HARD_LIMIT = 300K, PLANNER_TOKEN_BUDGET = 100K
COST_HARD_LIMIT = $1.00 (planner+worker合计)
```

## 13. 工具输出摘要化 + 历史自动裁剪

### 动机
phys_opt_design / route_design 等工具的原始 Vivado 日志单次可达 25k+ 字符，
充斥 INFO 与重复的时序路径明细，导致模型注意力被稀释。

### 实现

**1. `_summarize_tool_result()`** (dcp_optimizer.py，位于 `_filter_tool_result()` 之后)

每个工具调用返回时自动提取结构化 YAML 摘要替代原始日志：
```yaml
tool_result:
  tool: vivado_phys_opt_design
  summary: "WNS: -0.939, TNS: -834.718, Failing endpoints: 1529"
  key_details:
    wns: -0.939
    wns_delta: +0.039
    tns: -834.718
    failing_endpoints: 1529
  status: completed
  raw_output_truncated: true
  raw_output_chars: 45231
```

- 利用已有 `parse_timing_summary_static()` 提取 WNS/TNS/failing_endpoints
- 与 `_prev_best_wns` 对比计算 delta
- 摘要替换原始文本进入对话历史（message pipeline 中 `call_tool()` → `_summarize_tool_result()` → `add_message()`）

**2. `_raw_tool_outputs`** (dcp_optimizer.py)

完整原始日志存储在 side buffer dict `{(iteration, round_index): raw_text}` 中，
FIFO 淘汰（最多 50 条）。仅当 LLM 调 `vivado_get_raw_tool_output` 时返回。

**3. `vivado_get_raw_tool_output`** (dcp_optimizer.py)

内部工具，不走 MCP 服务器。注册在 `_collect_tools()` 末尾，schema 包含迭代号/轮次/工具名筛选。

**4. 压缩阶段旧消息裁剪** (yaml_structured_compress.py)

`_compress_outdated_tool_results()`: 迭代差 > 2 的工具消息替换为：
- YAML 格式: `[Tool: vivado_phys_opt_design (iteration 3), wns=-0.939]`
- 旧格式: 正则提取 WNS + 首行截断
- 放在 timing report 压缩之后，系统消息分离之前

**5. 压缩后角色保留** (yaml_structured_compress.py, 2026-05-02 新增)

`preserve_role_turns=6`: 压缩后最近6条消息不进入YAML，而是保留原始API role：
```python
api_messages = [
  system("SYSTEM_PROMPT + WNS state"),      # 系统指令
  system("YAML compressed OLDER messages"),  # 旧消息YAML化
  user("..."),                               # ← 保留role
  assistant("...", tool_calls=[...]),        # ← 保留role
  tool("..."),                               # ← 保留role
]
```