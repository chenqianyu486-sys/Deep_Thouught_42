# Deep Thought 42

[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE-APACHE-2.0.txt)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![FPGA](https://img.shields.io/badge/FPGA-Vivado%20%2B%20RapidWright-green)](#)
[![LLM](https://img.shields.io/badge/LLM-DeepSeek%20V4%20Flash-purple)](#)
[![Contest](https://img.shields.io/badge/contest-FPL%202026-orange)](#)

**Autonomous LLM-driven FPGA timing closure agent.** Orchestrates Vivado and RapidWright to iteratively optimize P&R strategies until WNS >= 0 — with formal logic equivalence guarantees.

> 🇨🇳 中文版请参见 [README.md](README.md)

---

## Why This Project?

- **No manual timing closure loops.** The agent autonomously analyzes critical paths, selects optimization strategies, executes them, and evaluates results.
- **Logic equivalence guaranteed.** Every optimization is verified by `validate_dcps.py` (structural diff + functional simulation), ensuring the design behavior never changes.
- **Dual architecture.** V2 state machine for production reliability; V1 conversational loop removed (deprecated).
- **Real-time observability.** Web Dashboard with 20 panels — 7-module StateSpace (agent data input layer) + 13 legacy detail panels. Every flow control decision, WNS trajectory, and LLM call is traceable.
- **14 validation-safe strategies.** PBLOCK, PhysOpt, Fanout, PinSwap, LUTCascade, CellReplication, CongestionSpreading, NetSwap, OptDesign, LogicResynthesis, PhysOptAggressive, plus 3 new inter-layer combinational-logic strategies: CombinationalRebalance (validation-safe retiming — rebalances LUT6/MUXF7/MUXF8 cascade depth via logic-equivalent resynthesis, no FF insert), LUTMUXFRepack (LUT6+MUXF co-repack for NN/wide-datapath cones that exceed 6-input LUT limit), MUXFTreeReorder (MUXF7/MUXF8 tree reorder — the carry-reorder analogue for designs without CARRY4). Strategies that insert new pipeline FFs (RegisterRetiming, SmartRetiming, PhysOpt+RegisterRetiming) are excluded from the catalog because they would fail cycle-exact functional validation.
- **Multi-strategy loop.** Up to 5 strategies can be tried per iteration, with TTL-based strategy retry (3 iterations). Failed strategies auto-unblock after TTL expires.

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

| # | Principle | Implementation |
|---|-----------|----------------|
| 1 | Fail-safe, not stuck | When `report_step_state` is absent, auto-synthesize `CONTINUE` signal |
| 2 | Facts, not judgment | Dashboard contains only raw measurements, injected as the last user message |
| 3 | Eliminate redundancy | Dashboard is the single live data source; only iteration memory is passed during handoff |
| 4 | Explicit > implicit | 9-node state machine + typed dataclass state slices |
| 5 | Separation of concerns | Worker (250K tokens, execution) vs. Planner (1M tokens, strategic decisions) |
| 6 | Single calling path | V2 uses native function calls only; no XML/YAML text fallback |
| 7 | Single source of truth | Runtime data stored in `OptimizerState`; no shadow copies in `MemoryManager` |
| 8 | Encode domain knowledge | 14 strategies with trigger conditions; LLM selects autonomously |
| 9 | Data trustworthiness | `DASHBOARD_REFRESH_MAP` tracks field freshness; auto-annotates stale data; EXECUTE phase auto-overrides LLM-provided `critical_paths`/`critical_path_cells` with verified state data to block incorrect TCL-extraction pollution |
| 10 | Information retention | Compression markers retain key metrics (WNS/TNS/FE/delta/status) |
| 11 | Logic equivalence hard constraint | All optimizations verified by `validate_dcps.py` (structural + functional) |
| 12 | DCP identity integrity | `vivado_open_checkpoint` removed from LLM tool whitelist during EXECUTE |
| 13 | **Tool result caching** | Same tool + args within same phase auto-hit cache, avoiding duplicate LLM calls; cache invalidated after execution tools (place_design, route_design, etc.) to prevent stale physical data |
| 14 | **Read-only tool whitelist control** | Tools redundant with Dashboard data (`get_wns`, `get_resource_counts`) removed from ANALYZE/EVALUATE whitelists; rate limiting (`search_cells` max 3/phase, `vivado_run_tcl` max 5/phase) prevents LLM from wasting rounds |
| 15 | **Adaptive PBLOCK tightening** | Formula `M = max(1.10, 1.2 + util_local x 0.3 - 0.1 x log10(N_LUT))`; low utilization auto-tightens region, high utilization auto-relaxes |
| 16 | **LLM prompt caching** | Each API call sends `{"cache": {"prompt": true}}` via `extra_body`. OpenRouter caches system prompt prefix across repeated calls within the same session, saving ~4KB × 44 ≈ 176KB tokens per iteration. Shared function `build_llm_extra_body()` in `optimizer/pure/constants.py`. |
| 17 | **Dashboard data trustworthiness annotation** | Dashboard strictly distinguishes `None` (not analyzed) from `[]`/`0` (analyzed but zero). Every N/A and empty list carries a machine-readable reason: `"N/A(congestion_analysis_not_supported)"`, `[]  # no_high_fanout_nets_found`. |
| 18 | **Vivado timeout auto-restart** | Tcl timeout corrupts the Vivado session. Instead of unreliable `sync_after_timeout()`, the MCP server auto-kills → restarts → reopens DCP. Removes `_command_pending` global state. |
| 19 | **Unrouted DCP save guard** | Before writing output DCP, `save_output` queries `get_property STATUS [current_design]`, falling back to the timing report `Design State` field (recognizes `routed`/`placed`/`optimized`). If unrouted, restores from best_checkpoint or auto-executes `place_design` + `route_design`. Re-verifies design state after writing; logs warning if not `routed`. Prevents `validate_dcps.py` failure from saving an unrouted DCP. |
| 20 | **False positive WNS detection** | `_post_eval_hook` and `_track_wns_from_result` check `Design State` in timing reports. If not `Routed`, logs warning and appends to evaluation notification (`[WARNING: design not routed]`). Place-only WNS check also validates design state — skips WNS check if `Optimized` (unplaced) to avoid false positives based on estimated delay. |
| 21 | **Unplace auto-rollback** | EXECUTE phase tracks `place_design -unplace` calls. If the phase exits without a subsequent `place_design` (non-unplace), auto-restores from pre-unplace checkpoint and refreshes WNS. |
| 22 | **Multi-strategy loop** | Up to 5 strategies per iteration (`MAX_STRATEGY_CYCLES=5`). EVALUATE's `SWITCH_STRATEGY` signal triggers loop back to SELECT_STRATEGY (skipping ANALYZE). Prevents wasting an iteration on a single failing strategy. |
| 23 | **TTL strategy retry** | `FailedStrategyRecord.blocked_until_iter` adds TTL to strategy blocking. `strategy_ineffective` strategies auto-unblock after `STRATEGY_RETRY_TTL=3` iterations. Prevents strategy catalog exhaustion from permanent blocking. |
| 24 | **EXECUTE constraint relaxation** | After executing strategy tools, LLM can call `rapidwright_report_timing` for fast feedback (~2.5s vs ~14s full Vivado timing), then signal EXEC_DONE. Provides quick directional checks. |
| 25 | **Context engineering: weak guidance** | System prompts and FORMAT_GUARD describe the problem and constraints, not prescriptive solutions. Tool filtering + auto-chain handle execution mechanics. LLM retains autonomous strategy selection and diagnostic decisions. |
| 26 | **Design consistency verification tools** | 4 verification tools (`vivado_check_design_status`, `vivado_validate_timing`, `rapidwright_estimate_timing`, `rapidwright_compare_designs`) available in all phases. LLM can autonomously verify design state after modifications. |
| 27 | **Independent RapidWright tools** | 19 RapidWright tools (8 analysis + 10 execution + 1 verification) exposed to LLM for fine-grained control. LLM autonomously selects tool combinations rather than being restricted to hardcoded chains. |
| 28 | **Optional chain validation** | `OPTIONAL_CHAIN_VALIDATION` provides 4 optional verification chains; LLM chooses whether to insert verification steps before/after execution. Verification tools ensure design consistency. |
| 29 | **Vivado execution tool error detection** | `place_design`, `route_design`, `phys_opt_design`, `opt_design`, `physopt_and_route` detect Vivado `ERROR: [` text in MCP server, return JSON `{"error": ...}` response. Chain execution (`phase_execute.py`) checks both JSON `error` key and text `ERROR: [` pattern, ensuring chain abort and rollback on Vivado command failure. |
| 30 | **Strategy blocking status visibility** | Dashboard `strategy_lifecycle` always displays `blocked_this_iteration` (cooldown strategies) and `blocked_ttl` (TTL-persistent blocked strategies with unblock countdown). Prevents LLM from re-selecting blocked strategies after context compression drops `[BLOCKED]` messages. |

---

## Optimization Strategies

| Strategy | Trigger Condition | Platform |
|----------|-------------------|----------|
| **PBLOCK** | Scattered paths (avg distance > 70) | Vivado + RapidWright |
| **PhysOpt** | 1–2 scattered critical paths, WNS > -2.0 | Vivado |
| **OptDesign** | Logic depth limited (logic_delay > 70%), 6-7 LUT levels | Vivado (via RapidWright skill + auto-chain) |
| **Fanout** | Fanout > 100, not scattered | RapidWright + Vivado |
| **PinSwap** | WNS stalled at ~-0.3ns, high LUT pin delay variance | RapidWright + Vivado |
| **LUTCascade** | >3 LUTs in series | RapidWright + Vivado |
| **CellReplication** | Fanout > 10 or delay > 0.3ns | RapidWright + Vivado |
| **CongestionSpreading** | Congestion = HIGH | RapidWright + Vivado |
| **NetSwap** | Intra-SLICE routing congestion | RapidWright + Vivado |
| **LogicResynthesis** | NN/datapath design with MUXF7/8 cascades, deep combinational levels on critical paths | Vivado (synth_design -remap) |
| **PhysOptAggressive** | WNS > -3.0, logic-depth limited design with spread | Vivado (Explore directives) |
| **CombinationalRebalance** | Deep combinational chains between registers (LUT6/MUXF7/MUXF8 cascades, logic levels >= 3) | Vivado (opt_design -remap via RapidWright targeted analysis + auto-chain) |
| **LUTMUXFRepack** | NN/wide datapath, MUXF7/MUXF8 + LUT6 cascades on critical paths | Vivado (opt_design -AddRemap via RapidWright targeted analysis + auto-chain) |
| **MUXFTreeReorder** | NN design without CARRY4, MUXF7/MUXF8 mux trees >= 2 levels on critical paths, route-dominated delay profile | Vivado (phys_opt_design without -retime via RapidWright targeted analysis + auto-chain) |

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

The dashboard provides 20 real-time panels:

**7-Module StateSpace (Agent Data Input Layer):**

| Module | Panel | Content |
|--------|-------|---------|
| **M1** | **Global State & Targets** | Phase label, WNS/TNS/WHS/THS slack, LUT/FF/DSP/BRAM utilization progress bars |
| **M2** | **Timing Path Clusters** | Top-20 violating endpoints with clock group, logic/wire delay ratio, logic levels |
| **M3** | **Physical & Congestion** | Global congestion score, hot zones (bbox + severity + module), Pblock overflow count |
| **M4** | **Netlist Quality** | High-fanout nets (with replication status), control sets, clock-domain-crossing paths, inference failure list |
| **M5** | **Constraints Environment** | Clock table (name → frequency), false/multicycle path count, IO delay coverage, PVT corner |
| **M6** | **Dynamic Gradient (Delta)** | delta_WNS, delta_TNS, delta_congestion, previous action + action status |
| **M7** | **Architecture Overview** | Module-level timing heatmap (critical_path_hits, path_coverage), cross-/intra-module critical path counts, deepest-logic module. Zero cost: resolved from critical path cell names. |

**Legacy Detail Panels:**

| Panel | Content |
|-------|---------|
| **Timing** | WNS / TNS / failing endpoints with mini sparkline |
| **Iteration** | Counter, no-improvement tracking, strategy sequence |
| **Strategy Lifecycle** | 4-stage indicator + current strategy / evaluation |
| **Model** | Current model, fallback status, call count |
| **Cost** | Total cost with progress bar, token breakdown |
| **Control** | Runtime status, elapsed time, DCP path |
| **Critical Paths** | Cell list with timing details per path |
| **LLM Log** | Latest prompt/response + full call history |
| **Transition History** | Node-to-node transitions with WNS snapshots |
| **Tool Call Trace** | All tool calls with duration and status |
| **Flow Control Log** | Color-coded signal trail (DONE/SWITCH/ROLLBACK) |
| **Phase History** | Timestamped phase transitions |
| **WNS Trajectory** | Cumulative improvement across iterations |

---

## Project Structure

```
Deep_Thouught_42/
├── dcp_optimizer.py          # Main entry: V2 state machine CLI + model config
├── optimizer/                # V2 state machine framework
│   ├── state.py              # Typed dataclasses: 7 state subslices
│   ├── graph.py              # NodeGraph: execution engine
│   ├── nodes/                # 9 node implementations + llm_tool_loop subgraph
│   └── pure/                 # 14 stateless pure function modules (unit-testable), incl. state_space.py (7-module StateSpace)
├── strategy_library.py       # 14 strategies with trigger conditions
├── skills/                   # Skill framework: 14 registered skills
├── RapidWrightMCP/           # RapidWright MCP server
├── VivadoMCP/                # Vivado MCP server
├── context_manager/          # Memory/compression management
├── dashboard/                # Web dashboard (aiohttp + WebSocket)
├── architecture.md           # Architecture technical details (migration maps, compression pipeline, flow_control)
├── CONTRIBUTING.md           # Contribution workflow and sync checklist
├── validate_dcps.py          # DCP logic equivalence verifier
├── model_config.yaml         # LLM tier and fallback configuration
├── Makefile                  # Build automation
└── docs/                     # Competition submission documents
```

For more details, see [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md).

---

## Model Configuration

Two model tiers, differentiated by context window and compression parameters:

| Parameter | Worker | Planner |
|-----------|--------|---------|
| Model | `deepseek/deepseek-v4-pro` | `deepseek/deepseek-v4-pro` |
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

### `Vivado license not found`

```bash
# Verify Vivado is accessible
which vivado
# Load Vivado environment if needed
source /opt/Xilinx/Vivado/2024.1/settings64.sh
```

### `OPENROUTER_API_KEY not set`

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
# Add to ~/.bashrc to persist
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
# Try alternative port
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090
```

### WNS not improving after many iterations

- Agent auto-rolls back when WNS degrades by >30ps
- Check the Flow Control Log on the dashboard for `EXHAUSTED` signals
- Increase `no_improvement_limit` in state config
- Verify that `validate_dcps.py` passes on your baseline DCP

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