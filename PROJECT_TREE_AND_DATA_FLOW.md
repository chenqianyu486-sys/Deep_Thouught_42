# FPL26 优化竞赛 - 项目结构与数据流

> 读者：贡献者/评委。高层架构概览见 [README.md](README.md)，实现级技术细节见 [architecture.md](architecture.md)。

## 1. 项目结构（模块级）

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # 主入口：LLM 编排、模型选择、V1/V2 中枢
├── optimizer/                    # V2 状态机框架（LangGraph 风格）
│   ├── state.py                  # OptimizerState + 7 个子切片 dataclass
│   ├── deps.py                   # NodeDeps：外部依赖容器
│   ├── graph.py                  # NodeGraph：节点注册、边注册、run 循环
│   ├── edges.py                  # 条件边函数 + NodeName 枚举
│   ├── color.py / tracing.py     # 工具：ANSI 着色 + 状态转换追踪
│   ├── llm_call_logger.py        # LLM 调用日志记录
│   ├── nodes/                    # 9 个节点实现 + llm_tool_loop 子图
│   │   ├── init_analysis.py      # 初始化分析
│   │   ├── iteration_start.py    # 迭代开始
│   │   ├── select_model.py       # 模型选择
│   │   ├── prepare_context.py    # 上下文准备
│   │   ├── iteration_end.py      # 迭代结束
│   │   ├── check_exit.py         # 退出检查
│   │   ├── rollback.py           # 回滚
│   │   ├── save_output.py        # 保存输出
│   │   └── subgraphs/            # llm_tool_loop + 4 阶段
│   └── pure/                     # 12 个无状态纯函数模块（可独立单测）
├── architecture.md               # 架构技术细节（迁移映射、压缩管线、消息流等）
├── config_loader.py              # 模型配置加载器
├── model_config.yaml             # 模型层级与 fallback 配置
├── validate_dcps.py              # DCP 等价性验证器
├── strategy_library.py           # 9 种策略库
├── Makefile                      # 构建自动化
├── SYSTEM_PROMPT.TXT             # 系统提示词
├── CLAUDE.md                     # 项目指令文件
├── RapidWrightMCP/               # RapidWright MCP 服务器
├── VivadoMCP/                    # Vivado MCP 服务器
├── dashboard/                    # Web Dashboard（aiohttp + WebSocket）
│   ├── server.py                 # aiohttp 服务器 + DashboardStateTracer
│   ├── serializer.py             # OptimizerState → JSON
│   └── static/index.html         # 自包含前端（暗色主题，13 面板）
├── context_manager/              # 内存/压缩管理
│   ├── manager.py                # MemoryManager 中心编排
│   ├── estimator.py              # ContextEstimator（tiktoken cl100k_base，全局统一token估算基准）
│   ├── events.py                 # EventBus
│   ├── interfaces.py / compat.py / lightyaml.py
│   ├── stores/ + memory/         # 存储层 + 内存实现
│   └── strategies/               # YAML 压缩策略（planner/worker）
├── skills/                       # Skill 框架（Skill Descriptor v3）
│   ├── base.py / context.py / registry.py / skill_decorator.py
│   ├── telemetry.py / errors.py / idempotency.py / tracing.py
│   ├── descriptor.py / validate_descriptors.py
│   ├── strategy_plan.py
│   └── 14 个 Skill 实现文件 + 测试 + JSON 描述符
├── docs/                         # GitHub Pages 竞赛提交文档
└── (various config files)
```

## 2. 状态机驱动 Agent 架构（optimizer/）

### 2.1 状态模型（顶层结构）

```
OptimizerState (可变 dataclass，7 个子切片)
├── TimingState     — WNS/TNS/best_wns/关键路径/新鲜度追踪
├── IterationState  — 迭代计数/no_improvement/工具名列表/narratives
├── ModelState      — 模型选择/fallback/交接提示词
├── CostState       — token 用量/成本追踪
├── ContextState    — 压缩计数/原始工具输出缓冲/LLM 消息日志/FC 决策轨迹/失败策略记录
├── ControlState    — 退出条件/DCP 路径/step_state
└── StrategyState   — 4 阶段策略生命周期（current_phase/策略/阶段历史/评估结果）
```

> 完整字段定义见 [optimizer/state.py](optimizer/state.py)。

### 2.2 图拓扑

```
init_analysis → [条件: timing met?]
  ├─ YES → save_output → end
  └─ NO  → iteration_start → select_model → prepare_context
            → llm_tool_loop(子图) → iteration_end → check_exit
            → [条件: done?]
              ├─ YES → save_output → end
              ├─ rollback? → ROLLBACK → iteration_start (循环)
              └─ NO  → iteration_start (循环)
```

### 2.3 子图: llm_tool_loop（4 阶段状态机）

```
llm_tool_loop_node (调度器)
  │  while True:
  │    phase = PHASE_RUNNERS[phase](state, deps)
  │    if phase==EVALUATE && done_reason: exit
  │
  ├── ANALYZE ─────────→ SELECT_STRATEGY
  │  仅分析工具(~18个)   极简工具(~4个)
  │  最多12轮             最多6轮
  │
  ├── SELECT_STRATEGY ─→ EXECUTE
  │  策略说明+执行计划    全工具(~25个, 不含vivado_open_checkpoint)
  │                      最多30轮, SKILL_CHAIN 自动串联
  │
  ├── EXECUTE ─────────→ EVALUATE
  │  链式动作+事后评估    评估工具(~8个)
  │  DCP身份保护          最多8轮
  │
  └── EVALUATE → (exit) 或 ANALYZE
      DONE/WNS>=0 → ITERATION_END; CONTINUE → ANALYZE
      NEXT_ITERATION/SWITCH_STRATEGY/ROLLBACK → ITERATION_END
```

阶段切换时：当前阶段消息压缩存档→HistoricalMemory，下一阶段注入 PhaseHandoff 摘要上下文。

> 阶段内完整消息流程、压缩细节、handoff 格式见 [architecture.md §3](architecture.md)。

### 2.4 关键设计原则

| # | 原则 | 实现 |
|---|------|------|
| 1 | 故障安全，不阻塞 | `report_step_state` 缺失时自动合成 `CONTINUE` |
| 2 | 事实而非判断 | Dashboard 仅含原始测量值，作为最后一条 user 消息注入 |
| 3 | 消除冗余 | Dashboard 是唯一实时数据源，handoff 仅传递迭代记忆 |
| 4 | 显式优于隐式 | 9 节点状态机 + 类型化 dataclass 状态切片 |
| 5 | 关注点分离 | Worker（250K）执行 vs Planner（1M）策略决策 |
| 6 | 单一调用路径 | V2 仅原生函数调用，无 XML/YAML 回退 |
| 7 | 单一事实来源 | 运行时数据在 OptimizerState；MemoryManager 仅存消息+执行压缩引擎；DCPOptimizerCompat 仅 V1 使用 |
| 8 | 领域知识编码 | 9 策略含触发条件，LLM 自主选择 |
| 9 | 数据可信度 | DASHBOARD_REFRESH_MAP 追踪字段新鲜度 |
| 10 | 信息保留 | 压缩标记保留 WNS/TNS/FE/delta/status |
| 11 | 逻辑等价性硬约束 | validate_dcps.py 验证（结构+功能） |
| 12 | DCP 身份完整 | EXECUTE 阶段移除 vivado_open_checkpoint 白名单 |

## 3. 核心数据流

### 3.1 数据流总览

```
add_message() → WorkingMemory → _compress_context() [ContextEstimator(tiktoken)精确计数] → MemoryManager._compress()
                                                            ↓
                                                  YAMLStructuredCompressor:
                                                    1. 归档→清空→YAML摘要→保留最近N条
                                                    2. 时序报告智能截断
                                                    3. 失败策略工具提前压缩
                                                    4. 反"鬼打墙"保护机制
                                                            ↓
_prepare_api_messages():
  1. auto_compact_messages()  ← 去重
  2. 增强系统提示词(scenario hint + skill catalog)
  3. 注入迭代 handoff（迭代边界）
  4. inject_context_snapshot_at_end() ← 数据 Dashboard 作为最后一条 user 消息
                                                            ↓
                                                   LLM API Call
```

> 完整消息流程、顺序压缩步骤、压缩参数表见 [architecture.md §3.1-§3.2](architecture.md)。

### 3.2 Agent 上下文 Dashboard

每轮 LLM 调用前注入纯数据 Dashboard（作为最后一条 user 消息，最大注意力权重）：

```
--- Optimization Dashboard ---
clock_period / wns_current / wns_best / tns / failing_endpoints
trajectory: [iter{N} strategy wns_before→wns_after delta]
design_signals: max_fanout / high_fanout_count / cp_spread / 资源利用率
critical_paths: [top 8 paths, 6 cells/path]
strategy_lifecycle: current_phase / current_strategy
skill_guidance: primary_tool / auto_chain / avoid
--- End Dashboard ---
```

- 纯数据，无判断标签 → LLM 自主推理
- 每次重建，不进入 MessageStore
- `do_not_repeat` 自动推导（>3次调用 + delta < 0.01ns）

> 完整 Dashboard 格式、新鲜度机制、critical path 管理见 [architecture.md §3.4-§3.5](architecture.md)。

### 3.3 关键信息保护

| 类型 | 保护机制 |
|------|----------|
| System 消息 | 压缩前分离，始终前置 |
| WNS/TNS/策略状态 | 上下文 Dashboard（user message，独立于压缩系统） |
| 失败策略 | `state.context.failed_strategies`（FailedStrategyRecord 列表）+ `record_strategy_failure()` 去重写入 |
| 工具调用摘要 | V2: `state.iteration.tools_used` 直接追加 |
| 最近 N 轮消息 | `preserve_role_turns=6` 保留原始 role |
| report_step_state 格式 | 双重提醒：① 一次性 User FORMAT_GUARD ② 每调用前 System prompt 压印 |
| 工具重复检测 | `_recent_tools` 滑动窗口，>=3次+delta<0.05ns → REPETITION DETECTED |
| 周期反思 | 每 8 tool_round 注入 REFLECTION CHECKPOINT |
| DCP 身份 | EXECUTE 阶段从白名单移除 `vivado_open_checkpoint`；`current_dcp_path` 全程追踪 |

### 3.4 模型选择

Planner（1M max）vs Worker（250K max），迭代边界切换。

`compute_model_scores()` 7 维度评分（margin=1 防震荡）：

| 维度 | Planner | Worker |
|------|---------|--------|
| 上下文复杂度 >=6 | +2 | +1 |
| 历史能力 >=70% | - | +2 |
| 历史能力 <30% | +2 | - |
| 连续失败 >=2 次 | +4 | - |
| 连续成功 >=3 次 | - | +1 |
| 全局无改善 >=2.5 次 | - | +1 |
| 上下文容量 >=60% | +2 | - |
| WNS 严重倒退 | +3 | - |
| 预算 >80% | - | +3 |
| 预算 >60% | - | +1 |

> 完整模型选择逻辑、handoff 提示词格式见 [architecture.md §3.7](architecture.md)。

### 3.5 Skill 机制

**已注册 Skills**（13 个分析型 + 1 个测试用）：

| Skill | 类型 | 说明 |
|-------|------|------|
| `analysis.net_detour@1.0.0` | READ-ONLY | 关键路径网络绕路分析 |
| `placement.optimize_cell@1.0.0` | non-idempotent | 基于重心优化单元布局 |
| `placement.smart_region@1.0.0` | READ-ONLY | 智能 PBlock 区域搜索 |
| `optimization.pblock_strategy@1.0.0` | READ-ONLY | PBLOCK 区域分析 |
| `optimization.execute_pblock_strategy@1.0.0` | non-idempotent | PBLOCK 全策略（分析+执行+自动串联） |
| `optimization.physopt_strategy@1.0.0` | non-idempotent | Physical Optimization |
| `optimization.fanout_strategy@1.0.0` | non-idempotent | 高扇出网线优化 |
| `analysis.analyze_congestion@1.0.0` | READ-ONLY | 布线拥塞分析 |
| `analysis.analyze_congestion_spreading@1.0.0` | READ-ONLY | 拥塞感知扩散分析 |
| `optimization.execute_congestion_spreading@1.0.0` | non-idempotent | 拥塞感知单元扩散 |
| `analysis.analyze_register_retiming@1.0.0` | READ-ONLY | Register Retiming 分析 |
| `optimization.execute_register_retiming@1.0.0` | non-idempotent | Register Retiming 执行 |
| `optimization.pin_swapping_strategy@1.0.0` | non-idempotent | 引脚交换优化 |
| `analysis.analyze_net_swapping@1.0.0` | READ-ONLY | Net Swapping 分析 |
| `optimization.execute_net_swapping@1.0.0` | non-idempotent | Net Swapping 执行 |
| `optimization.lut_cascade_flattening@1.0.0` | non-idempotent | LUT 串联展平 |
| `optimization.critical_path_cell_replication_strategy@1.0.0` | non-idempotent | 关键路径 Cell 复制 |

> 完整调用链、超时映射、推荐机制见 [architecture.md §4.1](architecture.md)。

### 3.6 策略库清单（strategy_library.py）

| 策略 | 触发条件 | 关联 Skill |
|------|---------|-----------|
| PBLOCK | 分布式场景 (avg_distance>70) | `rapidwright_execute_pblock_strategy` |
| PhysOpt | 1-2 paths with spread, WNS>-2.0 | `rapidwright_execute_physopt_strategy` |
| Fanout | fanout>100, 无 spread | `rapidwright_execute_fanout_strategy` |
| PinSwap | WNS 卡在 ~-0.3ns | `rapidwright_analyze_net_swapping` |
| LUTCascade | >3 级 LUT 串联 | `rapidwright_optimize_lut_input_cone` |
| CellReplication | fanout>10 或 delay>0.3ns | Vivado `phys_opt_design` |
| CongestionSpreading | congestion=HIGH | `rapidwright_analyze_congestion_spreading` |
| RegisterRetiming | 深组合逻辑链 (>2 LUTs) | `rapidwright_analyze_register_retiming` |
| NetSwap | SLICE 内布线拥塞 | `rapidwright_analyze_net_swapping` |

### 3.7 Tool 描述增强

在工具 description 中标注禁忌症、结果解读指南、策略交互警告。详见 [architecture.md §11](architecture.md)。

### 3.8 phys_opt_design 安全守卫

VivadoMCP 服务端 + dcp_optimizer.py 入口双层守卫，阻止以下指令：
- `AlternateFlowWithRetiming`、`AddRetime`（retiming 改变流水线结构）
- `retime=true`、`interconnect_retime=true`（布尔选项）

## 4. 迭代控制

### 4.1 常量

```python
MAX_TOOL_ROUNDS_PER_ITERATION = 80
GLOBAL_NO_IMPROVEMENT_LIMIT = 3
WNS_TARGET_THRESHOLD = 0.0
```

### 4.2 flow_control 信号处理

| 场景 | 行为 |
|------|------|
| `ANALYZE_DONE` | 切换到 SELECT_STRATEGY 阶段 |
| `EXEC_DONE` | 切换到 EVALUATE 阶段 |
| `DONE`, WNS<0 | 进入下一迭代 |
| `DONE`, WNS>=0 | 退出优化 |
| `SWITCH_STRATEGY` (EVALUATE) | 强制结束迭代 + 记录策略失败 + 下一轮从 ANALYZE 开始 |
| `NEXT_ITERATION` (EVALUATE) | 结束迭代 + 不记录失败 |
| `CONTINUE` (EVALUATE) | 回到 ANALYZE 阶段 |
| `detect_rollback_needed()` | WNS 退化时自动恢复最佳 checkpoint |
| `ROLLBACK` (EVALUATE) | LLM 主动请求回滚 |

所有信号通过 `record_flow_signal()` 录制到 `state.context.flow_control_log`，Dashboard 颜色编码展示。

> 完整行为矩阵、可观测性、StepState/FlowControlRecord 数据结构见 [architecture.md §5.2-§5.4](architecture.md)。

### 4.3 退出原因

| 原因 | 描述 |
|------|------|
| `cost_limit` | 达到成本硬限制 |
| `wns_target_met` | WNS>=0.0（时序收敛） |
| `max_iterations_reached` | 3 次迭代无改进 |
| `tool_round_limit` | 工具轮次达限 |
| `user_requested` | 用户输入 quit |
| `rollback` | WNS 退化且恢复后仍不改善 |

## 5. 429 降级机制

按层级 fallback 列表轮询 → 耗尽追踪 → 全耗尽切另一层级 → 清空双方耗尽集合。

## 6. 控制台退出

stdin 监听线程 → `state.control.user_exit_requested` → `save_output` → end（清标志防死循环）。

## 7. 事件系统

```python
EventBus: subscribe(event_type, handler) → token → unsubscribe_by_token(token) → emit(event)
EventTypes: CONTEXT_COMPRESSED, LAYER_PROMOTED, BRANCH_CREATED, BRANCH_MERGED
```

## 8. DCP 验证（硬约束）

两阶段验证（`validate_dcps.py`）：
- **Phase 1 结构对比**（RapidWright）：EDIF 网表结构一致性
- **Phase 2 功能仿真**（Vivado xsim）：10000 向量 LFSR 测试激励

每 5 次迭代中间 checkpoint 验证（500 向量），完成时完整验证。

> 完整验证策略、安全约束见 [architecture.md §7](architecture.md)。

## 9. 工具输出摘要化

大输出（Vivado 时序报告）→ 提取 WNS/TNS/failing_endpoints YAML 摘要，`raw_output_truncated: true`。小型输出（<3KB 非 timing）→ 直通嵌入。

原始日志存储在 side buffer（FIFO 50 条），LLM 可调 `vivado_get_raw_tool_output` 获取。

> 完整实现细节见 [architecture.md §6](architecture.md)。
