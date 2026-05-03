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

### 2.3 WNS/TNS状态注入（已迁移至上下文快照）

```
API调用前 → _inject_wns_state_to_system_prompt()
    - 追加数据驱动scenario hint（avg_distance>70 → "distributed"场景 → PBLOCK推荐）
    - 追加analysis skill guide（get_skill_guide()，一次性注入含"Skill Catalog"标记）
    → 仅处理静态上下文增强，不再注入WNS状态

WNS动态状态 → 已迁移至 _build_context_snapshot()，作为 user message 注入（见 2.3.1）
```

### 2.3.1 Agent 上下文快照注入（user message，紧凑 YAML）

```
API调用前 → _prepare_api_messages() 中末尾调用 _inject_context_snapshot()
    ↓
_build_context_snapshot() 从现有实例变量构建 YAML：
    # === FPGA Context Snapshot ===
    primary_goal: "Achieve WNS >= 0 ns"
    current_best_wns: "-0.978 ns"
    remaining_violation: "0.978 ns"
    active_strategy: "PBLOCK (ACTIVE) -> PhysOpt (PLATEAUED)"
    failed_strategies: []
    do_not_repeat:
      - "phys_opt_design (already called 12 times with no improvement)"
    ↓
_inject_context_snapshot(api_messages):
    1. 扫描 api_messages 查找以 "# === FPGA Context Snapshot ==="
       开头的 user 消息 → 找到则移除（防止累积）
    2. 调用 _build_context_snapshot() 构建新 YAML
    3. 在第一个非 system 消息位置插入新的 user 消息
    → 每次 API 调用最多一条快照消息，零残留
```

**设计要点**：
- **User 角色**：不同于旧的 WNS 状态注入（system prompt），上下文快照放在 user 消息中，处于 system 消息之后、对话历史之前
- **紧凑格式**：仅 6 个字段，~150 tokens；合并 `current_wns` + `best_wns` 为 `current_best_wns`；`active_strategy` 展示策略链及各状态（ACTIVE/FAILED/PLATEAUED）
- **无持久化**：快照不进入 MessageStore，完全绕过压缩系统，每次 API 调用从当前状态重建
- **`do_not_repeat` 推导**：从 `tool_call_details` 聚合被调用 > 3 次且 WNS delta < 0.01ns 的工具，最多 5 条

### 2.4 关键信息保护

| 类型 | 存储位置 | 保护机制 |
|------|----------|----------|
| System消息 | Working memory（受保护） | 压缩前分离，始终前置 |
| WNS/TNS/策略状态 | 上下文快照（user message，独立于压缩系统） | 通过 `_build_context_snapshot()` → `_inject_context_snapshot()` 每 API 调用前注入为第一条 user 消息 |
| 失败策略 | CompressionContext | 存入YAML输出；`record_failure()` 在6个检测点被调用（工具异常/工具错误/SWITCH_STRATEGY/未完成策略检测/PBLOCK验证失败/路由失败） |
| Tool调用摘要 | MemoryManager._tool_call_details | 独立存储 |
| 最近N轮消息 | Working memory（role保留） | preserve_role_turns=6, 保持 user/assistant/tool 原始role不压缩进YAML |
| step: YAML 格式要求 | ① User message（会话起始）② System prompt 头部压印（每API调用前）③ System reminder/blockade（每API调用前） | 三重防御：User role 高注意力 + System prompt 前导压印 + 末尾贴近生成点；连续2次失败升级为 FORMAT BLOCKADE |
| Agent 上下文快照 | 临时 api_messages 列表（不进入 MessageStore） | 每次 API 调用前通过 `_inject_context_snapshot()` 注入为第一条 user 消息；查找并替换旧快照防止累积；包含 current_best_wns/active_strategy(含状态)/failed_strategies/do_not_repeat |
| 工具重复检测 | DCPOptimizer._recent_tools（滑动窗口） | 连续>=3次相同工具且WNS总变化<0.05ns时，注入 REPETITION DETECTED 警告 |
| 周期反思 | get_completion() 内嵌 | 每8个 tool_round 注入 REFLECTION CHECKPOINT，要求LLM评估策略有效性并显式 justify CONTINUE vs SWITCH_STRATEGY |
| Pblock合规性 | Vivado MCP 返回 + Summarizer 解析 | `create_and_apply_pblock` 追加 cells 计数；`_summarize_tool_result()` 解析 `cells_in_pblock`/`cells_in_design`，部分成功时设置 `status=partial`、添加 `compliance` 字段 |
| Tcl多行 | Vivado MCP `run_tcl_command()` | 按 `\n` 分割多行脚本，在同一 Vivado 会话中顺序执行（变量跨行持久化） |

### 2.5 模型选择

```
PLANNER: deepseek/deepseek-v4-pro (1M context, 复杂推理)
WORKER: deepseek/deepseek-v4-flash (250K context, 快速执行)
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
| smart_region | **60000** (1min) | 60000 / 120000 | 60.0 |
| pblock_strategy | **60000** (1min) | 60000 / 120000 | 60.0 |
| net_detour | 30000 (30s) | 30000 / 60000 | 120.0 |
| physopt_strategy | 360000 (6min) | 360000 / 720000 | 360.0 |
| fanout_strategy | 300000 (5min) | 300000 / 600000 | 300.0 * nets |
| optimize_cell | 60000 (1min) | 60000 / 120000 | 360.0 |

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

Skill 推荐机制 (`_build_skill_recommendation()`, 6 条件按优先级排列):
├── stagnation (no_improvement>=1) + PBLOCK not failed → rapidwright_analyze_pblock_region [诊断]
├── stagnation + Fanout not failed                    → rapidwright_execute_fanout_strategy [诊断]
├── stagnation + 都失败                                → rapidwright_analyze_net_detour [诊断]
├── avg_distance > 70 + PBLOCK not failed             → rapidwright_analyze_pblock_region
├── max_fanout > 100 + Fanout not failed              → rapidwright_execute_fanout_strategy
├── no_improvement>=2 + physopt tried                 → rapidwright_analyze_net_detour（分析型）
├── WNS > -2.0 + PhysOpt not failed                   → rapidwright_execute_physopt_strategy
└── 以上均不匹配                                        → 空（不推荐）

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

```yaml
# Worker: 速度优化, 250K max
worker:
  soft_threshold: 175K, hard_limit: 200K
  token_budget: 80K
  preserve_turns: 40, preserve_turns_aggressive: 10
  min_importance: 0.15, min_importance_aggressive: 0.7
  preserve_role_turns: 6
  fallback_models: ["stepfun/step-3.5-flash", "xiaomi/mimo-v2-flash"]

# Planner: 推理优化, 1M max
planner:
  soft_threshold: 200K, hard_limit: 300K
  token_budget: 80K
  preserve_turns: 60, preserve_turns_aggressive: 10
  min_importance: 0.1, min_importance_aggressive: 0.7
  preserve_role_turns: 6
  fallback_models: ["qwen/qwen3.6-plus", "xiaomi/mimo-v2.5-pro"]
```

## 5. 迭代控制

```python
MAX_TOOL_ROUNDS_PER_ITERATION = 80
GLOBAL_NO_IMPROVEMENT_LIMIT = 3
WNS_TARGET_THRESHOLD = 0.0  # 0.0ns = 时序收敛

迭代流程:
1. get_completion() → LLM tool-calling 循环
2. checkpoint 保存 + get_wns 确认 WNS → 更新 best_wns/latest_tns/latest_failing_endpoints
3. 计算 wns_improved → _on_iteration_end() → _prev_best_wns (在 checkpoint 确认后)
4. 中间验证 (每 N 迭代)
5. 下一迭代

继续条件: iteration<50 AND WNS<0 AND global_no_improvement<3 AND tool_rounds<=MAX_TOOL_ROUNDS_PER_ITERATION
         AND checkpoint保存成功 AND get_wns返回有效值

WNS回归处理: WNS<0且差于best时自动回滚
完成判定: 使用latest_wns（当前），非best_wns（历史）
```

### 5.1 迭代边界模型切换

**机制**:
- `_on_iteration_end()` 时调用 `_select_model()` 决定下一迭代模型，存入 `_next_iteration_model`
- 下一迭代 `get_completion()` 开头直接使用预定模型，不再重新选择
- 交接提示词迭代结束时生成，模型分层专属

**交接提示词**:
- **Planner** (~500-800 tokens): `EXIT REASON` → `CONTINUATION DIRECTIVE` → `ITERATION TRAJECTORY` → `NEXT OPTIMIZATION GOAL` → `RECOMMENDED SKILL`
- **Worker** (~200-300 tokens): `CONTINUATION` → `RECENT TRAJECTORY (last 3)`
- 策略中断检测: `_detect_unfinished_strategy()` 检查最后 2 步是否有 report_timing_summary
- 首次迭代: 注入 `**FIRST ITERATION** - Begin with initial design analysis...`
- Handoff 注入: 独立 system message（index=1）
- 辅助数据: `_iteration_narratives[]`（最多 20 条）、`_build_tool_effect_summary()`（最近 8 条）、`_build_failed_strategy_summary()`（最近 5 条）
- 数据驱动目标: `_build_data_driven_goal()` 基于 WNS 轨迹和策略效果

**限制迭代内切换**: 仅首次迭代或 fallback 场景允许迭代内模型重新选择。

### 5.2 WNS解析

`parse_timing_summary_static()` 会跳过许可证消息、命令回显和 info/warning 消息，在整个输出中搜索时序头，而非假设在开头。

### 5.3 flow_control 信号处理

**语义定义**:
- `flow_control: DONE` = 当前迭代分析完成，需要进入下一迭代继续优化（非退出信号）
- `flow_control: SWITCH_STRATEGY` = 当前策略已耗尽，系统强制执行迭代切换，注入分析引导 + skill推荐 + 强制下一轮先分析再选策略
- `flow_control: RETRY/ROLLBACK` = LLM级别指导，系统信任LLM执行，不作强制迭代切换
- 真正退出条件 = WNS >= 0

**行为矩阵**:
| 场景 | 行为 |
|------|------|
| `flow_control: DONE`，WNS=-0.538 | 进入下一迭代 |
| `flow_control: DONE`，WNS>=0 | 退出优化 |
| 无 tool_calls，无 DONE 信号 | 继续循环（纯文本处理） |
| `flow_control: SWITCH_STRATEGY` | 强制结束迭代 + 记录策略失败 + 注入分析引导 + skill推荐 + 下一轮先分析再行动 |
| 连续调用 physopt 无改进 | 降级推荐 analyze_net_detour 诊断绕路问题 |

### 5.4 DONE 优化补丁

关键修复：
- **WNS 改善判定时序**: `_on_iteration_end()` 和 `_prev_best_wns` 移到 checkpoint/get_wns 成功之后执行，确保 counter、model selection、handoff prompt 都使用确认后的 WNS
- **退出原因传递**: 所有 `break` 改为 `return content, is_done`，确保 `cost_limit` 等退出原因正确传递
- **LLM 过早 DONE 抑制**: SYSTEM_PROMPT 中 DONE 语义收紧为 `WNS >= 0 achieved`；注入 `current_tns` 和 `failing_endpoints` 让 LLM 感知问题规模

**新增状态变量**:
- `latest_tns: Optional[float]` — 最新 TNS
- `latest_failing_endpoints: Optional[int]` — 最新失败端点计数

### 5.5 Step YAML 状态追踪

每轮 LLM 响应到达后，先解析 content 中的 step YAML，flow_control 优先于工具执行。

**流程**：
```
LLM response arrives
  ↓
1. _parse_step_yaml(message.content) → StepState
2. 如果 flow_control ∈ {DONE, SWITCH_STRATEGY}
   → 跳过工具执行（即使同时有 tool_calls），跳转到 flow_control 处理
   else if tool_calls
   → 正常执行工具
   else （纯文本）
   → 现有纯文本处理逻辑
3. DONE/SWITCH_STRATEGY 时将 analysis 存入 _last_analysis
```

**StepState 数据结构**：
```python
@dataclass
class StepState:
    step_id: Optional[int] = None
    result_status: Optional[str] = None        # SUCCESS | PARTIAL | FAIL
    flow_control: Optional[str] = None         # CONTINUE | SWITCH_STRATEGY | DONE | RETRY | ROLLBACK
    analysis: dict = field(default_factory=dict)
    has_tool_calls: bool = False
    raw_content: str = ""
    parse_error: Optional[str] = None
```

**增强内容**: 迭代叙事新增 `result_status` 和 `scenario_match`；Planner handoff 新增 `=== LLM'S OWN ANALYSIS ===` 段。

**边界情况**: content=None + tool_calls → 空 StepState；DONE + tool_calls → flow_control 优先；旧模型不输出 YAML → 回退 `_parse_action_from_yaml`。

### 5.6 失败策略追踪（分级格式）

`record_failure()` 的 8 个触发点：

| 触发点 | 记录的策略 | reason | 条件 |
|--------|-----------|--------|------|
| SWITCH_STRATEGY 处理 | 当前迭代推断的策略 | `strategy_ineffective` | `_infer_strategy_from_tools()` 返回非 Information/Unknown |
| 工具调用超时 | 工具所属策略 | `tool_error` | 工具超时 |
| 工具调用异常 | 工具所属策略 | `tool_error` | 工具执行抛出异常 |
| 工具结果含错误 | 工具所属策略 | `tool_error` | 工具结果含 error/failed 关键字 |
| PBLOCK validation_failed | PBLOCK | `execution_failure` | `create_and_apply_pblock` 结果含 validation_failed |
| Fanout后评估缺失 | Fanout | `execution_failure` | Fanout优化后缺少post-eval |
| 路由失败 | PlaceRoute | `execution_failure` | `route_design`/`place_design` 失败 |
| 策略中断检测 | PBLOCK/Fanout | `execution_failure` | `_detect_unfinished_strategy()` 检测到验证缺失 |

**分级格式** (`list[dict]`，原为 `list[str]`)：
```python
{"strategy": "PBLOCK", "reason": "execution_failure",  # tool_error | execution_failure | strategy_ineffective
 "tool": "vivado_create_and_apply_pblock", "iteration": 3, "detail": "..."}
```

**`_build_skill_recommendation()` 分级过滤**：
- `reason="strategy_ineffective"` → 永久排除（LLM 自主判定策略无效）
- `reason ∈ {tool_error, execution_failure}` → 冷却 2 个迭代后可重试（工具/执行问题，非策略本身无效）
- `_strategy_blocked(name)` 辅助函数统一判断逻辑

**`failed_strategies` 的使用**：
- `_build_skill_recommendation()`: 分级过滤（truly_failed vs tool_failed），而非简单的 `set` 排除
- `_build_failed_strategy_summary()`: 展示策略名 + reason + tool + iteration
- `YAMLStructuredCompressor._compress_outdated_tool_results()`: 失败策略的工具结果优先压缩（兼容新旧格式）
- `_is_failed_strategy_tool_result()`: 兼容 `str`（旧格式）和 `dict`（新格式）
- `failed_strategy_names` 属性（compat.py/manage.py）: 向后兼容返回纯策略名列表

**向后兼容**：
- `failed_strategies` 属性仍返回列表（元素从 `str` 变为 `dict`）
- 新增 `failed_strategy_names` 属性返回 `list[str]`，供仅需策略名的代码使用（如 `_inject_wns_state_to_system_prompt()` 的 `tried` 集合计算）

### 5.7 step: YAML 格式要求三重防御

三重注入策略，逐级升级：

**注入 1 — User Message（一次性，会话起始）**
```
optimize() 中:
1. system_prompt → system prompt
2. user(FORMAT_GUARD)  ← 完整的 YAML 格式定义
3. user(initial_optimization_instructions)
```
User role 消息注意力权重大于 System role，只需注入一次。

**注入 2 — System Prompt 头部压印（每 LLM API 调用前）**
```
get_completion() 中 _inject_wns_state_to_system_prompt() 返回后:
system_content = "[FORMAT: EVERY response MUST include step: YAML block with "
                 "step_id/result_status/flow_control. No XML tags, no markdown fences.]\n\n"
                 + updated_content
```
确保格式约束始终处于 system prompt 最前沿，注意力权重最高。

**注入 3 — System Reminder/BLOCKADE（每 LLM API 调用前，末尾位置）**
```
get_completion() 中:
if _consecutive_format_failures >= 2:
    → FORMAT BLOCKADE（含完整 YAML 模板，~200 tokens）
else:
    → 简短 REMINDER（<10 tokens）
```
贴近模型生成点。`_consecutive_format_failures` 计数器：
- `_parse_step_yaml()` 有 `parse_error` 时 +1
- 成功解析 valid step_id 或 flow_control 时重置为 0
- 达到 2 时升级为 BLOCKADE，恢复后自动降级

**格式约束**: response 中必须包含一个 `step:` YAML 控制块（允许在 `step:` 块之前输出自然语言思维链推理）。解析代码已支持从文本中任意位置提取最后一个 `step:` 块。

### 5.8 工具重复检测器

`get_completion()` 工具循环内的实时检测：

```python
_recent_tools: list[tuple[str, float]]  # 滑动窗口 (tool_name, wns)，最多5条
```

**检测条件**：连续 >=3 次相同工具 + WNS 总变化 < 0.05ns

**触发时行为**：
```
REPETITION DETECTED: 'phys_opt_design' called 3+ consecutive times
with marginal WNS change (+0.020ns total).
Consider: (1) report_timing_summary to re-assess;
(2) if plateaued, diagnose root cause before continuing.
```
- 注入 user 消息到对话上下文
- 清空 `_recent_tools` 窗口防止重复报警
- 关联触发周期反思（不等下一周期）

**关键行为**：
- 中间穿插其他工具时正确重置窗口
- WNS 显著改善时（>=0.05ns）不触发
- 仅当 `_get_current_wns()` 返回有效值（非 None）时记录

### 5.9 周期反思触发器

`get_completion()` 工具循环内，每 8 个 `tool_round` 注入：

```
REFLECTION CHECKPOINT (tool round 8):
- Current WNS: -0.352ns (best: -0.300ns)
- Tools called this iteration: 8
- Step back and evaluate:
  1. Is your current strategy producing significant WNS improvement?
  2. If yes, continue. If no, is it time to SWITCH_STRATEGY?
  3. If unsure, call report_timing_summary to re-assess.
- Your next response MUST explicitly justify CONTINUE vs SWITCH_STRATEGY
  in the analysis.strategy_rationale field.
```

**触发规则**：
- `tool_round > 1` 且 `tool_round % 8 == 0`
- 重复检测器触发时：跳过周期等待，**立即**注入反思
- 与 `flow_control` 的 SWITCH_STRATEGY 语义一致

**联动**: 重复检测触发 → 清空窗口 + 注入重复警告 → 立即触发周期反思。两者形成"检测—警告—反思"的完整干预链条。

### 5.10 工具状态合规性

**Pblock Cells 计数** (Vivado MCP `create_and_apply_pblock()`)：
- `add_cells_to_pblock` 执行后，追加 `llength [get_cells -hierarchical -filter {pblock==<name>}]`
- 输出 `Cells in pblock: N` 和 `Total cells in design: M`

**Summarizer 合规性解析** (`_summarize_tool_result()` pblock 分支)：
- 解析 `Cells in pblock:` / `Total cells in design:` 行
- `cells_in_pblock < cells_in_design` 时：
  - `key_details["compliance"] = "added N/M cells (PARTIAL)"`
  - `status = "partial"`（非 `"success"`）
- LLM 可在 tool_result YAML 中直接识别部分成功状态

**Tcl 多行命令支持** (`run_tcl_command()`):
- 按 `\n` 分割多行脚本
- 在同一 Vivado 会话中逐行执行（`_run_single_tcl()`）
- 变量跨行持久化，解决之前的"无状态子shell"问题
- 单行命令行为不变（向后兼容）

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
| `tool_round_limit` | MAX_TOOL_ROUNDS_PER_ITERATION 轮工具调用达限 |
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
WORKER_HARD_LIMIT = 200K, WORKER_TOKEN_BUDGET = 80K
PLANNER_HARD_LIMIT = 300K, PLANNER_TOKEN_BUDGET = 80K
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

**5. 压缩后角色保留** (yaml_structured_compress.py)

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