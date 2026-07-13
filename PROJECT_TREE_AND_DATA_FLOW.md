# FPL26 优化竞赛 - 项目结构与数据流

> 读者：贡献者/评委。高层架构概览见 [README.md](README.md)（中文）/[README_EN.md](README_EN.md)（英文），实现级技术细节见 [architecture.md](architecture.md)。

## 1. 项目结构（模块级）

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # CLI 入口：V2 状态机启动、模型配置
├── optimizer/                    # V2 状态机框架（LangGraph 风格）
│   ├── state.py                  # OptimizerState + 7 个子切片 dataclass + EntityRegistry 实体注册表
│   ├── deps.py                   # NodeDeps：外部依赖容器
│   ├── graph.py                  # NodeGraph：节点注册、边注册、run 循环
│   ├── edges.py                  # 条件边函数 + NodeName 枚举
│   ├── color.py / tracing.py     # 工具：ANSI 着色 + 状态转换追踪
│   ├── llm_call_logger.py        # LLM 调用日志记录
│   ├── nodes/                    # 9 个节点实现 + llm_tool_loop 子图
│   │   ├── init_analysis.py      # 初始化分析
│   │   ├── iteration_start.py    # 迭代开始（迭代边界消息清理：archive non-system -> HistoricalMemory -> restore system）（迭代边界消息清理：archive non-system -> HistoricalMemory -> restore system）
│   │   ├── select_model.py       # 模型选择
│   │   ├── prepare_context.py    # 上下文准备（迭代 handoff / BUDGET 注入 + compress_context 触发；FORMAT_GUARD by inject_merged_dashboard）
│   │   ├── iteration_end.py      # 迭代结束
│   │   ├── check_exit.py         # 退出检查
│   │   ├── rollback.py           # 回滚
│   │   ├── save_output.py        # 保存输出
│   │   └── subgraphs/            # llm_tool_loop + 4 阶段
│   └── pure/                     # 18 个无状态纯函数模块（可独立单测），含 state_space.py（7 模块 StateSpace 构建器，含 Module 7 Architecture Overview；时钟名从 critical_paths 提取，非硬编码）、timing.py（时序/路由/控制集/CDC/设计信息解析）、tool_filter.py（阶段白名单）、tool_router.py（MCP 路由+缓存+cell 名边界校验 + design_data_read/design_data_list_snapshots 内部工具）、critical_path.py（关键路径解析 + 数据质量验证）、entities.py（EntityRegistry 实体注册表 + cell 名校验 SSOT + Pinned 层渲染 + stale/fresh 标记 + 富错误反馈）、context_snapshot.py（Dashboard 注入 + Pinned cell 注册表注入 + per-phase FORMAT_GUARD 注入 + 共享 extract_system_message + DesignDataManager 全量数据持久化触发）、tool_summary.py（tool result 摘要 + compact_tool_summary 共享函数）、tool_error_classify.py（P0 ③A 结构化错误信封：classify_tool_error 将工具错误分类为 category+fix_hint+retryable，由 tool_summary 在错误摘要注入）、execute_contracts.py（P1 ②A compute_param_signature 策略参数组合指纹 + combo_is_cooled EXECUTE 组合守卫；兼 chain 步参数解析 resolve_chain_step_arguments）、**design_data.py**（DesignDataManager 设计数据持久化 + 截断聚合统计 compute_unshown_path_stats / compute_unshown_hotspot_stats）、cost_tracking.py（四阶段共享 LLM 调用成本累计 track_llm_call_cost，修复仅 EXECUTE 计数的 2.77x 低估，见 architecture.md §15.10）、**freshness.py**（R1 新鲜度写入路径统一：mark_all_fields_stale / mark_critical_paths_stale / mark_critical_paths_fresh 三个纯函数消除 field_freshness dict 与 critical_paths_stale bool 的双写漂移；R2 数据驱动阶段入口刷新：RefreshSpec 声明式表 + run_phase_entry_refresh 替代 4 个硬编码 ANALYZE 刷新块 + 1 个 SELECT 块，新增 route_status / congestion_data 自动刷新覆盖）
├── architecture.md               # 架构技术细节（迁移映射、压缩管线、消息流、数据质量守卫、冷却逻辑等）
├── config_loader.py              # 模型配置加载器
├── model_config.yaml             # 模型层级与 fallback 配置
├── validate_dcps.py              # DCP 等价性验证器
├── strategy_library.py           # 16 种策略库（含LogicResynthesis、PhysOptAggressive、CombinationalRebalance、LUTMUXFRepack、MUXFTreeReorder、PlaceRouteDirectiveExplore、CongestionRouteExplore）
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
│   └── strategies/               # YAML 压缩策略（参数化 max_chars_tier，替代继承）
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
OptimizerState (可变 dataclass，7 个子切片 + entity_registry 实体注册表)
├── TimingState     — WNS/TNS/best_wns/field_freshness(每字段fresh/stale状态)/hold时序/设备容量/拥塞数据/路由状态/控制集/设计信息/CDC/约束/PVT/violation_summary(路径聚类)/failing_endpoint_names
├── IterationState  — 迭代计数/no_improvement/工具名列表/narratives
├── ModelState      — 模型选择/fallback/交接提示词
├── CostState       — token 用量/成本追踪
├── ContextState    — 压缩计数/原始工具输出缓冲/工具结果缓存/LLM 消息日志/FC 决策轨迹/失败策略记录/优化历史(optimization_history)/连续无进展计数(consecutive_no_progress)
├── ControlState    — 退出条件/DCP 路径/step_state
├── StrategyState   — 4 阶段策略生命周期（current_phase/策略/阶段历史/评估结果）
└── entity_registry — EntityRegistry（canonical cell 名 SSOT + Pinned 上下文层，抗压缩；cells/by_module/snapshot_version）
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
  │  [2026-07] 阶段入口自动刷新 stale WNS (vivado_report_timing_summary)
  │
  ├── SELECT_STRATEGY ─→ EXECUTE
  │  Dashboard+Handoff决策  极简工具(~4个,仅report_step_state+raw_tool_output)
  │  raw_tool_outputs侧缓冲(键=iteration+phase+round+tool_name)    可见ANALYZE/EVALUATE阶段原始工具结果
  │  可查询                              默认最多6轮；仅纯数据描述，无策略推荐引导
  │  Dashboard含9个模块(新增 design_structure+recent_analysis)
  │  [2026-07] 阶段入口自动刷新 stale WNS；FORMAT_GUARD 含策略-工具映射表
  │
  ├── EXECUTE ─────────→ EVALUATE
  │  链式动作+事后评估    评估工具(~7个, 不含vivado_get_wns)
  │  工具结果缓存(同phase内相同参数自动命中, 执行工具后自动失效)
  │  PBLOCK自适应multiplier(公式C)+透明化返回(input/final/transform)
  │  DCP身份保护          动态轮数预算（默认5，复杂策略8）
  │  策略短冷却            无提升/预检拒绝后禁止重复；chain失败例外(保持retriable)
  │  设计一致性验证工具(4个)  LLM可自主验证设计状态
  │  独立RapidWright工具(19个)  LLM可自主选择工具组合
  │  [2026-07] 指令黑名单回退；检查点重载跳过优化
  │
  └── EVALUATE → (exit) 或 SELECT_STRATEGY 或 ANALYZE
      DONE/WNS>=0 → ITERATION_END
      SWITCH_STRATEGY → SELECT_STRATEGY (多策略循环, 最多5轮/迭代)
      NEXT_ITERATION/ROLLBACK → ITERATION_END
      CONTINUE → ANALYZE
      [auto] 连续3次无进展 → 强制 SWITCH_STRATEGY
```

**多策略循环**: 一次迭代内最多尝试 5 个策略 (`MAX_STRATEGY_CYCLES=5`)。EVALUATE 阶段的 `SWITCH_STRATEGY` 信号触发循环回 SELECT_STRATEGY（跳过 ANALYZE）。失败策略通过 TTL 机制按原因区分处理：`strategy_ineffective`（1 轮后解封）、`strategy_not_applicable`（5 轮后解封，`STRATEGY_NOT_APPLICABLE_TTL`）、`no_improvement`（3 轮后解封）、`regression`（2 轮后解封）、`tool_error`/`data_quality_error`（无 TTL，可重试）。**P0 ②B/②C（2026-07-13）**：可重试失败（`tool_error`/`data_quality_error`）不再从目录消失，而是带 `detail` 与剩余重试次数显示为 `[RETRY: ...]`（`get_strategy_catalog(retryable_strategies=...)`），LLM 可用调整后的参数重选同一策略；连续重试 `RETRY_BUDGET=2` 次后自动升级为 `strategy_ineffective`（TTL=1 冷却），防止无限重试（`record_strategy_failure` 内 `retry_count` 累计 + 升级）。EVALUATE 阶段还新增连续无进展检测：连续 3 次评估无改善时自动强制 `SWITCH_STRATEGY`。**P1 ②A（2026-07-13）**：`FailedStrategyRecord.param_signature`（directive 对等组合指纹，`execute_contracts.compute_param_signature`）+ `record_strategy_failure` 按 `(strategy, param_signature)` 去重；directive 类策略的 tool_error 重试预算按组合独立（OptDesign directive A 升级不阻塞 directive B）。升级组合**不在 SELECT_STRATEGY 阻断整策略**（`_get_permanently_blocked_strategies` 仅阻断 `param_signature==""` 的策略级失败），而由 EXECUTE `combo_is_cooled` 守卫拦截已冷却组合的重试（emit `[COMBO COOLED]`）。catalog 三态：`[RETRY]`/`[BLOCKED]`/`[COMBO COOLED]`。**P1 ③C**：`PhaseHandoff.recent_failures` 跨阶段携带最近工具错误摘要。

阶段切换时：当前阶段消息压缩存档→HistoricalMemory，下一阶段注入 PhaseHandoff 摘要上下文。切换时通过 `design_fingerprint`（当前 `best_checkpoint_path`）判断设计是否变更：若指纹不变（设计未修改过），tool cache 被保留避免 EVALUATE→CONTINUE→ANALYZE 循环中缓存数据不必要的丢失。

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
  4. inject_pinned_cell_registry() ← [CELL REGISTRY] 作为 system 后的独立 user 消息（Pinned 层，每轮重建，抗压缩）
  5. inject_merged_dashboard() ← 数据 Dashboard 作为最后一条 user 消息
     └── DesignDataManager.store_snapshot() → {run_dir}/design_data/iteration_{N}/
         ├── critical_paths.json（全量，不截断）
         ├── high_fanout_nets.json, congestion.json, route_status.json
         └── tool_output_{name}_{round}.json（每次工具调用后持久化）
         （LLM 可通过 design_data_read 工具访问，不触发 Vivado/RapidWright）
                                                             ↓
                                                   LLM API Call
```

> **分层上下文注入**（STATIC > PINNED > FORMAT_GUARD > DYNAMIC > EPHEMERAL，详见 [architecture.md §11](architecture.md)）：
> - **STATIC**（L0）：`SYSTEM_PROMPT.TXT`，启动时注入，作为首个 system message 经 `extra_body[system]` 传入 API（provider 可缓存）。
> - **PINNED**（L2）：`[CELL REGISTRY]` 由 `state.entity_registry` 每轮重建，不进入 MessageStore，天然抗压缩；附带 `stale`/`fresh` 新鲜度标记与 `iter=N` 版本号。为 LLM 提供 canonical cell 名唯一权威来源。
> - **FORMAT_GUARD**（L1）：每 phase 按 `build_phase_format_guard(phase)` 动态生成 BASE + per-phase addendum，由 `inject_merged_dashboard()` 注入为 system message（幂等，marker 去重）。包含输出格式、Cell Name Contract、**NET NAME CONTRACT（2026-07-12 新增，fanout `nets` 命名空间引导）**、**PIN NAME CONTRACT（2026-07-12 新增，lut_input_cone `pins` 命名空间引导）**、设计一致性验证流程、**STALE DATA HANDLING 指令（2026-07 新增）**、禁止事项、phase-gated tool 可用性。SELECT_STRATEGY 阶段还额外注入策略-工具映射表（`_STRATEGY_MAPPING_LINES`，2026-07 恢复）。
> - **DYNAMIC**（L3）：7 模块 StateSpace Dashboard + **`strategy_outcomes:` 策略结果表（2026-07 新增）**，作为最后一条 user 消息注入。非 EXECUTE/EVALUATE 阶段抑制 `current_strategy` 残留；时钟名从 `critical_paths[0].clock.source_clock` 提取。
> - **EPHEMERAL**：tool result summaries、handoff prompts、budget messages。
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
  iterations_remaining: 5;  budget_used: $0.2350;  budget_remaining: $0.7650
  wns_gap_to_zero: 0.523ns  # positive = still negative slack to close

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

**新鲜度标记**: Dashboard 中每个数据字段后显示 `[fresh]` 或 `[stale]` 标记，由 `field_freshness: dict[str, str]` 逐字段追踪。`init_analysis` 完成后全部初始化为 `[fresh]`；工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段为 `[fresh]`；设计修改工具（`DESIGN_MODIFICATION_TOOLS` 共 24 个，2026-06-27 补充 5 个）执行后全部字段降级为 `[stale]`（EXECUTE 和 EVALUATE 对称处理）。LLM 根据标记决定是否信任数据或重新获取。

**StateSpace 新增字段**: Module 1 新增 `iterations_remaining`、`budget_used`、`budget_remaining`、`wns_gap_to_zero`，帮助 LLM 评估优化进度和剩余预算。Dashboard 末尾新增 `applied_optimizations` 独立段落，列出成功应用于 `best_checkpoint` 的策略历史（策略名 + WNS 前后对比 + 迭代号）。

**Phase-aware filtering**: `PHASE_STATESPACE_MODULES` 按阶段控制模块——ANALYZE 看 7 模块（M5 隐藏），SELECT_STRATEGY 看 9 模块（新增 M4b `design_structure` + M8 `recent_analysis`），EXECUTE/EVALUATE 看 M1 + M2b (紧凑摘要) + M6。

**截断透明化（2026-07 新增）**: Dashboard 在被截断的模块后添加未显示数据的聚合统计：

- `timing_clusters` 中：`unshown_path_stats` 显示截断路径的总数、slack 范围/均值、严重度分布、时钟域分解、常见 cell 类型
- `physical_congestion` 中：`unshown_hotspots` 显示未显示的热点数量和严重度范围
- `netlist_quality` 中：`unshown_high_fanout_nets` 显示未显示的高扇出网络数量
- Dashboard 末尾：`truncation_advisory` 区段列出各模块截断量、`design_data_path`、`design_data_read` 存取命令

所有新增统计字段均带 `[fresh]`/`[stale]` 新鲜度标注，与已有字段一致。聚合统计的计算函数为纯函数（`design_data.py:compute_unshown_path_stats()`），基于全量 `critical_paths` 数据，不触发额外工具调用。

**数据持久化（2026-07 新增）**: `inject_merged_dashboard()` 每次调用时通过 `DesignDataManager` 将全量设计数据（不截断的关键路径、高扇出网络、拥塞数据、路由状态、设计信息）持久化为 `{run_dir}/design_data/iteration_{N}/` 下的 JSON 文件。LLM 可通过 `design_data_read(iteration=N, data_type='critical_paths')` 内部工具直接读取完整数据，无需重新运行 Vivado/RapidWright。

**LLM 防歧义注解**: 所有 N/A 和空列表带机器可读原因——`"N/A(initial_state)"`、`"N/A(no_io_ports)"`、`[]  # no_high_fanout_nets_found`。纯数据无判断标签。每次通过 `build_state_space()` 重建，不进入 MessageStore，同时通过 WebSocket 推送到前端。

**设计状态标注（DesignState 枚举）**: 从 `Design State` 字段解析为 `UNPLACED` / `PLACED` / `ROUTED` 三级，Dashboard 根据状态显示对应粒度的准确性警告。未布线时 Level 1 RW 预检查自动跳过。解析失败时返回 `None` 并保留上次已知状态（2026-07 修复：避免 `physopt_and_route` 后误判为 UNPLACED、对真实 WNS 误报线负载估计）；`route_design`/`physopt_and_route` 后显式置 `ROUTED`。

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
| **Pinned cell 注册表（L2）** | **`inject_pinned_cell_registry()` 每轮从 `state.entity_registry` 重建为独立 user 消息（system 之后），不进入 MessageStore，天然抗压缩；LLM 引用 cell 名的唯一权威来源。rollback 后 `entity_registry.clear()` 清除陈旧名称** |
| WNS/TNS | 上下文 Dashboard（user message，独立于压缩系统） |
| 最近消息 | `preserve_role_turns=6` 保留原始 API role |
| 工具缓存 | `state.context.tool_cache` — 同 phase 同参数返回 `[CACHED]`；执行工具后 clear() |
| 调用频率限制 | 超限返回 `[RATE LIMITED]`（`search_cells`:3, `vivado_run_tcl`:2 等） |
| DCP 身份 | EXECUTE 阶段移除白名单中的 `vivado_open_checkpoint` |
| 策略 catalog 排除 | 按原因分级（P0 ②C 更新）：`tool_error`/`data_quality_error` **不再移出目录**，带 `detail` + 剩余重试次数标为 `[RETRY: ...]` 供 LLM 调参重试；`strategy_ineffective`（TTL=1）、`no_improvement`（TTL=3）、`strategy_not_applicable`（TTL=5）、`regression`（TTL=2）从目录标为 `[BLOCKED]` 占位符（含剩余轮数）；冷却策略在目录中标 `[BLOCKED: cooldown]` |
| 空结果 | `optimized_count: 0` → `tool_error`（可重试）非 `strategy_ineffective`（永久） |
| **cell 名边界校验** | **`tool_router.call_tool` 在 LLM→MCP 咽喉校验 cell 名（`validate_and_sanitize_cell_args`，部分放行+警告，2026-06-27 新增 `allow_unverified` 参数）；全部非法返回富错误（含候选名建议），不调用 MCP；设计修改工具强制 strict 模式（拒绝 unverified 名）** |
| 细胞名验证 | `_is_valid_cell_name()`（SSOT 在 `entities.py`，`critical_path.py` re-export）过滤非细胞字符串；>50% 无效整条跳过 |
| TCL 拦截 | `tool_router.py` 检测 `get_timing_paths`+`get_cells` 返回 `[AUTO-GUIDANCE]` |
| 冷却分层 | **策略工具失败**→跳过冷却；**仅辅助工具失败**→应用冷却；阈值 0.050ns。**P0 ②B**：可重试失败累计 `retry_count`，达 `RETRY_BUDGET=2` 升级 `strategy_ineffective` |
| **结构化错误信封（P0 ③A）** | **`tool_error_classify.classify_tool_error()`** 将工具错误分类为 `category`（bad_cell_name/bad_directive/tcl_blocked/timeout/vivado_error/rw_error/schema_validation/partial_failure/rate_limited/unknown）+ `fix_hint` + `retryable`，由 `tool_summary.summarize_tool_result()` 在错误摘要中注入 `error_category`/`fix_hint`/`retryable` 三段；EVALUATE 阶段工具错误额外注入 `[EVAL ERROR]` user message（P0 ③B）。LLM 据此调整参数重试 |
| **参数覆盖显式 opt-out（P0 ①A）** | pblock/fanout skill 的 `inputSchema` 新增 `trust_llm_input: bool`（`server.py`）；`phase_execute` 在调用前 `pop` 该 flag，为 True 时 pblock 用 LLM 的 `critical_path_cells`（仍经 `EntityRegistry.contains` 校验，无效则回退 state 数据）、fanout 用 LLM 的 `nets`（无 registry，保留 tool 端 `MIN_FANOUT_TO_SPLIT` 守卫）；`resource_multiplier` 2.0x 地板不放开 |

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

### 3.9 Auto-chain Directive Tuning

自动链（`SKILL_CHAIN_ACTIONS`）扩展支持LLM可调布局布线指令：8个技能包装器（pblock、physopt、opt_design、combinational_rebalancing、lut_muxf_repack、muxf_tree_reorder、fanout、flatten_lut_cascade）现接受可选的 `place_directive`/`route_directive` 参数，通过 `_attach_chain_directives()` 注入链式动作；LLM可在 `PLACE_SAFE_DIRECTIVES`/`ROUTE_SAFE_DIRECTIVES` 白名单内自由选择，省略时回退为"Explore"。修复了 `_strategy_plan_to_dict`（`RapidWrightMCP/rapidwright_tools.py`）中opt/physopt指令因嵌套于 `analysis_summary` 内而被忽略、始终回退为"Explore"的bug。白名单在VivadoMCP服务端强制执行，register_retiming因破坏周期精确等价性而排除。现在这一机制升级为三层回退：LLM 显式传入 > 策略默认值 > 硬编码 "Explore"。策略默认值通过 `STRATEGY_DEFAULT_DIRECTIVES` 字典（`optimizer/pure/constants.py`）激活 `PR_DIRECTIVE_COMBINATIONS` 场景目录，为各策略链匹配典型瓶颈指令对。例如：`opt_design`/`combinational_rebalancing`/`flatten_lut_cascade` → `("ExtraTimingOpt", "NoTimingRelaxation")`；`physopt`/`muxf_tree` → `("Explore", "Explore")`；`pblock`/`fanout` → `(None, "NoTimingRelaxation")`（路由专用）。三层回退逻辑位于 `_execute_chain_actions`（`optimizer/nodes/subgraphs/phase_execute.py`），通过 `"args_from_skill" in step` 守卫确保指令回退仅作用于含 `args_from_skill` 的 place/route 步。**PBLOCK 链改造**（2026-07-04）：step1 由全局 `place_design -unplace` 改为 `vivado_unplace_cells(cells=critical_path_cells)`（局部 unplace 仅关键 cell），`create_and_apply_pblock` 传 `cells=critical_path_cells`（局部 pblock），route 保留 `reuse:True`（局部 unplace 后其余设计仍布线）。同时 PBLOCK 移出 `PLACE_ONLY_CHECK_SKILLS`（局部 unplace 后 route 前关键 net 暂未布线，place-only WNS 为伪退化）。**PBLOCK 区域尺寸绑定 cell 化**（2026-07-05）：配合局部 pblock，`generate_pblock_plan` 当 `critical_path_cells` 可解析（≥50% 匹配）时按绑定 cell 资源（`_estimate_bound_cell_resources` 按 `LUT*`/`FD*`/`MUXF*`/`DSP*`/`RAMB*` 分类求和）× multiplier 计算区域尺寸，`utilization_density` 改为绑定 cell 真实密度（不再是全设计/区域），`is_soft` 随之判定（低密度→硬 pblock `IS_SOFT=0`）；不可解析回退全设计尺寸。修复此前「全设计尺寸大区域 + 50 绑定 cell + is_soft=True」零约束空壳（`dcp_optimizer_run-20260705_130916`）。新增 result 字段 `sizing_basis`/`bound_resources`/`bound_cell_count`。详见 [architecture.md §3.4](architecture.md)。**route `reuse` 后续移除**（2026-07-05）：实测 Vivado `route_design` 无 `-reuse` 选项（`Unknown option '-reuse'`），全部 chain 的 `reuse:True` 配置、`phase_execute.py` route-reuse guard、MCP `reuse` 参数已彻底移除——Vivado 默认自动复用未变更网线布线。 **directive 白名单收紧 + MCP 动态回退**（P0-2，2026-07-11）：Vivado 2025.1 以 Constraints 18-641 拒绝部分白名单 directive（place 的 `NetDelay_high/medium/low`、route 的 `Congestion_Explore`/`Congestion_NetDelay_*`——后者实为 Vivado 策略预设名而非 route_design -directive），已从 `PLACE_SAFE_DIRECTIVES`/`ROUTE_SAFE_DIRECTIVES` 移除；MCP `place_design`/`route_design` handler 检测到 18-641 时自动用默认命令重试一次（`_is_unrecognized_directive_error`），白名单漂移不再瞬时失败。CongestionRouteExplore route directive 由 `Congestion_Explore` 改为合法的 `AlternateRoutability`。（2026-07-12 复审：`AlternateRoutability` 同为 Vivado 策略预设名、亦被 18-641 拒绝；已按 2025.1 man page 全面核验 `PLACE_SAFE_DIRECTIVES`/`ROUTE_SAFE_DIRECTIVES`，移除全部策略预设名条目，CongestionRouteExplore 改用合法的 `AggressiveExplore`。）

## 4. MCP 服务器架构

### 4.1 VivadoMCP

[VivadoMCP/vivado_mcp_server.py](VivadoMCP/vivado_mcp_server.py) — 通过 pexpect 管理 Vivado Tcl 子进程，24 个工具（不含已移除的 get_resource_counts）。

[VivadoMCP/tcl_security.py](VivadoMCP/tcl_security.py) — TCL 安全原语（blocked-command 检测、安全引号、行完整性检查），从 vivado_mcp_server.py 抽取以便独立单元测试。

```
LLM → MCP tool call → vivado_mcp_server.py → pexpect → vivado -mode tcl
                                               ← stdout/stderr ←
                                                      ↓
                                               JSON parse + error detect
```

**核心机制**: 超时自动 kill→restart→reopen DCP；`^ERROR: [` 匹配返回 `{"error": "..."}`；retiming 指令守卫；多行 Tcl 支持。

### 4.2 RapidWrightMCP

[RapidWrightMCP/server.py](RapidWrightMCP/server.py) + [rapidwright_tools.py](RapidWrightMCP/rapidwright_tools.py) — 通过 JPype 桥接 Java RapidWright API，40 个工具（不含已移除的废弃工具 route_design_rwroute）。

```
LLM → MCP tool call → server.py → JPype → Java RapidWright API → Python dict
```

**核心机制**: 内存中持有完整 EDIF 网表+布局（跨调用持久化）；细胞级操作（LUT 交换、MUXF 重排、单元复制）；快速时序估计(~2.5s)；拥塞评分(0-1)；`smart_region_search` 距离加权因子 0.3。

### 4.3 MCP 工具路由（tool_router.py）

[optimizer/pure/tool_router.py](optimizer/pure/tool_router.py) — 前缀分发（`vivado_*`/`rapidwright_*`）+ 工具结果缓存 + TCL 提取拦截 + 调用频率限制 + **cell 名边界校验**（`validate_and_sanitize_cell_args`，部分放行+富错误反馈，全部非法时不调用 MCP 而返回候选名建议）。

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

**核心测试文件**: [optimizer/test_mode.py](optimizer/test_mode.py)(76K, 完整V2编排)、[test_graph.py](optimizer/test_graph.py)(28K, NodeGraph测试)、[test_pure.py](optimizer/test_pure.py)(21K, 纯函数测试)、[skills/test_skill_framework.py](skills/test_skill_framework.py)(21K)、[VivadoMCP/test_vivado_mcp.py](VivadoMCP/test_vivado_mcp.py)(22K)、[tests/test_p2_fixes.py](tests/test_p2_fixes.py)(P2 回归：phys_opt WNS 守卫 / place_design 跳过 / fallback 去重，见 architecture.md §15.11)、[tests/test_p0_robustness_fixes.py](tests/test_p0_robustness_fixes.py)(P0 鲁棒性回归：skip-reopen 脏设计守卫 / directive 白名单收紧 + 动态回退，见 architecture.md §4.16/§8.1)。

---

## 附录 A. 已知数据流陷阱（2026-07-04 运行日志分析发现）

以 `dcp_optimizer_run-20260704_085355` 日志交叉分析为基础，以下数据流问题会导致 LLM 被误导：

> **修复状态（2026-07-04 第二轮审计）**：A.1–A.4 全部已修；另发现并修复 20 项新缺陷（PVT 伪造默认值、`vivado_run_tcl` 虚假新鲜、双陈旧度系统漂移、`delta_wns` 残留、新鲜度标签缺失、零值歧义、持久化文件名跨阶段覆盖等）。完整修复清单与文件定位见 [architecture.md §15.6](architecture.md)。

### A.1 `check_design_status` 返回错误状态

`VivadoMCP/vivado_mcp_server.py` 的 `check_design_status` 工具使用 `get_property STATUS [current_design]` 检测设计状态。在 `open_checkpoint` 后 Vivado 2025.1 的 `STATUS`/`IS_PLACED`/`IS_ROUTED` 均返回**空字符串**，导致 `is_routed=false`、`is_placed=false`。已修：当三者均空时，调用 `report_route_status -return_string` 解析实际网线布线状态兜底（`report_route_status` 直接遍历网线路由，不依赖元数据属性，可靠）。

### A.2 高扇出网线初始扫描结果为 0

`init_analysis` 以 `min_fanout=100` 调用 `vivado_get_critical_high_fanout_nets`，`parse_high_fanout_nets()` 解析出 0 条。但 LLM 在 ANALYZE 阶段以 `min_fanout=50` 重查得 34 条。阈值过高 + 父网线名解析边界情况导致 Dashboard 初始承载空数据。**补充（P1-1，2026-07-11）**：新增 `derive_high_fanout_nets_from_paths()`（`optimizer/pure/critical_path.py`），在 `init_analysis` 提取 critical_path 后从其 `nodes`（已含 `fanout`）派生高扇出网补充到 `high_fanout_nets`，绕过脆弱文本解析（run-20260711_164134：fanout=107 关键网漏报为 0）。

**补充（2026-07-12，run-20260712_013828 P0-1 回归根因）**：即使 ANALYZE 拿到真实高扇出网，数据也只存在于对话历史——`phase_analyze.py` 工具循环未将 `vivado_get_critical_high_fanout_nets` 结果解析回 `state.timing.high_fanout_nets`（仅 `init_analysis` 解析），叠加 rollback 清空（`rollback.py:157`），EXECUTE 阶段缓存与 `design_data_read` 均返回空。LLM 在缓存空时幻觉网络名（用时序报告热点标签 `M1[21]` 而非真实 `M1w[21]`）喂给 fanout 工具，造成 -1.220ns 回归。修复三处：(1) `phase_analyze.py` 新增 handler 将 live 高扇出结果解析入 state + 第 4 个 ANALYZE 入口自动刷新块（stale 时重新获取，rollback 后恢复）；(2) `VivadoMCP` `extract_critical_path_cells` 对 `top_delay_nodes` net 节点用 `get_property PARENT` 解析为父网络名，消除热点标签与网表名歧义；(3) `phase_execute.py` Fanout 注入改为 overriding（`state.timing.high_fanout_nets` 非空时覆盖 LLM nets，附 `[DATA INTEGRITY]`）+ `optimize_fanout_batch` 加 `MIN_FANOUT_TO_SPLIT=50` 守卫跳过低扇出网。

### A.3 Module 3 在策略切换后消失

`state_space.py` 构建的 Module 3（物理与拥塞指标）在 EVALUATE/SELECT_STRATEGY 阶段被过滤移出 Dashboard。第二次策略选择时 LLM 无法获取拥塞/高扇出/路由状态数据，基于过期信息做出决策。

### A.4 `rapidwright_analyze_congestion` 摘要为空

`tool_summary.py` 的 `compact_tool_summary` 因该工具 JSON 缺乏顶层 `message` 键而生成为 `{`。10 个拥塞列的数据被 LLM 忽略。

### A.5 `route_status.total_nets` 解析为 0（2026-07-04 已修复）

`parse_route_status()` 对 Vivado `report_route_status` 输出格式波动的容错不足，`total_nets` 定位偏移导致解析为 0。

**根因（两层）**：① MCP 工具把真实输出塞进 JSON `raw_report` 字段返回，解析器未解包，`split('\n')` 后整段报告变成一整行；② 真实标签为 `# of logical nets` / `# of fully routed nets` / `# of nets with routing errors`，而解析器找 `'total nets'`/`'routed'` 子串，定位偏移 → `total_nets=0`，`routed_nets` 也错成 37081（logical nets）。

**修复**：`timing.py:284` 解析前解包 JSON `raw_report`，按真实标签匹配；`phase_execute.py` route-reuse guard 原改查 `routed_nets > 0`（`total_nets`=logical nets 恒 >0，无法 gate reuse）。详见 [architecture.md §15.5](architecture.md)。`TestParseRouteStatus` 5 项单测覆盖。**2026-07-05**：route-reuse guard 因 `-reuse` 为 Vivado 非法选项已整体移除。

**数据流误导链总结**：
```
init_analysis: 高扇出=0(错误) + check_design_status: unrouted(错误)
  → Dashboard Module 3 承载空数据
    → LLM 自行发现 34 条高扇出网线(但系统未保留)
      → PBLOCK 后 Module 3 被过滤消失
        → 第二次策略选择的基期数据: 过期 + 错误 | 导致选错策略
```

### A.6 PBLOCK 策略三个矛盾（2026-07-04 日志深度分析，已修复）

同一份日志的 PBLOCK 策略执行链暴露三个工程实现矛盾，均已在 2026-07-04 修复：

- **矛盾一（route reuse 死配置）**：8 条 auto-chain 全带 `reuse:True`，但 5 条 route 前无先验布线（unplace / open_checkpoint+place / opt+place 后），reuse 永远无效；A.5 的 `total_nets=0` 解析 bug 恰好掩盖。修复：移除 5 条死配置链的 `reuse:True`，保留 phys_opt 链与 PBLOCK（局部 unplace 后保留布线）。**2026-07-05 彻底移除**：发现 Vivado `route_design` 根本不支持 `-reuse` 选项（实测被拒），其余 `reuse:True` 配置、route-reuse guard、MCP `reuse` 参数全部删除。
- **矛盾二（全局 unplace 核弹级重做）**：PBLOCK 链 step1 全局 `place_design -unplace`（37000 cell）+ `apply_to=current_design`（pblock 约束全部 cell），破坏其他路径。修复：改为 `vivado_unplace_cells(cells=critical_path_cells)` 局部 unplace + `create_and_apply_pblock(cells=...)` 局部 pblock + 增量 place/route。PBLOCK 移出 `PLACE_ONLY_CHECK_SKILLS`（局部 unplace 后 route 前关键 net 暂未布线，place-only WNS 为伪退化）。
- **矛盾三（epsilon 阈值不一致）**：best_保存用 `>0`、EXECUTE verdict 用 `>0.001`、冷却/无进展用 `>0.050`。PBLOCK +0.049ns 被存为 best 却被冷却（"见好就收"）。修复：冷却改 `delta>0` 跳过；无进展计数改 `delta≤0` 递增、`delta>0.050` 重置、`(0,0.050]` 既不重置也不递增。

修复实现见 [docs/plans/pblock-three-contradictions-fix.md](docs/plans/pblock-three-contradictions-fix.md)。A.5（`total_nets=0`）已于 2026-07-04 修复（见上文）。

---

## 附录 B. 迭代控制概览

- **常量**: `MAX_TOOL_ROUNDS=80`, `GLOBAL_NO_IMPROVEMENT_LIMIT=3`, `WNS_TARGET=0.0`
- **退出原因**: `cost_limit` / `wns_target_met` / `max_iterations_reached` / `tool_round_limit` / `user_requested` / `rollback`
- **429 降级**: fallback 轮询→耗尽→切层级→清空
- **DCP 验证**: Phase 1 结构对比(RapidWright) + Phase 2 功能仿真(Vivado xsim, 200向量)。每5次迭代中间验证。详见 [architecture.md §6](architecture.md)
- **工具输出摘要化**: 大输出提取WNS/TNS摘要 + `raw_output_truncated: true`；小型(<3KB)直通嵌入。侧缓冲 FIFO 50条。详见 [architecture.md §5](architecture.md)
- **迭代开始 checkpoint**: `iteration_start` 节点自动保存 `iteration_{iter}_start.dcp` 作为 rollback 基线；优先从 `best_checkpoint.dcp` 拷贝（`shutil.copy2`）而非 Vivado 序列化（节省 ~5s/次）；`_reload_baseline_on_switch` 优先从 `best_checkpoint_path` 恢复，回退到 iteration start DCP
- **设计指纹缓存**: `transition_phase` 接收 `design_fingerprint`（`best_checkpoint_path` 字符串）判断设计是否变更；指纹不变时 tool cache 跨阶段保留，避免 EVALUATE→CONTINUE→ANALYZE 循环中不必要的缓存失效
- **优化历史追踪**: `optimization_history`（`OptimizationAppliedRecord` 列表，含 strategy/params/wns_before/wns_after/iteration/checkpoint_path）在每次保存 `best_checkpoint` 时追加记录，注入 handoff 和 Dashboard

---

## 附录 C. 上下文工程矛盾修复（2026-07-09，run-20260709_123409 分析）

基于 iter2 完全无效（32 次 LLM 调用、$0.11、WNS 零改善、stale 占比 69.3%）的日志矛盾分析，修复框架"声明规则"与"实际执行"之间的 6 类系统性矛盾。核心原则：让框架遵守自己定义的规则（修正数据/标签错误 + 尊重 LLM 自主信号 + 改写指令文本对齐实际自动刷新行为）。完整技术细节见 [architecture.md §4.3/§4.4/§4.11/§4.12/§5.2/§6.6](architecture.md)。

| 类别 | 根因（file:line） | 修复 |
|------|-------------------|------|
| **P0 数据诚实性** | delta 用迭代初冻结的 `prev_best_wns`（phase_execute）；`_reload_baseline_on_switch` 刷新 WNS 后误标全 stale；小输出错误硬编码 `status:completed`（tool_summary）；JSON 错误仅 INFO 级致 fpl26-error.log 空白（tool_router）；失败归因用跨策略累积的 `tools_used[:3]`（iteration_end） | delta 改用 `best_wns_at_entry`；刷新成功后 `_mark_timing_fresh`；错误检测移至绕过前；JSON 错误升 ERROR 级；失败 tool 字段用 `get_strategy_primary_tool` |
| **P1 历史完整性** | 迭代内 SWITCH 绕过 iteration_end，无改进策略不入 `failed_strategies`（黑洞）；iteration_end 独立重扫把 `strategy_not_applicable` 覆盖为 `tool_error` | `_handle_switch_strategy` 切换时新增 `no_improvement` 记录；`record_strategy_failure` 不降级更严格分类（TTL 比较） |
| **P2 自主性** | EXECUTE 中 EXHAUSTED 仅 break 不设 is_done（phase_execute）；`consecutive_no_progress` 跨迭代不重置，3->11 无升级终止 | EXHAUSTED 三入口均设 `is_done`；计数器在 `iteration_start` 重置（终止由 global_no_improvement + MAX_STRATEGY_CYCLES 覆盖） |
| **P3 规则对齐** | STALE DATA HANDLING 要求 LLM 手动刷新，但框架已自动刷新；CELL NAME CONTRACT 称 canonical 却强制覆盖；truncation_advisory 未说明 design_data_read 返回持久化快照 | 改写 STALE DATA HANDLING 如实描述自动刷新范围；CELL NAME CONTRACT 补充 `[DATA INTEGRITY]` 覆盖通知；truncation_advisory 注明持久化快照需提取工具刷新 |

**验证**：`tests/test_context_engineering_fixes.py` 新增 `TestContradictionFixesP0`/`P1`（6 项单测，覆盖 delta 基线、错误状态、归因、失败分类不覆盖）。全套 302 测试通过。回放验证（用本次 run 的 DCP 重跑 iter2）需用户手动执行 `make run_optimizer`，对照 stale 占比下降、strategy_outcomes 含全部已试策略、EXHAUSTED 触发终止三项硬指标。
