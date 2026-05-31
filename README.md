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
- **Dual architecture.** V2 state machine for production reliability; V1 conversational loop for rapid experimentation.
- **Real-time observability.** Web Dashboard with 20 panels — 7-module StateSpace (agent data input layer) + 13 legacy detail panels. Every flow control decision, WNS trajectory, and LLM call is traceable.
- **12 battle-tested strategies.** PBLOCK, PhysOpt, Fanout, PinSwap, LUTCascade, CellReplication, CongestionSpreading, RegisterRetiming, SmartRetiming, NetSwap, PhysOpt+RegisterRetiming, OptDesign.

---

## Quick Start

```bash
# 1. Clone and set up environment
git clone https://github.com/chenqianyu486-sys/Deep_Thouught_42.git
cd Deep_Thouught_42
make setup

# 2. Set your OpenRouter API key
export OPENROUTER_API_KEY="sk-or-..."

# 3. Run optimization (V2 state machine — recommended)
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
                    │   (main entry + v1/v2 hub)   │
                    └──────────┬──────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                                 ▼
   ┌──────────────────┐              ┌──────────────────┐
   │   V1: Message     │              │   V2: State       │
   │   Conversation    │              │   Machine (9 nodes)│
   │   (legacy)        │              │   ← recommended    │
   └──────────────────┘              └────────┬─────────┘
                                              │
                         ┌────────────────────┼────────────────────┐
                         ▼                    ▼                    ▼
                  ┌────────────┐      ┌────────────┐      ┌────────────┐
                  │ Vivado MCP │      │RapidWright │      │   LLM      │
                  │  Server    │      │ MCP Server │      │(DeepSeek)  │
                  └────────────┘      └────────────┘      └────────────┘
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
  │       ▲                                  │
  │       └────── CONTINUE ──────────────────┘
  │                                          │
  │       DONE / NEXT / SWITCH / ROLLBACK ──► iteration_start
```

### Key Design Principles

| # | Principle | Implementation |
|---|-----------|----------------|
| 1 | Fail-safe, not blocking | Auto-synthesize `CONTINUE` when `report_step_state` is missing |
| 2 | Facts, not judgments | Dashboard contains raw measurements only, injected as last user message |
| 3 | Eliminate redundancy | Dashboard is the single real-time data source; handoff passes only iteration memory |
| 4 | Explicit over implicit | 9-node state machine + typed dataclass state slices |
| 5 | Separation of concerns | Worker (250K tokens, execution) vs. Planner (1M tokens, strategic decisions) |
| 6 | Single invocation path | V2 uses native function calls only; no XML/YAML text fallback |
| 7 | Single source of truth | Runtime data in `OptimizerState`; no shadow copies in `MemoryManager` |
| 8 | Domain knowledge encoded | 12 strategies with trigger conditions; LLM selects autonomously |
| 9 | Data trustworthiness | `DASHBOARD_REFRESH_MAP` tracks field freshness; stale data auto-annotated |
| 10 | Information preservation | Compression markers retain key metrics (WNS/TNS/FE/delta/status) |
| 11 | Logic equivalence hard constraint | All optimizations verified by `validate_dcps.py` (structural + functional) |
| 12 | DCP identity integrity | `vivado_open_checkpoint` removed from LLM tool whitelist in EXECUTE phase |
| 13 | **Critical path-aware PBLOCK** | PBLOCK region selection centers on critical-path cells (top 10 paths) via automatic `critical_path_cells` injection in EXECUTE phase. Distance weight `0.3` balances proximity vs. region tightness — configurable via `distance_weight_factor`. Principle #7: `state.timing.critical_paths` as single source of truth for cell positions. |
| 14 | **Tool result caching** | Phase-local tool cache avoids redundant MCP calls when LLM requests the same tool+args repeatedly. Cache auto-invalidated after any side-effect execution tool (place_design, route_design, etc.) to prevent stale physical data. |
| 15 | **Read-only tool whitelist control** | Tools redundant with Dashboard data (`get_wns`, `get_resource_counts`) removed from ANALYZE/EVALUATE whitelists. Rate limiting (max 3 calls/phase for `search_cells`, max 5 for `vivado_run_tcl`) prevents LLM from exhausting rounds on repeated queries. |
| 16 | **Adaptive PBLOCK multiplier** | `M = max(1.10, 1.2 + util_local x 0.3 - 0.1 x log10(N_LUT))` — automatically tightens PBLOCK region at low utilization, expands at high utilization. `util_local` preferred, falls back to global chip utilization. |
| 17 | **LLM prompt caching** | Every API call sends `{"cache": {"prompt": true}}` via `extra_body`. OpenRouter caches the system prompt prefix across repeated calls in the same session, saving ~4KB per call × 44 calls ≈ 176KB per iteration. Shared `build_llm_extra_body()` in `optimizer/pure/constants.py`. |
| 18 | **Data trustworthiness annotations** | Dashboard distinguishes `None` (not analyzed) from `[]`/`0` (analyzed but zero). Every N/A and empty list carries a machine-readable reason: `"N/A(congestion_analysis_not_supported)"`, `[]  # no_high_fanout_nets_found`. |
| 19 | **Vivado timeout auto-restart** | Tcl timeout poisons the Vivado session. The MCP server kills, restarts, and reopens the DCP automatically, then returns a structured `[ERROR]` string (not an exception). Reentry guard `_restarting` prevents recursive restart. Agent framework detects `[ERROR]` patterns and invalidates tool cache. |
| 20 | **Unplaced DCP save guard** | Before writing the output DCP, `save_output` queries `get_property STATUS [current_design]`. If the design is not routed, it auto-repairs via `place_design` + `route_design` before saving. Prevents saving unplaced DCPs that would fail `validate_dcps.py`. |
| 21 | **False-positive WNS detection** | `_post_eval_hook` and `_track_wns_from_result` check `Design State` in timing reports. If not `Routed`, a warning is logged and appended to the eval notice (`[WARNING: design not routed]`), alerting the LLM that WNS may be inaccurate. |
| 22 | **Unplace auto-rollback** | EXECUTE phase tracks `place_design -unplace` calls. If the phase exits without a subsequent `place_design` (non-unplace), the design is automatically restored from a pre-unplace checkpoint and WNS refreshed. |
| 23 | **phys_opt_design safety guard (string bypass fix)** | `_is_truthy()` normalizes boolean-like values (`"true"`, `"1"`, `"yes"`) before checking blocked retiming options (`retime`, `interconnect_retime`). Prevents LLM from bypassing the safety guard via string-typed arguments. |
| 24 | **MCP error response detection** | `tool_router.py` detects `[ERROR]` patterns in MCP tool responses (timeout, restart, multi-line abort). Error responses invalidate the entire tool cache (like side-effect tools) and are never cached, ensuring the agent framework correctly records failures and Dashboard `action_status` reflects errors. |
| 25 | **Per-tool timeout with design-size scaling** | `_TOOL_TIMEOUT_DEFAULTS` maps 30+ tools to base timeouts (30s–3600s). `call_tool()` accepts `design_size_factor` parameter; final timeout = `min(base × factor, 900s)`. User-specified `timeout` argument takes priority. |
| 26 | **Multi-line Tcl transaction safety** | `run_tcl_command` pre-checks multi-line scripts with `info complete` (skipping lines with unbalanced braces). On execution failure, returns structured `[ERROR]` string instead of raising — the agent framework can continue using the recovered session. |
| 27 | **Design state flag synchronization** | `_sync_design_open_flag()` queries `get_property STATUS [current_design]` to synchronize the `_design_open` flag with actual Vivado state, checking `[ERROR]`, `ERROR:`, and `no current design` patterns. Called after `close_design` and DCP reopen. |
| 28 | **PBLOCK cell filter (CLOCK/IO exclusion)** | `create_and_apply_pblock` with `apply_to="current_design"` now uses `-filter {IS_PRIMITIVE == TRUE && PRIMITIVE_GROUP != CLOCK && PRIMITIVE_GROUP != IO}` to exclude clock and IO primitives. Controlled by `exclude_clocks: bool = True` parameter. |
| 29 | **Explicit pipeline data flow** | `init_analysis` Phase B pipelines return `dict` instead of using `nonlocal` variables. `asyncio.gather()` returns `(vivado_result, rw_result)`; Phase C reads `cell_names_for_spread` from `vivado_result.get()`, eliminating implicit data flow. |
| 30 | **EXECUTE no-progress acceleration** | `NO_PROGRESS_LIMIT` reduced from 12 → 6; pending-tool-call guard prevents counting rounds where slow execution tools (place_design, route_design) are still running. Coordinated with `_TOOL_TIMEOUT_DEFAULTS` so the 6-round window (~60s LLM time) is balanced against tool execution timeouts. |
| 31 | **vivado_run_tcl rate-limit enforcement** | Per-phase call limit for `vivado_run_tcl` reduced from 5 → 2 in EXECUTE phase. RATE LIMITED message now directs LLM to Dashboard data and dedicated tools (`vivado_report_timing_summary`) instead of raw Tcl. Prevents LLM "analysis paralysis" in EXECUTE phase. |
| 32 | **FF utilization-aware strategy guidance** | When FF utilization < 2%, `RegisterRetiming` and `SmartRetiming` get `⚠️ FF utilization` warnings in strategy catalog and Dashboard `ff_warning` hints. LLM retains final decision authority, but warnings ensure it accounts for physical feasibility (e.g., 0.21% FF → retiming impact minimal). |
| 33 | **Pre-placement logic optimization (opt_design)** | New 11th strategy: Vivado `opt_design` with `SKILL_CHAIN_ACTIONS` auto-chaining (`opt_design → place_design → route_design → report_timing_summary`). Targets pure logic-depth bottlenecks (6-7 LUT levels, 100% logic delay) where PhysOpt is ineffective. `--skip-structural` flag in `validate_dcps.py` allows Phase 1 bypass when netlist is intentionally remapped. |
| 34 | **Initial checkpoint save for rollback** | `init_analysis` now unconditionally saves `best_checkpoint.dcp` after analysis completes. Previously `best_checkpoint_path` was `None` until WNS improved, making rollback impossible when the first iteration degrades timing. The `[ROLLBACK] No checkpoint at None` error is eliminated. (`optimizer/nodes/init_analysis.py`) |
| 35 | **Heavy chain gate excludes pblock_strategy** | `rapidwright_execute_pblock_strategy` is an analysis-only skill — it computes pblock ranges but does not modify the netlist. The actual netlist mutation happens via `SKILL_CHAIN_ACTIONS` (unplace → create_pblock → place → route). Removed from `HEAVY_CHAIN_SKILLS` so the chain always runs, preventing the chain-gate from incorrectly skipping execution. (`optimizer/pure/constants.py`) |
| 36 | **EXECUTE strategy enforcement** | After `inject_merged_dashboard`, `_call_phase_llm` appends an `[EXECUTE CONSTRAINT]` user message mapping the selected strategy to its execution tool and forbidding analysis tools or strategy switches mid-execution. Prevents LLM from re-analyzing or drifting to a different strategy. (`optimizer/nodes/subgraphs/phase_execute.py`) |
| 37 | **Stale dashboard suppression in ANALYZE** | `_build_dynamic_gradient` now only populates `last_action_taken` and `action_status` during `EXECUTE_STRATEGY` or `EVALUATE` phases. In `ANALYZE`/`SELECT_STRATEGY` phases, these fields are cleared to prevent stale data from the previous iteration misleading the LLM. (`optimizer/pure/state_space.py`) |

---

## Optimization Strategies

| Strategy | Trigger Condition | Platform |
|----------|-------------------|----------|
| **PBLOCK** | Distributed paths (avg_distance > 70) — region centers on critical-path cells | Vivado + RapidWright |
| **PhysOpt** | 1–2 critical paths with spread, WNS > -2.0 | Vivado |
| **OptDesign** | Logic-depth limited (>70% logic delay), PhysOpt ineffective, 6-7 LUT levels | Vivado (via RapidWright skill + auto-chain) |
| **Fanout** | Fanout > 100, no spread | RapidWright + Vivado |
| **PinSwap** | WNS stuck at ~-0.3ns, LUT pin delay variance | RapidWright + Vivado |
| **LUTCascade** | >3 LUTs in series | RapidWright + Vivado |
| **CellReplication** | Fanout > 10 or delay > 0.3ns | RapidWright + Vivado |
| **CongestionSpreading** | Congestion=HIGH, PBLOCK/PhysOpt ineffective | RapidWright + Vivado |
| **RegisterRetiming** | Deep combinational chains (>2 LUTs) | RapidWright + Vivado |
| **SmartRetiming** | WNS stuck, deep combinational chains (>2 LUTs) between pipeline registers, FF > 0 | RapidWright + Vivado |
| **NetSwap** | Intra-SLICE routing congestion | RapidWright + Vivado |
| **PhysOpt+RegisterRetiming** | Logic-depth limited (logic_delay > 70%), WNS > -2.0, deep chains (>2 LUTs), FF > 0 | Vivado + RapidWright (atomic) |

---

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
# V2 state machine (default, recommended)
python dcp_optimizer.py input.dcp --v2

# V1 conversational loop (legacy)
python dcp_optimizer.py input.dcp

# With 30-minute timeout and custom output
python dcp_optimizer.py input.dcp --v2 --timeout 1800 --output output.dcp
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

The dashboard provides 20 real-time panels (7-module StateSpace + 13 legacy detail panels):

**7-Module StateSpace (Agent Data Input Layer):**

| Module | Panel | Content |
|--------|-------|---------|
| **M1** | **Global State & Targets** | Stage badge, WNS/TNS/WHS/THS margins, best WNS, LUT/FF/DSP/BRAM utilization bars, cell/net count |
| **M2** | **Timing Path Clusters** | Top-20 violating endpoints with clock groups, logic/route delay ratio, logic levels |
| **M3** | **Physical & Congestion** | Global congestion score, hotspots (bbox + severity + module), pblock overflows |
| **M4** | **Netlist Quality** | High fanout nets (with replication status), control sets, cross-domain paths, failed inferences |
| **M5** | **Constraints Environment** | Clock table (name→frequency), false/multicycle path counts, IO delay coverage, PVT corner |
| **M6** | **Dynamic Gradient (Delta)** | delta_WNS, delta_TNS, delta_congestion, last action + action status (Success/Failed/Timeout) |
| **M7** | **Architecture Overview** | Module-level timing hotspots (critical_path_hits, path_coverage), cross/intra-module path counts, deepest logic module. Zero-cost: parsed from critical path cell names.

**Legacy Detail Panels:**

| Panel | Content |
|-------|---------|
| **Timing** | WNS / TNS / Failing Endpoints with sparkline chart |
| **Iteration** | Counter, no-improvement tracking, strategy sequence |
| **Strategy Lifecycle** | 4-phase indicator + current strategy/evaluation |
| **Model** | Current model, fallback state, call count |
| **Cost** | Total cost with progress bar, token breakdown |
| **Control** | Runtime status, elapsed time, DCP paths |
| **Critical Paths** | Cell lists with per-path timing detail |
| **LLM Log** | Latest prompt/response + full call history |
| **Transition History** | Node-to-node transitions with WNS snapshots |
| **Tool Call Trace** | All tool invocations with timing and status |
| **Flow Control Log** | Color-coded signal trail (DONE/SWITCH/ROLLBACK) |
| **Phase History** | Phase transitions with timestamps |
| **WNS Trajectory** | Cumulative improvement over iterations |

---

## Project Structure

```
Deep_Thouught_42/
├── dcp_optimizer.py          # Main entry: LLM orchestration, model selection
├── optimizer/                # V2 state machine framework
│   ├── state.py              # Typed dataclass: 7 state sub-slices
│   ├── graph.py              # NodeGraph: execution engine
│   ├── nodes/                # 9 node implementations + llm_tool_loop subgraph
│   └── pure/                 # 14 stateless pure-function modules (unit-testable), incl. state_space.py (7-module StateSpace)
├── strategy_library.py       # 12 strategies with trigger conditions
├── skills/                   # Skill framework: 14 registered skills
├── RapidWrightMCP/           # RapidWright MCP server
├── VivadoMCP/                # Vivado MCP server
├── context_manager/          # Memory/compression management
├── dashboard/                # Web Dashboard (aiohttp + WebSocket)
├── architecture.md           # Implementation details (migration, compression, flow control)
├── CONTRIBUTING.md           # Contribution workflow & sync checklist
├── validate_dcps.py          # DCP logic equivalence validator
├── model_config.yaml         # LLM tier & fallback configuration
├── Makefile                  # Build automation
└── docs/                     # Competition submission docs
```

---

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

*Results vary by design complexity and initial timing violation severity.*

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution workflow, test modes, and the checklist for adding new strategies/tools.

---

## Troubleshooting

### `Vivado license not found`

```bash
# Verify Vivado is accessible
which vivado
# Source Vivado settings if needed
source /opt/Xilinx/Vivado/2024.1/settings64.sh
```

### `OPENROUTER_API_KEY not set`

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
# Add to ~/.bashrc for persistence
echo 'export OPENROUTER_API_KEY="sk-or-v1-..."' >> ~/.bashrc
```

### `RapidWright Java error`

```bash
# Ensure Java 11+ is installed
java -version
# Set JAVA_HOME if needed
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
```

### Dashboard not loading

```bash
# Check port availability
lsof -i :8080
# Try alternate port
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090
```

### WNS not improving after many iterations

- The agent automatically rolls back when WNS degrades >30ps
- Check the Flow Control Log in dashboard for `EXHAUSTED` signals
- Try increasing `no_improvement_limit` in the state configuration
- Verify `validate_dcps.py` passes on your baseline DCP

---

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
- **双重架构。** V2 状态机用于保障生产环境的可靠性；V1 对话循环用于快速实验。
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

# 3. 运行优化（V2 状态机 —— 推荐）
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
                    │   (主入口 + v1/v2 中枢)      │
                    └──────────┬──────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                                 ▼
   ┌──────────────────┐              ┌──────────────────┐
   │   V1: 消息        │              │   V2: 状态机      │
   │   对话循环        │              │   (9 个节点)      │
   │   (旧版)          │              │   ← 推荐          │
   └──────────────────┘              └────────┬─────────┘
                                              │
                         ┌────────────────────┼────────────────────┐
                         ▼                    ▼                    ▼
                  ┌────────────┐      ┌────────────┐      ┌────────────┐
                  │ Vivado MCP │      │RapidWright │      │   LLM      │
                  │  Server    │      │ MCP Server │      │(DeepSeek)  │
                  └────────────┘      └────────────┘      └────────────┘
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
| 19 | **未布局 DCP 保存防护** | 在写入输出 DCP 前，`save_output` 查询 `get_property STATUS [current_design]`。若设计未布线，自动执行 `place_design` + `route_design` 修复后再保存。防止保存未布局 DCP 导致 `validate_dcps.py` 验证失败。 |
| 20 | **虚假正 WNS 检测** | `_post_eval_hook` 和 `_track_wns_from_result` 检查时序报告中的 `Design State`。若非 `Routed`，记录警告并追加到评估通知（`[WARNING: design not routed]`），提醒 LLM WNS 可能不准确。 |
| 21 | **Unplace 自动回滚** | EXECUTE 阶段追踪 `place_design -unplace` 调用。若阶段退出时未执行后续 `place_design`（非 unplace），自动从 pre-unplace checkpoint 恢复设计并刷新 WNS。 |

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
# V2 状态机（默认，推荐）
python dcp_optimizer.py input.dcp --v2

# V1 对话循环（旧版）
python dcp_optimizer.py input.dcp

# 设置 30 分钟超时并自定义输出
python dcp_optimizer.py input.dcp --v2 --timeout 1800 --output output.dcp
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
├── dcp_optimizer.py          # 主入口：LLM 编排、模型选择
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