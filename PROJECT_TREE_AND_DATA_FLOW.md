# FPL26 优化竞赛 - 项目结构与数据流

## 1. 项目结构

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # 主Agent: LLM编排、模型选择、压缩触发、_build_skill_recommendation()
├── config_loader.py              # 模型配置加载器（单例）
├── model_config.yaml             # 模型层级与fallback配置
├── validate_dcps.py              # DCP等价性验证器
├── SYSTEM_PROMPT.TXT             # 系统提示词
├── requirements.txt
├── CLAUDE.md                     # 项目指令文件
├── strategy_library.py           # 策略库
├── context_manager/              # 内存管理模块
│   ├── __init__.py
│   ├── manager.py                # MemoryManager - 中心编排，单次_compress()触发
│   ├── estimator.py              # TokenEstimator (tiktoken)
│   ├── events.py                 # EventBus - 订阅/取消订阅
│   ├── lightyaml.py              # YAML解析器
│   ├── interfaces.py             # 核心数据类
│   ├── agent_context.py          # AgentContextManager - 多Agent分支
│   ├── compat.py                  # 兼容性包装
│   ├── logging_config.py          # 日志配置
│   ├── stores/                    # 存储层
│   │   ├── __init__.py
│   │   └── memory_store.py
│   ├── memory/                    # 内存实现
│   │   ├── __init__.py
│   │   ├── historical_memory.py
│   │   └── working_memory.py
│   └── strategies/
│       ├── __init__.py
│       ├── base.py                # 压缩策略基类
│       ├── yaml_structured_compress.py  # YAML压缩基类 + 时序报告智能截断 + 过时时序报告替换
│       ├── planner_compress.py         # PlannerCompressor: 100K token_budget, preserve_turns=60, preserve_role_turns=6
│       └── worker_compress.py          # WorkerCompressor: 35K token_budget, preserve_turns=40, preserve_role_turns=6
├── RapidWrightMCP/               # RapidWright MCP服务器
│   ├── rapidwright_tools.py      # 工具函数实现
│   ├── server.py                 # MCP服务器入口
│   ├── test_server.py            # 服务器测试
│   └── requirements.txt
├── VivadoMCP/                    # Vivado MCP服务器
│   ├── vivado_mcp_server.py      # Vivado MCP服务器实现
│   ├── test_vivado_mcp.py        # 测试
│   └── requirements.txt
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
│   ├── strategy_plan.py             # 共享数据结构：StrategyPlan, StrategyStep
│   ├── net_detour_optimization.py   # Skill类 + 纯函数：绕路比率分析 + 重心放置优化
│   ├── smart_region_search.py       # Skill类 + 纯函数：智能PBlock区域搜索
│   ├── pblock_strategy.py           # Skill类：PBLOCK-Based Re-placement 策略
│   ├── physopt_strategy.py          # Skill类：Physical Optimization 策略
│   ├── fanout_strategy.py           # Skill类：High Fanout Net Optimization
│   ├── SKILL_SPECIFICATION.md        # Skill规范文档
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
    - **失败策略工具消息提前压缩**：已知失败策略的工具结果不受迭代年龄限制，直接压缩为 `[Tool: name (iteration N)]` 标记（节省 token）
    - `_is_failed_strategy_tool_result()`: 按工具名模式匹配 failed_strategies 列表（PBLOCK→含pblock, PhysOpt→含phys_opt, Fanout→含fanout/optimize_fanout, PlaceRoute→含place_design/route_design）
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
    - 追加数据驱动scenario hint（avg_distance>70 → "distributed"场景 → PBLOCK推荐）
    - 追加analysis skill guide（get_skill_guide()，一次性注入含"Skill Catalog"标记）
    - 不再注入 FORMAT_GUARD（已改为 User message 机制，见 5.7）
    → 模型始终看到当前状态，不依赖working memory
```

### 2.4 关键信息保护

| 类型 | 存储位置 | 保护机制 |
|------|----------|----------|
| System消息 | Working memory（受保护） | 压缩前分离，始终前置 |
| WNS状态 | MemoryManager（独立于WM） | API调用时注入 |
| TNS/Failing Endpoints | DCPOptimizer.latest_tns/latest_failing_endpoints | API调用时随 WNS 一同注入 |
| Tool调用摘要 | MemoryManager._tool_call_details | 独立存储 |
| 失败策略 | CompressionContext | 存入YAML输出；`record_failure()` 在6个检测点被调用（工具异常/工具错误/SWITCH_STRATEGY/未完成策略检测/PBLOCK验证失败/路由失败）|
| 最近N轮消息 | Working memory（role保留） | preserve_role_turns=6, 保持 user/assistant/tool 原始role不压缩进YAML |
| step: YAML 格式要求 | ① User message（会话起始）② System reminder（每API调用前） | 双重注入：User role 高注意力权重 + 末尾 System 贴近生成点 |

### 2.5 模型选择

```
PLANNER: openrouter/owl-alpha (1M context, 复杂推理)
WORKER: tencent/hy3-preview:free (250K context, 快速执行)
- 429降级: 按层级fallback列表，轮询+耗尽追踪
- 迭代边界切换: 模型切换在迭代结束保存检查点后，下一迭代开始时发生
- 交接提示词: 新模型收到包含最优状态、下一步目标的上下文
```

### 2.5.1 模型选择维度（`_select_model()`）

评分系统（8维度变量，前2个已注释，当前6个生效，加权得分高的模型胜出，margin=2防止震荡）：

| 维度 | 条件 | Planner得分 | Worker得分 |
|------|------|-----------|-----------|
| 1. 工具映射 | - | - | - (已注释) |
| 2. 任务类别 | - | - | - (已注释) |
| 3. 上下文复杂度 | >=6 | +2 | +1 (<3) |
| 4. 历史能力 | >=70%成功率 | - | +2 |
| 5. 历史能力 | <30%成功率 | +2 | - |
| 6. 连续失败 | >=2次 | +4 | - |
| 7. 连续成功 | >=3次 | - | +1 |
| 8. 全局无改善 | >=2.5次 | - | +1 |
| 9. 上下文容量 | >=60% worker限制 | +2 | - |
| 10. WNS状态 | 严重倒退(>-2.0ns) | +3 | - |


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
│   ├── strategy_plan.py             # 共享数据结构：StrategyPlan, StrategyStep
│
├── telemetry.py                    # SkillTelemetry + SkillExecutionTimer
├── net_detour_optimization.py      # Skill类 + 纯函数
├── smart_region_search.py          # Skill类 + 纯函数
├── pblock_strategy.py           # Skill类：PBLOCK-Based Re-placement 策略
├── physopt_strategy.py          # Skill类：Physical Optimization 策略
├── fanout_strategy.py           # Skill类：High Fanout Net Optimization
├── SKILL_SPECIFICATION.md        # Skill规范文档
├── descriptors/                 # 自动生成的JSON描述符文件（含test_mock_skill）
├── test_net_detour_optimization.py  # 单元测试（_group_pins_by_cell）
└── test_skill_framework.py      # 28项集成测试（含test_mock_skill）

已注册 Skills:
├── analysis.net_detour@1.0.0           # 分析关键路径网络的绕路比率（READ-ONLY）
├── placement.optimize_cell@1.0.0       # 基于重心优化单元布局（non-idempotent）
├── placement.smart_region@1.0.0        # 智能 PBlock 区域搜索（READ-ONLY）
├── optimization.pblock_strategy@1.0.0   # PBLOCK-Based Re-placement 策略
├── optimization.physopt_strategy@1.0.0  # Physical Optimization 策略
├── optimization.fanout_strategy@1.0.0   # High Fanout Net Optimization
└── analysis.test_mock_skill@1.0.0      # 测试用Mock Skill

Skill 超时映射（三层）:
| Skill | @skill decorator `timeout_ms` | JSON descriptor `defaultMs/maxMs` | 测试调用 `timeout` |
|-------|-------------------------------|-----------------------------------|-------------------|
| smart_region | 600000 (10min) | 600000 / 720000 | 600.0 |
| pblock_strategy | 600000 (10min) | 600000 / 720000 | 600.0 |
| net_detour | 30000 (30s) | 30000 / 60000 | 120.0 |
| physopt_strategy | **360000** (6min) ⚠️ | 30000 / 60000 | 360.0 |
| fanout_strategy | 300000 (5min) | 300000 / 600000 | 300.0 * nets |
| optimize_cell | **60000** (1min) ⚠️ | 360000 / 420000 | 360.0 |

⚠️ physopt_strategy 和 optimize_cell 的 @skill decorator timeout_ms 与 JSON descriptor 不一致，疑似遗留值。

三层超时的作用域:
1. **@skill decorator** — 技能框架内部心跳检测阈值（skills/*.py）
2. **JSON descriptor** — 声明性元数据，供外部系统参考（skills/descriptors/*.json）
3. **测试调用 timeout** — asyncio.wait_for 实际截止时间（dcp_optimizer.py call_rapidwright_tool）

分析型 vs 策略型 Skill:
├── 分析型 (net_detour/optimize_cell/smart_region): 诊断+微观优化，推荐工作流三步走
│   ├── Step1 DIAGNOSE: analyze_net_detour → 找出绕路比>2.0的cell
│   ├── Step2 FIX: optimize_cell_placement → 移动到连接质心
│   └── Step3 CONTAIN: smart_region_search + strategy skills → 地理约束
├── 策略型 (physopt): 封装完整多步策略工作流，一键式执行
│   ├── analyze_pblock_region: avg_distance>70 → READ-ONLY分析, 返回pblock_ranges (LLM自行调Vivado工具串)
│   │   └── Vivado工具串: place_design -unplace → create_and_apply_pblock → place_design → route_design → report_timing_summary
│   ├── execute_physopt_strategy: 1-2 paths with spread, WNS>-2.0 → phys_opt+route+timer
│   └── execute_fanout_strategy: fo>100 → optimize_fanout_batch+write_checkpoint, 返回优化结果(LLM自行调Vivado工具串)

Skill 推荐机制 (_build_skill_recommendation()):
├── avg_distance > 70 AND PBLOCK not failed  → rapidwright_analyze_pblock_region
├── max_fanout > 100 AND Fanout not failed   → rapidwright_execute_fanout_strategy
├── no_improvement >=2 + physopt tried       → rapidwright_analyze_net_detour（分析型）
├── WNS > -2.0 AND PhysOpt not failed        → rapidwright_execute_physopt_strategy
└── 以上均不匹配                               → 空（不推荐）

Skill 推荐注入点:
├── _build_data_driven_goal() → 追加在NEXT OPTIMIZATION GOAL末尾
├── _generate_planner_handoff() → 新增=== RECOMMENDED SKILL ===段
├── _generate_worker_handoff() → 新增=== RECOMMENDED SKILL ===段
└── SWITCH_STRATEGY 处理器 → 注入消息末尾追加skill推荐

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

> **注意**: YAML文件中的值与compress.py中硬编码的token_budget不一致。实际生效值以compress.py为准（worker=35K, planner=100K），YAML中的token_budget为80K/80K。

```yaml
# Worker: 速度优化, 250K max
worker:
  soft_threshold: 175K, hard_limit: 200K
  token_budget: 35K (compress.py硬编码), YAML中为80K
  preserve_turns: 40, preserve_turns_aggressive: 10
  min_importance: 0.15, min_importance_aggressive: 0.7
  preserve_role_turns: 6
  fallback_models: ["deepseek/deepseek-v4-flash", "xiaomi/mimo-v2-flash"]

# Planner: 推理优化, 1M max
planner:
  soft_threshold: 200K, hard_limit: 300K
  token_budget: 100K (compress.py硬编码), YAML中为80K
  preserve_turns: 60, preserve_turns_aggressive: 10
  min_importance: 0.1, min_importance_aggressive: 0.7
  preserve_role_turns: 6
  fallback_models: ["qwen/qwen3.6-plus", "xiaomi/mimo-v2.5-pro"]
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
  - `=== EXIT REASON ===`: 上一轮退出原因（Tool Round Limit / Premature DONE / Cost Limit / Strategy Switch）+ 当前 WNS
  - `=== CONTINUATION DIRECTIVE ===`: 显式续接指令（"不要从头重启，从中断处继续"）+ 中断策略详情
  - `=== ITERATION TRAJECTORY ===`: 完整迭代轨迹
  - `=== NEXT OPTIMIZATION GOAL ===`: 含 continuation 前缀的数据驱动目标（WNS未收敛时追加skill推荐）
  - `=== RECOMMENDED SKILL ===`: 基于 spread/fanout/WNS轨迹 推荐的具体skill工具名
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
- `flow_control: SWITCH_STRATEGY` = 当前策略已耗尽，系统强制执行迭代切换，注入分析引导（含skill推荐）+ 强制下一轮先分析再选策略
- `flow_control: RETRY/ROLLBACK` = LLM级别指导，系统信任LLM执行，不作强制迭代切换
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
| LLM 返回 `flow_control: SWITCH_STRATEGY` | 与CONTINUE同处理，继续循环 | 强制结束迭代 + 记录当前策略为失败 + 注入分析引导 + skill推荐 + 下一轮先分析再行动 |
| LLM 连续调用 physopt 无改进 | 继续循环调用physopt | 降级推荐 analyze_net_detour 诊断绕路问题 |

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

### 5.5 Step YAML 状态追踪（2026-05-02 新增）

**问题**：LLM 使用原生 tool calls 时，只返回工具调用，不返回 `step:` YAML 块中的状态控制数据（`step_id`, `result_status`, `flow_control`, `analysis`）。只有纯文本响应（无 tool_calls）才会进入 `_parse_action_from_yaml()` 解析 flow_control。

**修复**：三处改动：

1. **新增 `StepState` dataclass** + `_parse_step_yaml()` 统一解析器
2. **每轮都解析 step YAML**：无论是否有 tool_calls，先解析 content 中的 step YAML
3. **flow_control 先于工具执行检查**：若 flow_control 是 DONE/SWITCH_STRATEGY，跳过工具执行

**新流程**：
```
LLM response arrives
  ↓
1. _parse_step_yaml(message.content)  →  StepState
2. Log [STEP_STATE] (step_id, result_status, flow_control, scenario_match, hypothesis)
3. 如果 flow_control ∈ {DONE, SWITCH_STRATEGY}
   → 警告矛盾信号（若同时有 tool_calls），跳过工具执行
   → 跳转到 flow_control 处理
   否则如果有 tool_calls
   → 正常执行工具
   否则（纯文本）
   → 现有纯文本处理逻辑（不变）
4. flow_control 分支使用已解析的 StepState（回退 _parse_action_from_yaml）
5. DONE/SWITCH_STRATEGY 时将 analysis 存入 _last_analysis
```

**新增数据结构**：
```python
@dataclass
class StepState:
    step_id: Optional[int] = None
    result_status: Optional[str] = None        # SUCCESS | PARTIAL | FAIL
    flow_control: Optional[str] = None         # CONTINUE | SWITCH_STRATEGY | DONE | RETRY | ROLLBACK
    analysis: dict = field(default_factory=dict)  # observed_signals, scenario_match, hypothesis, strategy_rationale
    has_tool_calls: bool = False
    raw_content: str = ""
    parse_error: Optional[str] = None
```

**实例变量**：
- `self._step_state: Optional[StepState]` — 最近一次解析的 step state
- `self._last_analysis: dict` — 最近一次 DONE/SWITCH_STRATEGY 的 analysis

**迭代叙事增强**：`_append_iteration_narrative()` 新增字段：
- `result_status`: step state 中的 result_status
- `scenario_match`: step state 中的 scenario_match

**Handoff 增强**：`_generate_planner_handoff()` 新增 `=== LLM'S OWN ANALYSIS ===` 段，包含 LLM 自身的 scenario_match、hypothesis、strategy_rationale、observed_signals。

**边界情况处理**：
- `content=None + tool_calls` → 空 StepState，正常执行工具
- 矛盾信号（DONE + tool_calls）→ 打印警告，flow_control 优先，不执行工具
- 旧模型不输出 YAML → 空 StepState，回退到 `_parse_action_from_yaml`
- 多轮工具调用 → 每轮都解析 step YAML，step_id 可用于追踪轮次

### 5.6 失败策略追踪增强（2026-05-02 新增）

**新增 `record_failure()` 调用点**（共 8 处触发点）：

| 触发点 | 记录的策略 | 条件 |
|--------|-----------|------|
| SWITCH_STRATEGY 处理 | 当前迭代推断的策略 | `_infer_strategy_from_tools()` 返回非 Information/Unknown |
| 工具调用超时 | 工具所属策略 | 工具超时（dcp_optimizer.py:3146） |
| 工具调用异常 | 工具所属策略 | 工具执行抛出异常（dcp_optimizer.py:3204） |
| 工具结果含错误 | 工具所属策略 | 工具结果含 error/failed 关键字（dcp_optimizer.py:4062） |
| PBLOCK validation_failed | PBLOCK | `create_and_apply_pblock` 结果含 validation_failed（dcp_optimizer.py:4073） |
| Fanout后评估缺失 | Fanout | Fanout优化后缺少post-eval（dcp_optimizer.py:2164） |
| 路由失败 | PlaceRoute | `route_design`/`place_design` 失败（非超时，dcp_optimizer.py:4453） |
| 策略中断检测 | PBLOCK/Fanout | `_detect_unfinished_strategy()` 检测到验证缺失 |

**`failed_strategies` 的使用**：
- `_build_skill_recommendation()`: 跳过已失败策略的推荐（PBLOCK/Fanout/PhysOpt 分别检查）
- `YAMLStructuredCompressor._compress_outdated_tool_results()`: 失败策略的工具结果优先压缩，不受迭代年龄限制
- `_is_failed_strategy_tool_result()`: 按工具名模式匹配（PBLOCK→pblock, PhysOpt→phys_opt, Fanout→fanout/optimize_fanout, PlaceRoute→place_design/route_design）

### 5.7 step: YAML 格式要求双重注入（2026-05-03 新增）

**问题**：`_inject_format_requirement_to_system_prompt()` 将 FORMAT_GUARD 预置到 system prompt 开头，但长上下文（~94K tokens）中该指令容易被淹没。

**方案**：双重注入，兼顾注意力权重与位置近端性：

**注入 1 — User Message（一次性，会话起始）**
```
optimize() 中:
1. system_prompt  →  system prompt（不含 FORMAT_GUARD）
2. user(FORMAT_GUARD)  ← 新增：完整的 YAML 格式定义
3. user(initial_optimization_instructions)
```
- User role 消息的注意力权重大于 System role 内容
- 只需注入一次，不需要重复
- 代码位置: [dcp_optimizer.py:4325-4353](dcp_optimizer.py#L4325-L4353)

**注入 2 — System Reminder（每 LLM API 调用前）**
```
get_completion() 中:
messages 末尾追加:
  {"role": "system", "content": "REMINDER: Your response MUST contain a 'step:' YAML block ..."}
```
- 极简内容（<10 tokens），几乎不增加成本
- 始终在 messages 最后一条，贴近模型生成点
- 压缩删掉也没关系——下次调用前会重新注入
- 代码位置: [dcp_optimizer.py:3786](dcp_optimizer.py#L3786)

**清理**：
- 删除了 `_inject_format_requirement_to_system_prompt()` 方法
- `_inject_wns_state_to_system_prompt()` 不再注入格式要求

**2026-05-03 更新**：放宽格式约束。原规则要求"每条 response 必须以 `step:` 开头"，改为"response 中必须包含一个 `step:` YAML 控制块"。允许在 `step:` 块之前输出自然语言思维链推理，降低 LLM 因格式约束而产生的认知负担。解析代码（`_parse_step_yaml` 等）已支持从文本中任意位置提取最后一个 `step:` 块。

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
| `switch_strategy` | LLM返回SWITCH_STRATEGY，系统强制执行迭代切换，下一轮分析后选新策略 |

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