# FPL26 优化竞赛 - 项目结构与数据流

> 读者：贡献者/评委。高层架构概览见 [README.md](README.md)，实现级技术细节见 [architecture.md](architecture.md)。

## 1. 项目结构（模块级）

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # CLI 入口：V2 状态机启动、模型配置
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
│   └── pure/                     # 14 个无状态纯函数模块（可独立单测），含 state_space.py（7 模块 StateSpace 构建器，含 Module 7 Architecture Overview）、timing.py（时序/路由/控制集/CDC/设计信息解析）、tool_filter.py（阶段白名单）、tool_router.py（MCP 路由+缓存）
├── architecture.md               # 架构技术细节（迁移映射、压缩管线、消息流等）
├── config_loader.py              # 模型配置加载器
├── model_config.yaml             # 模型层级与 fallback 配置
├── validate_dcps.py              # DCP 等价性验证器
├── strategy_library.py           # 14 种策略库（含 LogicResynthesis、PhysOptAggressive）
├── Makefile                      # 构建自动化
├── SYSTEM_PROMPT.TXT             # 系统提示词
├── CLAUDE.md                     # 项目指令文件
├── RapidWrightMCP/               # RapidWright MCP 服务器
├── VivadoMCP/                    # Vivado MCP 服务器
├── dashboard/                    # Web Dashboard（aiohttp + WebSocket）
│   ├── server.py                 # aiohttp 服务器 + DashboardStateTracer
│   ├── serializer.py             # OptimizerState → JSON（含 state_space 键）
│   └── static/index.html         # 自包含前端（暗色主题，20 面板：7 模块 StateSpace + 13 旧版详情）
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
│   ├── opt_design_strategy.py       # opt_design RapidWright skill wrapper
│   └── 14 个 Skill 实现文件 + 测试 + JSON 描述符
├── docs/                         # GitHub Pages 竞赛提交文档
└── (various config files)
```

## 2. 状态机驱动 Agent 架构（optimizer/）

### 2.1 状态模型（顶层结构）

```
OptimizerState (可变 dataclass，7 个子切片)
├── TimingState     — WNS/TNS/best_wns/关键路径/新鲜度追踪/hold时序/设备容量/拥塞数据/路由状态/控制集/设计信息/CDC/约束/PVT/violation_summary(路径聚类)/failing_endpoint_names
├── IterationState  — 迭代计数/no_improvement/工具名列表/narratives
├── ModelState      — 模型选择/fallback/交接提示词
├── CostState       — token 用量/成本追踪
├── ContextState    — 压缩计数/原始工具输出缓冲/工具结果缓存/LLM 消息日志/FC 决策轨迹/失败策略记录
├── ControlState    — 退出条件/DCP 路径/step_state
└── StrategyState   — 4 阶段策略生命周期（current_phase/策略/阶段历史/评估结果）
+ 7 模块仪表盘容器 (纯输出 dataclass，由 state_space.py 构建): StateSpace → DashboardGlobalState / DashboardTimingClusters / DashboardPhysicalCongestion / DashboardNetlistQuality / DashboardConstraints / DashboardDynamicGradient / DashboardArchitectureOverview
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

### 2.3 子图: llm_tool_loop（4 阶段状态机 + 多策略循环）

```
llm_tool_loop_node (调度器)
  │  while True:
  │    phase = PHASE_RUNNERS[phase](state, deps)
  │    if phase==EVALUATE && done_reason: exit or reselect
  │
  ├── ANALYZE ─────────→ SELECT_STRATEGY
  │  仅分析工具(~16个)   极简工具(~4个)
  │  首轮最多8轮(Dashboard已预填)  最多6轮
  │  后续迭代最多12轮
  │
  ├── SELECT_STRATEGY ─→ EXECUTE
  │  策略说明+执行计划    全工具(~25个, 不含vivado_open_checkpoint)
  │                      最多30轮, SKILL_CHAIN 自动串联
  │                      执行后可调 rapidwright_report_timing 快速反馈
  │
  ├── EXECUTE ─────────→ EVALUATE
  │  链式动作+事后评估    评估工具(~7个, 不含vivado_get_wns)
  │  工具结果缓存(同phase内相同参数自动命中, 执行工具后自动失效)
  │  PBLOCK自适应multiplier(公式C)
  │  DCP身份保护          最多30轮
  │  设计一致性验证工具(4个)  LLM可自主验证设计状态
  │  独立RapidWright工具(19个)  LLM可自主选择工具组合
  │
  └── EVALUATE → (exit) 或 SELECT_STRATEGY 或 ANALYZE
      DONE/WNS>=0 → ITERATION_END
      SWITCH_STRATEGY → SELECT_STRATEGY (多策略循环, 最多3轮/迭代)
      NEXT_ITERATION/ROLLBACK → ITERATION_END
      CONTINUE → ANALYZE
```

**多策略循环**: 一次迭代内最多尝试 3 个策略 (`MAX_STRATEGY_CYCLES=3`)。EVALUATE 阶段的 `SWITCH_STRATEGY` 信号触发循环回 SELECT_STRATEGY（跳过 ANALYZE）。失败策略通过 TTL 机制（3 轮迭代后自动解封）而非永久阻止。

阶段切换时：当前阶段消息压缩存档→HistoricalMemory，下一阶段注入 PhaseHandoff 摘要上下文。

> 阶段内完整消息流程、压缩细节、handoff 格式见 [architecture.md §3](architecture.md)。

### 2.4 关键设计原则

> 40 条设计原则（故障安全、数据可信度、DCP 身份完整性、工具缓存、自适应 PBLOCK、LLM 提示缓存等）。完整列表见 [README.md](README.md) 中文版核心设计原则和 [architecture.md §13](architecture.md)。

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

### 3.1.1 init_analysis 增强数据提取

`init_analysis_node` 在优化开始前一次性提取所有可获取的设计数据，填入 Dashboard 的 7 个模块，避免 LLM 在 ANALYZE 阶段浪费工具调用轮次：

```
Phase A (并行初始化):
  vivado_open_checkpoint ∥ rapidwright_initialize_rapidwright

Phase B (跨服务器并行管线):
  Vivado pipeline (10步串行) ∥ RapidWright pipeline (3步串行)
  Vivado:  timing_summary → clock_period → hold_timing → high_fanout_nets
           → resource_utilization(get_cells -filter, 非report_*) → critical_path_cells → route_status
           → control_sets → false_paths → multicycle_paths → IO_delay → CDC
  RapidWright: read_checkpoint → device_topology → design_info

Phase C (跨服务器分析):
  critical_path_spread → congestion_analysis
```

**提取数据与 Dashboard 模块映射**:

| 数据 | Dashboard 模块 | 新鲜度 |
|------|---------------|--------|
| WNS/TNS/Failing endpoints | M1 Global State | 动态 (后续工具刷新) |
| Clock period, hold timing, utilization | M1 Global State | 静态 / 动态 |
| Critical path cells + spread | M2 Timing Clusters | 动态 |
| Best WNS / best_wns_iteration | M1 Global State | 静态(初始为 N/A(initial_state)) |
| Cell count / net count | M1 Global State | 静态(来自 RapidWright) |
| Route status (wirelength, long nets) | M3 Physical Congestion | 动态 |
| Congestion (global score) | M3 Physical Congestion | 动态 |
| Control sets, CDC paths, cell types | M4 Netlist Quality | 静态 |
| High fanout nets | M4 Netlist Quality | 动态 |
| Constraints (false/multicycle paths, IO delay) | M5 Constraints | 静态 |
| PVT corner | M5 Constraints | 静态 |
| Delta data | M6 Dynamic Gradient | 初始为空 |
| Module-level timing hotspots (from critical path cell names) | M7 Architecture Overview | 动态（随着 critical_paths 更新） |

**验证**: `make run_init_analysis DCP=<path>` 运行完整提取 + Dashboard 构建 + 字段完整性检查，无需 LLM。

### 3.2 Agent 上下文 Dashboard (7 模块 StateSpace)

每轮 LLM 调用前注入纯数据 Dashboard（作为最后一条 user 消息，最大注意力权重）。数据由 `optimizer/pure/state_space.py` 从 `OptimizerState` 构建为规范化的 7 模块 YAML：

```yaml
[ANALYZE — Context & Dashboard]

# Module 1: Global State & Targets
global_state:
  current_stage: PLACEMENT
  iteration_count: 5
  target_frequency: 300.0
  wns_setup: -0.523
  tns_setup: -12.340
  whs_hold: 0.045
  lut_utilization: 49.16%
  ff_utilization: 19.66%

# Module 2: Timing Path Clusters (Top 20)
timing_clusters:
  top_paths:
    - endpoint: u_core/u_alu/reg_0
      slack: -0.523
      logic_delay_pct: 0.45
      route_delay_pct: 0.55
      logic_levels: 12
      path_group: clk_fpl26contest

  # Violation summary: compact aggregate (always shown with full Module 2)
  severity_distribution:
    critical_slack_lt_-1.0ns: 12
    moderate_slack_-1.0_to_-0.3ns: 38
    marginal_slack_-0.3_to_0ns: 77
  delay_profile_breakdown:
    logic_dominated_paths: 45
    route_dominated_paths: 62
    mixed_paths: 20
    dominant_delay_type: route_dominated
  logic_level_distribution:
    levels_1_to_5: 15
    levels_6_to_10: 52
    levels_gt_10: 60
  top_violating_modules:
    aes_core: 38 endpoints, min_slack=-0.52ns
  path_clusters:  # Representative path per cluster (方案3: 路径聚类压缩)
    - cluster: logic_dominated_aes_core
      path_count: 12
      slack_range: -1.200ns to -0.850ns
      avg_logic_delay_pct: 0.82
      avg_logic_levels: 14.0
      module: aes_core
    - cluster: route_dominated_pcie_ctrl
      path_count: 25
      slack_range: -0.600ns to -0.300ns
      avg_logic_delay_pct: 0.25
      avg_logic_levels: 3.5
      module: pcie_ctrl
  top_failing_endpoints:  # 方案6: 端点缓存
    - top/aes_core/sbox/reg_0
    - top/aes_core/mix_cols/reg_1
    - top/pcie_ctrl/dma/reg_2

# Module 3: Physical & Congestion Metrics
physical_congestion:
  global_congestion_score: 0.65
  avg_wirelength: 12.3
  long_route_nets_count: 42
  hotspots: [...]

# Module 4: Netlist Quality Profiler
netlist_quality:
  total_control_sets: 5
  avg_control_sets_per_slice: 0.12
  cross_domain_paths_count: 3
  top_cell_types: LUT6:1234, FDRE:5678, CARRY8:210, LUT5:98, ...
  high_fanout_nets: [...]

# Module 5: Constraints Environment
constraints_env:
  clock_definitions:
    clk_fpl26contest: 300.0 MHz
  false_paths_count: 2
  multicycle_paths_count: 1
  io_delay_defined_pct: 85.00%
  pvt_corner: slow_0p95v_85c

# Module 6: Dynamic Gradient (Delta)
# NOTE: last_action_taken and action_status are only populated during
# EXECUTE_STRATEGY and EVALUATE phases. In ANALYZE/SELECT_STRATEGY
# phases they are cleared to prevent stale iteration data from
# misleading the LLM (see _build_dynamic_gradient in state_space.py).
dynamic_gradient:
  delta_wns: +0.0770
  last_action_taken: PhysOpt
  action_status: Success

# Module 7: Architecture Overview
architecture_overview:
  top_modules:
    - name: "aes_core"
      critical_path_hits: 38
      path_coverage: 52.8%
      sub_modules: [sbox, mix_cols, key_expand]
    - name: "pcie_ctrl"
      critical_path_hits: 21
      path_coverage: 29.2%
      sub_modules: [dma, tlp]
  cross_module_paths: 3
  intra_module_paths: 6
  deepest_module: "aes_core/sbox"
  total_cells_analyzed: 72
```

**Phase-aware filtering**: `PHASE_STATESPACE_MODULES` 按阶段控制模块可见性——ANALYZE 阶段看 6 模块（M5 constraints 在 ANALYZE 隐藏，SELECT_STRATEGY 才出现；M7 architecture_overview 两阶段均可见），EXECUTE/EVALUATE 阶段看 M1 + M2b (timing_violation_summary 紧凑摘要) + M6。

**LLM 防歧义注解**: Dashboard 所有 N/A 和空列表都带有动态原因，区分"未分析"与"确实为零"：
- `best_wns: "N/A(initial_state)"` — 首次迭代前尚未有最佳值
- `high_fanout_nets: []  # no_high_fanout_nets_found` — 已分析，结果为 0
- `global_congestion_score: "N/A(congestion_analysis_not_supported)"` — 当前设备不支持该分析
- `io_delay_defined_pct: "N/A(no_io_ports)"` — 设计中无 IO 端口
- `long_route_nets_count: "N/A(data_not_available)"` — 路由状态数据不可用

- 纯数据，无判断标签 → LLM 自主推理
- 每次通过 `build_state_space()` 重建，不进入 MessageStore
- 同时作为 Web 前端 `data.state_space` 通过 WebSocket 推送
- **设计状态标注（design_not_routed）**: 当 `_post_eval_hook` / `_track_wns_from_result` 检测到 `report_timing_summary` 输出中 `Design State` 不含 `Routed` 时，设置 `state.timing.design_not_routed = True`。Dashboard M1 `current_stage` 下方显示 `⚠️ WARNING: Design is NOT routed — WNS/TNS may be inaccurate`，防止 LLM 基于未布线设计的虚假 WNS 做出错误决策。

> 完整 Dashboard 格式、新鲜度机制、critical path 管理见 [architecture.md §3.4-§3.5](architecture.md)。

### 3.3 关键信息保护

| 类型 | 保护机制 |
|------|----------|
| System 消息 | 压缩前分离，始终前置 |
| WNS/TNS/策略状态 | 上下文 Dashboard（user message，独立于压缩系统） |
| 失败策略 | `state.context.failed_strategies`（FailedStrategyRecord 列表）+ `record_strategy_failure()` 去重写入 |
| 工具调用摘要 | V2: `state.iteration.tools_used` 直接追加 |
| 最近 N 轮消息 | `preserve_role_turns=6` 保留原始 role |
| report_step_state 格式 | ① 一次性 User FORMAT_GUARD（精简版：行为要求 + EXECUTE 工具映射） ② 工具 schema 自描述参数 |
| 工具重复检测 | `_recent_tools` 滑动窗口，>=3次+delta<0.05ns → REPETITION DETECTED |
| 工具结果缓存 | `state.context.tool_cache` — 同 phase 内相同参数工具调用自动返回 `[CACHED]`，避免重复执行；phase 切换时清空。执行工具后自动失效缓存（`tool_cache.clear()`），防止过期物理数据被误用 |
| 工具调用频率限制 | `state.context.tool_phase_call_counts` — 只读工具超限后返回 `[RATE LIMITED]` 消息，引导 LLM 使用 Dashboard 数据或批量参数 |
| 周期反思 | 每 8 tool_round 注入 REFLECTION CHECKPOINT |
| DCP 身份 | EXECUTE 阶段从白名单移除 `vivado_open_checkpoint`；`current_dcp_path` 全程追踪 |
| 策略 catalog 排除 | 已失败策略自动从 SELECT_STRATEGY 阶段的策略目录中移除，避免重复选中 |
| 空结果模式匹配 | 工具返回 `optimized_count: 0` / `cascades_found: 0` 等空结果时归类为 `tool_error`（可重试）而非 `strategy_ineffective`（永久排除） |

### 3.4 模型选择

Planner（1M max）vs Worker（250K max），迭代边界切换。

`compute_model_scores()` 7 维度评分（margin=2 防震荡）：

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

> 14 个注册 Skills + SKILL_CHAIN_ACTIONS 自动链式执行 + OPTIONAL_CHAIN_VALIDATION。完整调用链、超时映射、推荐机制见 [architecture.md §4](architecture.md)。

### 3.6 策略库清单

> 14 种策略及触发条件。完整列表见 [README.md](README.md) 中文版优化策略和 [strategy_library.py](strategy_library.py)。

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
| `SWITCH_STRATEGY` (EVALUATE) | 多策略循环：回到 SELECT_STRATEGY 尝试下一策略（最多 3 轮/迭代） |
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
- **Phase 2 功能仿真**（Vivado xsim）：LFSR 随机测试激励（默认 200 向量，先运行 100 向量 precheck；可通过 `--vectors`/`--precheck-vectors` 调整）

每 5 次迭代中间 checkpoint 验证（500 向量），完成时完整验证。

> 完整验证策略、安全约束见 [architecture.md §7](architecture.md)。

## 9. 工具输出摘要化

大输出（Vivado 时序报告）→ 提取 WNS/TNS/failing_endpoints YAML 摘要，`raw_output_truncated: true`。小型输出（<3KB 非 timing）→ 直通嵌入。

原始日志存储在 side buffer（FIFO 50 条），LLM 可调 `vivado_get_raw_tool_output` 获取。

> 完整实现细节见 [architecture.md §6](architecture.md)。

## 10. 设计一致性验证工具

> 4 个验证工具 + 19 个独立 RapidWright 工具 + 可选链验证。完整清单和设计一致性约束见 [architecture.md §4.2](architecture.md)。

