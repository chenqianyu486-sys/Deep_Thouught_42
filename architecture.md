# FPL26 优化竞赛 - 架构技术细节

> 本文档包含 V1→V2 迁移映射、压缩管线、消息流、SKILL_CHAIN、flow_control 等实现级细节。
> 读者：深度贡献者。高层架构概览请见 [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md)。

## 1. 部署与运行

### 1.1 入口命令

- `make run_optimizer DCP=input.dcp` — 默认走 v2 状态机路径（自动加 `--v2`）
- `make run_optimizer_v1 DCP=input.dcp` — 走旧 v1 消息对话路径
- `python dcp_optimizer.py input.dcp --v2` — 命令行直接指定 v2
- `python dcp_optimizer.py input.dcp` — 命令行走 v1（无 `--v2`）

### 1.2 V2 测试模式（无 LLM）

- `make run_test_v2 DCP=input.dcp` — 完整 v2 测试流程（工具+Skill+place/route）
- `make run_skill_test_v2 DCP=input.dcp` — 仅验证 Skill 调用（快速，无 place/route）
- `python dcp_optimizer.py input.dcp --test-v2` — 命令行直接指定
- `python dcp_optimizer.py input.dcp --test-v2-only-skills` — 仅 Skill 测试

V2 测试模式自动将所有控制台输出保存至 `run_dir/v2testmode.log`，使用 TeeLogger 实现 stdout 双写。

### 1.3 V2 Web Dashboard（实时状态监控）

- `python dcp_optimizer.py input.dcp --v2 --dashboard` — 启用 Web Dashboard（默认端口 8080）
- `python dcp_optimizer.py input.dcp --v2 --dashboard --dashboard-port 9090` — 自定义端口

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
1. **全状态快照**（`on_exit`）：每次 graph 节点退出时推送完整 `OptimizerState`
2. **LLM 调用实时推送**（`push_llm_event`）：每次 LLM 调用后立即推送 LLM 调用数据

**面板**: Timing（WNS/TNS/FE + sparkline）、Iteration、Strategy Lifecycle（4阶段指示器 + 当前策略/阶段/评估结果）、Model、Cost、Control、Critical Paths、LLM Log（最新 prompt/response + 完整调用历史）、Transition History（含 flow_control_signal/result_status）、Tool Call Trace、Flow Control Log（Signal/Phase/Strategy/WNS/Best/Status/Reason 颜色编码）、Phase History

**依赖**: `aiohttp>=3.9.0`

**LLM 消息记录**:
- `ContextState.latest_user_prompt` / `latest_assistant_response` 在 4 个 phase 文件中每次 LLM 调用后更新（截取 2000 字符），Dashboard 通过全状态快照展示
- `LLMCallLogger`（`optimizer/llm_call_logger.py`）在每次调用后写入 `llm_call_history.jsonl`（JSON Lines，程序解析）和 `llm_call_history.log`（人类可读），并通过 `push_llm_event()` 实时推送到 Dashboard
- `llm_call_update` WebSocket 消息包含 phase / model / iteration / 截取的 prompt/response / WNS / cost / best_wns / strategy 等信息

### 1.4 关键设计

- **节点签名**: `async (state, deps) -> str`（返回下一节点名）
- **依赖注入**: NodeDeps 容器（MCP 会话、MemoryManager），不存入状态
- **条件边**: 纯函数 `state -> next_node_name`，系统决定转换而非 LLM
- **状态追踪**: StateTracer 在每个节点边界记录快照，JSON 导出
- **可变模式**: 节点原地修改 state，通过 tracing 实现可追溯性
- **控制台退出**: `optimize_v2()` 启动 stdin 监听线程，输入 `quit` 设置 `state.control.user_exit_requested`
- **上下文压缩**: `pure/compress.py` 封装 `compress_context()` 纯函数，token 估算统一使用 `ContextEstimator`（tiktoken cl100k_base）
- **MemoryManager 同步**: `init_analysis_node` 调用 `set_initial_wns()`/`set_clock_period()`；`iteration_end_node` 调用 `advance_iteration()` 和 `record_failure()`
- **DCP 身份完整性**: `state.control.current_dcp_path` 在全流程中追踪 Vivado 打开的 DCP 文件。EXECUTE 阶段从 LLM 工具白名单中移除 `vivado_open_checkpoint`
- **Vivado Tcl 超时自动重启**: Tcl 命令超时会污染 Vivado session（`pexpect` 不返回 prompt）。MCP server 内部自动执行 `kill → restart → reopen DCP`，不尝试 `sync_after_timeout()`。移除 `_command_pending` 全局状态（`VivadoMCP/vivado_mcp_server.py`）
- **init_analysis 去 `report_*` 化**: Agent pipeline 禁用 cost 不可预测的 `report_*` 命令。`report_utilization -return_string` → 5 条 `get_cells -filter {PRIMITIVE_GROUP == ...}`（Vivado C++ filter 引擎，200K cells ~5-10s）
- **init_analysis 原子提交 checkpoint**: 7 个步骤各自独立原子提交（`run → validate → mark_done`）。Vivado 重启后自动跳过已完成步骤
- **设计规模感知自适应超时**: Phase A 后运行 `llength [get_cells -hier]` 探测 cell 数，`design_size_factor` 按 50K/150K 分档（1.0/1.5/3.0），所有后续 tool timeout 自动乘以该因子，硬上限 900s

## 2. V1→V2 迁移映射

> V1→V2 的架构决策详见 [README.md](README.md) 设计意图第 4、5、6 条。本节仅保留代码级映射表。

### 2.1 纯函数提取

| V1 方法 (DCPOptimizer) | V2 纯函数模块 | 说明 |
|------------------------|--------------|------|
| `_parse_timing_summary()` | `pure/timing.py` | 时序摘要解析、高扇出网线解析、资源利用率解析 |
| `_select_model()` | `pure/model_select.py` | 任务分类、9维评分、模型选择 |
| `_summarize_tool_result()` | `pure/tool_summary.py` | 工具结果YAML摘要化 |
| `_on_iteration_end()` | `pure/iteration_logic.py` | 迭代计数器更新、策略推断、迭代叙事 |
| `_build_context_snapshot()` | `pure/context_snapshot.py` | 数据dashboard构建与注入（V1: 首条user msg；V2: 末条user msg via `inject_context_snapshot_at_end`） |
| `_generate_*_handoff()` | `pure/handoff.py` | 交接提示词、状态摘要、状态信号 |
| `call_tool()` | `pure/tool_router.py` | MCP工具路由（含 phys_opt 安全守卫） |
| `_extract_step_state()` | `pure/step_state.py` | 仅原生tool call解析 |
| `_compress_context()` | `pure/compress.py` | ContextEstimator(tiktoken)精确token计数 + CompressionContext构建 + 阈值检查 + 同步调用 |
| 散布的常量/枚举 | `pure/constants.py` | TaskCategory/ModelTier/阈值常量 |
| `vivado_extract_critical_path_cells` 结果解析 | `pure/critical_path.py` | Critical path 解析/更新/格式化（V2新增） |

### 2.2 状态迁移

```
DCPOptimizer 实例属性 → OptimizerState（7个dataclass子切片）
├── self.latest_wns/best_wns/failing_endpoints → state.timing: TimingState
├── self._iteration_count/no_improvement_count → state.iteration: IterationState
├── self.model_worker/model_planner/fallback  → state.model: ModelState
├── self.total_cost/total_tokens              → state.cost: CostState
├── self._compression_count/raw_tool_outputs  → state.context: ContextState
└── self._user_exit_requested/is_done         → state.control: ControlState
```

### 2.3 流程迁移

| V1 | V2 |
|----|-----|
| `optimize()` 主循环 (line 5174) | `NodeGraph.run()` + 条件边 `after_check_exit` |
| `get_completion()` LLM循环 (line 4535) | `llm_tool_loop` 子图 (`nodes/subgraphs/llm_tool_loop.py`) |
| `_perform_initial_analysis()` | `init_analysis_node` (`nodes/init_analysis.py`) |
| `_on_iteration_end()` | `iteration_end_node` (`nodes/iteration_end.py`) |
| 模型选择散布在 `get_completion()` 中 | `select_model_node` (`nodes/select_model.py`) |
| 上下文压缩散布在多处 | `prepare_context_node` (`nodes/prepare_context.py`) |

### 2.4 共享组件（不迁移）

- `DCPOptimizerBase`: `start_servers`, `cleanup`, `calculate_fmax`, `get_clock_period`（V2节点通过 `vivado_run_tcl` 直接查询 `clk_fpl26contest`，无需注册独立MCP工具）, `_parse_resource_utilization`, `_parse_hold_timing`, `check_hold_timing`, `_is_routing_failure`, `_start_tool_heartbeat`
- `MemoryManager`, `EventBus`, MCP 服务器（`RapidWrightMCP/`, `VivadoMCP/`）
- Skills 框架（`skills/`），`strategy_library.py`

## 3. 核心数据流细节

### 3.1 消息流程

```
add_message(role, content)
         ↓
WorkingMemory.add_message()  # 无自动压缩
         ↓
DCPOptimizer._compress_context()  ← ContextEstimator(tiktoken)精确token计数 → 软/硬阈值触发
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

压缩参数（来自 model_config.yaml）:
- 正常模式: preserve_turns=40/min_importance=0.15/preserve_role_turns=6 (worker), preserve_turns=60/min_importance=0.1/preserve_role_turns=6 (planner)
- 激进模式(hard_limit触发): preserve_turns=25(worker)/40(planner), min_importance=0.35(worker)/0.25(planner)
- system消息始终保护
- preserve_role_turns=6: 最近6条消息保留原始API role（user/assistant/tool），不塞进YAML
- 两轮预算分配: 60%高重要性 + 40%中等重要性
- preserve_turns预留预算: ~1500 tokens/turn, 最多10K
- 工具调用保留参数（最多5个）
- 时序报告智能截断（5项改进：动态预算/阈值过滤/起终点成对/时钟域分组/回退保护）
- 过时时序报告替换：迭代 < current_iteration-1 的长时序报告 → `[Outdated timing report from iteration N]`
- **失败策略工具消息提前压缩**：已知失败策略的工具结果不受迭代年龄限制，直接压缩为 `[SYSTEM COMPRESSED TOOL: name (iteration N)]` 标记
- **反"鬼打墙"机制**（2026-05 新增）：
  - 受保护工具列表 `PROTECTED_ANALYSIS_TOOLS`（frozenset）：分析型工具不被压缩为标记，保留完整 YAML 摘要
  - 标记格式改为 `[SYSTEM COMPRESSED TOOL: ...]`，明确标注系统主动压缩而非截断
  - 压缩发生后注入 `SYSTEM NOTICE: ...` 通知消息
- `_is_failed_strategy_tool_result()`: 按工具名模式匹配 failed_strategies 列表
- WNS状态注入时机: API调用时（不在working memory）

### 3.2 顺序压缩流程

```
1. 分离system消息（受保护）
2. HistoricalMemory.add(summary, importance=0.8)  ← 先归档
3. WorkingMemory.clear()                        ← 再清空
4. 添加system + YAML摘要（旧消息）              ← YAML压缩
5. 添加最近 preserve_role_turns=6 条消息        ← 保留原始 role（user/assistant/tool）
    注：`getattr(self, 'preserve_role_turns', 3)`，回退值硬编码为 3
```

### 3.3 WNS/TNS 状态注入

```
API调用前 → _inject_wns_state_to_system_prompt()
    - 追加数据驱动scenario hint（avg_distance>70 → "distributed"场景 → PBLOCK推荐）
    - 追加analysis skill guide（get_skill_guide()，一次性注入含"Skill Catalog"标记）
    → 仅处理静态上下文增强，不再注入WNS状态

WNS动态状态 → 已迁移至 _build_context_snapshot()，作为 user message 注入（见 3.4）
```

### 3.4 Agent 上下文快照注入（user message，数据 Dashboard）

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
    1. 扫描 api_messages 查找以 "--- Optimization Dashboard ---" 开头的 user 消息 → 找到则移除
    2. 追加为最后一条 user 消息（最大注意力权重）
    → 每次 API 调用最多一条快照消息，零残留
```

**数据来源**:
- `tools_used` 来自 `state.iteration.tools_used`
- `iteration_narratives` 来自 `state.iteration.narratives`
- `critical_paths` 来自 `state.timing.critical_paths`
- `input_dcp` 来自 `state.control.input_dcp`
- `output_dcp` 来自 `state.control.output_dcp`
- 其余指标从 `state.timing`/`state.cost`/`state.control` 直接读取
- `prepare_context_node` 不再注入 Dashboard（仅做压缩 + FORMAT_GUARD注入 + handoff injection）

**设计要点**:
- **LLM 防歧义注解**: _annotated_list() / _annotated_val() 辅助函数确保 [] 和 N/A 都携带机器可读原因: "N/A(congestion_analysis_not_supported)"、[]  # no_high_fanout_nets_found- **Module 1 新增**: best_wns/best_wns_iteration(追踪优化进展)、cell_count/net_count(设计规模上下文)- **类型契约**: Dashboard 严格区分 None(未分析) 与 []/0(已分析但为零)
- **纯数据 Dashboard**: 只呈现客观测量值，不含 FAILED/PLATEAUED/do_not_repeat 等判断标签
- **trajectory**: 记录每轮迭代策略名、前后 WNS、delta
- **design_signals**: 从原始数据计算的客观信号（max_fanout、critical_path_spread、资源利用率等）
- **design_type**: 当 FF=0 时自动添加 `design_type: combinational_only`
- **design_type_note**: 当 `design_type == "combinational_only"` 时注入策略优先级提示
- **Dashboard 新鲜度机制**: `DASHBOARD_REFRESH_MAP`（constants.py）映射工具名→Dashboard 字段。工具执行后 `state.timing.refreshed_fields` 更新。Dashboard 展示时 `_stale_annotation()` 检查字段新鲜度并标注
- **active_tools**: 最近使用过的工具列表（去重保序）
- **明确声明**: "This is a factual data dashboard" + "You decide the next action"
- **无持久化**: 快照不进入 MessageStore，完全绕过压缩系统，每次 API 调用从当前状态重建
- **`do_not_repeat` 推导**: 从 `state.iteration.tools_used` 聚合被调用 > 3 次且 WNS delta < 0.01ns 的工具，最多 5 条
- **`iteration_history` 注入**: 来自 `_iteration_narratives`，格式为 `iter{N}({OUTCOME}): {before}->{after}ns({delta}) {tool_count}toks {strategy_label}`
- **`strategy_catalog`**（SELECT_STRATEGY 阶段独占）：当 `show_strategy_catalog=True` 时，在 dashboard 首部注入策略名称+触发条件
- **`strategy_catalog` 排除机制**：已记录在 `state.context.failed_strategies` 中的策略（不限 reason 类型）自动从 catalog 中排除，避免 LLM 重复选择已知无效的策略。排除逻辑在 `inject_merged_dashboard()` → `format_state_space_for_llm(exclude_strategies=...)` → `get_strategy_catalog(exclude_strategies=...)` 链路中实现（`context_snapshot.py:392-402` → `state_space.py:266` → `strategy_library.py:412`）。与 `phase_select_strategy.py:254` 的 `_get_permanently_blocked_strategies()` 后验检查形成双重保护。
- **`skill_guidance`**（EXECUTE 阶段）：当 `current_strategy` 非空时注入 primary skill 工具名 + SKILL_CHAIN_ACTIONS + 执行序列

### 3.5 动态 Critical Path 管理

**数据结构**（`optimizer/state.py`）：
```python
@dataclass
class CriticalPathEntry:
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

**双重更新触发**:
```
触发1（被动）: LLM 调用 vivado_extract_critical_path_cells
  → tool_router 返回 JSON [{"cells":[...], ...}]
  → llm_tool_loop._update_critical_paths_from_tool() 解析结果
  → pure/critical_path.update_critical_paths() 存入 state.timing.critical_paths

触发2（主动）: phys_opt_design / route_design / place_design / create_and_apply_pblock 执行后
  → state.timing.critical_paths_stale = True
  → 工具循环末尾 auto-refresh:
    → _auto_refresh_critical_paths() 调用 vivado_extract_critical_path_cells(num_paths=10)
    → 解析结果存入 state.timing.critical_paths
    → critical_paths_stale = False
```

**展示位置**:
| 位置 | 来源 | 限制 |
|------|------|------|
| Context Dashboard | `build_context_snapshot(critical_paths=...)` | top 8, 6 cells/path, 含 slack/logic/net/levels |
| Planner Handoff | `_generate_planner_handoff()` | top 5, 6 cells/path, 含 slack |
| Worker Handoff | `_generate_worker_handoff()` | top 3, 6 cells/path, 含 slack |

**纯函数**（`optimizer/pure/critical_path.py`）：
- `parse_critical_path_cells(result: str) -> list[dict]`
- `update_critical_paths(state, cell_paths, iteration)`
- `format_critical_paths_snapshot(critical_paths, limit)` — YAML 格式化
- `format_critical_paths_handoff(critical_paths, limit)` — 纯文本格式化

**节流**：仅在 `critical_paths_stale == True` 时触发 auto-refresh。

### 3.6 关键信息保护

| 类型 | 存储位置 | 保护机制 |
|------|----------|----------|
| System消息 | Working memory（受保护） | 压缩前分离，始终前置 |
| WNS/TNS/策略状态 | 上下文Dashboard（user message，独立于压缩系统） | 通过 `build_context_snapshot()` → `inject_context_snapshot()` 每 API 调用前注入 |
| 失败策略 | CompressionContext | 存入YAML输出；`record_failure()` 在8个检测点被调用 |
| Tool调用摘要 | V1: MemoryManager._tool_call_details / V2: state.iteration.tools_used | V2 中工具名直接追加到 state |
| 最近N轮消息 | Working memory（role保留） | preserve_role_turns=6 |
| report_step_state tool call 格式 | ① User message（会话起始）+ ② System prompt 头部压印（每API调用前） | 双重提醒 |
| Agent 上下文Dashboard | 临时 api_messages 列表（不进入 MessageStore） | V2: tool loop 每轮 LLM 调用前通过 `inject_context_snapshot_at_end()` 注入为最后一条 user 消息 |
| 动态 Critical Path | `state.timing.critical_paths` | 被动更新 + 主动更新，top 10 路径按长度排序 |
| 工具重复检测 | DCPOptimizer._recent_tools（滑动窗口） | 连续>=3次相同工具且WNS总变化<0.05ns时，注入 REPETITION DETECTED 警告 |
| 周期反思 | get_completion() 内嵌 | 每8个 tool_round 注入 REFLECTION CHECKPOINT |
| Pblock合规性 | Vivado MCP 返回 + Summarizer 解析 | `create_and_apply_pblock` 追加 cells 计数 |
| Tcl多行 | Vivado MCP `run_tcl_command()` | 按 `\n` 分割多行脚本，在同一 Vivado 会话中顺序执行 |

### 3.7 模型选择

```
PLANNER: 见 model_config.yaml planner.model_name（推理优化, 1M max）
WORKER: 见 model_config.yaml worker.model_name（速度优化, 250K max）
- 429降级: 按层级fallback列表，轮询+耗尽追踪
- 迭代边界切换: 模型切换在迭代结束保存检查点后，下一迭代开始时发生
- 交接提示词: 新模型收到包含最优状态、下一步目标的上下文
- 推理模式: model_config.yaml 中 reasoning_enabled=true 时，通过 extra_body={"reasoning": {"enabled": true}} 开启
- 推理token追踪: CostState.total_reasoning_tokens 累计推理token用量
```

#### 模型选择维度（`compute_model_scores()`）

评分系统（7个生效维度，加权得分高的模型胜出，margin=1防止震荡）：

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

## 4. Skill 机制细节

### 4.1 Skill 调用链

```
Agent → MCP Tool → rapidwright_tools.py wrapper → SkillRegistry.get()
         ↓
   SkillContext(design, call_id, idempotency_key)
         ↓
   Skill.execute_with_telemetry(context, **kwargs)
     ├── 幂等性检查（idempotent/non-idempotent）
     ├── Heartbeat daemon（30秒间隔）
     ├── self.execute(context, **kwargs)
     ├── 协程检测安全网：若 execute 返回协程则 asyncio.run() 兜底
     ├── 追踪属性发射（SkillTraceAttributes）
     ├── SkillTelemetry.record_execution(duration_ms, status, error_code)
     └── 返回 SkillResult(success, data, error, error_code)
```

#### Skill 超时映射（三层）

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
2. **JSON descriptor** — 声明性元数据（skills/descriptors/*.json）
3. **测试调用 timeout** — asyncio.wait_for 实际截止时间（dcp_optimizer.py call_rapidwright_tool）

#### 分析型 vs 策略型 Skill

- **分析型 (net_detour/optimize_cell/smart_region)**: 诊断+微观优化，推荐工作流三步走（DIAGNOSE → FIX → CONTAIN）
- **策略型 (physopt/pblock_strategy)**: 封装完整多步策略工作流，一键式执行

#### Skill 推荐机制

`_build_skill_recommendation()` 5 主条件按优先级排列:
- stagnation（global_no_improvement>=2 AND best_wns<0）+ PBLOCK not failed → rapidwright_analyze_pblock_region
- stagnation + Fanout not failed → rapidwright_execute_fanout_strategy
- stagnation + CongestionSpreading not failed → rapidwright_analyze_congestion_spreading
- stagnation + 都失败 → rapidwright_analyze_net_detour
- avg_distance > 70 + PBLOCK not failed → rapidwright_analyze_pblock_region
- max_fanout > 100 + Fanout not failed → rapidwright_execute_fanout_strategy
- no_improvement>=2 + physopt tried → rapidwright_analyze_net_detour
- WNS > -2.0 + PhysOpt not failed → rapidwright_execute_physopt_strategy
- 以上均不匹配 → 空

### 4.2 SKILL_CHAIN_ACTIONS 自动工具链执行

**动机**：分析型 Skill 返回结果后需 LLM 手动串联多步 Vivado 命令。LLM 常被其他策略分散注意力，导致核心策略分析完成但未实际应用。

**方案**：定义 `SKILL_CHAIN_ACTIONS` 映射（`optimizer/pure/constants.py`），当特定 Skill 执行后，`llm_tool_loop` 自动串联后续 Vivado MCP 工具。

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
- **`args_from_skill`**: 参数从 Skill 返回值中动态提取
- **`is_soft`**: 从 skill 结果动态读取 `is_soft_recommended`
- **自动检测资源**: `execute_pblock_strategy` 支持零参数调用，自动从 RapidWright design 遍历实例计数
- **`POST_EVAL_TOOLS`**: `rapidwright_execute_pblock_strategy` 已在其中，chain 完成后自动强制 `vivado_report_timing_summary`
- **Chain 步骤状态检测**: 每个 chain 步骤检查 `"error"` 键，有错则中断 chain
- **错误恢复**: 链式动作开始前保存快照，单步失败时自动恢复
- **Critical path stale 标记**: `vivado_place_design` 和 `vivado_create_and_apply_pblock` 执行后自动标记 `critical_paths_stale = True`
- **WNS 追踪**: 每步执行后解析时序结果，更新 `state.timing.latest_wns`

**与 `POST_EVAL_TOOLS` 的区别**：
| 机制 | 触发 | 执行内容 |
|------|------|---------|
| `POST_EVAL_TOOLS` | 指定工具执行后 | 仅 `report_timing_summary`（单一评估） |
| `SKILL_CHAIN_ACTIONS` | 指定 Skill 返回后 | 完整工具链（多步串联，含参数传递） |

### 4.3 关键路径感知的 PBLOCK 区域选择（2026-05 新增）

**动机**：PBLOCK 区域选择原仅基于资源容量，区域评分函数中距离权重仅 `0.001`，导致区域选择完全由"最小宽度"决定，忽略与关键路径的邻近度。

**方案**：
1. **数据源**: `phase_execute.py` 自动从 `state.timing.critical_paths` 提取 cell 名列表，注入到工具参数
2. **质心计算**: `smart_region_search._compute_critical_path_center_from_cells()` 通过 RapidWright API 查找各 cell 的 tile 坐标，取算术平均
3. **评分函数**: `_score(width, dist) = width + dist * distance_weight_factor`（默认 `distance_weight_factor=0.3`）
4. **参考点优先级**: 关键路径 cell 质心 > 显式传入坐标 > 全局 cell 质心

**自动注入逻辑**:
```python
if tool_name in ("rapidwright_execute_pblock_strategy", "rapidwright_analyze_pblock_region"):
    if not arguments.get("critical_path_cells") and state.timing.critical_paths:
        for cp in state.timing.critical_paths[:10]:
            for cell_name in cp.cells:
                ...  # 去重后注入
```

## 5. 迭代控制细节

### 5.1 迭代边界模型切换

**机制**:
- `iteration_end_node` 调用 `_select_model()` 决定下一迭代模型，存入 `_next_iteration_model`
- 下一迭代 `get_completion()` 开头直接使用预定模型
- 交接提示词迭代结束时生成，模型分层专属

**交接提示词**:
- **Planner** (~600-1000 tokens): EXIT REASON → CONTINUATION DIRECTIVE → ITERATION TRAJECTORY → CURRENT STATE → CRITICAL PATHS (top 5) → NEXT OPTIMIZATION GOAL → LAST ITERATION TOOLS → FAILED STRATEGIES → RECOMMENDED SKILL → STAGNATION SIGNAL → SKILL INVOCATIONS → INCOMING MODEL
- **Worker** (~300-500 tokens): CONTINUATION → EXIT LABEL → RECENT TRAJECTORY (last 3) → STATE → CRITICAL PATHS (top 3) → GOAL → LAST ITERATION TOOLS → AVOID → RECOMMENDED SKILL → STAGNATION SIGNAL → SKILL INVOCATIONS
- **首次迭代**: 注入 `**FIRST ITERATION** - Begin with initial design analysis...`
- Handoff 注入: 独立 system message（index=1）
- 辅助数据: `_iteration_narratives[]`（最多 20 条）、`_build_tool_effect_summary()`（最近 8 条）、`_build_failed_strategy_summary()`（最近 5 条）
- 状态摘要: `build_situation_summary()` 纯事实状态（WNS/best/no-improvement/time）

**限制迭代内切换**: 仅首次迭代或 fallback 场景允许迭代内模型重新选择。

### 5.2 flow_control 信号处理

**语义定义**:
- `flow_control: DONE` = 当前迭代分析完成，需要进入下一迭代继续优化（非退出信号）
- `flow_control: SWITCH_STRATEGY` = 当前策略已耗尽/失败，系统强制执行迭代切换
- `flow_control: NEXT_ITERATION` = 本轮取得显著改善，进入下一轮迭代。不记录失败。
- `flow_control: RETRY/ROLLBACK` = LLM级别指导，系统信任LLM执行。V2中 ROLLBACK 触发 checkpoint restore + 新 iteration
- 真正退出条件 = WNS >= 0 **且** DCP 逻辑等效已验证通过

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
| `detect_rollback_needed()` (EVALUATE入口) | latest_wns << best_wns 时自动设 done_reason=rollback |
| `flow_control: ROLLBACK` (EVALUATE) | LLM 主动请求回滚，与自动检测共享 done_reason=rollback 机制 |
| 连续调用 physopt 无改进 | 降级推荐 analyze_net_detour 诊断绕路问题 |

**可观测性**:
- 所有信号通过 `record_flow_signal(signal, reason, phase, strategy, result_status)` 录制到 `state.context.flow_control_log`
- 每笔 `FlowControlRecord` 包含：signal, iteration, tool_round, done_reason, phase, strategy, result_status, wns_at_decision, wns_best, timestamp
- 控制台统一 `[FC]` 日志格式：`[FC] {signal:20s} phase={phase:18s} iter={N} round={N} wns={x.xxx} best={x.xxx} reason={...}`
- Dashboard Flow Control Log 面板显示全部字段，信号类型颜色编码
- `StateTracer.on_exit()` 录制 flow_control_signal + result_status + current_phase + current_strategy

### 5.3 DONE 优化补丁

关键修复：
- **WNS 改善判定时序**: `_on_iteration_end()` 和 `_prev_best_wns` 移到 checkpoint/get_wns 成功之后执行
- **退出原因传递**: DONE 处理器设 flags 后走迭代结束处理
- **LLM 过早 DONE 抑制**: SYSTEM_PROMPT 中 DONE 语义收紧为 `WNS >= 0 achieved`；注入 `current_tns` 和 `failing_endpoints` 让 LLM 感知问题规模

**新增状态变量**: `latest_tns: Optional[float]`, `latest_failing_endpoints: Optional[int]`

### 5.4 Step 状态追踪（`report_step_state` Tool）

每轮 LLM 响应到达后，先从 `message.tool_calls` 中提取 `report_step_state` 参数，flow_control 优先于工具执行。

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
   → 跳过工具执行，跳转到 flow_control 处理
   else if tool_calls
   → 正常执行工具
   else （纯文本）
   → 现有纯文本处理逻辑
```

**StepState 数据结构**：
```python
@dataclass
class StepState:
    step_id: Optional[int] = None
    result_status: Optional[str] = None        # SUCCESS | PARTIAL | FAIL
    flow_control: Optional[str] = None         # ANALYZE_DONE | EXEC_DONE | CONTINUE | SWITCH_STRATEGY | NEXT_ITERATION | DONE | RETRY | ROLLBACK | EXHAUSTED
    has_tool_calls: bool = False
    raw_content: str = ""
    strategy_phase: Optional[str] = None       # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    strategy_name: Optional[str] = None        # PBLOCK | PhysOpt | Fanout | ...
```

**FlowControlRecord 数据结构**：
```python
@dataclass
class FlowControlRecord:
    signal: str = ""          # DONE | SWITCH_STRATEGY | NEXT_ITERATION | EXHAUSTED | ROLLBACK | ...
    iteration: int = 0
    tool_round: int = 0
    done_reason: str = ""
    phase: str = ""
    strategy: str = ""
    result_status: str = ""
    wns_at_decision: float | None = None
    wns_best: float | None = None
    timestamp: float = field(default_factory=time.time)
```

**StrategyState 数据结构**：
```python
@dataclass
class StrategyState:
    current_phase: str = ""                  # ANALYZE | SELECT_STRATEGY | EXECUTE_STRATEGY | EVALUATE
    current_strategy: str = ""               # PBLOCK, PhysOpt, Fanout, etc.
    phase_history: list[PhaseEntry] = []     # 阶段转换记录（上限100条）
    analysis_summary: str = ""
    strategy_rationale: str = ""
    evaluation_wns_delta: float = 0.0
    evaluation_result: str = "PENDING"       # IMPROVED | REGRESSION | UNCHANGED | PENDING
```

### 5.5 失败策略追踪（分级格式）

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
| 策略中断检测 | PBLOCK/Fanout | `execution_failure` | `_detect_unfinished_strategy()` |
| 工具返回空结果 | 工具所属策略 | `tool_error` | 工具输出匹配 `_EMPTY_RESULT_PATTERNS` |

**`_EMPTY_RESULT_PATTERNS` 空结果模式匹配**（`iteration_end.py:210-220`）：当 LLM 调用 `SWITCH_STRATEGY` 且工具输出包含以下模式时，归类为 `tool_error`（冷却后可重试）而非 `strategy_ineffective`（永久排除）：
- `"0 candidates"` / `"no candidates"` — 无候选优化对象
- `"no deep combinational"` / `"no high fanout"` — 无符合条件的目标
- `"optimized_count": 0` / `"cascades_found": 0` — LUTCascade 找到 cascade 但无法优化（如 NN 宽输入锥超出 6-input LUT 物理极限）

**分级格式**:
```python
{"strategy": "PBLOCK", "reason": "execution_failure", "tool": "vivado_create_and_apply_pblock", "iteration": 3, "detail": "..."}
```

**`_build_skill_recommendation()` 分级过滤**：
- `reason="strategy_ineffective"` → 永久排除
- `reason ∈ {tool_error, execution_failure}` → 冷却 2 个迭代后可重试
- `_strategy_blocked(name)` 辅助函数统一判断逻辑

**`_infer_strategy_from_tools()` 策略推断映射**：
- PBLOCK → 工具名含 `pblock`
- PhysOpt → 工具名含 `phys_opt`
- Fanout → 工具名含 `fanout` 或 `optimize_fanout`
- CongestionSpreading → 工具名含 `congestion_spread` 或 `execute_congestion_spreading`
- RegisterRetiming → 工具名含 `register_retiming` 或 `register_retime`
- PlaceRoute → 工具名含 `place_design` 或 `route_design`
- 以上均不匹配 → Information/Unknown（不记录失败）

**向后兼容**：
- `failed_strategies` 属性仍返回列表（元素从 `str` 变为 `dict`）
- 新增 `failed_strategy_names` 属性返回 `list[str]`

### 5.6 report_step_state 格式提醒

双重提醒机制：

**提醒 1 — User Message（一次性，会话起始）**
```
V2 optimize_v2() 中:
1. system_prompt → system prompt
2. prepare_context_node 首次执行时: user(FORMAT_GUARD)  ← state.model.format_guard_injected 标志控制
```

**提醒 2 — System Prompt 头部压印（每 LLM API 调用前）**
```
V2 llm_tool_loop 中 _prepare_api_messages():
api_messages[0]["content"] = FORMAT_STAMP + "\n\n" + system_content
（仅在 system_content 不以 "[FORMAT:" 开头时追加，防止重复）
```

**`report_step_state` Tool 定义**：
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

**4阶段状态机流程**：
| 阶段 | 可用工具 | LLM行为 | 状态转移信号 |
|------|---------|---------|------------|
| ANALYZE | 分析工具(~18个) | 收集时序/拥塞/扇出/布局数据 | ANALYZE_DONE |
| SELECT_STRATEGY | 极简(~4个) | 基于分析摘要选择策略 | strategy_name 非空 |
| EXECUTE | 全工具(~24个，不含vivado_open_checkpoint) | 执行策略工具，链式动作自动处理 | EXEC_DONE |
| EVALUATE | 评估工具(~8个) | 对比WNS变化，决定下一步 | DONE/NEXT/SWITCH/CONTINUE |

阶段切换时：当前阶段消息压缩存档→HistoricalMemory，下一阶段注入 PhaseHandoff 摘要上下文。

### 5.7 工具重复检测器

```python
_recent_tools: list[tuple[str, float]]  # 滑动窗口 (tool_name, wns)，最多5条
```

**检测条件**：连续 >=3 次相同工具 + WNS 总变化 < 0.05ns

**触发时行为**：注入 REPETITION DETECTED 警告 user 消息，清空窗口，关联触发周期反思。

### 5.8 周期反思触发器

每 8 个 `tool_round` 注入 REFLECTION CHECKPOINT，要求 LLM 显式 justify CONTINUE vs SWITCH_STRATEGY。

**触发规则**：
- `tool_round > 1` 且 `tool_round % 8 == 0`
- 重复检测器触发时：跳过周期等待，立即注入反思

**联动**: 重复检测触发 → 清空窗口 + 注入重复警告 → 立即触发周期反思。

### 5.9 工具状态合规性

**Pblock Cells 计数**：`create_and_apply_pblock` 追加 cells 计数命令。Summarizer 解析合规性，`cells_in_pblock < cells_in_design` 时设 `status=partial`。

**Tcl 多行命令支持**：按 `\n` 分割，在同一 Vivado 会话中顺序执行，变量跨行持久化。

### 5.10 WNS解析

`parse_timing_summary_static()` 会跳过许可证消息、命令回显和 info/warning 消息，在整个输出中搜索时序头，而非假设在开头。

### 5.11 迭代控制常量

```python
MAX_TOOL_ROUNDS_PER_ITERATION = 80
GLOBAL_NO_IMPROVEMENT_LIMIT = 3
WNS_TARGET_THRESHOLD = 0.0
```

**继续条件**: iteration<50 AND WNS<0 AND global_no_improvement<3 AND tool_rounds<=MAX_TOOL_ROUNDS_PER_ITERATION AND checkpoint保存成功 AND get_wns返回有效值

**WNS回归处理**: WNS<0且差于best时自动回滚。完成判定使用 latest_wns（当前），非 best_wns（历史）。

### 5.12 退出原因

| 原因 | 描述 |
|------|------|
| `cost_limit` | 达到成本硬限制（worker: $2.00, planner: $1.00） |
| `wns_target_met` | WNS>=0.0（时序收敛） |
| `max_iterations_reached` | 3次迭代无改进 |
| `tool_round_limit` | MAX_TOOL_ROUNDS_PER_ITERATION 轮工具调用达限 |
| `user_requested` | 用户输入quit |
| `flow_control_done_next_iteration` | LLM返回DONE但目标未达成 |
| `switch_strategy` | LLM返回SWITCH_STRATEGY |
| `rollback` | EVALUATE 检测到 WNS 退化 |
| `iteration_success` | LLM返回NEXT_ITERATION |

### 5.13 控制台退出

```
V2:
  stdin监听线程: 输入"quit" → state.control.user_exit_requested = True
  检查点: NodeGraph.run()循环顶部 + llm_tool_loop工具轮次边界
  路由: user_exit_requested → save_output → end（清除标志防死循环）
  摘要输出: save_output_node 通过 print() 输出 Optimization Summary 到 stdout
            optimize_v2() 返回前打印最终结果行（best_wns + converged）
```

### 5.14 429降级机制

```
1. 保存rate_limited_model
2. 标记为耗尽
3. 尝试下一fallback（轮询）
4. 成功后清last_exception，更新model_worker
5. 全耗尽则切换到另一层级模型
6. 切换时清空双方耗尽集合
```

### 5.15 事件系统

```python
EventBus (events.py)
├── subscribe(event_type, handler) → token
├── unsubscribe_by_token(token)
├── emit(event)

EventTypes: CONTEXT_COMPRESSED, LAYER_PROMOTED, BRANCH_CREATED, BRANCH_MERGED
```

## 6. 工具输出摘要化 + 历史自动裁剪

### 6.1 动机

phys_opt_design / route_design 等工具的原始 Vivado 日志单次可达 25k+ 字符，充斥 INFO 与重复的时序路径明细，导致模型注意力被稀释。

### 6.2 实现

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

# 小型输出（<3KB，非 timing）→ 直通嵌入 raw_output
tool_result:
  tool: vivado_get_wns
  summary: "WNS: -0.352"
  status: completed
  raw_output_truncated: false
  raw_output_chars: 84
  raw_output: |
    -0.352
```

- `raw_output_truncated` 真实性：JSON 工具 → `false`；大输出 → `true`
- **小型输出直通**: char_count < 3000 且非 timing 工具时，不提取字段，直接嵌入完整 raw_output
- `vivado_extract_critical_path_pins`: 路径预览从 2 条扩展到 10 条，引脚预览从 4 个扩展到 6 个
- `rapidwright_analyze_net_detour`: 解析 JSON 输出，空结果时标注 `"No cells exceeded threshold"`
- **`llm_hint` 运行时注入**: 当 `optimized_count=0` 时，检测 Java 错误信息

**2. `_raw_tool_outputs`** (dcp_optimizer.py): FIFO 淘汰（最多 50 条），仅当 LLM 调 `vivado_get_raw_tool_output` 时返回。

**3. `vivado_get_raw_tool_output`**: 内部工具，不走 MCP 服务器。

**4. 压缩阶段旧消息裁剪** (yaml_structured_compress.py):
- `_compress_outdated_tool_results()`: 迭代差 > 2 的工具消息替换为 `[COMPRESSED: {tool} iter={N} | {summary} | ...]`
- 保留关键指标：summary 文本、WNS、TNS、failing_endpoints、delta、status
- 失败策略工具结果仅压缩非当前迭代的
- 受保护工具（PROTECTED_ANALYSIS_TOOLS）跳过压缩
- 压缩后注入通知消息

**5. 压缩后角色保留** (yaml_structured_compress.py):
```python
api_messages = [
  system("SYSTEM_PROMPT + WNS state"),
  system("YAML compressed OLDER messages"),
  user("..."),                               # ← 保留role
  assistant("...", tool_calls=[...]),        # ← 保留role
  tool("..."),                               # ← 保留role
]
```

## 7. DCP验证（硬约束）

**逻辑等价性是优化过程的硬约束**。

```
验证策略:
├── 每5次迭代: 中间 checkpoint 验证（500 向量）
├── 完成时: 完整验证（Phase1 + Phase2, 10000 向量）
└── 条件: validation_enabled AND intermediate_dcp存在 AND 非完成态 AND iteration%5==0
```

**Phase 1 — 结构对比（RapidWright）**：比较 golden 和 revised DCP 的 EDIF 网表结构。

**Phase 2 — 功能仿真（Vivado xsim）**：从 golden 和 revised DCP 分别导出 Verilog 仿真模型，生成 LFSR 驱动的随机测试激励（10000 向量）。

**安全约束**：
- 禁止使用 `phys_opt_design` 的 retiming 指令（`AlternateFlowWithRetiming`、`AddRetime`）
- RegisterRetiming skill 使用的局部 FF 插入比全局 retiming 更安全
- pin swapping 和 net swapping 仅交换等效引脚，不改变逻辑函数

## 8. 心跳日志系统

```
tool call 入口:
├── DCPOptimizer.call_tool()           # 主要入口
├── FPGAOptimizerTest.call_vivado_tool()    # 测试模式
└── FPGAOptimizerTest.call_rapidwright_tool() # 测试模式

心跳机制 (_start_tool_heartbeat):
- 每60秒打印 [HEARTBEAT #{n}] Tool '{name}' still running after {elapsed}s
- 工具完成时打印 [TOOL_COMPLETE] '{name}' completed in {elapsed}s (heartbeats: {n})
```

## 9. 配置（model_config.yaml）

> 模型名称和 fallback 列表以 `model_config.yaml` 为准。本节仅保留压缩/阈值参数说明。

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
  cost_hard_limit: 1.00  # USD
  reasoning_enabled: true
  reasoning_max_output_tokens: 16384

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
  cost_hard_limit: 1.00  # USD
  reasoning_enabled: true
  reasoning_max_output_tokens: 16384
```

## 10. 重要常量

```python
WORKER_HARD_LIMIT = 200K, WORKER_TOKEN_BUDGET = 80K
PLANNER_HARD_LIMIT = 300K, PLANNER_TOKEN_BUDGET = 80K

# Dashboard 新鲜度追踪 (工具→影响字段映射)
DASHBOARD_REFRESH_MAP: dict[str, frozenset[str]] = {
    "vivado_report_utilization_for_pblock": {"resource_utilization"},
    "vivado_get_critical_high_fanout_nets": {"high_fanout_nets"},
    "rapidwright_analyze_critical_path_spread": {"critical_path_spread"},
}

# init_analysis 自适应超时 (design_size_factor 分档)
DESIGN_SIZE_BINS = [(0, 50000, 1.0), (50000, 150000, 1.5), (150000, 2**31, 3.0)]
MAX_TIMEOUT = 900.0  # 单次工具调用硬上限 (秒)

# Vivado Tcl 命令规范: Agent pipeline 禁用 report_* (cost 不可预测)
# 改用 get_* 命令 (cost 线性可控), 详见 init_analysis.py 与 vivado_mcp_server.py
```

## 11. Tool 描述增强（2026-05 新增）

**1. LIMITATIONS / Contraindications**：在工具 description 中标注不适用场景
**2. RESULT INTERPRETATION**：指导 LLM 正确理解工具返回值
**3. STRATEGY INTERACTION WARNING**：策略交互警告
**4. `llm_hint` 运行时注入**：当工具返回异常结果时注入提示
**5. SYSTEM_PROMPT.TXT 策略排序约束**
**6. strategy_library.py SKILL_GUIDANCE 增强字段**

## 12. phys_opt_design 安全守卫（2026-05-18 新增）

**背景**：`phys_opt_design` 的 retiming 指令会导致 LUT 链密集的神经网络设计出现功能错误。

**两层守卫**:

**1. VivadoMCP 服务端守卫**：
```python
BLOCKED_DIRECTIVES = {"AlternateFlowWithRetiming", "AddRetime"}
BLOCKED_BOOL_OPTIONS = {"retime", "interconnect_retime"}
SAFE_DIRECTIVES = {"Default", "Explore", "AggressiveExplore", ...}
```

**2. call_tool 入口守卫**：检查 directive 参数和 retime/interconnect_retime 布尔选项。
