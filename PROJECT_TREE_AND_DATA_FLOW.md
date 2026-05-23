# FPL26 优化竞赛 - 项目结构与数据流

## 1. 项目结构

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # 主Agent: LLM编排、模型选择、压缩触发、_build_skill_recommendation()、optimize_v2()入口
├── optimizer/                    # 状态机驱动Agent框架（LangGraph风格）
│   ├── __init__.py               # build_optimizer_graph() 图构建入口，注册9个节点+条件边
│   ├── state.py                  # 状态dataclass: OptimizerState/TimingState/IterationState/ModelState/CostState/ControlState/ContextState/StrategyState/StepState/PhaseEntry
│   ├── deps.py                   # NodeDeps: 外部依赖容器（MCP会话、MemoryManager、OpenAI客户端）
│   ├── graph.py                  # NodeGraph: 图执行引擎（节点注册、边注册、run循环）
│   ├── edges.py                  # 条件边函数: after_init/after_check_exit + NodeName枚举
│   ├── color.py                  # ANSI颜色工具：green/yellow/red着色函数（TTY自动检测）
│   ├── tracing.py                # StateTracer: 状态转换日志（JSON导出）
│   ├── llm_call_logger.py        # LLMCallLogger: 每次LLM调用的完整记录（JSONL+可读日志+实时Dashboard推送）
│   ├── nodes/                    # 节点实现（全部完成）
│   │   ├── __init__.py
│   │   ├── init_analysis.py      # 初始化分析节点（MCP: 初始化RW/Vivado、解析WNS/TNS、run_tcl查询clk_fpl26contest周期、高扇出网线/资源利用率）
│   │   ├── iteration_start.py    # 迭代开始节点（递增计数器、保存prev_best_wns）
│   │   ├── select_model.py       # 模型选择节点（评分+阈值选择planner/worker）
│   │   ├── prepare_context.py    # 上下文准备节点（压缩、FORMAT_GUARD注入、注入handoff；Dashboard注入移至tool loop）
│   │   ├── iteration_end.py      # 迭代结束节点（更新计数器、构建narrative、预选下轮模型）
│   │   ├── check_exit.py         # 退出检查节点（WNS达标/无改善/超时/超成本）
│   │   ├── rollback.py           # 回滚节点（退化时恢复最佳 checkpoint）
│   │   ├── save_output.py        # 保存输出节点（写DCP、打印摘要、导出tracing）
│   │   └── subgraphs/
│   │       ├── llm_tool_loop.py          # 4阶段状态机调度器（ANALYZE→SELECT_STRATEGY→EXECUTE→EVALUATE）
│   │       ├── phase_handoff.py          # 阶段间结构化交接（PhaseHandoff dataclass + transition_phase）
│   │       ├── phase_analyze.py          # ANALYZE阶段：多维度时序/拥塞/扇出分析（仅分析工具，最多12轮）
│   │       ├── phase_select_strategy.py  # SELECT_STRATEGY阶段：选择优化策略（极简4工具）
│   │       ├── phase_execute.py          # EXECUTE阶段：执行策略工具（不含vivado_open_checkpoint，链式动作+事后评估+DCP身份保护）
│   │       └── phase_evaluate.py         # EVALUATE阶段：对比WNS决定下一步（评估工具）
│   ├── pure/                     # 从DCPOptimizer提取的无状态纯函数（可独立单测）
│   │   ├── __init__.py
│   │   ├── timing.py             # parse_timing_summary/parse_high_fanout_nets/parse_resource_utilization/is_valid_wns/compute_timing_hash
│   │   ├── constants.py          # TaskCategory/ModelTier/TOOL_MODEL_MAPPING/SKILL_TOOL_MAP/阈值常量
│   │   ├── tool_filter.py        # 按LoopPhase过滤工具列表（PHASE_TOOLS/PHASE_MAX_ROUNDS/filter_tools_for_phase）
│   │   ├── model_select.py       # classify_task/compute_model_scores/select_model/estimate_context_complexity
│   │   ├── tool_summary.py       # summarize_tool_result/filter_tool_result
│   │   ├── iteration_logic.py    # update_iteration_counters/infer_strategy_from_tools/build_iteration_narrative
│   │   ├── context_snapshot.py   # build_context_snapshot(phase感知区块过滤,含strategy_catalog/skill_guidance)/inject_merged_dashboard/PHASE_DASHBOARD_SECTIONS/STRATEGY_TO_PRIMARY_TOOL
│   │   ├── handoff.py            # build_handoff_prompt/build_status_signal（精简handoff：trajectory+failed_strategies+exit_reason，WNS/critical paths仅在Dashboard）
│   │   ├── tool_router.py        # call_tool（MCP路由）/is_routing_failure
│   │   ├── step_state.py         # extract_step_state（仅原生tool call，无XML/YAML回退）
│   │   ├── compress.py           # compress_context（CompressionContext构建+阈值检查+同步调用_compress）
│   │   └── critical_path.py      # parse_critical_path_cells/update_critical_paths/format_critical_paths（动态critical path管理）
│   ├── test_graph.py             # 21项单元测试（状态/边/图/追踪/集成）
│   └── test_mode.py              # V2测试模式：无LLM验证MCP工具调用和Skill调用（V2TestMode类）
├── config_loader.py              # 模型配置加载器（单例）
├── model_config.yaml             # 模型层级与fallback配置
├── validate_dcps.py              # DCP等价性验证器
├── SYSTEM_PROMPT.TXT             # 系统提示词
├── requirements.txt
├── CLAUDE.md                     # 项目指令文件
├── strategy_library.py           # 策略库
├── Makefile                      # 构建自动化
├── LICENSE-APACHE-2.0.txt        # Apache 2.0 许可证
├── RapidWright/                  # RapidWright Java 子模块（src/、jars/、python/、data/）
├── docs/                         # GitHub Pages 文档站点（benchmarks、FAQ、submission 指南等）
├── dashboard/                    # Web Dashboard 实时状态监控（aiohttp + WebSocket）
│   ├── __init__.py               # 导出 start_dashboard, DashboardStateTracer
│   ├── server.py                 # aiohttp 服务器 + DashboardStateTracer（继承 StateTracer）
│   ├── serializer.py             # OptimizerState → JSON dict 序列化
│   └── static/
│       └── index.html            # 自包含 HTML/CSS/JS 前端（暗色主题，13面板：Timing/Iteration/Strategy Lifecycle/Model/Cost/Control/Critical Paths/LLM Log/Transition History/Tool Call Trace/WNS Trajectory/Flow Control Log/Phase History）
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
│   ├── test_lightyaml.py          # YAML 解析器测试
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
│       ├── planner_compress.py         # PlannerCompressor（继承 YAMLStructuredCompressor，参数来自 model_config.yaml：preserve_turns=60/min_importance=0.1/时序10K字符）
│       └── worker_compress.py          # WorkerCompressor（继承 YAMLStructuredCompressor，参数来自 model_config.yaml：preserve_turns=40/min_importance=0.15/时序3K字符）
├── RapidWrightMCP/               # RapidWright MCP服务器（route_design_rwroute 已禁用，使用 Vivado route_design）
│   ├── rapidwright_tools.py      # 工具函数实现
│   ├── server.py                 # MCP服务器入口（RWRoute 已禁用：布线质量差，会导致 WNS 严重退化）
│   ├── test_server.py            # 服务器测试
│   ├── setup.sh                  # 设置脚本
│   ├── README.md                 # 自述文件
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
│   ├── pblock_strategy.py           # Skill类：PBLOCK-Based Re-placement 策略（分析 + 执行）
│   ├── physopt_strategy.py          # Skill类：Physical Optimization 策略
│   ├── fanout_strategy.py           # Skill类：High Fanout Net Optimization
│   ├── congestion_analysis.py        # Skill类 + 纯函数：Routing Congestion Analysis（READ-ONLY）
│   ├── congestion_spreading_strategy.py  # Skill类：Congestion-Aware Cell Spreading（分析+执行）
│   ├── register_retiming_strategy.py  # Skill类：Register Retiming（分析+执行，pipeline FF插入）
│   ├── pin_swapping_optimization_strategy.py  # Skill类：Pin Swapping Optimization（LUT引脚交换）
│   ├── net_swapping_strategy.py      # Skill类：Net Swapping（分析+执行，SLICE内BEL引脚网络交换）
│   ├── lut_cascade_flattening_strategy.py  # Skill类：LUT Cascade Flattening（non-idempotent，LUT串联展平）
│   ├── critical_path_cell_replication_strategy.py  # Skill类：Critical Path Cell Replication（non-idempotent）
│   ├── SKILL_SPECIFICATION.md        # Skill规范文档
│   ├── descriptors/                 # 自动生成的JSON描述符文件
│   ├── test_net_detour_optimization.py  # 单元测试（_group_pins_by_cell）
│   └── test_skill_framework.py      # 30项集成测试（注册/执行/遥测/错误/幂等/追踪）
```

## 1.1 状态机驱动Agent架构（optimizer/）

> 架构概述和设计意图详见 [README.md](README.md) 的"架构概述"和"设计意图"章节。本节仅包含技术实现细节。

### 入口
- `make run_optimizer DCP=input.dcp` — 默认走 v2 状态机路径（自动加 `--v2`）
- `make run_optimizer_v1 DCP=input.dcp` — 走旧 v1 消息对话路径
- `python dcp_optimizer.py input.dcp --v2` — 命令行直接指定 v2
- `python dcp_optimizer.py input.dcp` — 命令行走 v1（无 `--v2`）

### V2 测试模式（无 LLM）
- `make run_test_v2 DCP=input.dcp` — 完整 v2 测试流程（工具+Skill+place/route）
- `make run_skill_test_v2 DCP=input.dcp` — 仅验证 Skill 调用（快速，无 place/route）
- `python dcp_optimizer.py input.dcp --test-v2` — 命令行直接指定
- `python dcp_optimizer.py input.dcp --test-v2-only-skills` — 仅 Skill 测试

**日志功能**: V2测试模式自动将所有控制台输出保存至 `run_dir/v2testmode.log`，使用TeeLogger实现stdout双写。日志文件包含完整的测试执行记录，便于调试和问题排查。

### V2 Web Dashboard（实时状态监控）

- `python dcp_optimizer.py input.dcp --v2 --dashboard` — 启用 Web Dashboard（默认端口 8080）
- `python dcp_optimizer.py input.dcp --v2 --dashboard --dashboard-port 9090` — 自定义端口
- 浏览器打开 `http://localhost:8080` 查看实时状态

**架构**:
```
NodeGraph.run() ──on_exit()──> DashboardStateTracer（继承 StateTracer）
                                    │
                              serialize_state() → asyncio.Queue(maxsize=10)
                                    │                              LLMCallLogger.log_call()
                                    │                                    │
                              aiohttp WebSocket handler       push_llm_event()（轻量）
                                    │                              asyncio.Queue
                                    │                                    │
                                    └──────────┬─────────────────────────┘
                                               │
                                    Browser（自包含 HTML/CSS/JS 前端）
```

两种推送路径:
1. **全状态快照**（`on_exit`）：每次 graph 节点退出时推送完整 `OptimizerState`（含所有面板数据）
2. **LLM 调用实时推送**（`push_llm_event`）：每次 LLM 调用后立即推送仅 LLM 调用数据（`type: "llm_call_update"`），无需等待 phase 完成

**面板**: Timing（WNS/TNS/FE + sparkline）、Iteration、Strategy Lifecycle（4阶段指示器 + 当前策略/阶段/评估结果）、Model、Cost、Control、Critical Paths、LLM Log（最新 prompt/response + 完整调用历史）、Transition History、Tool Call Trace、Flow Control Log、Phase History

**依赖**: `aiohttp>=3.9.0`（通过 `requirements.txt`，`make setup` 自动安装）

**LLM 消息记录**:
- `ContextState.latest_user_prompt` / `latest_assistant_response` 在 4 个 phase 文件中每次 LLM 调用后更新（截取 2000 字符），Dashboard 通过全状态快照展示
- `LLMCallLogger`（`optimizer/llm_call_logger.py`）在每次调用后写入 `llm_call_history.jsonl`（JSON Lines，程序解析）和 `llm_call_history.log`（人类可读），并通过 `push_llm_event()` 实时推送到 Dashboard
- `llm_call_update` WebSocket 消息包含 phase / model / iteration / 截取的 prompt/response / WNS / cost / best_wns / strategy 等信息，前端 `updateLLMCall()` 即时更新 LLM Log 面板（prepend 到 history 列表，上限 100 条）

### 状态模型
```
OptimizerState (可变dataclass)
├── TimingState    — WNS/TNS/best_wns/latest_wns/milestones/critical_paths(动态列表)/critical_paths_stale/refreshed_fields(Dashboard字段新鲜度)
├── IterationState — 迭代计数器/no_improvement/tool_errors/narratives/tools_used(本迭代工具名列表)
├── ModelState     — 模型选择/fallback/交接提示词/format_guard_injected
├── CostState      — token用量/成本追踪
├── ContextState   — compression_count/raw_tool_outputs(raw输出FIFO缓冲, max 50)/latest_user_prompt/latest_assistant_response(LLM消息日志)/step_state_misses(连续未调用report_step_state计数)
├── ControlState   — 退出条件/路径/step_state/current_dcp_path(当前Vivado中打开的DCP路径)
└── StrategyState  — 4阶段策略生命周期追踪（current_phase/current_strategy/phase_history/evaluation_result）
```

### 图拓扑
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

### 子图: llm_tool_loop

重构为4阶段状态机，每个阶段拥有独立的上下文和精简工具列表：

```
llm_tool_loop_node (状态机调度器)
  │  while True:
  │    phase = PHASE_RUNNERS[phase](state, deps)
  │    if phase==EVALUATE && done_reason: exit
  │
  ├── phase=ANALYZE ─────────→ phase=SELECT_STRATEGY
  │  仅分析工具(~18个)          极简工具(~4个)
  │  最多12轮                   最多6轮
  │  LLM输出ANALYZE_DONE        LLM填写strategy_name
  │       │                          │
  │       └── PhaseHandoff ──────────┘
  │            (分析摘要→策略上下文)
  │
  ├── phase=SELECT_STRATEGY ─→ phase=EXECUTE
  │  策略说明+执行计划            全工具(~25个)
  │                               最多30轮
  │                               LLM输出EXEC_DONE
  │       │                          │
  │       └── PhaseHandoff ──────────┘
  │            (策略rationale→执行上下文)
  │
  ├── phase=EXECUTE ─────────→ phase=EVALUATE
  │  执行策略工具(不含vivado_open_checkpoint)  评估工具(~8个)
  │  链式动作(SKILL_CHAIN)                   最多8轮
  │  事后评估(POST_EVAL)                     LLM决定下一步
  │  DCP身份保护: vivado_open_checkpoint已从白名单移除,
  │  防止LLM意外打开错误设计; current_dcp_path全程追踪;
  │  每次DCP切换在日志中输出DESIGN_LOAD醒目标记
  │       │                          │
  │       └── PhaseHandoff ──────────┘
  │            (前后WNS→评估上下文)
  │
  └── phase=EVALUATE ─────────→ (exit) 或 ANALYZE
      DONE/WNS>=0 → ITERATION_END
      NEXT_ITERATION → ITERATION_END
      SWITCH_STRATEGY → ITERATION_END
      ROLLBACK → ITERATION_END（经 ROLLBACK 节点恢复后开始新 iteration）
      CONTINUE → 回到ANALYZE

阶段间消息隔离（方案B）:
  - 每个阶段独立消息列表
  - 阶段切换时: 当前阶段消息→HistoricalMemory(压缩存档)，仅保留system
  - 每次LLM调用前: 注入合并的 [Handoff摘要 + 阶段感知Dashboard] 作为最后一条user消息
  - Handoff摘要存储在 state.strategy.last_handoff_text，Dashboard按LLM Phase筛选区块
  - 合并消息结构: [PHASE — Context & Dashboard] → ## Previous Phase Summary → 各数据区块 → --- End Dashboard ---
```

### 关键设计
- **节点签名**: `async (state, deps) -> str`（返回下一节点名）
- **依赖注入**: NodeDeps 容器（MCP会话、MemoryManager），不存入状态
- **条件边**: 纯函数 `state -> next_node_name`，系统决定转换而非LLM
- **状态追踪**: StateTracer 在每个节点边界记录快照，JSON导出
- **可变模式**: 节点原地修改 state，通过 tracing 实现可追溯性
- **控制台退出**: `optimize_v2()` 启动 stdin 监听线程，输入 `quit` 设置 `state.control.user_exit_requested`；`NodeGraph.run()` 循环顶部检查该标志，路由到 `save_output`，并清除 `user_exit_requested` 标志防止死循环。`save_output_node` 通过 `print()` 输出 Optimization Summary（reason/iterations/WNS/tokens/cost/elapsed），`optimize_v2()` 返回前打印最终结果行
- **上下文压缩**: `pure/compress.py` 封装 `compress_context()` 纯函数，构建 `CompressionContext` + 阈值检查 + 同步调用 `MemoryManager._compress()`
- **V2 上下文数据流**: `compress_context()` 从 `OptimizerState`（canonical）读取 `iteration`/`best_wns`/`current_wns`/`clock_period`/`initial_wns`，而非从 `MemoryManager`（shadow）。`failed_strategies` 仍从 `deps.compat` 读取（由 `iteration_end_node` 调用 `record_failure()` 填充）。Dashboard 的 `tools_used` 从 `state.iteration.tools_used` 读取（tool loop 中每次工具执行后 append），不再依赖 `deps.compat.tool_call_details`（V2 中始终为空）。
- **MemoryManager 同步**: `init_analysis_node` 调用 `set_initial_wns()`/`set_clock_period()`；`iteration_end_node` 调用 `advance_iteration()` 和 `record_failure()`。`_sync_state_to_memory_manager()` 已删除（原实现访问不存在的 `_state` 属性，始终为空操作）。
- **DCP 身份完整性**: `state.control.current_dcp_path` 在全流程中追踪 Vivado 打开的 DCP 文件。EXECUTE 阶段从 LLM 工具白名单中移除 `vivado_open_checkpoint`，防止 LLM 意外打开错误设计。每次 DCP 切换在日志中输出 `━━━ [DESIGN_LOAD] ... ━━━` 醒目标记。详见设计意图 #12。

### 1.2 V1→V2 迁移映射

> V1→V2 的架构决策详见 [README.md](README.md) 设计意图第 4、5、6 条。本节仅保留代码级映射表。

**纯函数提取 (`optimizer/pure/`)**：

| V1 方法 (DCPOptimizer) | V2 纯函数模块 | 说明 |
|------------------------|--------------|------|
| `_parse_timing_summary()` | `pure/timing.py` | 时序摘要解析、高扇出网线解析、资源利用率解析 |
| `_select_model()` | `pure/model_select.py` | 任务分类、9维评分、模型选择 |
| `_summarize_tool_result()` | `pure/tool_summary.py` | 工具结果YAML摘要化 |
| `_on_iteration_end()` | `pure/iteration_logic.py` | 迭代计数器更新、策略推断、迭代叙事 |
| `_build_context_snapshot()` | `pure/context_snapshot.py` | 数据dashboard构建与注入（V1: 首条user msg；V2: 末条user msg via `inject_context_snapshot_at_end`） |
| `_generate_*_handoff()` | `pure/handoff.py` | 交接提示词、状态摘要、状态信号 |
| `call_tool()` | `pure/tool_router.py` | MCP工具路由（含 phys_opt 安全守卫） |
| `_extract_step_state()` | `pure/step_state.py` | 仅原生tool call解析（无XML/YAML回退） |
| `_compress_context()` | `pure/compress.py` | CompressionContext构建 + 阈值检查 + 同步调用 |
| 散布的常量/枚举 | `pure/constants.py` | TaskCategory/ModelTier/阈值常量 |
| `vivado_extract_critical_path_cells` 结果解析 | `pure/critical_path.py` | Critical path 解析/更新/格式化（V2新增，V1无对应） |

**状态迁移**：

```
DCPOptimizer 实例属性 → OptimizerState (6个dataclass子切片)
├── self.latest_wns/best_wns/failing_endpoints → state.timing: TimingState
├── self._iteration_count/no_improvement_count → state.iteration: IterationState
├── self.model_worker/model_planner/fallback  → state.model: ModelState
├── self.total_cost/total_tokens              → state.cost: CostState
├── self._compression_count/raw_tool_outputs  → state.context: ContextState
└── self._user_exit_requested/is_done         → state.control: ControlState
```

**流程迁移**：

| V1 | V2 |
|----|-----|
| `optimize()` 主循环 (line 5174) | `NodeGraph.run()` + 条件边 `after_check_exit` |
| `get_completion()` LLM循环 (line 4535) | `llm_tool_loop` 子图 (`nodes/subgraphs/llm_tool_loop.py`) |
| `_perform_initial_analysis()` | `init_analysis_node` (`nodes/init_analysis.py`) |
| `_on_iteration_end()` | `iteration_end_node` (`nodes/iteration_end.py`) |
| 模型选择散布在 `get_completion()` 中 | `select_model_node` (`nodes/select_model.py`) |
| 上下文压缩散布在多处 | `prepare_context_node` (`nodes/prepare_context.py`) |

**共享组件（不迁移）**：
- `DCPOptimizerBase` (line 320): `start_servers`, `cleanup`, `calculate_fmax`, `get_clock_period`（V2节点通过 `vivado_run_tcl` 直接查询 `clk_fpl26contest`，无需注册独立MCP工具）, `_parse_resource_utilization`, `_parse_hold_timing`, `check_hold_timing`, `_is_routing_failure`, `_start_tool_heartbeat`
- `MemoryManager`, `EventBus`, MCP 服务器 (`RapidWrightMCP/`, `VivadoMCP/`)
- Skills 框架 (`skills/`), `strategy_library.py`

## 2. 核心数据流

### 2.1 消息流程

```
add_message(role, content)
         ↓
WorkingMemory.add_message()  # 无自动压缩
         ↓
DCPOptimizer._compress_context()  ← token阈值触发（软/硬阈值）
         ↓
MemoryManager._compress("yaml_structured", context, model_tier)
         ↓
YAMLStructuredCompressor:

--- API 调用前（每轮必走）---

DCPOptimizer._prepare_api_messages()
         ↓
1. get_formatted_for_api()  ← 从 MemoryManager 获取消息列表
         ↓
2. _auto_compact_messages()  ← 轻量去重，不受 token 阈值控制
   - 重复 REFLECTION CHECKPOINT → 仅保留最新 1 条
   - 重复 REPETITION DETECTED → 仅保留最新 1 条
   - 重复 SYSTEM NOTICE → 仅保留最新 1 条
   - 重复 FORMAT GUARD → 仅保留最新 1 条（"CRITICAL OUTPUT FORMAT" 开头）
   - 连续同名工具结果 → 仅保留最后一条
         ↓
3. 增强系统提示词（scenario hint + skill catalog）
         ↓
4. 注入迭代交接提示词（迭代边界）
         ↓
5. 注入/替换上下文快照（_inject_context_snapshot）
   - current_best_wns / remaining_violation / active_strategy
   - failed_strategies / do_not_repeat
   - iteration_history（新增：最近5次迭代WNS轨迹）
         ↓
LLM API Call
```
    - 正常模式: preserve_turns=40/min_importance=0.15/preserve_role_turns=6 (worker), preserve_turns=60/min_importance=0.1/preserve_role_turns=6 (planner)（来自 model_config.yaml，详见第4节）
    - 激进模式(hard_limit触发): preserve_turns=25(worker)/40(planner), min_importance=0.35(worker)/0.25(planner)（来自 model_config.yaml，详见第4节）
    - system消息始终保护
    - preserve_role_turns=6: 最近6条消息保留原始API role（user/assistant/tool），不塞进YAML
    - 两轮预算分配: 60%高重要性 + 40%中等重要性
    - preserve_turns预留预算: ~1500 tokens/turn, 最多10K
    - 工具调用保留参数（最多5个）
    - 时序报告智能截断（5项改进：动态预算/阈值过滤/起终点成对/时钟域分组/回退保护）
    - 过时时序报告替换：迭代 < current_iteration-1 的长时序报告 → `[Outdated timing report from iteration N]`（节省 token）
    - **失败策略工具消息提前压缩**：已知失败策略的工具结果不受迭代年龄限制，直接压缩为 `[SYSTEM COMPRESSED TOOL: name (iteration N)]` 标记（节省 token）
    - **反"鬼打墙"机制**（2026-05 新增）：
      - 受保护工具列表 `PROTECTED_ANALYSIS_TOOLS`（frozenset，定义于 yaml_structured_compress.py:193）：分析型工具不被压缩为标记，保留完整 YAML 摘要
        - `rapidwright_analyze_pblock_region`、`rapidwright_analyze_fabric_for_pblock`、`rapidwright_analyze_net_detour`
        - `rapidwright_smart_region_search`、`rapidwright_read_checkpoint`
        - `vivado_get_cached_high_fanout_nets`、`vivado_get_raw_tool_output`
      - 标记格式改为 `[SYSTEM COMPRESSED TOOL: ...]`，明确标注系统主动压缩而非截断
      - 压缩发生后注入 `SYSTEM NOTICE: ...` 通知消息，告知模型标记含义，阻止重复调用
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
    注：`getattr(self, 'preserve_role_turns', 3)`，回退值硬编码为 3
```

### 2.3 WNS/TNS状态注入（已迁移至上下文快照）

```
API调用前 → _inject_wns_state_to_system_prompt()
    - 追加数据驱动scenario hint（avg_distance>70 → "distributed"场景 → PBLOCK推荐）
    - 追加analysis skill guide（get_skill_guide()，一次性注入含"Skill Catalog"标记）
    → 仅处理静态上下文增强，不再注入WNS状态

WNS动态状态 → 已迁移至 _build_context_snapshot()，作为 user message 注入（见 2.3.1）
```

### 2.3.1 Agent 上下文快照注入（user message，数据 Dashboard）

```
V2 tool loop 每轮 LLM 调用前 → _inject_dashboard_at_end()
    ↓
build_context_snapshot() 构建纯数据 Dashboard（参数来源：state 直接读取）：
    --- Optimization Dashboard ---
    This is a factual data dashboard for the current optimization state.
    All values are raw measurements. You decide the next action.

    clock_period: 1.500
    wns_current: -0.978
    wns_best: -0.978
    wns_best_iter: 1
    tns: -835.005
    failing_endpoints: 1529
    budget_remaining: $0.936
    elapsed: 125s

    paths:
      input_dcp: /home/.../logicnets_jscl.dcp (ALREADY OPEN in Vivado & RapidWright, DO NOT re-open)
      output_dcp: /home/.../logicnets_jscl_optimized_20260520_194714.dcp (save final result here)

    trajectory:
      - iter: 1
        strategy: pblock
        wns_before: -0.978
        wns_after: -0.978
        delta: +0.0000

    design_signals:
      max_fanout: 247
      high_fanout_count: 24
      cp_spread_max_distance: 3.2
      lut: 45.2

    critical_paths:
      - path1: cellA -> cellB -> cellC (3 cells, iter 1)

    active_tools:
      - rapidwright_report_timing

    strategy_lifecycle:
      current_phase: EXECUTE_STRATEGY
      current_strategy: PBLOCK

    skill_guidance:
      tool: rapidwright_execute_pblock_strategy
      auto_chain: vivado_place_design(unplace) → vivado_create_and_apply_pblock → vivado_place_design → vivado_route_design
      sequence: report_utilization_for_pblock(Vivado) → analyze_pblock_region(RapidWright) → [auto_chain] → report_timing_summary(Vivado)
      avoid: vivado_run_tcl — use the tool above instead.

    --- End Dashboard ---
    ↓
inject_context_snapshot_at_end(api_messages):
    1. 扫描 api_messages 查找以 "--- Optimization Dashboard ---"
       开头的 user 消息 → 找到则移除（防止累积）
    2. 追加为最后一条 user 消息（最大注意力权重）
    → 每次 API 调用最多一条快照消息，零残留
```

**数据来源**（V2 修复后）：
- `tools_used` 来自 `state.iteration.tools_used`（tool loop 中每次工具执行后 append）
- `iteration_narratives` 来自 `state.iteration.narratives`
- `critical_paths` 来自 `state.timing.critical_paths`
- `input_dcp` 来自 `state.control.input_dcp`（Path → str，带 "ALREADY OPEN" 标注）
- `output_dcp` 来自 `state.control.output_dcp`（Path → str，带 "save final result here" 标注）
- 其余指标从 `state.timing`/`state.cost`/`state.control` 直接读取
- `prepare_context_node` 不再注入 Dashboard（仅做压缩 + FORMAT_GUARD注入 + handoff injection）

**设计要点**：
- **纯数据 Dashboard**：只呈现客观测量值，不含 FAILED/PLATEAUED/do_not_repeat 等判断标签，让 LLM 自主推理
- **trajectory**：工作轨迹，记录每轮迭代策略名、前后 WNS、delta
- **design_signals**：从原始数据计算的客观信号（max_fanout、critical_path_spread、资源利用率等）。静态资源字段（LUT/DSP/BRAM/URAM）不标注新鲜度（设计资源在优化过程中不变）。注意：FF 不在此列——RegisterRetiming 会插入 pipeline FF，改变 FF 计数。动态字段未刷新时标注 `(initial, not refreshed)`
- **design_type**：当 FF=0 时自动添加 `design_type: combinational_only`，帮助 LLM 判断策略适用性（如跳过 RegisterRetiming）
- **design_type_note**：当 `design_type == "combinational_only"` 时注入策略优先级提示："PBLOCK placement is the primary lever for reducing routing delay. PhysOpt and RegisterRetiming have limited effect on routing-delay-dominated paths."
- **Dashboard 新鲜度机制**：`DASHBOARD_REFRESH_MAP`（constants.py）映射工具名→Dashboard 字段。工具执行后 `state.timing.refreshed_fields` 更新。Dashboard 展示时 `_stale_annotation()` 检查字段新鲜度并标注。新增工具只需在 MAP 中添加映射。当前映射：`vivado_report_utilization_for_pblock`→resource_utilization, `vivado_get_critical_high_fanout_nets`→high_fanout_nets, `rapidwright_analyze_critical_path_spread`/`vivado_extract_critical_path_pins`→critical_path_spread
- **active_tools**：最近使用过的工具列表（去重保序）
- **明确声明**："This is a factual data dashboard" + "You decide the next action"
- **无持久化**：快照不进入 MessageStore，完全绕过压缩系统，每次 API 调用从当前状态重建
- **`do_not_repeat` 推导**：从 `state.iteration.tools_used` 聚合被调用 > 3 次且 WNS delta < 0.01ns 的工具，最多 5 条
- **`iteration_history` 注入**：来自 `_iteration_narratives`，格式为 `iter{N}({OUTCOME}): {before}->{after}ns({delta}) {tool_count}toks {strategy_label}`（注意：无 "WNS" 字样，包含工具计数）
- **`strategy_catalog`**（SELECT_STRATEGY 阶段独占）：当 `show_strategy_catalog=True` 时，在 dashboard 首部注入 `strategy_library.get_strategy_catalog()` 输出，列出全部 9 个策略的名称+触发条件，让 LLM 在选择前看到完整策略空间
- **`skill_guidance`**（EXECUTE 阶段）：当 `current_strategy` 非空时注入，显示该策略对应的 primary skill 工具名（`STRATEGY_TO_PRIMARY_TOOL` 映射）、`SKILL_CHAIN_ACTIONS` 自动链步骤、`strategy_library.STRATEGIES` 执行序列、以及 `avoid: vivado_run_tcl` 提示，引导 LLM 使用专用 skill 而非 `vivado_run_tcl`

### 2.3.2 动态 Critical Path 管理

**数据结构**（`optimizer/state.py`）：
```python
@dataclass
class CriticalPathEntry:
    """Single critical path with cell list and per-path timing detail."""
    cells: list[str]           # Ordered cell names on this path
    path_length: int = 0       # Number of cells
    iteration: int = 0         # When this path was extracted
    slack: Optional[float] = None        # Per-path slack (ns)
    logic_delay: Optional[float] = None   # Total logic delay (ns)
    net_delay: Optional[float] = None     # Total net delay (ns)
    levels: Optional[int] = None          # Logic levels/depth
```

`TimingState` 新增字段：
```python
critical_paths: list[CriticalPathEntry]  # Top 10 paths sorted by length (longest first)
critical_paths_iteration: int = 0        # Last iteration when paths were extracted
critical_paths_stale: bool = False       # Set True after phys_opt/route_design
```

**双重更新触发**：

```
触发1（被动）: LLM 调用 vivado_extract_critical_path_cells
  → tool_router 返回 JSON [{"cells":[...], "slack":-0.493, "logic_delay":1.234, "net_delay":0.759, "levels":5}, ...]
  → llm_tool_loop._update_critical_paths_from_tool() 解析结果
  → pure/critical_path.update_critical_paths() 存入 state.timing.critical_paths

触发2（主动）: phys_opt_design / route_design / place_design / create_and_apply_pblock 执行后
  → state.timing.critical_paths_stale = True
  → 工具循环末尾 auto-refresh:
    → _auto_refresh_critical_paths() 调用 vivado_extract_critical_path_cells(num_paths=10)
    → 解析结果存入 state.timing.critical_paths
    → critical_paths_stale = False
```

**展示位置**：
| 位置 | 来源 | 限制 |
|------|------|------|
| Context Dashboard (2.3.1) | `build_context_snapshot(critical_paths=...)` | top 8, 6 cells/path, 含 slack/logic/net/levels |
| Planner Handoff (5.1) | `_generate_planner_handoff()` | top 5, 6 cells/path, 含 slack |
| Worker Handoff (5.1) | `_generate_worker_handoff()` | top 3, 6 cells/path, 含 slack |

**纯函数**（`optimizer/pure/critical_path.py`）：
- `parse_critical_path_cells(result: str) -> list[dict]` — 解析 JSON 工具结果（兼容新旧格式）
- `update_critical_paths(state, cell_paths, iteration)` — 更新 state，保留 top 10
- `format_critical_paths_snapshot(critical_paths, limit)` — YAML 格式化（快照用，含 slack/logic/net/levels）
- `format_critical_paths_handoff(critical_paths, limit)` — 纯文本格式化（handoff 用，含 slack）

**节流**：仅在 `critical_paths_stale == True` 时触发 auto-refresh，避免冗余 MCP 调用。

### 2.4 关键信息保护

| 类型 | 存储位置 | 保护机制 |
|------|----------|----------|
| System消息 | Working memory（受保护） | 压缩前分离，始终前置 |
| WNS/TNS/策略状态 | 上下文Dashboard（user message，独立于压缩系统） | 通过 `build_context_snapshot()` → `inject_context_snapshot()` 每 API 调用前注入为第一条 user 消息 |
| 失败策略 | CompressionContext | 存入YAML输出；`record_failure()` 在8个检测点被调用（工具超时/工具异常/工具错误/SWITCH_STRATEGY/PBLOCK验证失败/Fanout后评估缺失/路由失败/策略中断） |
| Tool调用摘要 | V1: MemoryManager._tool_call_details / V2: state.iteration.tools_used | V2 中工具名直接追加到 state，iteration_end_node 和 Dashboard 均从此读取 |
| 最近N轮消息 | Working memory（role保留） | preserve_role_turns=6, 保持 user/assistant/tool 原始role不压缩进YAML |
| report_step_state tool call 格式 | ① User message（会话起始）+ ② System prompt 头部压印（每API调用前） | 双重提醒：User role 高注意力 + System prompt 前导压印 |
| Agent 上下文Dashboard | 临时 api_messages 列表（不进入 MessageStore） | V2: tool loop 每轮 LLM 调用前通过 `inject_context_snapshot_at_end()` 注入为**最后一条** user 消息（最大注意力权重）；V1: `inject_context_snapshot()` 注入为第一条 user 消息；查找并替换旧快照防止累积；数据源：`state.iteration.tools_used`（非 `compat.tool_call_details`） |
| 动态 Critical Path | `state.timing.critical_paths` | 被动更新：LLM 调 `vivado_extract_critical_path_cells` 时解析缓存；主动更新：phys_opt/route_design 后标记 stale 并 auto-refresh；top 10 路径按长度排序 |
| 工具重复检测 | DCPOptimizer._recent_tools（滑动窗口） | 连续>=3次相同工具且WNS总变化<0.05ns时，注入 REPETITION DETECTED 警告 |
| 周期反思 | get_completion() 内嵌 | 每8个 tool_round 注入 REFLECTION CHECKPOINT，要求LLM评估策略有效性并显式 justify CONTINUE vs SWITCH_STRATEGY |
| Pblock合规性 | Vivado MCP 返回 + Summarizer 解析 | `create_and_apply_pblock` 追加 cells 计数；`_summarize_tool_result()` 解析 `cells_in_pblock`/`cells_in_design`，部分成功时设置 `status=partial`、添加 `compliance` 字段 |
| Tcl多行 | Vivado MCP `run_tcl_command()` | 按 `\n` 分割多行脚本，在同一 Vivado 会话中顺序执行（变量跨行持久化） |

### 2.5 模型选择

```
PLANNER: 见 model_config.yaml planner.model_name（推理优化, 1M max）
WORKER: 见 model_config.yaml worker.model_name（速度优化, 250K max）
- 两者可通过不同模型或相同模型 + 不同上下文窗口/压缩参数区分层级
- 429降级: 按层级fallback列表，轮询+耗尽追踪（见 model_config.yaml fallback_models）
- 迭代边界切换: 模型切换在迭代结束保存检查点后，下一迭代开始时发生
- 交接提示词: 新模型收到包含最优状态、下一步目标的上下文
- 推理模式: model_config.yaml 中 reasoning_enabled=true 时，通过 extra_body={"reasoning": {"enabled": true}} 开启 OpenRouter reasoning 模式；reasoning_max_output_tokens 限制推理链长度；planner 和 worker 均可独立配置
- 推理token追踪: CostState.total_reasoning_tokens 累计推理token用量，LLM日志中 reasoning>0 时显示
```

### 2.5.1 模型选择维度（`compute_model_scores()`）

评分系统（7个生效维度，编号3-9，前2个已移除。加权得分高的模型胜出，margin=1防止震荡——代码实现 `planner_score > worker_score + 1`）：

| 维度 | 条件 | Planner得分 | Worker得分 |
|------|------|-----------|-----------|
| 3. 上下文复杂度 | >=6 | +2 | +1 (<3) |
| 4. 历史能力 | >=70%成功率 | - | +2 |
| 5. 历史能力 | <30%成功率 | +2 | - |
| 6. 连续失败 | >=2次 | +4 | - |
| 7. 连续成功 | >=3次 | - | +1 |
| 8. 全局无改善 | >=2.5次 | - | +1 |
| 9. 上下文容量 | >=60% worker限制 | +2 | - |
| 10. WNS状态 | 严重倒退(>-2.0ns) | +3 | - |
| 11. 预算感知 | 费用>80%上限 | - | +3 |
| 12. 预算感知 | 费用>60%上限 | - | +1 |


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
├── lut_cascade_flattening_strategy.py  # Skill类：LUT Cascade Flattening
├── critical_path_cell_replication_strategy.py  # Skill类：Critical Path Cell Replication
├── SKILL_SPECIFICATION.md        # Skill规范文档
├── descriptors/                 # 自动生成的JSON描述符文件（含test_mock_skill）
├── test_net_detour_optimization.py  # 单元测试（_group_pins_by_cell）
└── test_skill_framework.py      # 30项集成测试（含test_mock_skill）

已注册 Skills:
├── analysis.net_detour@1.0.0           # 分析关键路径网络的绕路比率（READ-ONLY）
├── placement.optimize_cell@1.0.0       # 基于重心优化单元布局（non-idempotent）
├── placement.smart_region@1.0.0        # 智能 PBlock 区域搜索（READ-ONLY）
├── optimization.pblock_strategy@1.0.0   # PBLOCK Region Analysis（READ-ONLY）
├── optimization.execute_pblock_strategy@1.0.0  # PBLOCK Full Strategy（分析+执行，自动串联Vivado工具）
├── optimization.physopt_strategy@1.0.0  # Physical Optimization 策略
├── optimization.fanout_strategy@1.0.0   # High Fanout Net Optimization
├── analysis.analyze_congestion@1.0.0    # Routing Congestion Analysis（READ-ONLY，JSON序列化已修复）
├── analysis.analyze_congestion_spreading@1.0.0  # 拥塞感知扩散分析（READ-ONLY）
├── optimization.execute_congestion_spreading@1.0.0  # 拥塞感知单元扩散（non-idempotent）
├── analysis.analyze_register_retiming@1.0.0  # Register Retiming 分析（READ-ONLY）
├── optimization.execute_register_retiming@1.0.0  # Register Retiming 执行（non-idempotent）
├── optimization.pin_swapping_strategy@1.0.0  # Pin Swapping Optimization（LUT引脚交换，getBELPins已修复）
├── analysis.analyze_net_swapping@1.0.0  # Net Swapping 分析（READ-ONLY）
├── optimization.execute_net_swapping@1.0.0  # Net Swapping 执行（non-idempotent）
├── optimization.lut_cascade_flattening@1.0.0  # LUT Cascade Flattening（non-idempotent，LUT串联展平）
├── optimization.critical_path_cell_replication_strategy@1.0.0  # Critical Path Cell Replication（non-idempotent）
└── analysis.test_mock_skill@1.0.0      # 测试用Mock Skill

Skill 超时映射（三层）:
| Skill | @skill decorator `timeout_ms` | JSON descriptor `defaultMs/maxMs` | 测试调用 `timeout` |
|-------|-------------------------------|-----------------------------------|-------------------|
| smart_region | **60000** (1min) | 60000 / 120000 | 60.0 |
| pblock_strategy | **60000** (1min) | 60000 / 120000 | 60.0 |
| execute_pblock_strategy | **120000** (2min) | 120000 / 240000 | - |
| analyze_congestion | **30000** (30s) | 30000 / 60000 | - |
| analyze_congestion_spreading | **60000** (1min) | 60000 / 120000 | - |
| execute_congestion_spreading | **300000** (5min) | 300000 / 600000 | - |
| net_detour | 30000 (30s) | 30000 / 60000 | 120.0 |
| physopt_strategy | 360000 (6min) | 360000 / 720000 | 360.0 |
| fanout_strategy | 300000 (5min) | 300000 / 600000 | 300.0 * nets |
| optimize_cell | 60000 (1min) | 60000 / 120000 | 360.0 |
| analyze_register_retiming | **60000** (1min) | 60000 / 120000 | - |
| execute_register_retiming | **300000** (5min) | 300000 / 600000 | - |
| pin_swapping_strategy | **120000** (2min) | 120000 / 240000 | - |
| analyze_net_swapping | **60000** (1min) | 60000 / 120000 | - |
| execute_net_swapping | **120000** (2min) | 120000 / 240000 | - |
| lut_cascade_flattening | **300000** (5min) | 300000 / 600000 | - |

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
│   ├── execute_fanout_strategy: fo>100 → optimize_fanout_batch+write_checkpoint, 返回优化结果(LLM自行调Vivado工具串)
│   └── execute_congestion_spreading: severity=HIGH → 评分拥塞cell+扩散+write_checkpoint (LLM自行调Vivado工具串)
│   ├── analyze_register_retiming: deep combinational chains → 识别FF-to-FF深链段+插入点（READ-ONLY）
│   └── execute_register_retiming: 插入pipeline FF到组合逻辑链中+write_checkpoint (LLM自行调Vivado工具串)
│   ├── analyze_net_swapping: SLICE内LUT对 → 识别可交换网络候选（READ-ONLY）
│   └── execute_net_swapping: 交换BEL引脚网络+write_checkpoint (LLM自行调Vivado工具串)

Skill 推荐机制 (`_build_skill_recommendation()`, 5 主条件按优先级排列, 注意 stagnation 条件含隐含的 best_wns < 0 检查):
├── stagnation (global_no_improvement>=2 AND best_wns<0) + PBLOCK not failed → rapidwright_analyze_pblock_region [诊断]
├── stagnation + Fanout not failed                    → rapidwright_execute_fanout_strategy [诊断]
├── stagnation + CongestionSpreading not failed        → rapidwright_analyze_congestion_spreading [诊断]
├── stagnation + 都失败                                → rapidwright_analyze_net_detour [诊断]
├── avg_distance > 70 + PBLOCK not failed             → rapidwright_analyze_pblock_region（推荐改用 `rapidwright_execute_pblock_strategy` 自动串联 Vivado 工具）
├── max_fanout > 100 + Fanout not failed              → rapidwright_execute_fanout_strategy
├── no_improvement>=2 + physopt tried                 → rapidwright_analyze_net_detour（分析型）
├── WNS > -2.0 + PhysOpt not failed                   → rapidwright_execute_physopt_strategy
└── 以上均不匹配                                        → 空（不推荐）

Skill 推荐注入点:
├── build_situation_summary() → 纯事实状态摘要（无推荐）
├── _generate_planner_handoff() → 简化交接（SITUATION/STATE/TRAJECTORY/STATUS）
├── _generate_worker_handoff() → 精简交接（STATE/CRITICAL PATHS/STATUS）
└── SWITCH_STRATEGY 处理器 → 注入消息末尾追加skill推荐

调用链:
Agent → MCP Tool → rapidwright_tools.py wrapper → SkillRegistry.get()
         ↓
   SkillContext(design, call_id, idempotency_key)
         ↓
   Skill.execute_with_telemetry(context, **kwargs)
     ├── 幂等性检查（idempotent/non-idempotent）
     ├── Heartbeat daemon（30秒间隔）
     ├── self.execute(context, **kwargs)  # 必须同步（def），不支持 async def
     ├── 协程检测安全网：若 execute 返回协程则 asyncio.run() 兜底
     ├── 追踪属性发射（SkillTraceAttributes）
     ├── SkillTelemetry.record_execution(duration_ms, status, error_code)
     └── 返回 SkillResult(success, data, error, error_code)

### 2.6.2 SKILL_CHAIN_ACTIONS 自动工具链执行

**动机**：分析型 Skill（如 `analyze_pblock_region`）返回 `pblock_ranges` 后需 LLM 手动串联 5 步 Vivado 命令。LLM 常被其他策略分散注意力，未完成后续步骤，导致核心策略（PBLOCK）分析完成但未实际应用。

**方案**：定义 `SKILL_CHAIN_ACTIONS` 映射（`optimizer/pure/constants.py`），当特定 Skill 执行后，`llm_tool_loop` 自动串联后续 Vivado MCP 工具，绕过 LLM 决策。

```python
SKILL_CHAIN_ACTIONS: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": [
        {"tool": "vivado_place_design", "args": {"directive": "unplace"}},
        {"tool": "vivado_create_and_apply_pblock",
         "args_from_skill": {
             "pblock_name": "pblock_name",
             "pblock_ranges": "pblock_ranges",
             "is_soft": "is_soft_recommended",
         }},
        {"tool": "vivado_place_design", "args": {}},
        {"tool": "vivado_route_design", "args": {}},
    ],
}
```

**执行流程**：
```
Skill execute() 返回 SkillResult(success=True, data={pblock_ranges, pblock_name, ...})
         ↓
llm_tool_loop._execute_chain_actions() 
         ↓
遍历 SKILL_CHAIN_ACTIONS[tool_name]:
  ├── 从 skill_result_data 解析 args_from_skill 参数
  ├── 调用 call_tool_fn() 执行 Vivado MCP 工具
  ├── summarize_tool_result() + add_message("user", "[AUTO-CHAIN] ...")
  ├── _track_wns_from_result() 更新 WNS 状态
  └── state.iteration.tools_used.append(target_tool)
         ↓
链式执行完成 → LLM 在下一轮对话中看到 [AUTO-CHAIN] 结果
```

**关键设计**：
- **`args_from_skill`**：参数从 Skill 返回值中动态提取，如 `pblock_ranges`、`pblock_name`
- **`is_soft`**：从 skill 结果动态读取 `is_soft_recommended`。`pblock_strategy` skill 分析利用率密度，当 >80% 时推荐 IS_SOFT=1（软约束），否则 IS_SOFT=0
- **错误恢复**：链式动作开始前保存 `/tmp/pre_chain_pblock.dcp` 快照。单步失败时自动恢复快照并 break（不继续剩余步骤），避免设计处于未布局的中间状态
- **Critical path stale 标记**：`vivado_place_design` 和 `vivado_create_and_apply_pblock` 执行后自动标记 `critical_paths_stale = True`，触发下一轮 auto-refresh 关键路径数据
- **WNS 追踪**：每步执行后解析时序结果，更新 `state.timing.latest_wns`
- **新增 Skill**：`optimization.execute_pblock_strategy@1.0.0`（`skills/pblock_strategy.py`），默认 `resource_multiplier=1.2x`（比旧 `analyze_pblock_region` 的 1.5x 更紧凑）

**与 `POST_EVAL_TOOLS` 的区别**：
| 机制 | 触发 | 执行内容 |
|------|------|---------|
| `POST_EVAL_TOOLS` | 指定工具执行后 | 仅 `report_timing_summary`（单一评估） |
| `SKILL_CHAIN_ACTIONS` | 指定 Skill 返回后 | 完整工具链（多步串联，含参数传递） |

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

### 2.6.1 策略库完整清单（strategy_library.py）

`strategy_library.py` 定义了 9 个优化策略，按场景匹配推荐给 LLM：

**注意**: `route_design_rwroute`（RapidWright RWRoute）已禁用 — 布线质量差，会导致 WNS 严重退化。所有布线操作应使用 Vivado 的 `route_design`。

| 策略键 | 名称 | 触发条件 | 关联 Skill |
|--------|------|---------|-----------|
| `PBLOCK` | Pblock-Based Re-placement | distributed 场景（avg_distance>70） | `rapidwright_execute_pblock_strategy`（分析+自动串联Vivado） / `rapidwright_analyze_pblock_region`（仅分析） |
| `PhysOpt` | Physical Optimization | 1-2 paths with spread, WNS>-2.0 | `rapidwright_execute_physopt_strategy` |
| `Fanout` | High Fanout Net Optimization | fanout>100, 无 spread | `rapidwright_execute_fanout_strategy` |
| `PinSwap` | Pin Swapping | WNS 卡在 ~-0.3ns, LUT 输入引脚延迟差异 | `rapidwright_analyze_net_swapping` |
| `LUTCascade` | LUT Cascade Flattening | >3 级 LUT 串联 | `rapidwright_optimize_lut_input_cone` |
| `CellReplication` | Critical Path Cell Replication | fanout>10 或 delay>0.3ns | Vivado `phys_opt_design` |
| `CongestionSpreading` | Congestion-Aware Cell Spreading | congestion=HIGH, PBLOCK/PhysOpt 无效 | `rapidwright_analyze_congestion_spreading` |
| `RegisterRetiming` | Register Retiming | 深组合逻辑链（>2 LUTs） | `rapidwright_analyze_register_retiming` |
| `NetSwap` | Net Swapping | SLICE 内布线拥塞 | `rapidwright_analyze_net_swapping` |

**场景检测矩阵** (`SCENARIO_DETECTION_MATRIX`): 7 个场景 — `wide_lut`, `high_fanout`, `distributed`, `control_imbalance`, `congestion`, `congestion_spread`, `deep_chain`

**已知问题**: 修复后 `get_strategy_catalog()` 遍历全部 9 个策略 `["PBLOCK", "PhysOpt", "Fanout", "PinSwap", "LUTCascade", "CellReplication", "CongestionSpreading", "RegisterRetiming", "NetSwap"]`。

### 2.7 Tool 描述增强（2026-05 新增）

为提升 LLM 决策准确率，对 Tool 和 Skill 描述进行了增强，增加三类信息：

**1. LIMITATIONS / Contraindications（禁忌症）**
在工具 description 中明确标注不适用场景：
- `optimize_lut_input_cone`: 明确标注不适合神经网络/宽数据通路设计（逻辑锥 75+ 输入远超 6 输入 LUT 物理限制）
- `execute_fanout_strategy`: 标注 PBLOCK 后运行高风险（会破坏 PBLOCK 密集布局导致 WNS 恶化）

**2. RESULT INTERPRETATION（结果解读指南）**
指导 LLM 正确理解工具返回值：
- `analyze_net_detour`: 空结果 = 布线已紧凑（有效诊断，非失败）
- `optimize_lut_input_cone`: `optimized_count=0` 时检查 per-pin status；Java ERROR 关于 "6 maximum inputs" = 设计不适合此工具
- `execute_fanout_strategy`: 成功后必须验证 WNS delta；如果 WNS 恶化则 ROLLBACK

**3. STRATEGY INTERACTION WARNING（策略交互警告）**
- `execute_fanout_strategy`: PBLOCK 后执行扇出分裂会破坏密集布局
- `analyze_pblock_region`: resource_multiplier 过高（默认 1.5x）可能生成不必要的大区域

**4. `llm_hint` 运行时注入（optimize_lut_input_cone）**
当 `optimize_lut_input_cone` 返回 `optimized_count=0` 时，在返回值顶层注入 `llm_hint` 字段：
- 检测到 "6 maximum inputs" Java 错误 → 提示此设计不适合 LUT 锥优化，建议切换策略
- 无输入超限错误 → 提示逻辑深度可能已最小，建议尝试其他策略

**5. SYSTEM_PROMPT.TXT 策略排序约束**
```
ordering_constraints:
  - "PBLOCK MUST be applied BEFORE fanout on distributed designs (avg_distance > 70)."
  - "If execute_fanout_strategy runs before PBLOCK on distributed design and WNS regresses: ROLLBACK immediately."
  - "Pure combinational designs (FF=0): PBLOCK placement is the primary lever."
  - "optimize_lut_input_cone: Skip for neural network / wide-datapath designs."
  - "analyze_net_detour returning zero results = routing already compact, not a failure."
```

**6. strategy_library.py SKILL_GUIDANCE 增强字段**
- `analyze_net_detour`: 新增 `interpretation` 字段（解释空结果含义）
- `execute_fanout_strategy`: 新增 `prerequisite`、`risk` 和 `contraindications` 字段（标注 PBLOCK 前置依赖、运行风险及实测数据：-0.978→-1.660ns）
- `execute_fanout_strategy` description (server.py): 新增 `ORDERING CONSTRAINT` + `CONTRAINDICATION` 含实测退化数据

### 2.8 phys_opt_design 安全守卫（2026-05-18 新增）

**背景**：经功能验证发现，`phys_opt_design` 的 retiming 指令（`AlternateFlowWithRetiming`、`AddRetime`）会导致 LUT 链密集的神经网络设计出现功能错误（200 测试向量中 9 个不匹配）。

**修复方案**：在两层添加安全守卫，阻止危险指令：

**1. VivadoMCP 服务端守卫**（`VivadoMCP/vivado_mcp_server.py`）
```python
# 在 phys_opt_design 处理逻辑中添加
BLOCKED_DIRECTIVES = {"AlternateFlowWithRetiming", "AddRetime"}
BLOCKED_BOOL_OPTIONS = {"retime", "interconnect_retime"}
SAFE_DIRECTIVES = {
    "Default", "Explore", "AggressiveExplore", "RuntimeOptimized",
    "ExploreWithHoldFix", "ExploreWithAggressiveHoldFix",
    "AlternateReplication", "AggressiveFanoutOpt", "RQS",
}
```

**2. dcp_optimizer.py call_tool 入口守卫**
```python
# 在 call_tool() 方法中添加
if tool_name == "vivado_phys_opt_design":
    # 检查 directive 参数
    # 检查 retime/interconnect_retime 布尔选项
    # 返回清晰的错误信息
```

**禁止的指令/选项**：
- `AlternateFlowWithRetiming` （retiming 改变流水线结构）
- `AddRetime` （retiming 改变流水线结构）
- `retime=true` （布尔选项）
- `interconnect_retime=true` （布尔选项）

**允许的指令**：
- `Default` 
- `Explore` 
- `AggressiveExplore` 
- `RuntimeOptimized` 
- 其他安全指令 

## 3. 事件系统

```python
EventBus (events.py)
├── subscribe(event_type, handler) → token
├── unsubscribe_by_token(token)
├── emit(event)

EventTypes: CONTEXT_COMPRESSED, LAYER_PROMOTED, BRANCH_CREATED, BRANCH_MERGED
```

## 4. 配置（model_config.yaml）

> 模型名称和 fallback 列表以 `model_config.yaml` 为准，此处不重复列举。
> 文档仅保留压缩/阈值参数说明。

```yaml
# Worker: 速度优化, 250K max
worker:
  model_name: "<见 model_config.yaml>"
  soft_threshold: 175K, hard_limit: 200K
  token_budget: 80K
  preserve_turns: 40, preserve_turns_aggressive: 10
  min_importance: 0.15, min_importance_aggressive: 0.7
  preserve_turns_hard_limit: 25, min_importance_threshold_hard_limit: 0.35
  preserve_role_turns: 6
  history_retrieval_limit: 8, history_retrieval_min_importance: 0.5
  fallback_models: "<见 model_config.yaml>"
  cost_hard_limit: 1.00  # USD
  reasoning_enabled: true           # 开启推理模式（OpenRouter reasoning）
  reasoning_max_output_tokens: 16384  # 推理链最大token数（None=不限制）

# Planner: 推理优化, 1M max
planner:
  model_name: "<见 model_config.yaml>"
  soft_threshold: 200K, hard_limit: 300K
  token_budget: 80K
  preserve_turns: 60, preserve_turns_aggressive: 10
  min_importance: 0.1, min_importance_aggressive: 0.7
  preserve_turns_hard_limit: 40, min_importance_threshold_hard_limit: 0.25
  preserve_role_turns: 6
  history_retrieval_limit: 10, history_retrieval_min_importance: 0.3
  fallback_models: "<见 model_config.yaml>"
  cost_hard_limit: 1.00  # USD
  reasoning_enabled: true           # 开启推理模式（OpenRouter reasoning）
  reasoning_max_output_tokens: 16384  # 推理链最大token数（None=不限制）
```

## 5. 迭代控制

```python
MAX_TOOL_ROUNDS_PER_ITERATION = 80
GLOBAL_NO_IMPROVEMENT_LIMIT = 3
WNS_TARGET_THRESHOLD = 0.0  # 0.0ns = 时序收敛（get_completion() 方法内局部变量）

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
- **Planner** (~600-1000 tokens): `EXIT REASON` → `CONTINUATION DIRECTIVE` → `ITERATION TRAJECTORY` → `CURRENT STATE` → `CRITICAL PATHS (top 5)` → `NEXT OPTIMIZATION GOAL` → `LAST ITERATION TOOLS` → `FAILED STRATEGIES` → `RECOMMENDED SKILL` → `STAGNATION SIGNAL` → `SKILL INVOCATIONS` → `INCOMING MODEL`
- **Worker** (~300-500 tokens): `CONTINUATION` → `EXIT LABEL` → `RECENT TRAJECTORY (last 3)` → `STATE` → `CRITICAL PATHS (top 3)` → `GOAL` → `LAST ITERATION TOOLS` → `AVOID` → `RECOMMENDED SKILL` → `STAGNATION SIGNAL` → `SKILL INVOCATIONS`
- 策略中断检测: `_detect_unfinished_strategy()` 检查最后 2 步是否有 report_timing_summary
- 首次迭代: 注入 `**FIRST ITERATION** - Begin with initial design analysis...`
- Handoff 注入: 独立 system message（index=1）
- 辅助数据: `_iteration_narratives[]`（最多 20 条）、`_build_tool_effect_summary()`（最近 8 条）、`_build_failed_strategy_summary()`（最近 5 条）
- 状态摘要: `build_situation_summary()` 纯事实状态（WNS/best/no-improvement/time）

**限制迭代内切换**: 仅首次迭代或 fallback 场景允许迭代内模型重新选择。

### 5.2 WNS解析

`parse_timing_summary_static()` 会跳过许可证消息、命令回显和 info/warning 消息，在整个输出中搜索时序头，而非假设在开头。

### 5.3 flow_control 信号处理

**语义定义**:
- `flow_control: DONE` = 当前迭代分析完成，需要进入下一迭代继续优化（非退出信号）
- `flow_control: SWITCH_STRATEGY` = 当前策略已耗尽/失败，系统强制执行迭代切换，注入分析引导 + skill推荐 + 强制下一轮先分析再选策略
- `flow_control: NEXT_ITERATION` = 本轮取得显著改善，当前策略边际收益已趋零，进入下一轮迭代（新上下文 + 模型重评估 + 更新的 critical path 数据）。不记录失败。
- `flow_control: RETRY/ROLLBACK` = LLM级别指导，系统信任LLM执行，不作强制迭代切换。V2中 ROLLBACK 触发 complete checkpoint restore + 新 iteration（见回滚节点说明）
- 真正退出条件 = WNS >= 0 **且** DCP 逻辑等效已验证通过（所有优化操作在优化过程中不得改变逻辑功能）

**行为矩阵**:
| 场景 | 行为 |
|------|------|
| `flow_control: ANALYZE_DONE` (ANALYZE阶段) | 切换到 SELECT_STRATEGY 阶段 |
| `flow_control: EXEC_DONE` (EXECUTE阶段) | 切换到 EVALUATE 阶段 |
| `flow_control: DONE`，WNS=-0.538 | 进入下一迭代 |
| `flow_control: DONE`，WNS>=0 | 退出优化 |
| 无 tool_calls，无 DONE 信号 | 继续循环（纯文本处理） |
| `flow_control: SWITCH_STRATEGY` (EVALUATE) | 强制结束迭代 + 记录策略失败 + 下一轮从ANALYZE开始 |
| `flow_control: NEXT_ITERATION` (EVALUATE) | 结束迭代 + 不记录失败 + 自然 handoff + 进入下一轮 |
| `flow_control: CONTINUE` (EVALUATE) | 回到 ANALYZE 阶段，重新分析设计 |
| `detect_rollback_needed()` (EVALUATE入口) | latest_wns << best_wns 时自动设 done_reason=rollback，触发 ROLLBACK 节点恢复最佳 checkpoint |
| `flow_control: ROLLBACK` (EVALUATE) | LLM 主动请求回滚，与自动检测共享 done_reason=rollback 机制 |
| 连续调用 physopt 无改进 | 降级推荐 analyze_net_detour 诊断绕路问题 |

### 5.4 DONE 优化补丁

关键修复：
- **WNS 改善判定时序**: `_on_iteration_end()` 和 `_prev_best_wns` 移到 checkpoint/get_wns 成功之后执行，确保 counter、model selection、handoff prompt 都使用确认后的 WNS
- **退出原因传递**: DONE 处理器设 flags 后走迭代结束处理（设 `_end_iteration_on_return = True` → 迭代循环检测 → _on_iteration_end()），确保 `cost_limit` 等退出原因正确传递
- **LLM 过早 DONE 抑制**: SYSTEM_PROMPT 中 DONE 语义收紧为 `WNS >= 0 achieved`；注入 `current_tns` 和 `failing_endpoints` 让 LLM 感知问题规模

**新增状态变量**:
- `latest_tns: Optional[float]` — 最新 TNS
- `latest_failing_endpoints: Optional[int]` — 最新失败端点计数

### 5.5 Step 状态追踪（`report_step_state` Tool）

每轮 LLM 响应到达后，**先**从 `message.tool_calls` 中提取 `report_step_state` 参数，flow_control 优先于工具执行（DONE/SWITCH_STRATEGY 跳过昂贵的 FPGA 工具调用）。

**流程**：
```
LLM response arrives
  ↓
1. 扫描 message.tool_calls 寻找 report_step_state
   ↓ 找到
   解析 JSON arguments → StepState
   将 report_step_state 从 tool_calls 中移除（不进入工具执行）
   message.tool_calls = remaining_calls（仅余真正需执行的工具）
   ↓ 未找到
   StepState 保持为空（flow_control=None，纯文本视为 CONTINUE）
   ↓
2. 如果 flow_control ∈ {DONE, SWITCH_STRATEGY, NEXT_ITERATION}
   → 跳过工具执行（即使同时有 tool_calls），跳转到 flow_control 处理
   else if tool_calls
   → 正常执行工具
   else （纯文本）
   → 现有纯文本处理逻辑
3. DONE/SWITCH_STRATEGY/NEXT_ITERATION 时跳过工具执行
```

**StepState 数据结构**（控制信令 + 策略生命周期追踪）：
```python
@dataclass
class StepState:
    step_id: Optional[int] = None
    result_status: Optional[str] = None        # SUCCESS | PARTIAL | FAIL
    flow_control: Optional[str] = None         # ANALYZE_DONE | EXEC_DONE | CONTINUE | SWITCH_STRATEGY | NEXT_ITERATION | DONE | RETRY | ROLLBACK | EXHAUSTED
    has_tool_calls: bool = False
    raw_content: str = ""
    strategy_phase: Optional[str] = None       # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    strategy_name: Optional[str] = None        # PBLOCK | PhysOpt | Fanout | PinSwap | LUTCascade | CellReplication | CongestionSpreading | RegisterRetiming | NetSwap
```

**StrategyState 数据结构**（4阶段策略生命周期）：
```python
@dataclass
class StrategyState:
    current_phase: str = ""                  # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    current_strategy: str = ""               # PBLOCK, PhysOpt, Fanout, etc.
    phase_history: list[PhaseEntry] = []     # 阶段转换记录（上限100条）
    analysis_summary: str = ""               # 当前分析发现
    strategy_rationale: str = ""             # 策略选择理由
    evaluation_wns_delta: float = 0.0        # 执行后WNS变化
    evaluation_result: str = "PENDING"       # IMPROVED | REGRESSION | UNCHANGED | PENDING
```

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
| 策略中断检测 | PBLOCK/Fanout | `execution_failure` | `_detect_unfinished_strategy()`: 退出原因 ∈ {tool_round_limit/flow_control_done_next_iteration/switch_strategy} + 策略可执行 + 最后2个工具无 report_timing_summary；额外检测 PBLOCK validation_failed 和 Fanout 后评估缺失 |

**分级格式** (`list[dict]`，原为 `list[str]`)：
```python
{"strategy": "PBLOCK", "reason": "execution_failure",  # tool_error | execution_failure | strategy_ineffective
 "tool": "vivado_create_and_apply_pblock", "iteration": 3, "detail": "..."}
```

**`_build_skill_recommendation()` 分级过滤**：
- `reason="strategy_ineffective"` → 永久排除（LLM 自主判定策略无效）
- `reason ∈ {tool_error, execution_failure}` → 冷却 2 个迭代后可重试（工具/执行问题，非策略本身无效）
- `_strategy_blocked(name)` 辅助函数统一判断逻辑

**`_determine_failure_reason()` 精细化判定**（iteration_end.py）：
- 工具错误（`state.iteration.tool_errors` 非空）→ `tool_error`
- SWITCH_STRATEGY + 工具返回空结果（`_EMPTY_RESULT_PATTERNS` 匹配：`0 candidates`、`no candidates`、`no cells exceeded` 等）→ `tool_error`（可重试）
- SWITCH_STRATEGY + 非空结果 → `strategy_ineffective`（永久排除）
- 其他 → `no_improvement`

**`failed_strategies` 的使用**：
- `_build_skill_recommendation()`: 分级过滤（truly_failed vs tool_failed），而非简单的 `set` 排除
- `_build_failed_strategy_summary()`: 展示策略名 + reason + tool + iteration
- `YAMLStructuredCompressor._compress_outdated_tool_results()`: 失败策略的工具结果优先压缩（兼容新旧格式）
- `_is_failed_strategy_tool_result()`: 兼容 `str`（旧格式）和 `dict`（新格式）
- `failed_strategy_names` 属性（compat.py/manage.py）: 向后兼容返回纯策略名列表

**`_infer_strategy_from_tools()` 策略推断映射**（用于 record_failure、handoff 生成等）：
- PBLOCK → 工具名含 `pblock`
- PhysOpt → 工具名含 `phys_opt`
- Fanout → 工具名含 `fanout` 或 `optimize_fanout`
- CongestionSpreading → 工具名含 `congestion_spread` 或 `execute_congestion_spreading`
- RegisterRetiming → 工具名含 `register_retiming` 或 `register_retime`
- PlaceRoute → 工具名含 `place_design` 或 `route_design`
- 以上均不匹配 → Information/Unknown（不记录失败）

**向后兼容**：
- `failed_strategies` 属性仍返回列表（元素从 `str` 变为 `dict`）
- 新增 `failed_strategy_names` 属性返回 `list[str]`，供仅需策略名的代码使用（如 `_inject_wns_state_to_system_prompt()` 的 `tried` 集合计算）

### 5.7 `report_step_state` Tool 格式提醒

`report_step_state` tool call 通过双重提醒机制确保 LLM 遵守格式：

**提醒 1 — User Message（一次性，会话起始）**
```
V1 optimize() 中:
1. system_prompt → system prompt
2. user(FORMAT_GUARD)  ← report_step_state tool 完整定义（参数、枚举值、禁止项）
3. user(initial_optimization_instructions)

V2 optimize_v2() 中:
1. system_prompt → system prompt
2. prepare_context_node 首次执行时: user(FORMAT_GUARD)  ← state.model.format_guard_injected 标志控制
```
User role 消息注意力权重大于 System role，只需注入一次。

**提醒 2 — System Prompt 头部压印（每 LLM API 调用前）**
```
V1 get_completion() 中 _inject_wns_state_to_system_prompt() 返回后:
system_content = "[FORMAT: EVERY response MUST call the report_step_state tool with "
                 "step_id/result_status/flow_control. Chain-of-thought text OK.]\n\n"
                 + updated_content

V2 llm_tool_loop 中 _prepare_api_messages():
api_messages[0]["content"] = FORMAT_STAMP + "\n\n" + system_content
（仅在 system_content 不以 "[FORMAT:" 开头时追加，防止重复）
```
确保格式约束始终处于 system prompt 最前沿，注意力权重最高。

**格式约束**: response 中必须调用 `report_step_state` tool（在结构化 function/tool calls 中，不在文本中）。允许在 tool call 之外输出自然语言思维链推理。

**`report_step_state` Tool 定义**（控制信令 + 策略生命周期，分析在文本中）：
```python
{
    "name": "report_step_state",
    "parameters": {
        "step_id": {"type": "integer"},
        "result_status": {"enum": ["SUCCESS", "PARTIAL", "FAIL"]},
        "flow_control": {"enum": ["ANALYZE_DONE", "EXEC_DONE", "CONTINUE", "NEXT_ITERATION", "SWITCH_STRATEGY", "DONE", "RETRY", "ROLLBACK", "EXHAUSTED"]},
        "strategy_phase": {"enum": ["ANALYZE", "SELECT_STRATEGY", "EXECUTE_STRATEGY", "EVALUATE"]},
        "strategy_name": {"enum": ["PBLOCK", "PhysOpt", "Fanout", "PinSwap", "LUTCascade", "CellReplication", "CongestionSpreading", "RegisterRetiming", "NetSwap"]}
    },
    "required": ["step_id", "result_status", "flow_control"]
}
```

**4阶段状态机流程**（llm_tool_loop 内部状态机，方案B：阶段隔离+结构化交接）：
| 阶段 | 可用工具 | LLM行为 | 状态转移信号 |
|------|---------|---------|------------|
| ANALYZE | 分析工具(~18个) | 收集时序/拥塞/扇出/布局数据，识别瓶颈 | ANALYZE_DONE |
| SELECT_STRATEGY | 极简(~4个) | 基于分析摘要选择策略 | strategy_name 非空 |
| EXECUTE | 全工具(~24个，不含vivado_open_checkpoint) | 执行策略工具，链式动作自动处理 | EXEC_DONE |
| EVALUATE | 评估工具(~8个) | 对比WNS变化，决定下一步 | DONE/NEXT/SWITCH/CONTINUE |

阶段切换时：当前阶段消息压缩存档→HistoricalMemory，下一阶段注入 PhaseHandoff 摘要上下文。
每个阶段内保留完整消息历史，聚焦当前任务，"一次LLM调用只做好一件事"。

`strategy_phase` 和 `strategy_name` 为可选参数（向后兼容）。阶段转换自动记录到 `StrategyState.phase_history`，Dashboard 实时展示阶段指示器。迭代结束时系统自动判定 `evaluation_result`（IMPROVED/REGRESSION/UNCHANGED）。

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
- Your next response MUST explicitly justify CONTINUE vs SWITCH_STRATEGY.
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
3. 尝试下一fallback（轮询，列表见 model_config.yaml fallback_models）
4. 成功后清last_exception，更新model_worker
5. 全耗尽则切换到另一层级模型
6. 切换时清空双方耗尽集合

_select_model()检查: worker在耗尽列表则强制planner
```

## 7. 控制台退出

```
V1:
  _user_exit_requested: threading.Event   # 同步退出标志
  _async_exit_requested: asyncio.Event    # 异步退出标志（与async代码兼容）
  检查点: optimize()循环开始、get_completion()工具轮次间、LLM调用返回后
  输入"quit"请求优雅退出
  响应延迟: LLM调用完成后立即检查（最多等待LLM调用完成）

V2:
  stdin监听线程: 输入"quit" → state.control.user_exit_requested = True
  检查点: NodeGraph.run()循环顶部 + llm_tool_loop工具轮次边界
  路由: user_exit_requested → save_output → end（清除标志防死循环）
  摘要输出: save_output_node 通过 print() 输出 Optimization Summary 到 stdout
            optimize_v2() 返回前打印最终结果行（best_wns + converged）
```

## 8. 退出原因

| 原因 | 描述 |
|------|------|
| `cost_limit` | 达到成本硬限制（worker: $2.00, planner: $1.00, v1代码使用worker的$2.00作为全局限制） |
| `wns_target_met` | WNS>=0.0（时序收敛） |
| `max_iterations_reached` | 3次迭代无改进 |
| `tool_round_limit` | MAX_TOOL_ROUNDS_PER_ITERATION 轮工具调用达限 |
| `user_requested` | 用户输入quit |
| `flow_control_done_next_iteration` | LLM返回flow_control=DONE但目标未达成，进入下一迭代 |
| `switch_strategy` | LLM返回SWITCH_STRATEGY，系统强制执行迭代切换，下一轮分析后选新策略 |
| `rollback` | EVALUATE 检测到 WNS 退化（或 LLM 请求 ROLLBACK），系统经 ROLLBACK 节点恢复最佳 checkpoint 后开始新 iteration |
| `iteration_success` | LLM返回NEXT_ITERATION，本轮成功改善，进入下一轮继续优化（不记录失败） |

## 11. DCP验证（硬约束）

**逻辑等价性是优化过程的硬约束**，所有优化操作（包括 retiming、replication、pin swapping、LUT cascade flattening 等）必须保证不改变设计的逻辑功能。

```
验证策略:
├── 每5次迭代: 中间 checkpoint 验证（500 向量）
├── 完成时: 完整验证（Phase1 + Phase2, 10000 向量）
├── 触发条件: validation_enabled AND intermediate_dcp存在 AND 非完成态 AND iteration%5==0
└── 输出 DCP 必须通过验证方可作为最终结果
```

两阶段验证（`validate_dcps.py`）：

**Phase 1 — 结构对比（RapidWright）**：
- 比较 golden DCP 和 revised DCP 的 EDIF 网表结构
- 检查 cell 类型、端口连接、层次结构的一致性
- 识别结构性差异（missing cells, extra cells, 端口不匹配等）
- 部分差异（如综合命名变化）标记为 INFO，不阻断验证

**Phase 2 — 功能仿真（Vivado xsim）**：
- 从 golden 和 revised DCP 分别导出 Verilog 仿真模型
- 生成 LFSR 驱动的随机测试激励（默认 10000 向量）
- 在相同输入下逐周期比较所有输出端口
- 处理含加密 IP 的设计（自动跳过仿真，仅保留结构验证）

**安全约束**：
- 禁止使用 `phys_opt_design` 的 retiming 指令（`AlternateFlowWithRetiming`、`AddRetime`）— 已知会导致含 LUT 链的神经网络设计功能错误（200 向量中 9 个不匹配）
- RegisterRetiming skill 使用的局部 FF 插入比全局 retiming 更安全
- pin swapping 和 net swapping 仅交换等效引脚，不改变逻辑函数

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
cost_hard_limit（从 model_config.yaml 加载）

# Dashboard freshness tracking
DASHBOARD_REFRESH_MAP: dict[str, frozenset[str]] = {
    "vivado_report_utilization_for_pblock": {"resource_utilization"},
    "vivado_get_critical_high_fanout_nets": {"high_fanout_nets"},
    "rapidwright_analyze_critical_path_spread": {"critical_path_spread"},
}
```

## 13. 工具输出摘要化 + 历史自动裁剪

### 动机
phys_opt_design / route_design 等工具的原始 Vivado 日志单次可达 25k+ 字符，
充斥 INFO 与重复的时序路径明细，导致模型注意力被稀释。

### 实现

**1. `_summarize_tool_result()`** (dcp_optimizer.py，位于 `_filter_tool_result()` 之后)

每个工具调用返回时自动提取结构化 YAML 摘要替代原始日志，分两种模式：
```yaml
# 大输出（timing report）→ 仅提取 WNS/TNS，raw_output_truncated: true
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

# 小型输出（<3KB，非 timing）→ 直通嵌入 raw_output，raw_output_truncated: false
tool_result:
  tool: vivado_get_wns
  summary: "WNS: -0.352"
  status: completed
  raw_output_truncated: false
  raw_output_chars: 84
  raw_output: |
    -0.352
```

- 利用已有 `parse_timing_summary_static()` 提取 WNS/TNS/failing_endpoints
- 与 `_prev_best_wns` 对比计算 delta
- **`raw_output_truncated` 真实性**：不再硬编码 `true`。JSON 工具（`rapidwright_*`、`vivado_extract_critical_path_pins`、`vivado_create_and_apply_pblock`）→ `false`；仅提取摘要指标的大输出（timing/route/place）→ `true`；fallback → `true`
- **小型输出直通**（`_summarize_tool_result()`）：`char_count < SMALL_OUTPUT_THRESHOLD(3000)` 且非 timing 工具时，不提取字段，直接将完整 `raw_output` 嵌入 YAML 的 `raw_output: |` 块，`raw_output_truncated: false`
- `vivado_extract_critical_path_pins`: 路径预览从 2 条扩展到 10 条，引脚预览从 4 个扩展到 6 个；全部 `pin_paths` 列表保存在 `key_details.pin_paths` 中供 LLM 完整读取
- `rapidwright_analyze_net_detour`: 解析 JSON 输出（cells_analyzed/detour_threshold/results）；空结果时 summary 明确标注 `"No cells exceeded threshold"` + `key_details.has_detour_issues=false`；有结果时列出 top-5 cell 及其绕路比率；同时在 `get_completion()` 中检测到空结果时额外注入 NOTICE guidance 引导 LLM 重新评估后续步骤
- **`llm_hint` 运行时注入**（2026-05 新增）：`rapidwright_optimize_lut_input_cone` 在结果顶层注入 `llm_hint` 字段。当 `optimized_count=0` 时，检测 Java 错误信息中是否含 "6 maximum inputs" → 提示此设计不适合 LUT 锥优化；否则提示逻辑深度已最小化，建议切换策略
- 摘要替换原始文本进入对话历史（message pipeline 中 `call_tool()` → `_summarize_tool_result()` → `add_message()`）

**2. `_raw_tool_outputs`** (dcp_optimizer.py)

完整原始日志存储在 side buffer dict `{(iteration, round_index): raw_text}` 中，
FIFO 淘汰（最多 50 条）。仅当 LLM 调 `vivado_get_raw_tool_output` 时返回。

**3. `vivado_get_raw_tool_output`** (dcp_optimizer.py)

内部工具，不走 MCP 服务器。注册在 `_collect_tools()` 末尾，schema 包含迭代号/轮次/工具名筛选。

**4. 压缩阶段旧消息裁剪** (yaml_structured_compress.py)

`_compress_outdated_tool_results()`: 迭代差 > 2 的工具消息替换为（反"鬼打墙"机制）：
- 增强格式: `[COMPRESSED: {tool} iter={N} | {summary} | WNS={w} TNS={t} FE={f} delta={d} | status={s}]`
- 保留关键指标：summary 文本、WNS、TNS、failing_endpoints、delta、status
- 失败策略工具结果仅压缩非当前迭代的（`msg_iter < current_iteration` 守卫）
- 受保护工具（PROTECTED_ANALYSIS_TOOLS，14个）跳过压缩，保留完整 YAML 摘要
- 压缩后注入通知消息，告知 LLM 标记格式和 `vivado_get_raw_tool_output` 可用性
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