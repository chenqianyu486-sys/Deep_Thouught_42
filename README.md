# Deep Thought 42

[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE-APACHE-2.0.txt)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![FPGA](https://img.shields.io/badge/FPGA-Vivado%20%2B%20RapidWright-green)](#)
[![LLM](https://img.shields.io/badge/LLM-DeepSeek%20V4%20Flash-purple)](#)
[![Contest](https://img.shields.io/badge/contest-FPL%202026-orange)](#)

**Autonomous LLM-driven FPGA timing closure agent.** Orchestrates Vivado and RapidWright to iteratively optimize P&R strategies until WNS >= 0 — with formal logic equivalence guarantees.

---

## Why This Project?

- **No manual timing closure loops.** The agent autonomously analyzes critical paths, selects optimization strategies, executes them, and evaluates results.
- **Logic equivalence guaranteed.** Every optimization is verified by `validate_dcps.py` (structural diff + functional simulation), ensuring the design behavior never changes.
- **Dual architecture.** V2 state machine for production reliability; V1 conversational loop removed (deprecated).
- **Real-time observability.** Web Dashboard with 20 panels — 7-module StateSpace (agent data input layer) + 13 legacy detail panels. Every flow control decision, WNS trajectory, and LLM call is traceable.
- **14 battle-tested strategies.** PBLOCK, PhysOpt, Fanout, PinSwap, LUTCascade, CellReplication, CongestionSpreading, RegisterRetiming, SmartRetiming, NetSwap, PhysOpt+RegisterRetiming, OptDesign, LogicResynthesis, PhysOptAggressive.
- **Multi-strategy loop.** Up to 3 strategies can be tried per iteration, with TTL-based strategy retry (3 iterations). Failed strategies auto-unblock after TTL expires.

---

## Quick Start

```bash
# 1. Clone and set up environment
git clone https://github.com/chenqianyu486-sys/Deep_Thouught_42.git
cd Deep_Thouught_42
make setup

# 2. Set your OpenRouter API key
export OPENROUTER_API_KEY="sk-or-..."

# 3. Run optimization (state machine)
make run_optimizer DCP=input.dcp

# 4. With live dashboard
make run_optimizer_dashboard DCP=input.dcp
# Open http://localhost:8080
```

---

## Architecture

```
                    ┌─────────────────────────────┐
                    │     dcp_optimizer.py         │
                    │   (CLI entry + V2 hub)       │
                    └──────────┬──────────────────┘
                               │
                               ▼
                    ┌──────────────────┐
                    │   V2: State       │
                    │   Machine (9 nodes)│
                    └────────┬─────────┘
                             │
                ┌────────────┼────────────┐
                ▼            ▼            ▼
         ┌────────────┐ ┌────────────┐ ┌────────────┐
         │ Vivado MCP │ │RapidWright │ │   LLM      │
         │  Server    │ │ MCP Server │ │(DeepSeek)  │
         └────────────┘ └────────────┘ └────────────┘
```

### V2 State Machine Topology

```
init_analysis ──► [WNS >= 0?]
  │  YES ──► save_output ──► end
  │  NO  ──► iteration_start ──► select_model ──► prepare_context
  │            ──► llm_tool_loop ──► iteration_end ──► check_exit
  │                  │                       │
  │       ┌──────────┴──────────┐           │
  │       ▼          ▼          ▼           │
  │   ANALYZE ──► SELECT ──► EXECUTE ──► EVALUATE
  │       ▲                    ▲              │
  │       └────── CONTINUE ───┼──────────────┘
  │                            │
  │       SWITCH ─────────────┘  (multi-strategy loop, max 3 per iteration)
  │                                          │
  │       DONE / NEXT / ROLLBACK ──► iteration_start
```

### Key Design Principles

40 principles covering fail-safety, data trustworthiness, DCP identity, tool caching, adaptive PBLOCK, LLM prompt caching, and more. See the Chinese section below (核心设计原则) for the full table.


## Optimization Strategies

14 strategies: PBLOCK, PhysOpt, OptDesign, Fanout, PinSwap, LUTCascade, CellReplication, CongestionSpreading, RegisterRetiming, SmartRetiming, NetSwap, PhysOpt+RegisterRetiming, LogicResynthesis, PhysOptAggressive. See the Chinese section below (优化策略) for trigger conditions and platform details.


## Prerequisites

| Dependency | Minimum Version | Purpose |
|------------|-----------------|---------|
| Python | 3.10+ | Agent runtime |
| Vivado | 2024.1+ | P&R, timing analysis, Tcl scripting |
| Java (JRE) | 11+ | RapidWright runtime |
| RapidWright | (bundled as submodule) | Cell-level manipulation |
| OpenRouter API | — | LLM access (DeepSeek V4 Flash) |

---

## Environment Variables

```bash
OPENROUTER_API_KEY    # Required — OpenRouter API key
VIVADO_EXEC           # Optional — Vivado executable path (default: vivado)
JAVA_HOME             # Optional — Java installation path (RapidWright dependency)
```

---

## Usage

### Basic Optimization

```bash
# State machine (default)
python dcp_optimizer.py input.dcp

# With 30-minute timeout and custom output
python dcp_optimizer.py input.dcp --timeout 1800 --output output.dcp
```

### Testing (No LLM)

```bash
# Full V2 test (tools + skills + place/route)
make run_test_v2 DCP=demo_corundum_25g_misses_timing.dcp

# Skill-only test (fast, no place/route)
make run_skill_test_v2 DCP=demo_corundum_25g_misses_timing.dcp
```

### Dashboard

```bash
# Launch with dashboard on port 8080
make run_optimizer_dashboard DCP=input.dcp

# Custom port
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090
```

The dashboard provides 20 real-time panels: 7-module StateSpace (Global State, Timing Clusters, Physical/Congestion, Netlist Quality, Constraints, Dynamic Gradient, Architecture Overview) + 13 legacy detail panels. See the Chinese section below (仪表盘) for the full panel listing.


## Project Structure

See the Chinese section below (项目结构) for the full directory tree, or refer to [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md).


## Model Configuration

Two model tiers, differentiated by context window and compression parameters:

| Parameter | Worker | Planner |
|-----------|--------|---------|
| Model | `deepseek/deepseek-v4-flash` | `deepseek/deepseek-v4-flash` |
| Max tokens | 250K | 1M |
| Soft threshold | 175K | 200K |
| Hard limit | 200K | 300K |
| Preserve turns | 40 / 25 (hard) | 60 / 40 (hard) |
| Cost hard limit | $1.00 | $1.00 |

Edit `model_config.yaml` to customize models, thresholds, and fallback chains.

---

## Performance

Benchmarks from the `demo_corundum_25g_misses_timing` baseline (typical scenario):

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| WNS | -2.347 ns | 0.012 ns | +2.359 ns |
| TNS | -48.2 ns | 0.0 ns | +48.2 ns |
| Failing Endpoints | 127 | 0 | -127 |
| Iterations | — | 4–8 | — |
| LLM Cost | — | ~$0.15–$0.40 | — |

*Results vary by design complexity and initial timing violation severity. Recent improvements: timing-aware placement/routing (Explore directives), PBLOCK multiplier variation, AggressiveExplore PhysOpt, relaxed pre-check thresholds, extended iteration budget.*

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution workflow, test modes, and the checklist for adding new strategies/tools.

---

## Troubleshooting

See the Chinese section below (故障排除) for common issues and solutions.


## License

Apache 2.0 — see [LICENSE-APACHE-2.0.txt](LICENSE-APACHE-2.0.txt).

Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.

---

## Acknowledgments

- **Vivado** and **RapidWright** by AMD/Xilinx — the EDA backbone
- **DeepSeek V4 Flash** via OpenRouter — the LLM reasoning engine
- **MCP (Model Context Protocol)** — tool-calling infrastructure
- **FPL 2026** — competition driving this research
- **Douglas Adams** — inspiration for the project name


# Deep Thought 42

[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE-APACHE-2.0.txt)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![FPGA](https://img.shields.io/badge/FPGA-Vivado%20%2B%20RapidWright-green)](#)
[![LLM](https://img.shields.io/badge/LLM-DeepSeek%20V4%20Flash-purple)](#)
[![Contest](https://img.shields.io/badge/contest-FPL%202026-orange)](#)

**自主 LLM 驱动的 FPGA 时序收敛智能体。** 协调 Vivado 和 RapidWright，迭代优化布局布线（P&R）策略，直至最差负裕量（WNS）>= 0 —— 并提供形式化的逻辑等价性保证。

---

## 为什么选择这个项目？

- **无需手动时序收敛循环。** 智能体自主分析关键路径，选择优化策略，执行操作并评估结果。
- **保证逻辑等价性。** 每次优化均由 `validate_dcps.py`（结构差异比对 + 功能仿真）进行验证，确保设计行为永不改变。
- **双重架构。** V2 状态机用于保障生产环境的可靠性；V1 对话循环已弃用并移除。
- **实时可观测性。** 包含 20 个面板的 Web 仪表盘 —— 7 模块 StateSpace（Agent 数据输入层）+ 13 个旧版详情面板。每个流控决策、WNS 轨迹和 LLM 调用均可追踪。
- **12 种久经考验的策略。** PBLOCK、PhysOpt、Fanout、PinSwap、LUTCascade、CellReplication、CongestionSpreading、RegisterRetiming、SmartRetiming、NetSwap、PhysOpt+RegisterRetiming、OptDesign。

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
init_analysis ──► [WNS >= 0?]
  │  YES ──► save_output ──► end
  │  NO  ──► iteration_start ──► select_model ──► prepare_context
  │            ──► llm_tool_loop ──► iteration_end ──► check_exit
  │                  │                       │
  │       ┌──────────┴──────────┐           │
  │       ▼          ▼          ▼           │
  │   ANALYZE ──► SELECT ──► EXECUTE ──► EVALUATE
  │       ▲                                  │
  │       └────── CONTINUE ──────────────────┘
  │                                          │
  │       DONE / NEXT / SWITCH / ROLLBACK ──► iteration_start
```

### 核心设计原则

| # | 原则 | 实现方式 |
|---|-----------|----------------|
| 1 | 故障安全，不阻塞 | 当 `report_step_state` 缺失时，自动合成 `CONTINUE` 信号 |
| 2 | 事实，而非主观判断 | 仪表盘仅包含原始测量数据，作为最后一条用户消息注入 |
| 3 | 消除冗余 | 仪表盘是唯一的实时数据源；交接时仅传递迭代记忆 |
| 4 | 显式优于隐式 | 9 节点状态机 + 类型化数据类（dataclass）状态切片 |
| 5 | 关注点分离 | Worker（250K tokens，负责执行） vs. Planner（1M tokens，负责战略决策） |
| 6 | 单一调用路径 | V2 仅使用原生函数调用；无 XML/YAML 文本回退 |
| 7 | 单一事实来源 | 运行时数据存储在 `OptimizerState` 中；`MemoryManager` 中无影子副本 |
| 8 | 编码领域知识 | 12 种策略带有触发条件；LLM 自主选择 |
| 9 | 数据可信度 | `DASHBOARD_REFRESH_MAP` 追踪字段新鲜度；自动注释过期数据 |
| 10 | 信息保留 | 压缩标记保留关键指标（WNS/TNS/FE/delta/status） |
| 11 | 逻辑等价性硬约束 | 所有优化均由 `validate_dcps.py` 验证（结构 + 功能） |
| 12 | DCP 身份完整性 | 在 EXECUTE 阶段，将 `vivado_open_checkpoint` 从 LLM 工具白名单中移除 |
| 13 | **工具结果缓存** | 同 phase 内相同工具+参数自动命中缓存，避免 LLM 重复调用；执行工具（place_design, route_design 等）后自动失效缓存防止物理数据过期 |
| 14 | **只读工具白名单控制** | 与 Dashboard 数据冗余的工具（`get_wns`, `get_resource_counts`）从 ANALYZE/EVALUATE 白名单移除；Rate limiting（`search_cells` 最多 3 次/phase, `vivado_run_tcl` 最多 5 次/phase）防止 LLM 浪费轮次 |
| 15 | **PBLOCK 自适应紧缩** | 公式 `M = max(1.10, 1.2 + util_local x 0.3 - 0.1 x log10(N_LUT))`，低利用率自动紧缩 region，高利用率自动宽松 |
| 16 | **LLM 提示缓存** | 每次 API 调用通过 `extra_body` 发送 `{"cache": {"prompt": true}}`。OpenRouter 在同一会话的重复调用间缓存系统提示前缀，每轮迭代节省约 4KB×44 次 ≈ 176KB tokens。共享函数 `build_llm_extra_body()` 位于 `optimizer/pure/constants.py`。 |
| 17 | **Dashboard 数据可信度注解** | Dashboard 严格区分 `None`（未分析）与 `[]`/`0`（已分析但为零）。每个 N/A 和空列表携带机器可读原因: `"N/A(congestion_analysis_not_supported)"`、`[]  # no_high_fanout_nets_found`。 |
| 18 | **Vivado 超时自动重启** | Tcl 超时会污染 Vivado session。不再使用不可靠的 `sync_after_timeout()`，改为 MCP server 内部自动 kill→restart→reopen DCP。移除 `_command_pending` 全局状态。 |
| 19 | **未布局 DCP 保存防护** | 在写入输出 DCP 前，`save_output` 查询 `get_property STATUS [current_design]`，回退到时序报告 `Design State` 字段（识别 `routed`/`placed`/`optimized` 三种状态）。若设计未布线，从 best_checkpoint 恢复或自动执行 `place_design` + `route_design` 修复后再保存。写入后再次验证设计状态，若非 `routed` 则记录警告。防止保存未布局 DCP 导致 `validate_dcps.py` 验证失败。 |
| 20 | **虚假正 WNS 检测** | `_post_eval_hook` 和 `_track_wns_from_result` 检查时序报告中的 `Design State`。若非 `Routed`，记录警告并追加到评估通知（`[WARNING: design not routed]`），提醒 LLM WNS 可能不准确。Place-only WNS 检查也验证设计状态——若为 `Optimized`（未布局），跳过 WNS 检查避免基于估计延迟的虚假正信号。 |
| 29 | **Vivado 执行工具错误检测** | `place_design`、`route_design`、`phys_opt_design`、`opt_design`、`physopt_and_route` 在 MCP 服务器中检测 Vivado `ERROR: [` 文本，返回 JSON `{"error": ...}` 响应。链式执行（`phase_execute.py`）同时检查 JSON `error` 键和文本 `ERROR: [` 模式，确保 Vivado 命令失败时链中止并回滚，而非静默继续。 |
| 21 | **Unplace 自动回滚** | EXECUTE 阶段追踪 `place_design -unplace` 调用。若阶段退出时未执行后续 `place_design`（非 unplace），自动从 pre-unplace checkpoint 恢复设计并刷新 WNS。 |
| 22 | **多策略循环** | 一次迭代内最多尝试 3 个策略 (`MAX_STRATEGY_CYCLES=3`)。EVALUATE 的 `SWITCH_STRATEGY` 信号触发循环回 SELECT_STRATEGY（跳过 ANALYZE）。防止单次迭代因单一失败策略浪费。 |
| 23 | **TTL 策略重试** | `FailedStrategyRecord.blocked_until_iter` 为策略阻止添加 TTL。`strategy_ineffective` 策略在 `STRATEGY_RETRY_TTL=3` 轮迭代后自动解封。防止策略目录被永久阻止耗尽。 |
| 24 | **EXECUTE 约束放宽** | 执行策略工具后，LLM 可调用 `rapidwright_report_timing` 快速反馈（~2.5s vs ~14s 全 Vivado 时序），然后信号 EXEC_DONE。提供快速方向性检查。 |
| 25 | **上下文工程：弱引导** | 系统提示词和 FORMAT_GUARD 描述问题和约束，而非处方解决方案。工具过滤 + auto-chain 处理执行机制。LLM 保留自主策略选择和诊断决策权。 |
| 26 | **设计一致性验证工具** | 4 个验证工具（`vivado_check_design_status`, `vivado_validate_timing`, `rapidwright_estimate_timing`, `rapidwright_compare_designs`）在所有阶段可用。LLM 可自主验证设计状态，确保修改后一致性。 |
| 27 | **独立 RapidWright 工具** | 19 个 RapidWright 工具（8 个分析 + 10 个执行 + 1 个验证）暴露给 LLM，支持细粒度控制。LLM 可自主选择工具组合，而非被硬编码链限制。 |
| 28 | **可选链验证** | `OPTIONAL_CHAIN_VALIDATION` 提供 4 个可选验证链，LLM 可选择是否在执行前后插入验证步骤。验证工具（`vivado_check_design_status`, `vivado_validate_timing`, `rapidwright_compare_designs`）确保设计一致性。 |

---

## 优化策略

| 策略 | 触发条件 | 平台 |
|----------|-------------------|----------|
| **PBLOCK** | 分散的路径（平均距离 > 70） | Vivado + RapidWright |
| **PhysOpt** | 1–2 条分散的关键路径，WNS > -2.0 | Vivado |
| **OptDesign** | 逻辑深度受限（logic_delay > 70%），PhysOpt 无效，6-7 级 LUT | Vivado（通过 RapidWright 技能 + 自动链式调用） |
| **Fanout** | 扇出 > 100，无分散 | RapidWright + Vivado |
| **PinSwap** | WNS 停滞在 ~-0.3ns，LUT 引脚延迟方差大 | RapidWright + Vivado |
| **LUTCascade** | >3 个 LUT 串联 | RapidWright + Vivado |
| **CellReplication** | 扇出 > 10 或延迟 > 0.3ns | RapidWright + Vivado |
| **CongestionSpreading** | 拥塞=HIGH，PBLOCK/PhysOpt 无效 | RapidWright + Vivado |
| **RegisterRetiming** | 深层组合逻辑链（>2 个 LUT） | RapidWright + Vivado |
| **SmartRetiming** | WNS 停滞，深层组合逻辑链（>2 个 LUT）位于流水线寄存器之间，FF > 0 | RapidWright + Vivado |
| **NetSwap** | SLICE 内部布线拥塞 | RapidWright + Vivado |
| **PhysOpt+RegisterRetiming** | 逻辑深度受限（logic_delay > 70%），WNS > -2.0，深层链（>2 个 LUT），FF > 0 | Vivado + RapidWright（原子操作） |
| **LogicResynthesis** | 100% 逻辑延迟，NN/数据通路设计含 MUXF7/8 级联，PBLOCK 已应用 | Vivado (synth_design -remap) |
| **PhysOptAggressive** | WNS 在 PBLOCK 后停滞，需要更激进的优化 | Vivado (Explore 指令) |

---

## 先决条件

| 依赖项 | 最低版本 | 用途 |
|------------|-----------------|---------|
| Python | 3.10+ | 智能体运行时 |
| Vivado | 2024.1+ | 布局布线、时序分析、Tcl 脚本编写 |
| Java (JRE) | 11+ | RapidWright 运行时 |
| RapidWright | (作为子模块捆绑) | 单元级操作 |
| OpenRouter API | — | LLM 访问 (DeepSeek V4 Flash) |

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

仪表盘提供 20 个实时面板（7 模块 StateSpace + 13 个旧版详情面板）：

**7 模块 StateSpace（Agent 数据输入层）：**

| 模块 | 面板 | 内容 |
|--------|-------|---------|
| **M1** | **Global State & Targets** | 阶段标签、WNS/TNS/WHS/THS 裕量、LUT/FF/DSP/BRAM 利用率进度条 |
| **M2** | **Timing Path Clusters** | Top-20 违例端点，含时钟组、逻辑/布线延迟占比、逻辑级数 |
| **M3** | **Physical & Congestion** | 全局拥塞评分、热点区域（bbox + 严重度 + 模块）、Pblock 溢出数 |
| **M4** | **Netlist Quality** | 高扇出网络（含复制状态）、控制集、跨时钟域路径、推理失败列表 |
| **M5** | **Constraints Environment** | 时钟表（名称→频率）、伪/多周期路径数、IO 延迟覆盖率、PVT corner |
| **M6** | **Dynamic Gradient (Delta)** | delta_WNS、delta_TNS、delta_congestion、上一步动作 + 动作状态 |
| **M7** | **Architecture Overview** | 模块级时序热力图（critical_path_hits, path_coverage）、跨/模块内关键路径计数、最深逻辑模块。零成本：从关键路径 cell 名解析。

**旧版详情面板：**

| 面板 | 内容 |
|-------|---------|
| **Timing (时序)** | WNS / TNS / 失败端点及迷你折线图 |
| **Iteration (迭代)** | 计数器、无改善追踪、策略序列 |
| **Strategy Lifecycle (策略生命周期)** | 4 阶段指示器 + 当前策略/评估 |
| **Model (模型)** | 当前模型、回退状态、调用次数 |
| **Cost (成本)** | 总成本及进度条、Token 细分 |
| **Control (控制)** | 运行时状态、已用时间、DCP 路径 |
| **Critical Paths (关键路径)** | 单元列表及每条路径的时序详情 |
| **LLM Log (LLM 日志)** | 最新提示词/响应 + 完整调用历史 |
| **Transition History (转换历史)** | 节点到节点的转换及 WNS 快照 |
| **Tool Call Trace (工具调用追踪)** | 所有工具调用的耗时和状态 |
| **Flow Control Log (流控日志)** | 颜色编码的信号轨迹 (DONE/SWITCH/ROLLBACK) |
| **Phase History (阶段历史)** | 带时间戳的阶段转换 |
| **WNS Trajectory (WNS 轨迹)** | 随迭代累计的改善情况 |

---

## 项目结构

```text
Deep_Thouught_42/
├── dcp_optimizer.py          # 主入口：V2 状态机 CLI + 模型配置
├── optimizer/                # V2 状态机框架
│   ├── state.py              # 类型化数据类：7 个状态子切片
│   ├── graph.py              # NodeGraph：执行引擎
│   ├── nodes/                # 9 个节点实现 + llm_tool_loop 子图
│   └── pure/                 # 14 个无状态纯函数模块（可单元测试），含 state_space.py（7 模块 StateSpace）
├── strategy_library.py       # 12 种策略及触发条件
├── skills/                   # 技能框架：14 个注册技能
├── RapidWrightMCP/           # RapidWright MCP 服务器
├── VivadoMCP/                # Vivado MCP 服务器
├── context_manager/          # 内存/压缩管理
├── dashboard/                # Web 仪表盘 (aiohttp + WebSocket)
├── architecture.md           # 架构技术细节（迁移映射、压缩管线、flow_control）
├── CONTRIBUTING.md           # 贡献工作流与同步清单
├── validate_dcps.py          # DCP 逻辑等价性验证器
├── model_config.yaml         # LLM 层级与回退配置
├── Makefile                  # 构建自动化
└── docs/                     # 竞赛提交文档
```

---

## 模型配置

两个模型层级，根据上下文窗口和压缩参数进行区分：

| 参数 | Worker | Planner |
|-----------|--------|---------|
| 模型 | `deepseek/deepseek-v4-flash` | `deepseek/deepseek-v4-flash` |
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

### `Vivado license not found` (未找到 Vivado 许可证)

```bash
# 验证 Vivado 是否可访问
which vivado
# 如有需要，加载 Vivado 环境变量
source /opt/Xilinx/Vivado/2024.1/settings64.sh
```

### `OPENROUTER_API_KEY not set` (未设置 OPENROUTER_API_KEY)

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
# 添加到 ~/.bashrc 以持久化
echo 'export OPENROUTER_API_KEY="sk-or-v1-..."' >> ~/.bashrc
```

### `RapidWright Java error` (RapidWright Java 错误)

```bash
# 确保已安装 Java 11+
java -version
# 如有需要，设置 JAVA_HOME
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
```

### Dashboard not loading (仪表盘无法加载)

```bash
# 检查端口可用性
lsof -i :8080
# 尝试备用端口
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090
```

### WNS not improving after many iterations (多次迭代后 WNS 未改善)

- 当 WNS 恶化 >30ps 时，智能体会自动回滚
- 检查仪表盘中的 Flow Control Log（流控日志）以获取 `EXHAUSTED` 信号
- 尝试在状态配置中增加 `no_improvement_limit`
- 验证 `validate_dcps.py` 是否能通过你的基线 DCP

---

## 许可证

Apache 2.0 — 请参阅 [LICENSE-APACHE-2.0.txt](LICENSE-APACHE-2.0.txt)。

Copyright (C) 2026, Advanced Micro Devices, Inc. 保留所有权利。

---

## 致谢

- **Vivado** 和 **RapidWright** (由 AMD/Xilinx 提供) —— EDA 核心基石
- **DeepSeek V4 Flash** (通过 OpenRouter) —— LLM 推理引擎
- **MCP (Model Context Protocol)** —— 工具调用基础设施
- **FPL 2026** —— 推动本项研究的竞赛
- **Douglas Adams** —— 项目名称的灵感来源