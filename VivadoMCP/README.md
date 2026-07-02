# Vivado MCP Server

An MCP (Model Context Protocol) server that provides AI assistant access to [Vivado 2025.1](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado.html) via a managed Tcl subprocess.

This server enables AI assistants to run Vivado commands, analyze timing, optimize placement/routing, and manage design checkpoints (.dcp) — all through natural language.

## Features

- **Design Checkpoint Management**: Open, write, and inspect .dcp files
- **Timing Analysis**: Full `report_timing_summary` with WNS/TNS/WHS/THS extraction
- **Place & Route**: Run `place_design`, `route_design`, `phys_opt_design` with safe directive whitelists
- **Logic Optimization**: `opt_design` with multiple safe directives
- **Critical Path Analysis**: Extract cell-level and pin-level critical path data
- **Pblock Management**: Create and apply area constraints (pblocks)
- **Security Layer**: Tcl injection prevention, retiming guard, directive whitelist

## Prerequisites

- **Vivado 2025.1** installed and licensed
- Python 3.8+
- The `mcp` Python package (`pip install mcp`)
- `pexpect` Python package (`pip install pexpect`)

## Usage

The VivadoMCP server is launched by the optimizer framework (`dcp_optimizer.py`) as a stdio subprocess:

```bash
python VivadoMCP/vivado_mcp_server.py \
    --vivado-path /path/to/vivado \
    --vivado-log /path/to/vivado.log \
    --vivado-journal /path/to/vivado.journal
```

### CLI Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--vivado-path` | `vivado` (auto-detect) | Path to Vivado executable |
| `--vivado-log` | `vivado_mcp_{timestamp}.log` | Vivado Tcl stdout log |
| `--vivado-journal` | auto-generated | Vivado journal file |

## Available Tools

### Checkpoint Operations
| Tool | Description |
|------|-------------|
| `open_checkpoint` | Load a .dcp design checkpoint into Vivado |
| `write_checkpoint` | Save current design to a .dcp file |
| `set_incremental_checkpoint` | Set incremental compile checkpoint |

### Timing & Analysis
| Tool | Description |
|------|-------------|
| `report_timing_summary` | Full timing summary (WNS/TNS/WHS/THS) |
| `get_wns` | Quick WNS query (framework-internal; LLM prefers `report_timing_summary`) |
| `report_qor_suggestions` | Get QoR improvement suggestions |
| `report_high_fanout_nets` | List nets with fanout > threshold |
| `report_route_status` | Route status report |
| `get_critical_high_fanout_nets` | Combine timing + fanout analysis |
| `extract_critical_path_cells` | Cell-level critical path data (default: 50 paths) |
| `extract_critical_path_pins` | Pin-level critical path data (default: 10 paths) |
| `check_design_status` | Quick design state check |

### Implementation
| Tool | Description |
|------|-------------|
| `place_design` | Run placement (safe directives whitelisted in schema) |
| `route_design` | Run routing (safe directives whitelisted; supports `-reuse`) |
| `phys_opt_design` | Physical optimization (retiming blocked) |
| `physopt_and_route` | Combined phys_opt + route (single atomic call) |
| `opt_design` | Logic-level optimization (AddRetime blocked) |

### Pblock
| Tool | Description |
|------|-------------|
| `report_utilization_for_pblock` | Resource counts for pblock sizing |
| `create_and_apply_pblock` | Create area constraint from `ranges` string |

### Utility
| Tool | Description |
|------|-------------|
| `run_tcl` | Execute arbitrary Tcl command (security-filtered) |
| `write_edif` | Export design to EDIF netlist format |
| `write_verilog_simulation` | Export Verilog simulation netlist |
| `restart_vivado` | Kill and restart Vivado subprocess (session recovery) |
| `validate_timing` | Validate timing with multiple metrics |

## Security Layer

### Blocked Tcl Commands
The server blocks dangerous Tcl commands at the pexpect layer:
- `exit`, `stop`, `close_project`, `remove_design`, `reset_run`
- `remove_cell`, `rename_net`, `rename_cell` (ECO operations that break netlist equivalence)
- `set_property` on blacklisted properties

### Directive Whitelists
Each implementation tool has a `SAFE_DIRECTIVES` frozenset that validates directives at runtime:
- `PLACE_SAFE_DIRECTIVES` — 30+ safe placement directives
- `ROUTE_SAFE_DIRECTIVES` — 23 safe routing directives
- `PHYSOPT_SAFE_DIRECTIVES` — 9 safe physical optimization directives
- `OPT_SAFE_DIRECTIVES` — 9 safe logic optimization directives

The JSON Schema `enum` field (added in this version) mirrors the frozenset at runtime, so LLMs see only valid directives at the schema level.

### Retiming Guard
Retiming is **permanently blocked** to preserve functional equivalence:
- Blocked directives: `AddRetime`, `AlternateFlowWithRetiming`
- Blocked boolean options: `retime`, `interconnect_retime`, `insert_negative_edge_ffs`, `restruct_opt`
- Both schema-level (enum) and handler-level (frozenset) enforcement

## Architecture

```
LLM → MCP tool call → vivado_mcp_server.py → pexpect → vivado -mode tcl
                                         ← stdout/stderr ←
                                                ↓
                                         JSON parse + ERROR detection
```

- **Process Management**: Vivado runs as a pexpect subprocess; timeout triggers kill → restart → reopen DCP
- **Error Detection**: `^ERROR: [` pattern matching returns structured `{"error": "..."}` responses
- **Multi-line Tcl**: Supports multi-line Tcl commands via line-completeness detection

## See Also

- [RapidWrightMCP README](../RapidWrightMCP/README.md)
- [Vivado_RapidWright_Usage_Reference.md](../Vivado_RapidWright_Usage_Reference.md)
