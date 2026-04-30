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
│       ├── yaml_structured_compress.py  # YAML压缩基类 + 时序报告智能截断
│       ├── planner_compress.py         # PlannerCompressor: 100K token_budget, preserve_turns=60
│       └── worker_compress.py          # WorkerCompressor: 35K token_budget, preserve_turns=20
├── RapidWrightMCP/               # RapidWright MCP服务器
│   ├── rapidwright_tools.py      # 工具函数实现
│   ├── server.py                 # MCP服务器入口
│   └── test_server.py            # 服务器测试
├── VivadoMCP/                    # Vivado MCP服务器
├── skills/                       # Skill框架（标准接口、注册发现机制）
│   ├── __init__.py                  # 导出 Skill, SkillRegistry, SkillContext, SkillTelemetry
│   ├── base.py                      # Skill基类、元数据、结果定义
│   ├── context.py                   # SkillContext依赖注入
│   ├── registry.py                   # SkillRegistry注册发现
│   ├── skill_decorator.py           # @skill装饰器
│   ├── telemetry.py                  # 可观测性：执行记录、指标聚合、查询接口
│   ├── net_detour_optimization.py   # Skill类 + 纯函数（向后兼容）
│   ├── smart_region_search.py       # Skill类 + 纯函数：智能区域搜索（贪心扩展算法）
│   ├── test_net_detour_optimization.py  # 单元测试（_group_pins_by_cell）
│   └── test_skill_framework.py      # 集成测试（SkillRegistry/Skill/Telemetry）
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
    - 正常模式: preserve_turns=40/min_importance=0.15 (worker), preserve_turns=60/min_importance=0.1 (planner)
    - 激进模式(hard_limit触发): preserve_turns=10, min_importance=0.7
    - system消息始终保护
    - 两轮预算分配: 60%高重要性 + 40%中等重要性
    - preserve_turns预留预算: ~1500 tokens/turn, 最多10K
    - 工具调用保留参数（最多5个）
    - 时序报告智能截断（5项改进：动态预算/阈值过滤/起终点成对/时钟域分组/回退保护）
    - WNS状态注入时机: API调用时（不在working memory）
```

### 2.2 顺序压缩流程

```
1. 分离system消息（受保护）
2. HistoricalMemory.add(summary, importance=0.8)  ← 先归档
3. WorkingMemory.clear()                        ← 再清空
4. 添加system + 压缩后的非system消息            ← 重建
```

### 2.3 WNS状态注入（防压缩丢失）

```
API调用前 → _inject_wns_state_to_system_prompt()
    - 更新wns_ns、clock_period_ns
    - 追加"Current Optimization State:"块
    → 模型始终看到当前状态，不依赖working memory
```

### 2.4 关键信息保护

| 类型 | 存储位置 | 保护机制 |
|------|----------|----------|
| System消息 | Working memory（受保护） | 压缩前分离，始终前置 |
| WNS状态 | MemoryManager（独立于WM） | API调用时注入 |
| Tool调用摘要 | MemoryManager._tool_call_details | 独立存储 |
| 失败策略 | CompressionContext | 存入YAML输出 |

### 2.5 模型选择

```
PLANNER: xiaomi/mimo-v2.5-pro (1M context, 复杂推理)
WORKER: deepseek/deepseek-v4-flash (500K context, 快速执行)
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

（已移除：工具映射维度、任务类别维度）

### 2.6 Skill 机制

```
skills/
├── Skill (base.py)              # 抽象基类，定义 get_metadata() / execute()
├── SkillMetadata                # 元数据：name, description, category, parameters
├── SkillResult                  # 执行结果：success, data, error
├── SkillContext                 # 依赖注入：design, initialized, tools
├── SkillRegistry                # 注册/发现：register(), get(), list_all(), list_by_category()
├── @skill decorator             # 自动注册 Skill 类
├── SkillTelemetry               # 可观测性：record_execution(), get_metrics(), get_all_metrics()
├── SkillExecutionTimer           # 执行计时上下文管理器
├── net_detour_optimization.py   # Skill类 + 纯函数（向后兼容）
└── smart_region_search.py       # Skill类 + 纯函数：智能区域搜索

已注册 Skills:
├── analyze_net_detour           # 分析关键路径网络的绕路比率
├── optimize_cell_placement      # 基于重心优化单元布局
└── smart_region_search          # 智能 PBlock 区域搜索（贪心扩展）

LLM 调用 Skill 方式:
Agent → MCP Tool (如 smart_region_search)
         ↓
   rapidwright_tools.py wrapper
         ↓
   SkillRegistry.get("smart_region_search")
         ↓
   SmartRegionSearchSkill.execute_with_telemetry(context, **kwargs)
         ↓
   返回 SkillResult (success, data, error)

调用链:
Agent → MCP Tool → rapidwright_tools.py wrapper → SkillRegistry.get() → Skill.execute()
                                      ↓
                            SkillContext(design=_current_design)

Telemetry:
Skill.execute_with_telemetry() → 自动记录 duration_ms, status, params_summary
                                    ↓
                            SkillTelemetry.record_execution()
                                    ↓
                            SkillMetrics (聚合) + SkillExecutionRecord (历史)

Heartbeat:
- execute_with_telemetry() 启动 daemon heartbeat thread
- 每30秒打印 [SKILL_HEARTBEAT] Skill '{name}' still running after {elapsed}s
- 包含 extra: skill_name, heartbeat_elapsed, heartbeat_count
- 技能完成时打印 [SKILL_COMPLETE] '{name}' completed in {duration_ms}ms (heartbeats: {n})
- 快速完成的技能无 heartbeat 输出（30秒间隔内完成不触发）
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
  token_budget: 35K, preserve_turns: 40/10(激进), min_importance: 0.15/0.7(激进)
  max_chars_multiplier: 1.0 (正常) / 0.5 (激进)
  fallback_models: ["deepseek/deepseek-v4-flash", "stepfun/step-3.5-flash"]

# Planner: 推理优化, 1M max
planner:
  soft_threshold: 120K, hard_limit: 300K
  token_budget: 100K, preserve_turns: 60/10(激进), min_importance: 0.1/0.7(激进)
  max_chars_multiplier: 1.0 (正常) / 0.5 (激进)
  fallback_models: ["xiaomi/mimo-v2.5-pro"]
```

## 5. 迭代控制

```python
MAX_TOOL_ROUNDS_PER_ITERATION = 30
GLOBAL_NO_IMPROVEMENT_LIMIT = 5
WNS_TARGET_THRESHOLD = 0.0  # 0.0ns = 时序收敛

继续条件: iteration<50 AND WNS<0 AND global_no_improvement<5 AND tool_rounds<=22
         AND checkpoint保存成功 AND get_wns返回有效值

WNS回归处理: WNS<0且差于best时自动回滚
完成判定: 使用latest_wns（当前），非best_wns（历史）
```

### 5.1 迭代边界模型切换

**机制**:
- `_on_iteration_end()` 时调用 `_select_model()` 决定下一迭代使用的模型
- 预定的模型存入 `self._next_iteration_model`
- 下一迭代 `get_completion()` 开头直接使用预定模型，不再重新选择
- 交接提示词（`_iteration_handoff_prompt`）在迭代结束时生成，包含：
  - 当前/最佳 WNS 和检查点路径
  - 下一步优化目标
  - 最近使用的工具和失败策略

**限制迭代内切换**:
- 只有首次迭代或 fallback 场景才允许迭代内模型重新选择
- 预定模型场景下，迭代内任务类别变化不会触发模型切换

### 5.2 无工具调用迭代处理（Bug Fix）

**问题**: 当 LLM 返回无 tool_calls 时，原逻辑直接 return 导致迭代空转（无优化操作）

**修复**: `get_completion()` 中无 tool_calls 时：
- `is_done=False` → `continue` 回 while 循环，给 LLM 更多生成机会
- `is_done=True` → 正常 return 到主循环

**效果**: 避免空迭代浪费工具额度，5次硬限制改为真正无改进时触发

### 5.3 WNS解析修复（Bug Fix）

**问题**: `report_timing_summary` 在 `phys_opt_design` 后执行时，输出缓冲区包含前一个命令的残留内容（许可证消息、命令回显等），导致 `parse_timing_summary_static()` 无法找到 `WNS(ns) TNS(ns)` 头，行返回 `None`。

**修复**: `parse_timing_summary_static()` 增强了跳过非时序行的逻辑：
- 跳过许可证消息 (`Attempting to get a license`, `Got license`)
- 跳过 info/warning/error 消息 (`INFO:`, `WARNING:`, `ERROR:`, `Common 17-`)
- 跳过命令回显 (`Command:`, `phys_opt_design`, `place_design`, `route_design`, `report_`)
- 在整个输出中搜索时序头，而非假设在开头

### 5.4 flow_control=DONE 信号处理（Bug Fix）

**问题**: LLM 返回 `action: DONE` 或 `flow_control: DONE` 时，系统只解析 `tool_calls:` 字段，忽略 YAML 中的 `action`/`flow_control` 字段。导致系统继续在同一迭代内循环调用 LLM，产生垃圾输出（Minecraft 插件代码、Windows 路径等）。

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
| `max_iterations_reached` | 5次迭代无改进 |
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