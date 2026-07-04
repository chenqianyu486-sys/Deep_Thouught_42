# Deep Thought 42

[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE-APACHE-2.0.txt)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![FPGA](https://img.shields.io/badge/FPGA-Vivado%20%2B%20RapidWright-green)](#)
[![Contest](https://img.shields.io/badge/contest-FPL%202026-orange)](#)

**自主 LLM 驱动的 FPGA 时序收敛智能体。** 协调 Vivado 和 RapidWright，迭代优化布局布线（P&R）策略，直至最差负裕量（WNS）>= 0 —— 并提供形式化的逻辑等价性保证。

---

## 为什么选择这个项目？

- **无需手动时序收敛循环。** 智能体自主分析关键路径，选择优化策略，执行操作并评估结果。
- **保证逻辑等价性。** 每次优化均由 `validate_dcps.py`（结构差异比对 + 功能仿真）进行验证，确保设计行为永不改变。
- **双重架构。** V2 状态机用于保障生产环境的可靠性；V1 对话循环已弃用并移除。
- **实时可观测性。** 包含 20 个面板的 Web 仪表盘 —— 7 模块 StateSpace（Agent 数据输入层）+ 13 个旧版详情面板。每个流控决策、WNS 轨迹和 LLM 调用均可追踪。
- **16 种验证安全策略。** PBLOCK、PhysOpt、Fanout、PinSwap、LUTCascade、CellReplication、CongestionSpreading、NetSwap、OptDesign、LogicResynthesis、PhysOptAggressive，以及 3 个直击层间组合逻辑瓶颈的新策略：CombinationalRebalance（验证安全的 retiming——通过逻辑等价重综合重平衡 LUT6/MUXF7/MUXF8 级联深度，不插 FF）、LUTMUXFRepack（LUT6+MUXF 联合重打包，针对超过 6 输入 LUT 物理上限的 NN/宽数据通路锥）、MUXFTreeReorder（MUXF7/MUXF8 树重排——无 CARRY4 设计的 carry-reorder 对应物）、PlaceRouteDirectiveExplore（Place&Route指令探索，WNS停滞时扩探索空间）、CongestionRouteExplore（拥塞感知路由指令探索）。插入新流水线 FF 的策略（RegisterRetiming、SmartRetiming、PhysOpt+RegisterRetiming）因会改变设计延迟、无法通过逐周期功能仿真验证，已从策略目录中排除。

---

## 快速开始

```bash
# 1. 克隆仓库并设置环境
git clone https://github.com/chenqianyu486-sys/Deep_Thouught_42.git
cd Deep_Thouught_42
make setup

# 2. 设置你的 OpenRouter API 密钥
export OPENROUTER_API_KEY="sk-or-..."

# 3. 运行优化（状态机）
make run_optimizer DCP=input.dcp

# 4. 启动实时仪表盘
make run_optimizer_dashboard DCP=input.dcp
# 在浏览器打开 http://localhost:8080
```

---

## 架构

```text
                    ┌─────────────────────────────┐
                    │     dcp_optimizer.py         │
                    │   (CLI 入口 + V2 中枢)       │
                    └──────────┬──────────────────┘
                               │
                               ▼
                    ┌──────────────────┐
                    │   V2: 状态机      │
                    │   (9 个节点)      │
                    └────────┬─────────┘
                             │
                ┌────────────┼────────────┐
                ▼            ▼            ▼
         ┌────────────┐ ┌────────────┐ ┌────────────┐
         │ Vivado MCP │ │RapidWright │ │   LLM      │
         │  Server    │ │ MCP Server │ │(DeepSeek)  │
         └────────────┘ └────────────┘ └────────────┘
```

### V2 状态机拓扑

```text
init_analysis ──► [WNS >= 0?] ──YES──► save_output ──► end
                  └──NO──► iteration_start ──► select_model ──► prepare_context
                            ──► llm_tool_loop (4阶段子图: ANALYZE→SELECT→EXECUTE→EVALUATE)
                            ──► iteration_end ──► check_exit ──► loop/rollback/end
```

### 核心设计原则

**架构层面**:
| # | 原则 | 实现方式 |
|---|-----------|----------------|
| 1 | 故障安全，不阻塞 | 当 `report_step_state` 缺失时，自动合成 `CONTINUE` 信号 |
| 2 | 事实，而非主观判断 | 仪表盘仅包含原始测量数据，作为最后一条用户消息注入 |
| 3 | 关注点分离 | Worker（250K tokens，负责执行）vs. Planner（1M tokens，负责战略决策） |
| 4 | 单一调用路径 | V2 仅使用原生函数调用；无 XML/YAML 文本回退 |
| 5 | 单一事实来源 | 运行时数据存储在 `OptimizerState` 中；`MemoryManager` 中无影子副本 |

**数据与上下文层面**:
| # | 原则 | 实现方式 |
|---|-----------|----------------|
| 6 | 数据可信度 | `field_freshness` 追踪每字段状态 (`fresh`/`stale`)；Dashboard 每个值后显示 `[fresh]`/`[stale]` 标记；设计修改工具（`DESIGN_MODIFICATION_TOOLS`，2026-06-27 补充至 23 个）自动将所有字段降级为 `stale`（EXECUTE+EVALUATE 对称处理）；工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段为 `fresh`；EXECUTE 阶段自动用 state 可信数据覆盖 LLM 提供的 `critical_paths`/`critical_path_cells`；**FORMAT_GUARD 增加 `STALE DATA HANDLING` 章节明确指示 LLM 过期数据必须先刷新再做决策（2026-07）；ANALYZE/SELECT_STRATEGY 阶段入口自动调用 `vivado_report_timing_summary` 刷新过期 WNS（2026-07）** |
| 7 | 信息保留 | 压缩标记保留关键指标（WNS/TNS/FE/delta/status）；`preserve_role_turns=6` 保留原始 role |
| 8 | LLM 提示缓存 | 每 API 调用通过 `extra_body` 发送 `{"cache": {"prompt": true}}`，共享函数 `build_llm_extra_body()` |
| 9 | Dashboard 数据可信度注解 | 严格区分 `None`（未分析）与 `[]`/`0`（已分析但为零），带机器可读原因: `"N/A(congestion_analysis_not_supported)"` |
| 10 | 上下文工程：弱引导 | 系统提示词和 FORMAT_GUARD 描述问题和约束，而非处方解决方案。LLM 保留自主决策权 |
| 10b | 上下文工程：分层上下文管理 | 显式四层注入（STATIC > PINNED > DYNAMIC > EPHEMERAL）；CellNameRegistry 作为 Pinned 层每轮重建注入（绕过压缩），消除 LLM 在 EXECUTE 阶段"记忆重建"cell 名导致的幻觉 |
| 10c | 上下文工程：实体注册表 SSOT | `EntityRegistry`（`state.entity_registry`）为 canonical cell 名唯一权威来源；解析/search_cells 同步写入，设计修改后 `mark_stale()`，rollback 后 `clear()`；`tool_router` 在 LLM→MCP 边界校验（设计修改工具强制严格模式，拒绝注册表外名） |
| 10d | 上下文工程：策略结果表（2026-07） | Dashboard 末尾新增 `strategy_outcomes:` YAML 区块，分 `successful`（从 `optimization_history` 读取，含 WNS delta）和 `failed`（从 `failed_strategies` 读取，含阻塞原因和剩余轮数）两个子节。每次 LLM 调用可见，消除策略重复选择 |
| 10e | 上下文工程：策略-工具映射恢复（2026-07） | 在 SELECT_STRATEGY 阶段的 FORMAT_GUARD 中注入 `_STRATEGY_MAPPING_LINES`，让 LLM 在选择策略前就能验证执行工具是否存在，避免选择 CellReplication 等无对应工具的策略 |
| 10f | 上下文工程：Dashboard 截断透明化（2026-07） | 全量设计数据持久化为 `{run_dir}/design_data/` JSON 文件（`design_data.py`）；Dashboard 在被截断的模块后添加聚合统计（`unshown_path_stats`、`unshown_hotspots`、`unshown_high_fanout_nets`），LLM 无需额外工具调用即可了解未显示数据的整体特征；末尾 `truncation_advisory` 区段列出各模块截断量并给出 `design_data_read` 存取指南；新增数据同样带 `[fresh]`/`[stale]` 新鲜度标注 |

**验证与安全层面**:
| # | 原则 | 实现方式 |
|---|-----------|----------------|
| 11 | 逻辑等价性硬约束 | 所有优化均由 `validate_dcps.py` 验证（结构 + 功能） |
| 12 | DCP 身份完整性 | EXECUTE 阶段从工具白名单移除 `vivado_open_checkpoint` |
| 13 | 虚假正 WNS 检测 | 检查时序报告 `Design State`，若非 `Routed` 则标注警告 |
| 14 | 未布局 DCP 防护 | `save_output` 前检查设计状态，自动执行 place/route 修复 |
| 15 | Unplace 自动回滚 | 追踪 `place_design -unplace`，阶段退出时若未恢复则从 checkpoint 回滚 |
| 16 | Vivado 执行工具错误检测 | MCP 服务器检测 `ERROR: [` 文本，返回 JSON `{"error": "..."}` |
| 17 | retiming 安全守卫 | 阻止 `AlternateFlowWithRetiming`、`AddRetime` 等指令（双层防护） |
| 17b | 指令黑名单 + 自动回退（2026-07） | `KNOWN_BROKEN_DIRECTIVES` 列出因许可问题已知失败的指令（如 `Performance_ExtraTimingOpt`）；auto-chain 中检测到黑名单指令时自动回退到策略默认指令，避免 ~17 秒的失败 P&R 循环 |
| 17c | 检查点重载优化（2026-07） | `_reload_baseline_on_switch()` 在调用 `vivado_open_checkpoint` 前检查 `current_dcp_path` 是否已匹配目标检查点；若已加载则跳过重新打开，节省 ~27 秒/次 |
| 17d | 检查点同步修复（2026-07） | `_save_best_checkpoint()` 写入后同步更新 `state.control.current_dcp_path`，避免策略切换时因指针不一致触发无意义重载（~15s 浪费） |
| 17e | 迭代开始 checkpoint 拷贝优化（2026-07） | `iteration_start` 节点和 `_ensure_iteration_start_checkpoint` 优先从 `best_checkpoint.dcp` 拷贝而非通过 Vivado 序列化写入（节省 ~5s/次） |
| 17f | 临时路径修复（2026-07） | `pre_chain_pblock.dcp` 和 `pre_unplace_*` 检查点从硬编码 `/tmp/` 迁移到 `run_dir/`，消除并发任务覆盖风险 |

**策略与迭代层面**:
| # | 原则 | 实现方式 |
|---|-----------|----------------|
| 18 | 编码领域知识 | 16 种策略带有触发条件；LLM 自主选择 |
| 19 | 多策略循环 | 一次迭代内最多尝试 5 个策略 (`MAX_STRATEGY_CYCLES=5`) |
| 20 | TTL 策略重试（按原因分级） | `strategy_ineffective`→1 轮、`strategy_not_applicable`→2 轮、`no_improvement`→3 轮后自动解封；`tool_error`→无 TTL（立即重试） |
| 21 | 冷却逻辑分层 | 区分策略工具错误 vs 辅助工具错误；三档阈值：`delta>0` 不冷却（best 已保存）、`delta>0.050` 重置无进展计数、`delta≤0` 计无进展 |
| 22 | 工具结果缓存 | 同 phase 内相同参数自动命中缓存；执行工具后自动失效 |
| 23 | 工具调用频率限制 | `search_cells` 最多 3 次/phase，`vivado_run_tcl` 最多 2 次/phase |
| 24 | 收益递减自动检测 | 同一策略最近 2+ 次使用且每次 |delta| < 0.020ns 时标记为 `no_improvement`（TTL=3 轮） |
| 25 | 连续无进展检测 | EVALUATE 阶段连续 3 次无进展评估后强制 `SWITCH_STRATEGY` |
| 26 | 优化历史追踪 | 每次 `best_checkpoint` 更新时记录策略名/WNS 前后对比/迭代号，注入 handoff 和 Dashboard |
| 27 | 迭代开始 checkpoint | 每迭代开始时自动保存 DCP 快照，作为 `_reload_baseline_on_switch` 的次要回退基线；当 `best_checkpoint.dcp` 存在时使用 `shutil.copy2` 拷贝（节省 ~5s/次 Vivado 序列化） |
| 28 | 设计指纹缓存 | `transition_phase` 通过 `design_fingerprint` 比较决定是否保留 tool cache；设计未变更时跨阶段缓存保留 |

> 完整实现级技术细节见 [architecture.md](architecture.md)。

---

## 优化策略

| 策略 | 触发条件 | 平台 |
|----------|-------------------|----------|
| **PBLOCK** | 分散的路径（平均距离 > 70） | Vivado + RapidWright |
| **PhysOpt** | 1–2 条分散的关键路径，WNS > -2.0 | Vivado |
| **OptDesign** | 逻辑深度受限（logic_delay > 70%），6-7 级 LUT | Vivado（通过 RapidWright 技能 + 自动链式调用） |
| **Fanout** | 扇出 > 100，无分散 | RapidWright + Vivado |
| **PinSwap** | WNS 停滞在 ~-0.3ns，LUT 引脚延迟方差大 | RapidWright + Vivado |
| **LUTCascade** | >3 个 LUT 串联 | RapidWright + Vivado |
| **CellReplication** | 扇出 > 10 或延迟 > 0.3ns | RapidWright + Vivado |
| **CongestionSpreading** | 拥塞=HIGH | RapidWright + Vivado |
| **NetSwap** | SLICE 内部布线拥塞 | RapidWright + Vivado |
| **LogicResynthesis** | NN/数据通路设计含 MUXF7/8 级联，关键路径上组合级数深 | Vivado (synth_design -remap) |
| **PhysOptAggressive** | WNS > -3.0，逻辑深度受限且有散布的设计 | Vivado (Explore 指令) |
| **CombinationalRebalance** | 寄存器间深组合链（LUT6/MUXF7/MUXF8 级联，逻辑级数 >= 3） | Vivado (opt_design -remap，通过 RapidWright 定点分析 + 自动链式) |
| **LUTMUXFRepack** | NN/宽数据通路，MUXF7/MUXF8 + LUT6 级联在关键路径上 | Vivado (opt_design -AddRemap，通过 RapidWright 定点分析 + 自动链式) |
| **MUXFTreeReorder** | 无 CARRY4 的 NN 设计，MUXF7/MUXF8 树 >= 2 级在关键路径上，布线延迟主导 | Vivado (phys_opt_design 无 -retime，通过 RapidWright 定点分析 + 自动链式) |
| **PlaceRouteDirectiveExplore** | WNS停滞（近2轮 |delta|<0.05ns），指令组合未充分探索 | Vivado（Place&Route指令探索） |
| **CongestionRouteExplore** | 拥塞分析 severity=MEDIUM/HIGH，WNS > -1.0，拥塞场景路由指令未探索 | Vivado（拥塞感知路由指令探索） |

新增 `report_qor_suggestions`（ML驱动策略建议）、`report_high_fanout_nets`（高扇出网络原生报告）和 `set_incremental_checkpoint`（增量编译，可省30-50%迭代时间）等专用工具。place_design 和 route_design 现已接入安全指令白名单，新增 AddRemap 至 opt_design 安全指令列表（修复LUTMUXFRepack策略），RWRoute 禁用状态已文档化并增加环境变量开关。

LLM可调布局布线指令：8个技能包装器（pblock、physopt、opt_design、combinational_rebalancing、lut_muxf_repack、muxf_tree_reorder、fanout、flatten_lut_cascade）现支持可选的 `place_directive`/`route_directive` 参数，通过 `_attach_chain_directives()` 辅助函数注入自动链。LLM可在白名单内自由选择指令，省略时回退为"Explore"。修复了 `_strategy_plan_to_dict` 中opt/physopt指令因嵌套于 `analysis_summary` 内而被忽略、始终回退为"Explore"的bug。安全指令白名单（`PLACE_SAFE_DIRECTIVES`/`ROUTE_SAFE_DIRECTIVES`）在VivadoMCP服务端强制执行，register_retiming自动链因破坏周期精确等价性而排除。

现新增三层回退机制：LLM 显式传入 > 策略默认值 > 硬编码 "Explore"。当 LLM 省略指令时，`_execute_chain_actions`（`optimizer/nodes/subgraphs/phase_execute.py`）优先查询 `STRATEGY_DEFAULT_DIRECTIVES`（定义于 `optimizer/pure/constants.py`），为各策略匹配 `PR_DIRECTIVE_COMBINATIONS` 场景中的典型瓶颈指令对。例如 `opt_design`/`combinational_rebalancing`/`flatten_lut_cascade` → `("ExtraTimingOpt", "NoTimingRelaxation")`（逻辑深度受限设计）；`physopt`/`muxf_tree` → `("Explore", "Explore")`；`pblock`/`fanout` → 仅路由 `(None, "NoTimingRelaxation")`。`"args_from_skill" in step` 守卫确保指令回退仅作用于含 `args_from_skill` 的 place/route 步。PBLOCK 链 step1 已改为 `vivado_unplace_cells(cells=critical_path_cells)`（局部 unplace，2026-07-04，原为全局 `place_design -unplace`），`create_and_apply_pblock` 传 `cells` 实现局部 pblock。`place=None` 表示该策略链不含 place_design 步骤。

---

## 先决条件

| 依赖项 | 最低版本 | 用途 |
|------------|-----------------|---------|
| Python | 3.10+ | 智能体运行时 |
| Vivado | 2025.1+ | 布局布线、时序分析、Tcl 脚本编写 |
| Java (JRE) | 11+ | RapidWright 运行时 |
| RapidWright | (作为子模块捆绑) | 单元级操作 |
| OpenRouter API | — | LLM 访问 (DeepSeek V4 Pro) |

---

## 环境变量

```bash
OPENROUTER_API_KEY    # 必需 — OpenRouter API 密钥
VIVADO_EXEC           # 可选 — Vivado 可执行文件路径 (默认: vivado)
JAVA_HOME             # 可选 — Java 安装路径 (RapidWright 依赖)
```

---

## 使用方法

### 基础优化

```bash
# 状态机（默认）
python dcp_optimizer.py input.dcp

# 设置 30 分钟超时并自定义输出
python dcp_optimizer.py input.dcp --timeout 1800 --output output.dcp
```

### 测试（无 LLM）

```bash
# 完整 V2 测试（工具 + 技能 + 布局布线）
make run_test_v2 DCP=demo_corundum_25g_misses_timing.dcp

# 仅技能测试（快速，无布局布线）
make run_skill_test_v2 DCP=demo_corundum_25g_misses_timing.dcp
```

### 仪表盘

```bash
# 在 8080 端口启动仪表盘
make run_optimizer_dashboard DCP=input.dcp

# 自定义端口
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090
```

仪表盘提供 20 个实时面板：

**7 模块 StateSpace（Agent 数据输入层）**: M1 Global State（WNS/TNS/利用率）、M2 Timing Clusters（Top-20 违例路径）、M3 Physical Congestion（拥塞/热点）、M4 Netlist Quality（扇出/控制集）、M5 Constraints（时钟/约束）、M6 Dynamic Gradient（上一步 delta）、M7 Architecture Overview（模块级热力图）。

**13 个旧版详情面板**: Timing、Iteration、Strategy Lifecycle、Model、Cost、Control、Critical Paths、LLM Log、Transition History、Tool Call Trace、Flow Control Log、Phase History、WNS Trajectory。

---

## 项目结构

```
Deep_Thouught_42/
├── dcp_optimizer.py          # CLI 入口：V2 状态机启动 + 模型配置
├── optimizer/                # V2 状态机框架（45 文件）
│   ├── state.py              # OptimizerState + 7 子切片 dataclass
│   ├── deps.py               # NodeDeps：外部依赖容器
│   ├── graph.py / edges.py   # NodeGraph 执行引擎 + 条件边
│   ├── nodes/                # 9 个节点 + llm_tool_loop 子图（4 阶段）
│   └── pure/                 # 17 个纯函数模块（可独立单元测试，含 entities.py 实体注册表 + design_data.py 设计数据持久化）
├── strategy_library.py       # 16 种策略及触发条件
├── skills/                   # Skill 框架（渐进式三层加载，34 文件）
├── RapidWrightMCP/           # RapidWright MCP 服务器（39 工具）
├── VivadoMCP/                # Vivado MCP 服务器（20+ 工具）
├── context_manager/          # 内存/压缩管理（EventBus + yaml_structured 压缩器）
├── dashboard/                # Web 仪表盘（aiohttp + WebSocket，20 面板）
├── validate_dcps.py          # DCP 逻辑等价性验证器（结构 + 功能双阶段）
├── config_loader.py          # 模型配置加载器（单例）
├── model_config.yaml         # LLM 层级与回退配置
├── Makefile                  # 构建自动化（setup/run/test/validate）
├── CONTRIBUTING.md           # 贡献工作流与同步清单
├── docs/                     # 竞赛提交文档
└── .claude/                  # Claude Code 配置 + MCP 设置
```

> 完整模块级结构见 [PROJECT_TREE_AND_DATA_FLOW.md §1](PROJECT_TREE_AND_DATA_FLOW.md#1-项目结构模块级)。

---

## 模型配置

两个模型层级，根据上下文窗口和压缩参数进行区分：

| 参数 | Worker | Planner |
|-----------|--------|---------|
| 模型 | `deepseek/deepseek-v4-pro` | `deepseek/deepseek-v4-pro` |
| 最大 tokens | 250K | 1M |
| 软阈值 | 175K | 200K |
| 硬限制 | 200K | 300K |
| 保留轮次 | 40 / 25 (硬) | 60 / 40 (硬) |
| 成本硬限制 | $1.00 | $1.00 |

编辑 `model_config.yaml` 以自定义模型、阈值和回退链。

---

## 性能表现

基于 `demo_corundum_25g_misses_timing` 基线的基准测试（典型场景）：

| 指标 | 优化前 | 优化后 | 改善幅度 |
|--------|--------|-------|-------------|
| WNS | -2.347 ns | 0.012 ns | +2.359 ns |
| TNS | -48.2 ns | 0.0 ns | +48.2 ns |
| 失败端点 | 127 | 0 | -127 |
| 迭代次数 | — | 4–8 | — |
| LLM 成本 | — | ~$0.15–$0.40 | — |

*结果因设计复杂度和初始时序违例的严重程度而异。*

---

## 贡献指南

详见 [CONTRIBUTING.md](CONTRIBUTING.md)，包含贡献工作流、测试模式、以及新增策略/工具时的同步清单。

---

## 故障排除

### `Vivado license not found`（未找到 Vivado 许可证）

```bash
# 验证 Vivado 是否可访问
which vivado
# 如有需要，加载 Vivado 环境变量
source /opt/Xilinx/Vivado/2025.1/settings64.sh
```

### `OPENROUTER_API_KEY not set`（未设置 OPENROUTER_API_KEY）

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
# 添加到 ~/.bashrc 以持久化
echo 'export OPENROUTER_API_KEY="sk-or-v1-..."' >> ~/.bashrc
```

### `RapidWright Java error`（RapidWright Java 错误）

```bash
# 确保已安装 Java 11+
java -version
# 如有需要，设置 JAVA_HOME
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
```

### Dashboard not loading（仪表盘无法加载）

```bash
# 检查端口可用性
lsof -i :8080
# 尝试备用端口
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090
```

### WNS not improving after many iterations（多次迭代后 WNS 未改善）

- 当 WNS 恶化 >30ps 时，智能体会自动回滚
- 检查仪表盘中的 Flow Control Log（流控日志）以获取 `EXHAUSTED` 信号
- 尝试在状态配置中增加 `no_improvement_limit`
- 验证 `validate_dcps.py` 是否能通过你的基线 DCP

### Analysis data inconsistencies（分析数据不一致导致策略误判）

已知数据流缺陷可能导致 LLM 在策略选择时被错误或过期的分析数据误导，详见 [PROJECT_TREE_AND_DATA_FLOW.md 附录A](PROJECT_TREE_AND_DATA_FLOW.md) 和 [architecture.md §15](architecture.md)：

- **`check_design_status` 对已布线设计返回 `is_routed=false`** — `get_property STATUS` 在 `open_checkpoint` 后返回空字符串
- **高扇出网线初始扫描结果为 0** — `min_fanout=100` 阈值过高 + 父网线名解析边界情况
- **Module 3（物理与拥塞指标）在策略切换后消失** — EVALUATE/SELECT_STRATEGY 阶段 Dashboard 不包含拥塞/高扇出/路由数据
- **拥塞工具摘要为空** — `rapidwright_analyze_congestion` 的 `compact_tool_summary` 生成为 `{`（JSON 前缀）

---

## 许可证

Apache 2.0 — 请参阅 [LICENSE-APACHE-2.0.txt](LICENSE-APACHE-2.0.txt)。

Copyright (C) 2026, Advanced Micro Devices, Inc. 保留所有权利。

---

## 致谢

- **Vivado** 和 **RapidWright** (由 AMD/Xilinx 提供) —— EDA 核心基石
- **MCP (Model Context Protocol)** —— 工具调用基础设施
- **FPL 2026** —— 推动本项研究的竞赛
- **Douglas Adams** —— 项目名称的灵感来源
