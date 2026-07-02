# RapidWright MCP Server

An MCP (Model Context Protocol) server that provides AI assistant access to [RapidWright](https://github.com/Xilinx/RapidWright), an open-source FPGA design tool framework from AMD.

This server enables AI assistants like Cursor to interact with FPGA designs, query device information, analyze design checkpoints, and explore Xilinx/AMD device architectures through natural language.

## Features

- **Device Information**: Query supported FPGA devices and families, get detailed device specifications
- **Design Checkpoint Analysis**: Load Vivado .dcp files, inspect design statistics, search for cells
- **Device Architecture Exploration**: Get tile/site information, query device resources
- **Design Optimization**: LUT input cone optimization and high fanout net splitting

## Quick Start

### Prerequisites

- Python 3.8+
- Java 11+

### Installation

The recommended setup uses the contest repo's Makefile, which builds RapidWright from a git submodule:

```bash
cd fpl26_optimization_contest
make setup
```

This automatically:
1. Installs Python dependencies (including the `rapidwright` pip package for JPype bridging)
2. Builds RapidWright from the `RapidWright/` git submodule (`./gradlew compileJava`)
3. Sets `RAPIDWRIGHT_PATH` and `CLASSPATH` so the pip package uses the local source

To rebuild RapidWright after modifying its source code:

```bash
make build-rapidwright
```

### Manual Setup (standalone)

```bash
cd RapidWrightMCP
./setup.sh
python3 test_server.py
```

## Usage with Cursor

Add to your MCP configuration file:

```json
{
  "mcpServers": {
    "rapidwright": {
      "command": "python3",
      "args": ["/absolute/path/to/RapidWrightMCP/server.py"],
      "env": {
        "RAPIDWRIGHT_PATH": "/absolute/path/to/RapidWright",
        "CLASSPATH": "/absolute/path/to/RapidWright/bin:/absolute/path/to/RapidWright/jars/*"
      }
    }
  }
}
```

Restart Cursor after saving.

## Available Tools

### Device & Checkpoint
| Tool | Description |
|------|-------------|
| `initialize_rapidwright` | Initialize RapidWright (must be called first) |
| `get_supported_devices` | List all supported FPGA devices |
| `get_device_info` | Get detailed information about a specific device |
| `get_device_topology` | Get FPGA device topology (tile grid, clock regions) |
| `read_checkpoint` | Load a Vivado Design Checkpoint (.dcp) file |
| `write_checkpoint` | Save design to a .dcp file |
| `get_design_info` | Get statistics about the loaded design |

### Search
| Tool | Description |
|------|-------------|
| `search_cells` | Search for cells by name or type |
| `search_sites` | Search for sites by type on a device |
| `get_tile_info` | Get information about a specific tile |

### Analysis
| Tool | Description |
|------|-------------|
| `analyze_critical_path_spread` | Analyze Manhattan distance spread of critical path cells |
| `analyze_congestion` | Score congestion per tile region (0-1 scale) |
| `analyze_congestion_spreading` | Identify cells for congestion relief |
| `analyze_net_detour` | Analyze net detour ratios on critical paths |
| `analyze_fabric_for_pblock` | Analyze FPGA fabric for optimal pblock region |
| `analyze_pblock_region` | Read-only pblock resource analysis |
| `report_timing` | Approximate timing via RapidWright model (~2% error) |
| `estimate_timing` | Estimate timing impact of proposed changes |
| `compare_designs` | Structural comparison between two designs |

### Fanout & LUT Optimization
| Tool | Description |
|------|-------------|
| `optimize_lut_input_cone` | Combine chained LUTs into single LUTs |
| `optimize_fanout_batch` | Batch split high fanout nets by replicating drivers |

### Placement & Congestion
| Tool | Description |
|------|-------------|
| `optimize_cell_placement` | Move specific cells to reduce path delay |
| `smart_region_search` | Weighted-distance region search for optimal placement |
| `execute_congestion_spreading` | Spread cells from congested areas |
| `flatten_lut_cascade` | Flatten LUT cascades to reduce logic depth |
| `replicate_critical_cells` | Replicate high-delay cells to reduce fanout |
| `optimize_pin_swapping` | Swap LUT input pins to faster physical pins |
| `analyze_net_swapping` | Identify equivalent nets for intra-SLICE swapping |
| `execute_net_swapping` | Swap equivalent nets between BEL pins |

### Strategy Workflows
| Tool | Description |
|------|-------------|
| `execute_pblock_strategy` | Complete PBLOCK workflow (auto-chains Vivado tools) |
| `execute_physopt_strategy` | PhysOpt workflow with pre/post timing capture |
| `execute_opt_design_strategy` | opt_design workflow with auto place/route chain |
| `execute_fanout_strategy` | Fanout optimization + checkpoint workflow |
| `execute_combinational_rebalancing_strategy` | Combinational logic rebalancing |
| `execute_lut_muxf_repack_strategy` | LUT + MUXF repacking |
| `execute_muxf_tree_reorder_strategy` | MUXF tree reordering |

### Pblock Utilities
| Tool | Description |
|------|-------------|
| `convert_fabric_region_to_pblock` | Convert tile coordinates to pblock range strings |

### Cross-server Integration
- Analysis tools (`analyze_critical_path_spread`, `analyze_net_detour`) take input from **VivadoMCP** `extract_critical_path_cells` / `extract_critical_path_pins`.
- Mutation tools instruct LLM to call **VivadoMCP** `open_checkpoint` → `place_design`/`route_design` → `report_timing_summary`.
- The returned `pblock_ranges` should be passed as the `ranges` parameter to VivadoMCP `create_and_apply_pblock`.

### Removed / Blocked Tools
| Tool | Status |
|------|--------|
| `route_design_rwroute` | Removed — RWRoute degrades timing |
| `analyze_register_retiming` | Blocked — breaks functional equivalence |
| `execute_register_retiming` | Blocked — breaks functional equivalence |
| `smart_retiming` | Blocked — breaks functional equivalence |

## Example Usage

```
User: "Initialize RapidWright and show me what devices are available"
AI: [calls initialize_rapidwright and get_supported_devices]
    "RapidWright supports 50+ devices including xcvu3p, xcvu9p, xcku040..."

User: "Load my design from ~/my_design.dcp and tell me how many LUTs it uses"
AI: [calls read_checkpoint, then get_design_info and search_cells]
    "Your design uses 15,432 LUT6 cells..."

User: "Optimize the LUT input cone for pin 'top/cpu/alu/result[0]'"
AI: [calls optimize_lut_input_cone]
    "Successfully combined 3 chained LUTs into a single LUT6."

User: "The net 'clk_enable' has very high fanout. Split it into 4 parts."
AI: [calls optimize_fanout_batch]
    "Split 'clk_enable' (original fanout: 2,456) into 4 nets with ~614 loads each."
```

## Tips

- **Initialize once** per session, then use other commands freely
- **Use absolute paths** for .dcp files
- **Be specific** with device names (e.g., "xcvu9p" not "vu9p")
- **Chain requests**: "Load design X and tell me Y"
- Use natural language - no need for exact tool names

## Common Cell & Site Types

| Cell Type | Description |
|-----------|-------------|
| LUT6 | 6-input Lookup Table |
| FDRE | D Flip-Flop with Clock Enable and Sync Reset |
| FDCE | D Flip-Flop with Clock Enable and Async Clear |
| CARRY8 | Carry Logic (UltraScale+) |
| DSP48E2 | DSP Block (UltraScale+) |
| RAMB36E2 | 36Kb Block RAM (UltraScale+) |
| BUFGCE | Global Clock Buffer |

| Site Type | Description |
|-----------|-------------|
| SLICEL | Logic Slice (LUTs, FFs, Carry) |
| SLICEM | Memory Slice (+ Distributed RAM) |
| DSP48E2 | DSP/Math Block |
| RAMB36/RAMB18 | Block RAM |
| URAM288 | 288Kb Ultra RAM |

---

## Design Optimization Guide

### LUT Input Cone Optimization

Combines chained small LUTs into a single larger LUT (up to LUT6) to reduce logic depth.

```
Before:  Input -> LUT2 -> LUT3 -> LUT4 -> Output  (3 logic levels)
After:   Input -> LUT6 -> Output                   (1 logic level)
```

**When to use:**
- Critical path optimization when timing shows multiple LUT levels
- Post-place-and-route ECO fixes without full re-synthesis

**Parameters:**
- `hierarchical_input_pins`: List of pins to optimize (e.g., `["top/cpu/alu/result[0]"]`)
  The tool automatically saves checkpoints to the working directory.

**Limitations:**
- Maximum 6 inputs (LUT6 is the largest)
- Only works on paths driven exclusively by LUTs
- Cannot optimize paths with flip-flops, DSPs, or other non-LUT elements

### Fanout Optimization

Splits high-fanout nets by replicating the source driver. Each replica drives a subset of loads.

```
Before:  Driver -> [1000 loads]
After:   Driver_1 -> [250 loads]
         Driver_2 -> [250 loads]
         Driver_3 -> [250 loads]
         Driver_4 -> [250 loads]
```

**When to use:**
- High-fanout enable/control signals (>500 loads)
- Routing congestion from heavily loaded nets
- Timing closure for high-fanout paths

**Choosing split factor:**
- k=2: Moderately high fanout (500-1000 loads)
- k=3-4: Very high fanout (1000-3000 loads)
- k≥5: Extremely high fanout (>3000 loads)

**Trade-offs:**
- Higher k = lower fanout per net = better routing/timing
- Higher k = more driver cells = increased area and power

**Limitations:**
- Only works on routed nets
- Not beneficial for small fanouts (<100 loads)

### Optimization Troubleshooting

| Error | Solution |
|-------|----------|
| "Pin not found" | Check hierarchical path, use full name including top module |
| "No optimization possible" | Pin not driven by LUTs, or already optimal single LUT |
| "6 maximum inputs" | Cone requires >6 inputs; try optimizing a later stage |
| "Net not found" | Use physical net name, not hierarchical logical name |

---

## Architecture

```
┌─────────────────┐
│     Cursor      │
│  AI Assistant   │
└────────┬────────┘
         │ MCP Protocol (JSON-RPC over stdio)
┌────────▼────────┐
│  server.py      │  ← MCP Server
└────────┬────────┘
┌────────▼────────────┐
│ rapidwright_tools.py│  ← Tool Wrappers
└────────┬────────────┘
┌────────▼────────┐
│  RapidWright    │  ← pip package (JPype + Java libs)
└─────────────────┘
```

The `rapidwright` pip package provides the JPype/Python bridge, while the
`RAPIDWRIGHT_PATH` and `CLASSPATH` environment variables redirect it to use
Java classes compiled from the local `RapidWright/` git submodule. This allows
contestants to modify and rebuild RapidWright source code directly.

## Development

### Project Structure

```
RapidWrightMCP/
├── server.py              # Main MCP server
├── rapidwright_tools.py   # RapidWright wrapper functions
├── requirements.txt       # Python dependencies (mcp + rapidwright pip bridge)
├── setup.sh              # Setup script
├── test_server.py        # Test suite
└── rapidwright_mcp.log   # Log file (created at runtime)

../RapidWright/            # Git submodule (Xilinx/RapidWright source)
├── src/                   # Java source (modifiable by contestants)
├── bin/                   # Compiled classes (after ./gradlew compileJava)
├── jars/                  # Third-party dependencies
└── gradlew               # Gradle wrapper for building
```

### Adding New Tools

To add a new tool:

1. **Add the function** to `rapidwright_tools.py`:
   ```python
   def my_new_tool(param1: str) -> Dict[str, Any]:
       """Your tool implementation."""
       if not _initialized:
           return {"error": "RapidWright not initialized"}
       # ... implementation
       return {"status": "success", "result": ...}
   ```

2. **Register the tool** in `server.py` `list_tools()`:
   ```python
   Tool(
       name="my_new_tool",
       description="What your tool does",
       inputSchema={
           "type": "object",
           "properties": {
               "param1": {"type": "string", "description": "..."}
           },
           "required": ["param1"]
       }
   )
   ```

3. **Add the handler** in `call_tool()`:
   ```python
   elif name == "my_new_tool":
       result = rw.my_new_tool(arguments["param1"])
   ```

### Running Tests

```bash
python3 test_server.py
tail -f rapidwright_mcp.log
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "RapidWright not initialized" | Call `initialize_rapidwright` first; check Java 11+ is installed |
| Server not appearing | Use absolute paths in config; restart Cursor/Claude completely |
| Out of memory | Increase `jvm_max_memory` (e.g., "8G", "16G") |
| Local changes not picked up | Rebuild: `make build-rapidwright` from the repo root |
| Installation issues | `pip3 install --force-reinstall rapidwright` (for the JPype bridge) |

**Checklist:**
- [ ] Python 3.8+? (`python3 --version`)
- [ ] Java 11+? (`java -version`)
- [ ] RapidWright pip bridge installed? (`pip3 show rapidwright`)
- [ ] RapidWright submodule present? (`ls RapidWright/gradlew`)
- [ ] RapidWright compiled? (`ls RapidWright/build/libs/main.jar`)
- [ ] `RAPIDWRIGHT_PATH` set? (`echo $RAPIDWRIGHT_PATH`)
- [ ] Config paths absolute?
- [ ] Cursor restarted?
- [ ] Check logs? (`cat rapidwright_mcp.log`)


## Resources

- [RapidWright Documentation](https://www.rapidwright.io/docs/)
- [RapidWright Javadoc](https://www.rapidwright.io/javadoc/)
- [RapidWright GitHub](https://github.com/Xilinx/RapidWright)
- [Model Context Protocol](https://modelcontextprotocol.io/)

