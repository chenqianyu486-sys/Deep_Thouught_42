# FPL26 优化竞赛 - 项目结构与数据流

> 读者：贡献者/评委。高层架构概览见 [README.md](README.md)（中文）/[README_EN.md](README_EN.md)（英文），实现级技术细节见 [architecture.md](architecture.md)。

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
│   └── pure/                     # 15 个无状态纯函数模块（可独立单测），含 state_space.py（7 模块 StateSpace 构建器，含 Module 7 Architecture Overview）、timing.py（时序/路由/控制集/CDC/设计信息解析）、tool_filter.py（阶段白名单）、tool_router.py（MCP 路由+缓存）、critical_path.py（关键路径解析 + 数据质量验证）
├── architecture.md               # 架构技术细节（迁移映射、压缩管线、消息流、数据质量守卫、冷却逻辑等）
├── config_loader.py              # 模型配置加载器
├── model_config.yaml             # 模型层级与 fallback 配置
├── validate_dcps.py              # DCP 等价性验证器
├── strategy_library.py           # 14 种策略库（含 LogicResynthesis、PhysOptAggressive、CombinationalRebalance、LUTMUXFRepack、MUXFTreeReorder）
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
├── skills/                       # Skill 框架（Skill Descriptor v3 + 渐进式三层加载）
│   ├── base.py / context.py / registry.py / skill_decorator.py
│   ├── lazy_loader.py             # 三层加载：regex 扫描（发现层）→ 懒 import（激活层）
│   ├── telemetry.py / errors.py / idempotency.py / tracing.py
│   ├── descriptor.py / validate_descriptors.py
│   ├── strategy_plan.py
│   ├── descriptors/               # 自动导出的 JSON 描述符（发现层数据源）
│   ├── opt_design_strategy.py       # opt_design RapidWright skill wrapper
│   ├── combinational_rebalancing_strategy.py
│   ├── lut_muxf_repack_strategy.py
│   ├── muxf_tree_reorder_strategy.py
│   └── 22 个 Skill 实现文件 + 测试 + JSON 描述符
├── docs/                         # GitHub Pages 竞赛提交文档
└── (various config files)
```

## 2. 状态机驱动 Agent 架构（optimizer/）

### 2.1 状态模型（顶层结构）

```
OptimizerState (可变 dataclass，7 个子切片)
├── TimingState     — WNS/TNS/best_wns/field_freshness(每字段fresh/stale状态)/hold时序/设备容量/拥塞数据/路由状态/控制集/设计信息/CDC/约束/PVT/violation_summary(路径聚类)/failing_endpoint_names
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
  │  仅分析工具(~17个，含探索性执行工具)   极简工具(~4个)
  │  首轮最多8轮(Dashboard已预填)  最多6轮
  │  后续迭代最多12轮
  │
  ├── SELECT_STRATEGY ─→ EXECUTE
  │  Dashboard+Handoff决策  极简工具(~4个,仅report_step_state+raw_tool_output)
  │  raw_tool_outputs侧缓冲   可见ANALYZE/EVALUATE阶段原始工具结果
  │  可查询                              默认最多6轮；仅纯数据描述，无策略推荐引导
  │  Dashboard含9个模块(新增 design_structure+recent_analysis)
  │
  ├── EXECUTE ─────────→ EVALUATE
  │  链式动作+事后评估    评估工具(~7个, 不含vivado_get_wns)
  │  工具结果缓存(同phase内相同参数自动命中, 执行工具后自动失效)
  │  PBLOCK自适应multiplier(公式C)
  │  DCP身份保护          动态轮数预算（默认5，复杂策略8）
  │  策略短冷却            无提升/预检拒绝后，本迭代内禁止重复
  │  设计一致性验证工具(4个)  LLM可自主验证设计状态
  │  独立RapidWright工具(19个)  LLM可自主选择工具组合
  │
  └── EVALUATE → (exit) 或 SELECT_STRATEGY 或 ANALYZE
      DONE/WNS>=0 → ITERATION_END
      SWITCH_STRATEGY → SELECT_STRATEGY (多策略循环, 最多5轮/迭代)
      NEXT_ITERATION/ROLLBACK → ITERATION_END
      CONTINUE → ANALYZE
```

**多策略循环**: 一次迭代内最多尝试 5 个策略 (`MAX_STRATEGY_CYCLES=5`)。EVALUATE 阶段的 `SWITCH_STRATEGY` 信号触发循环回 SELECT_STRATEGY（跳过 ANALYZE）。失败策略通过 TTL 机制（3 轮迭代后自动解封）而非永久阻止。

阶段切换时：当前阶段消息压缩存档→HistoricalMemory，下一阶段注入 PhaseHandoff 摘要上下文。

> 阶段内完整消息流程、压缩细节、handoff 格式见 [architecture.md §1](architecture.md)。

### 2.4 关键设计原则

> 40 条设计原则（故障安全、数据可信度、DCP 身份完整性、工具缓存、自适应 PBLOCK、LLM 提示缓存等）。完整列表见 [README.md](README.md) 核心设计原则和 [architecture.md §11](architecture.md)。

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

> 完整消息流程、顺序压缩步骤、压缩参数表见 [architecture.md §1.1-§1.2](architecture.md)。

### 3.1.1 init_analysis 增强数据提取

`init_analysis_node` 在优化开始前一次性提取设计数据，填入 Dashboard 7 个模块，避免 LLM 在 ANALYZE 阶段浪费工具调用轮次：

```
Phase A (并行): vivado_open_checkpoint ∥ rapidwright_initialize_rapidwright
Phase B (并行管线):
  Vivado: timing_summary→clock_period→hold_timing→high_fanout→utilization→critical_path→route_status→control_sets→false_paths→multicycle→IO_delay→CDC
  RapidWright: read_checkpoint→device_topology→design_info
Phase C (跨服务器): critical_path_spread→congestion_analysis
```

提取数据对应 Dashboard 模块：M1(WNS/TNS/Utilization)、M2(Critical paths + spread)、M3(Congestion/Route status)、M4(Control sets/CDC/High fanout)、M4b(Cell composition)、M5(Constraints/PVT)、M6(Delta)、M7(Module hotspots)。新鲜度通过 `field_freshness: dict[str, str]` 逐字段追踪：`init_analysis` 完成后全部初始化为 `fresh`，工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段为 `fresh`，设计修改工具（`DESIGN_MODIFICATION_TOOLS`）执行后全部字段降级为 `stale`。Dashboard 中每个值后显示 `[fresh]`/`[stale]` 标记。

**验证**: `make run_init_analysis DCP=<path>` 运行完整提取 + Dashboard 字段完整性检查。

### 3.2 Agent 上下文 Dashboard (7 模块 StateSpace)

每轮 LLM 调用前注入纯数据 Dashboard（最后一条 user 消息，最大注意力权重）。由 `optimizer/pure/state_space.py` 从 `OptimizerState` 构建为规范化 YAML：

```yaml
[ANALYZE — Context & Dashboard]

# Module 1: Global State & Targets
global_state:
  current_stage: PLACEMENT;  iteration_count: 5;  target_frequency: 300.0
  wns_setup: -0.523 [fresh];  tns_setup: -12.340 [fresh];  whs_hold: 0.045 [fresh]
  lut_utilization: 49.16% [fresh];  ff_utilization: 19.66% [fresh]

# Module 2: Timing Path Clusters
timing_clusters:
  freshness: "extracted_iter=2, stale=false, total_failing=1529" [fresh]
  top_paths:
    - endpoint: u_core/u_alu/reg_0
      slack: -0.523;  logic_levels: 12;  logic_delay_pct: 0.45;  route_delay_pct: 0.55
      cell_type_chain: LUT→LUT6→MUXF7→LUT→FDRE   # D1: per-node delay breakdown
      delay_hotspots: [u_core/lut1 [LUT6] 0.082ns, u_core/launch_ff [FDRE] 0.079ns]
      source_clock: clk_a;  dest_clock: clk_b;  cross_clock: true  # D2: clock context
  severity_distribution: {critical: 12, moderate: 38, marginal: 77}
  path_clusters: [{cluster: logic_deep_aes, path_count: 12, slack_range: -1.200~-0.850ns}]

# Module 3: Physical & Congestion
physical_congestion:
  global_congestion_score: 0.65 [fresh];  avg_wirelength: 12.3 [fresh]
  hotspots: [...]

# Module 4: Netlist Quality
netlist_quality:
  total_control_sets: 5;  cross_domain_paths: 3 [fresh]
  high_fanout_nets: [fresh]  # 8 nets
  top_cell_types: "LUT6:24377, LUT5:4744, MUXF7:3016" [fresh]

# Module 5: Constraints Environment
constraints_env: {clock_defs: {clk: 300MHz}, false_paths: 2, pvt: slow_0p95v_85c}

# Module 6: Dynamic Gradient
dynamic_gradient: {delta_wns: +0.077, last_action: PhysOpt, status: Success}

# Module 7: Architecture Overview
architecture_overview:
  top_modules: [{name: aes_core, hits: 38, coverage: 52.8%, sub: [sbox, mix_cols]}]
  deepest_module: aes_core/sbox;  total_cells_analyzed: 72
```
```

**新鲜度标记**: Dashboard 中每个数据字段后显示 `[fresh]` 或 `[stale]` 标记，由 `field_freshness: dict[str, str]` 逐字段追踪。`init_analysis` 完成后全部初始化为 `[fresh]`；工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段为 `[fresh]`；设计修改工具（`DESIGN_MODIFICATION_TOOLS` 共 19 个）执行后全部字段降级为 `[stale]`。LLM 根据标记决定是否信任数据或重新获取。

**Phase-aware filtering**: `PHASE_STATESPACE_MODULES` 按阶段控制模块——ANALYZE 看 7 模块（M5 隐藏），SELECT_STRATEGY 看 9 模块（新增 M4b `design_structure` + M8 `recent_analysis`），EXECUTE/EVALUATE 看 M1 + M2b (紧凑摘要) + M6。

**LLM 防歧义注解**: 所有 N/A 和空列表带机器可读原因——`"N/A(initial_state)"`、`"N/A(no_io_ports)"`、`[]  # no_high_fanout_nets_found`。纯数据无判断标签。每次通过 `build_state_space()` 重建，不进入 MessageStore，同时通过 WebSocket 推送到前端。

**设计状态标注（design_not_routed）**: 检测到 `Design State` 不含 `Routed` 时，Dashboard 显示 `⚠️ WARNING: Design is NOT routed — WNS/TNS may be inaccurate`。

### 3.2.1 新增模块

**Module 4b `design_structure`** (ANALYZE+SELECT_STRATEGY 可见)：从 `top_cell_types` 推导细胞组成信号——`muxf_ratio`、`ff_to_lut_ratio`、`structural_patterns`(如 `MUXF7+MUXF8_cascade`)、`dominant_cell_types`。零 MCP 开销。

**Module 8 `recent_analysis`** (SELECT_STRATEGY 独占)：从 `raw_tool_outputs` 提取最近两轮分析工具摘要（timing_summary、critical_path_spread、congestion、fanout 等 8 种工具）。零 MCP 开销。

### 3.2.2 上下文工程变更（2026-06）

- **弱引导**: `design_delay_profile` 不再附带 `strategy_hint`，`_append_architecture_hints()` 重命名为 `_append_architecture_insights()`，仅输出纯数据描述
- **Handoff 增强**: `PhaseHandoff` 新增 `tool_results` 字段；SELECT_STRATEGY 阶段可调用 `vivado_get_raw_tool_output` 查询 ANALYZE/EVALUATE 阶段原始输出
- **细胞类型链**: Module 2 每路径新增 `cell_type_chain`（如 `LUT→MUXF7→LUT→FDRE`），零 MCP 开销
- **端点计数对齐**: `top_violating_endpoints` 显示 `showing N of M total failing`，避免误导
- **空数据优雅降级**: `critical_paths` 为空时显示 `status: not_extracted_or_all_cells_invalid`

> 完整 Dashboard 格式、新鲜度机制、critical path 管理见 [architecture.md §1.5-§2.1](architecture.md)。

### 3.3 关键信息保护

| 保护层 | 机制 |
|------|------|
| System 消息 | 压缩前分离，始终前置 |
| WNS/TNS | 上下文 Dashboard（user message，独立于压缩系统） |
| 最近消息 | `preserve_role_turns=6` 保留原始 API role |
| 工具缓存 | `state.context.tool_cache` — 同 phase 同参数返回 `[CACHED]`；执行工具后 clear() |
| 调用频率限制 | 超限返回 `[RATE LIMITED]`（`search_cells`:3, `vivado_run_tcl`:2 等） |
| DCP 身份 | EXECUTE 阶段移除白名单中的 `vivado_open_checkpoint` |
| 策略 catalog 排除 | 失败策略从目录移除 + `strategy_lifecycle` 显示 `blocked_this_iteration`/`blocked_ttl` |
| 空结果 | `optimized_count: 0` → `tool_error`（可重试）非 `strategy_ineffective`（永久） |
| 细胞名验证 | `_is_valid_cell_name()` 过滤非细胞字符串；>50% 无效整条跳过 |
| TCL 拦截 | `tool_router.py` 检测 `get_timing_paths`+`get_cells` 返回 `[AUTO-GUIDANCE]` |
| 冷却分层 | **策略工具失败**→跳过冷却；**仅辅助工具失败**→应用冷却；阈值 0.050ns |

> 完整保护机制表见 [architecture.md §2.2](architecture.md)。

### 3.4 模型选择

Planner(1M max) vs Worker(250K max)，迭代边界切换。`compute_model_scores()` 7 维度评分 (margin=2)：

| 维度 | P | W | 条件 |
|------|---|---|------|
| 上下文复杂 | +2 | +1 | >=6 |
| 连续失败>=2 | +4 | - | - |
| 连续成功>=3 | - | +1 | - |
| WNS 严重倒退 | +3 | - | - |
| 预算>80% | - | +3 | - |
| 历史能力>70% | - | +2 | - |
| 历史能力<30% | +2 | - | - |

> 完整 handoff 格式见 [architecture.md §4.1](architecture.md)。策略清单见 [README.md](README.md) 优化策略表。Skill 详见 [architecture.md §3](architecture.md)。

### 3.7 Tool 描述增强

在工具 description 中标注禁忌症、结果解读指南、策略交互警告。详见 [architecture.md §12](architecture.md)。

### 3.8 phys_opt_design 安全守卫

VivadoMCP 服务端 + dcp_optimizer.py 入口双层守卫，阻止以下指令：
- `AlternateFlowWithRetiming`、`AddRetime`（retiming 改变流水线结构）
- `retime=true`、`interconnect_retime=true`（布尔选项）

## 4. MCP 服务器架构

### 4.1 VivadoMCP

[VivadoMCP/vivado_mcp_server.py](VivadoMCP/vivado_mcp_server.py) — 通过 pexpect 管理 Vivado Tcl 子进程，约 20+ 工具。

```
LLM → MCP tool call → vivado_mcp_server.py → pexpect → vivado -mode tcl
                                               ← stdout/stderr ←
                                                      ↓
                                               JSON parse + error detect
```

**核心机制**: 超时自动 kill→restart→reopen DCP；`^ERROR: [` 匹配返回 `{"error": "..."}`；retiming 指令守卫；多行 Tcl 支持。

### 4.2 RapidWrightMCP

[RapidWrightMCP/server.py](RapidWrightMCP/server.py) + [rapidwright_tools.py](RapidWrightMCP/rapidwright_tools.py) — 通过 JPype 桥接 Java RapidWright API，19+ 工具。

```
LLM → MCP tool call → server.py → JPype → Java RapidWright API → Python dict
```

**核心机制**: 内存中持有完整 EDIF 网表+布局（跨调用持久化）；细胞级操作（LUT 交换、MUXF 重排、单元复制）；快速时序估计(~2.5s)；拥塞评分(0-1)；`smart_region_search` 距离加权因子 0.3。

### 4.3 MCP 工具路由（tool_router.py）

[optimizer/pure/tool_router.py](optimizer/pure/tool_router.py) — 前缀分发（`vivado_*`/`rapidwright_*`）+ 工具结果缓存 + TCL 提取拦截 + 调用频率限制。

## 5. Dashboard 架构

**数据流**: `NodeGraph.run()` → 每节点退出触发 `tracer.on_exit()` → `DashboardStateTracer` 序列化 → WebSocket 推送前端。

**核心文件**:
- [dashboard/server.py](dashboard/server.py) — aiohttp 服务器 + WebSocket 广播 + REST API
- [dashboard/serializer.py](dashboard/serializer.py) — OptimizerState→JSON（含 state_space 键）
- [dashboard/static/index.html](dashboard/static/index.html) — 自包含前端（暗色主题，20 面板）

**20 面板构成**: 7 模块 StateSpace（M1 Global State & Targets ~ M7 Architecture Overview）+ 13 旧版面板（Timing、Iteration、Strategy Lifecycle、Model、Cost、Control、Critical Paths、LLM Log、Transition History、Tool Call Trace、Flow Control Log、Phase History、WNS Trajectory）。

## 6. 测试基础设施

```
make test-quick           # 纯函数单元测试 (pytest, ~3s)
make test-unit            # 单元测试 (pytest, ~10s)
make test-skills          # Skill 框架测试 (~30-60s)
make run_skill_test_v2    # Skill-only 测试 (无布局布线)
make run_test_v2          # 完整 V2 测试 (工具+技能+P&R, ~10-30min)
make run_init_analysis    # 初始分析测试 (无LLM, 验证Dashboard完整性)
```

**核心测试文件**: [optimizer/test_mode.py](optimizer/test_mode.py)(76K, 完整V2编排)、[test_graph.py](optimizer/test_graph.py)(28K, NodeGraph测试)、[test_pure.py](optimizer/test_pure.py)(21K, 纯函数测试)、[skills/test_skill_framework.py](skills/test_skill_framework.py)(21K)、[VivadoMCP/test_vivado_mcp.py](VivadoMCP/test_vivado_mcp.py)(22K)。

---

## 附录 A. 迭代控制概览

- **常量**: `MAX_TOOL_ROUNDS=80`, `GLOBAL_NO_IMPROVEMENT_LIMIT=3`, `WNS_TARGET=0.0`
- **退出原因**: `cost_limit` / `wns_target_met` / `max_iterations_reached` / `tool_round_limit` / `user_requested` / `rollback`
- **429 降级**: fallback 轮询→耗尽→切层级→清空
- **DCP 验证**: Phase 1 结构对比(RapidWright) + Phase 2 功能仿真(Vivado xsim, 200向量)。每5次迭代中间验证。详见 [architecture.md §6](architecture.md)
- **工具输出摘要化**: 大输出提取WNS/TNS摘要 + `raw_output_truncated: true`；小型(<3KB)直通嵌入。侧缓冲 FIFO 50条。详见 [architecture.md §5](architecture.md)
