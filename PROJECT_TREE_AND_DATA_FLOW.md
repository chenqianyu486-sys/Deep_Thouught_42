# FPL26 Optimization Contest - Project Structure & Data Flow

## 1. Project Tree

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # Main entry point: FPGA optimization agent
├── validate_dcps.py              # DCP equivalence validator
├── SYSTEM_PROMPT.TXT             # System prompt for LLM
│
├── context_manager/              # Context management module
│   ├── __init__.py               # Module exports
│   ├── lightyaml.py              # LightYAML - pure Python YAML parser/generator (zero dependencies)
│   ├── test_lightyaml.py         # LightYAML test suite
│   ├── logging_config.py         # Logging configuration & utilities
│   ├── interfaces.py             # Core interfaces & data classes
│   ├── manager.py                # MemoryManager - central orchestration
│   ├── estimator.py              # ContextEstimator - token counting
│   ├── events.py                 # EventBus - event system
│   ├── compat.py                 # DCPOptimizerCompat - legacy adapter
│   ├── agent_context.py          # AgentContextManager - multi-agent branching
│   │
│   ├── memory/
│   │   ├── __init__.py           # Sub-package exports
│   │   ├── working_memory.py     # WorkingMemory - short-term context
│   │   └── historical_memory.py  # HistoricalMemory - long-term storage
│   │
│   ├── stores/
│   │   ├── __init__.py           # Sub-package exports
│   │   └── memory_store.py       # InMemoryContextStore - message storage
│   │
│   └── strategies/
│       ├── __init__.py           # Sub-package exports
│       └── yaml_structured_compress.py # YAMLStructuredCompressor - YAML format compression
│
├── RapidWrightMCP/                # RapidWright MCP server
│   ├── server.py                 # MCP server implementation
│   ├── rapidwright_tools.py      # RapidWright tool wrappers
│   └── test_server.py            # Server tests
│
├── VivadoMCP/                    # Vivado MCP server
│   ├── vivado_mcp_server.py      # MCP server implementation
│   └── test_vivado_mcp.py        # Server tests
│
├── RapidWright/                  # RapidWright SDK (vendor)
│   ├── python/                   # Python bindings
│   └── interchange/              # Interchange format
│
└── docs/                         # Documentation
    ├── index.md
    ├── FAQ.md
    ├── benchmarks.md
    ├── details.md
    └── ...
```

## 2. Data Flow Architecture

### 2.1 High-Level Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              DCPOptimizer                                     │
│                         (dcp_optimizer.py)                                   │
│                                                                               │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────────┐  │
│  │ RapidWright │    │   Vivado    │    │   Memory    │    │ AgentContext │  │
│  │    MCP      │    │    MCP      │    │  Manager    │    │   Manager     │  │
│  │  Session    │    │   Session   │    │             │    │  (branching)  │  │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘    └──────┬───────┘  │
│         │                   │                   │                   │         │
│         └───────────────────┼───────────────────┼───────────────────┘         │
│                             │                                                  │
│                      ┌──────▼──────┐                                          │
│                      │  Tool Call  │                                          │
│                      │   Results   │                                          │
│                      └─────────────┘                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │   Shared EventBus         │
                    │   (MemoryManager +        │
                    │    AgentContextManager)   │
                    └───────────────────────────┘
```

### 2.2 Context Manager Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              MemoryManager                                    │
│                            (manager.py)                                       │
│                                                                               │
│  add_message(role, content)                                                   │
│         │                                                                     │
│         ▼                                                                     │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ WorkingMemory.add_message() — adds message only, no auto-compression  │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                               │
│  NOTE: Compression is now triggered exclusively by DCPOptimizer._compress_context() │
│        (single trigger point - before LLM call).                             │
│        The automatic MESSAGE_ADDED subscription has been disabled.           │
│        Benefits: eliminates implicit behavior, fully controllable timing.    │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │   Shared EventBus         │
                    │   (MemoryManager +        │
                    │    AgentContextManager)   │
                    └───────────────────────────┘
```

### 2.3 Message Flow Through Memory Layers

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Message Flow                                    │
└─────────────────────────────────────────────────────────────────────────────┘

  add_message(role, content)
         │
         ▼
  ┌──────────────────┐
  │    Message       │  (dataclass: role, content, name, tool_calls,
  │    Creation      │          tool_call_id, metadata)
  └────────┬─────────┘
           │
           ▼
  ┌──────────────────┐
  │  WorkingMemory    │  ◄── Short-term storage (current task)
  │  .add_message()  │      - get_all()
  └────────┬─────────┘      - get_context_for_model()
           │                - estimate_tokens()
           │
           │  (when compression triggered by DCPOptimizer._compress_context()
           │   before LLM call - single trigger point)
           ▼
  ┌──────────────────┐
  │  Compression     │  ◄── Single strategy: YAMLStructuredCompressor
  │  Strategy        │      (intensity via force_aggressive flag)
  │  .compress()     │      NOTE: System messages are NEVER compressed
  └────────┬─────────┘
           │
           ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                      SEQUENTIAL Operations:                              │
  │  1. Separate system messages (protected) from others                    │
  │  2. Create summary from non-system messages                              │
  │  3. HistoricalMemory.add(summary, importance=0.8)  ← Archive first       │
  │  4. WorkingMemory.clear()                        ← Then clear           │
  │  5. Add system + compressed non-system messages    ← Repopulate         │
  │  6. EventBus.emit(CONTEXT_COMPRESSED)            ← Finally notify       │
  └─────────────────────────────────────────────────────────────────────────┘
```

### 2.4 Tool Call Result Tracking Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Tool Call Result Flow                                 │
└─────────────────────────────────────────────────────────────────────────────┘

  Tool Result Received (wns, error, etc.)
         │
         ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                    DCPOptimizer.add_tool_result()                         │
  │                    (via DCPOptimizerCompat)                               │
  └────────────────────────────────────────────────────────────────────────┘
         │
         ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │                       MemoryManager.add_tool_result()                    │
  │                                                                        │
  │   entry = {                                                             │
  │       "tool_name": tool_name,                                           │
  │       "result": result[:500],                                          │
  │       "wns": wns,                                                       │
  │       "error": error,                                                   │
  │       "iteration": self._iteration                                      │
  │   }                                                                     │
  │                                                                        │
  │   self._tool_call_details.append(entry)                                 │
  │   if wns > self._best_wns: self._best_wns = wns                         │
  └────────────────────────────────────────────────────────────────────────┘
         │
         ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │              CompressionContext.tool_call_details                        │
  │              (used during compression decisions)                        │
  └────────────────────────────────────────────────────────────────────────┘
```

### 2.5 Dual-Model (Planner/Worker) Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Dual-Model Architecture                             │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                         DCPOptimizer                                      │
  │                                                                          │
  │   Model Selection Logic (8 dimensions, weighted scoring):                  │
  │                                                                          │
  │   Dimension 1: Tool mapping (place/route → PLANNER, get_timing → WORKER) │
  │   Dimension 2: Task category (OPTIMIZATION → +2 PLANNER,                  │
  │                      INFORMATION → +1 WORKER)                              │
  │   Dimension 3: Context complexity (>=6 → +2 PLANNER,                      │
  │                      <3 → +1 WORKER)                                       │
  │   Dimension 4: Historical capability score (0.7+ → +2 WORKER,             │
  │                      <0.3 → +2 PLANNER)                                    │
  │   Dimension 5: Counter state (failures >= 2 → +4 PLANNER,                │
  │                      successes >= 3 → +1 WORKER)                           │
  │   Dimension 6: Global no-improvement (>= 2 → +1 WORKER)                   │
  │   Dimension 7: Context window capacity (>= 120K → +2 PLANNER)            │
  │   Dimension 8: WNS / timing state (urgency signal, NEW)                  │
  │       └── wns_improvement = best_wns - initial_wns                        │
  │       └── < -2.0: +3 PLANNER (severe regression)                         │
  │       └── < -0.5: +2 PLANNER (moderate regression)                        │
  │       └── < 0: +1 PLANNER (slight regression)                             │
  │                                                                          │
  │   TOOL_MODEL_MAPPING = {                                                 │
  │       "place_design": PLANNER,     # Complex, high-impact                │
  │       "route_design": PLANNER,                                           │
  │       "get_utilization": WORKER,    # Simple, read-only                  │
  │       "get_timing": WORKER,                                              │
  │       ...                                                               │
  │   }                                                                     │
  │                                                                          │
  │   Model Tiers:                                                           │
  │   - PLANNER: xiaomi/mimo-v2-pro (1M context, complex reasoning)          │
  │   - WORKER: xiaomi/mimo-v2-flash (200K context, fast execution)           │
  │                                                                          │
  │   Context Budget:                                                        │
  │   - WORKER_CONTEXT_WARN_TOKENS = 120K (60%) → bias toward PLANNER        │
  │   - WORKER_CONTEXT_FORCE_TOKENS = 170K (85%) → hard override to PLANNER  │
  │                                                                          │
  │   Decision Threshold (asymmetric):                                          │
  │   - planner_score > worker_score + 1 → PLANNER (margin of 2 required)      │
  │   - worker_score > planner_score → WORKER (margin of 1 required)           │
  │   - default → PLANNER (safe default)                                       │
  │                                                                             │
  │   Model Switching:                                                          │
  │   - Worker consecutive successes → bias toward staying with worker model             │
  │   - Worker failures累积 → upgrade to Planner                            │
  │   - WNS severe regression → upgrade to Planner (Dimension 8)            │
  │   - Global no-improvement count → force Planner                         │
  └──────────────────────────────────────────────────────────────────────────┘
         │
         ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                        LLM API Calls                                      │
  │                                                                          │
  │   openai.chat.completions.create(                                        │
  │       model=model_planner | model_worker,                                 │
  │       messages=get_formatted_for_api(system_prompt)                       │
  │   )                                                                      │
  │                                                                          │
  │   Token Usage Tracking:                                                   │
  │   - total_prompt_tokens                                                  │
  │   - total_completion_tokens                                              │
  │   - total_cost                                                           │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 2.6 Event System Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Event System                                      │
└─────────────────────────────────────────────────────────────────────────────┘

  EventBus (events.py)
  ├── _sync_handlers: dict[EventType, list[Callable]]
  ├── _global_handlers: list[Callable]
  ├── _event_history: list[ContextEvent]
  ├── _handler_tokens: dict[str, tuple[EventType, Callable]]  # token -> (event_type, handler)
  └── _global_handler_tokens: dict[str, Callable]             # token -> handler

  Methods:
  ├── subscribe(event_type, handler)              # Register handler, returns token
  ├── unsubscribe_by_token(token) -> bool        # Unsubscribe by token (prevents memory leaks)
  ├── unsubscribe(event_type, handler)           # Unsubscribe by reference
  ├── subscribe_global(handler)                   # Register global handler, returns token
  ├── unsubscribe_global_by_token(token) -> bool # Unsubscribe global by token
  ├── unsubscribe_global(handler)                 # Unsubscribe global by reference
  ├── emit(event)                                 # Fire event synchronously
  └── get_history(event_type, limit)             # Retrieve recent events

  Token-Based Unsubscribe (Recommended):
  - subscribe() and subscribe_global() return a UUID token
  - Use unsubscribe_by_token(token) or unsubscribe_global_by_token(token) to unsubscribe
  - This allows unsubscribing lambdas and bound methods without retaining the handler reference
  - Both reference-based and token-based unsubsribe update the token registries

  Event Types:
  ├── MESSAGE_ADDED       → (DISABLED) Previously triggered auto-compression; now disabled.
  │                        Compression is exclusively triggered by DCPOptimizer._compress_context()
  │                        (single explicit trigger point - eliminates dual-trigger conflicts)
  ├── CONTEXT_COMPRESSED  → Listeners notified after compression
  │                        Data: {"compression_type", "original_count", "compressed_count",
  │                               "original_tokens", "compressed_tokens",
  │                               "compression_ratio_token", "force_aggressive", "iteration"}
  ├── LAYER_PROMOTED     → Emitted by HistoricalMemory.add() after archiving an entry
  ├── BRANCH_CREATED      → New agent branch created (AgentContextManager)
  │                        Data: {"branch": AgentContext} (full object, not just ID)
  └── BRANCH_MERGED       → Branch merged into parent (AgentContextManager)
                              Data: {"source_branch": AgentContext, "target_branch": AgentContext, "strategy": str}

  emit(event):
         │
         ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │  1. Append to _event_history (max 1000)                                │
  │  2. Call type-specific handlers (_sync_handlers[event_type])           │
  │  3. Call global handlers (_global_handlers)                            │
  │  4. Log any handler exceptions (non-fatal)                             │
  └────────────────────────────────────────────────────────────────────────┘

  Shared EventBus in DCPOptimizer:
         │
         ▼
  ┌────────────────────────────────────────────────────────────────────────┐
  │  self._event_bus  ──►  MemoryManager                                  │
  │                  ──►  AgentContextManager                             │
  │  Both components share the same EventBus instance, so branch events  │
  │  (BRANCH_CREATED, BRANCH_MERGED) can trigger MemoryManager reactions  │
  └────────────────────────────────────────────────────────────────────────┘
```

### 2.7 Compression Flow (Unified YAML Strategy)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│              COMPRESSION FLOW - UNIFIED YAML (SINGLE TRIGGER POINT)         │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    DCPOptimizer._compress_context()                     │
  │                                                                        │
  │   Entry Point: Called before model selection AND before LLM API calls   │
  │   (Two calls in get_completion(): first for accurate token count         │
  │    used in model routing, second right before the API call)              │
  │   (No automatic triggering - fully controllable timing)                 │
  │                                                                        │
  │   Steps:                                                                │
  │   1. _sync_state_to_memory_manager()   ← Sync WNS, iteration, etc.   │
  │   2. _estimate_tokens()                 ← Calculate current token count │
  │   3. Build CompressionContext           ← Include tool_call_details,   │
  │                                           failed_strategies, etc.       │
  │   4. retrieve_historical()              ← Fetch recent high-importance │
  │                                           entries from HistoricalMemory │
  │   5. MemoryManager._compress("yaml_structured", context)               │
  │      └─ context.force_aggressive determines compression intensity       │
  │   6. Return                             ← Done (no extra steps)       │
  └──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    MemoryManager._compress(type, context)                │
  │                                                                        │
  │   NOTE: Only "yaml_structured" is used. Aggressive vs normal mode       │
  │   is determined by context.force_aggressive flag.                        │
  │                                                                        │
  │   Steps (inside this method, single atomic operation):                   │
  │   1. YAMLStructuredCompressor.compress()                               │
  │      └─ force_aggressive=False → preserve_turns=20, min_threshold=0.3  │
  │      └─ force_aggressive=True  → preserve_turns=3, min_threshold=0.8   │
  │      └─ dump() timing/error logging + optional roundtrip validation    │
  │   2. HistoricalMemory.add(summary) ← Archive before clearing           │
  │   3. WorkingMemory.clear()         ← Clear all messages                │
  │   4. Add compressed messages back  ← Repopulate with compressed msgs  │
  │   5. EventBus.emit(CONTEXT_COMPRESSED) ← Notify listeners              │
  │      Data enriched with: original_tokens, compressed_tokens,           │
  │      compression_ratio_token, force_aggressive, iteration               │
  │                                                                        │
  │   NOTE: No additional message replacement needed after this returns     │
  └──────────────────────────────────────────────────────────────────────────┘

  Key Design Decisions:
  ├── Single Trigger: Compression ONLY via DCPOptimizer._compress_context()
  ├── No Auto-Trigger: MESSAGE_ADDED auto-subscription is DISABLED
  ├── No Redundant Ops: Messages already replaced inside _compress()
  ├── Unified YAML: Only "yaml_structured" strategy is used
  │   └── Compression intensity controlled by force_aggressive flag
  └── Historical Retrieval: High-importance entries injected into context
```

### 2.8 YAML Structured Compression Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    YAML Structured Compression Flow                          │
│                    (context_manager/strategies/yaml_structured_compress.py) │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                  messages_to_yaml(messages, context)                       │
  │                                                                          │
  │  YAML output with FPGA design context (via LightYAML):                   │
  │                                                                          │
  │  meta:                                                                   │
  │    compression_type: yaml_structured | yaml_structured_aggressive       │
  │    message_count: 10                                                     │
  │  design_state:                                                           │
  │    timing:                                                               │
  │      clock_period: 5.0                                                   │
  │      initial_wns: -3.2                                                  │
  │      best_wns: -1.5                                                     │
  │      current_wns: -2.1                                                   │
  │    iteration: 5                                                          │
  │    blocked_strategies:                                                   │
  │      - route_design -directive Aggressive                               │
  │  system_messages:                                                        │
  │    - You are an FPGA optimizer...                                        │
  │  conversation:                                                           │
  │    - role: user                                                         │
  │      importance: 1.95                                                   │
  │      topics: [timing]                                                    │
  │      content: Optimize timing...                                         │
  │  historical_summary:                                                    │
  │    - timestamp: 1745320665.123                                          │
  │      importance: 0.85                                                    │
  │      task_type: compression_snapshot                                     │
  │      content: (truncated past optimization context)                       │
  └──────────────────────────────────────────────────────────────────────────┘

  Key Features:
  ├── FPGA design state section with timing metrics (WNS, TNS, clock_period)
  ├── Blocked strategies tracking (failed approaches to avoid)
  ├── Topic classification per message (placement/routing/timing)
  ├── Importance scoring preserved
  ├── Historical summary (retrieved from HistoricalMemory)
  ├── Compression intensity via context.force_aggressive:
  │   ├── False (normal): preserve_turns=20, min_importance_threshold=0.3
  │   └── True (aggressive): preserve_turns=3, min_importance_threshold=0.8
  └── Uses LightYAML (zero external dependencies)

### 2.9 LightYAML Module (lightyaml.py)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    LightYAML - Pure Python YAML Parser/Generator              │
│                    (context_manager/lightyaml.py)                             │
└─────────────────────────────────────────────────────────────────────────────┘

  Design Goals:
  ├── Zero dependencies: Uses only Python standard library (re, typing, collections)
  ├── Lightweight subset: Supports basic types and nested structures only
  ├── FPGA-friendly: Handles special characters in signal names ([, ], -, _)
  └── Performance optimized: Pre-compiled regex, fast path for common cases

  Supported Features:
  ├── Scalar types: strings (quoted/unquoted), integers, floats, booleans, null
  ├── Data structures: Mappings (key-value pairs), Sequences (ordered lists)
  ├── Nested structures: Arbitrary depth nesting
  ├── Indentation: Space-based (default 2 spaces)
  └── Comments: # inline comments (stripped during parsing)

  Explicitly NOT Supported (raises YAMLUnsupportedError):
  ├── Anchors (&) and aliases (*)
  ├── Multi-line block strings (|, >)
  └── Type tags (!!)

  API:
  ├── LightYAML.dump(data, indent=2, trace_id=None) -> str  # Serialize to YAML
  │   └─ Instr: timing/error logging, content size tracking
  ├── LightYAML.load(yaml_str, trace_id=None) -> dict|list   # Parse YAML string
  │   └─ Instr: parse duration logging, rich error context
  ├── LightYAML.validate(yaml_str) -> (bool, str)            # Validate YAML syntax
  ├── LightYAML.roundtrip(data) -> (str, Any)                # Dump + Load for verification
  └── LightYAML._estimate_node_count(data) -> int            # Node count for complexity metrics

  Performance Optimizations:
  ├── Pre-compiled regex patterns as class attributes (_NUMERIC_RE, _HEX_RE, _INT_RE)
  ├── Fast path in _unquote: skip processing when no backslashes present
  └── Reduced string traversals in type detection

  FPGA Signal Name Handling:
  ├── Signal names like clk[0], rst_n, io_out[3:0] are parsed correctly
  ├── Square brackets and hyphens in key names are allowed
  └── Underscores preserved without quoting

  Example Usage:
  ```python
  from context_manager.lightyaml import LightYAML

  # Serialize
  yaml_str = LightYAML.dump({
      "signals": {"clk[0]": True, "data[7:0]": 255},
      "timing": {"wns": -1.25, "fmax_mhz": 142.5}
  })

  # Parse
  data = LightYAML.load(yaml_str)

  # Roundtrip verification
  yaml_str, parsed = LightYAML.roundtrip(original_data)
  assert parsed == original_data
  ```

  Implementation Details:
  ├── Line-based recursive descent parser
  ├── Preprocessing: inline comment removal, BOM stripping, line ending normalization
  ├── Type inference: automatic detection of int/float/bool/string/null
  ├── Tab rejection: any Tab character raises YAMLParseError
  └── Roundtrip safety: strings starting with `-` or containing flow chars ([, {, }, ])
     are quoted on dump; `_is_simple_sequence` prevents block-format ambiguity
  └── Flow syntax support: [1, 2, 3] and {key: value} parsing
```

### 2.10 Initial Analysis YAML Format

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Initial Analysis YAML Output                              │
│                    (dcp_optimizer.py - perform_initial_analysis)              │
└─────────────────────────────────────────────────────────────────────────────┘

  The initial analysis (performed before LLM interaction) is now output
  in structured YAML format using LightYAML, replacing the previous
  plain text summary.

  YAML Structure:
  ```yaml
  meta:
    type: initial_analysis
    timestamp: "YYYY-MM-DD HH:MM:SS"
  timing:
    clock_period_ns: X.XXX
    target_fmax_mhz: X.XX
    wns_ns: X.XXX
    status: MET | VIOLATED
    achievable_fmax_mhz: X.XX
    tns_ns: X.XXX
    failing_endpoints: N
  critical_path_spread:
    max_distance_tiles: N
    avg_distance_tiles: X.X
    paths_analyzed: N
  recommendation: PBLOCK  # only present if avg_distance > 70 and paths >= 5
  high_fanout_nets:
    - rank: 1
      name: "net_name"
      fanout: N
      critical_paths: N
    ...
  total_high_fanout_nets: N
  ```

  Key Changes from Plain Text:
  ├── Structured fields enable easier parsing by LLM
  ├── `recommendation: PBLOCK` replaces "[WARNING] RECOMMENDATION: Use PBLOCK strategy"
  ├── `status: MET | VIOLATED` replaces "TIMING MET ✓" / "TIMING VIOLATED"
  ├── High fanout nets use array format instead of indented text
  └── All values are numeric/scalar for programmatic access

  Implementation:
  ├── _build_initial_analysis_yaml() method in DCPOptimizer
  ├── Uses OrderedDict to maintain field order
  ├── LightYAML.dump() for serialization
  └── Output wrapped with "---\n" prefix and "..." suffix

  SYSTEM_PROMPT.TXT Integration:
  The system prompt now includes YAML format documentation instructing
  the LLM to parse the `recommendation` field to determine PBLOCK strategy
  usage, replacing the previous text-based "look for 'Use PBLOCK strategy'" rule.
```

## 3. Key Data Classes

### 3.1 Core Interfaces (interfaces.py)

```python
MemoryLayer enum    # WORKING, HISTORICAL, ARCHIVE
MessageRole enum    # SYSTEM, USER, ASSISTANT, TOOL

Message             # role, content, name, tool_calls, tool_call_id, metadata
CompressionContext  # current_tokens, threshold_tokens, hard_limit_tokens,
                    # failed_strategies, tool_call_details, best_wns,
                    # initial_wns, current_wns, clock_period, iteration,
                    # force_aggressive (bool), retrieved_history (list),
                    # agent_id (optional)
ContextEvent        # event_type, timestamp, data, source_agent_id
ContextSnapshot     # timestamp, layer, message_count, token_estimate,
                    # agent_id, parent_snapshot_id
HistoricalEntry     # id, timestamp, content, importance_score, task_type,
                    # agent_id, tags, embedding
RetrievalQuery      # text, task_type, time_range, min_importance, limit, agent_id

ContextStore        # Abstract base class (ABC) for message stores
CompressionStrategy # Abstract base class (ABC) for compression strategies
```

### 3.2 Configuration Classes

```python
MemoryManagerConfig       # working_config, historical_config,
                          # soft_threshold (80K), hard_limit (150K)

WorkingMemoryConfig       # max_tokens (80K), hard_limit_tokens (150K),
                          # recent_window (20), tool_result_truncate (30K)

HistoricalMemoryConfig    # max_entries (10K), relevance_threshold (0.5),
                          # age_based_decay (0.95)

YAMLStructuredCompressor.__init__ params  # token_budget (80K), preserve_turns (20),
                          # min_importance_threshold (0.3)
                          # (configured via constructor args, not a separate config class)
                          # Used by YAMLStructuredCompressor

## 4. File Responsibilities

| File | Responsibility |
|------|-----------------|
| `dcp_optimizer.py` | Main agent: LLM orchestration, tool calls, model selection; owns shared EventBus + AgentContextManager; triggers compression via `_compress_context()` (single compression trigger point); `_build_initial_analysis_yaml()` provides YAML-formatted initial analysis to LLM |
| `SYSTEM_PROMPT.TXT` | System prompt with YAML format documentation for parsing initial analysis; `recommendation` field check replaces text-based PBLOCK detection |
| `validate_dcps.py` | DCP equivalence validation (Phase1: structural, Phase2: simulation) |
| `context_manager/manager.py` | Central memory orchestration; `_compress()` always uses "yaml_structured" (aggressive/light mode determined by `context.force_aggressive`); `retrieve_historical()` for historical memory queries; no auto-subscribes to MESSAGE_ADDED; `CONTEXT_COMPRESSED` event now includes token metrics (original_tokens, compressed_tokens, ratio) |
| `context_manager/events.py` | Event bus: subscribe/unsubscribe (both reference and token-based); emit/get_history; shared by MemoryManager and AgentContextManager |
| `context_manager/lightyaml.py` | LightYAML - pure Python YAML parser/generator; zero dependencies; supports basic types, nested structures, FPGA signal names; raises YAMLUnsupportedError for anchors, aliases, block strings, type tags; **performance optimized** with pre-compiled regex patterns; `dump()` now fully instrumented with timing, trace_id propagation, error logging, and node counting for observability (2026-04-25) |
| `context_manager/estimator.py` | Token estimation using content-type-aware method (Chinese: 1.5 chars/token, English: 3.5, Code: 2.5, Digits: 4.0, Whitespace: 5.0); includes `estimate_tokens()`, `estimate_from_messages()`, and `estimate_context_complexity()` methods |
| `context_manager/memory/working_memory.py` | Short-term message storage |
| `context_manager/memory/historical_memory.py` | Long-term archive with retrieval; uses indexes (_index_by_time, _index_by_importance, _index_by_task_type) for efficient lookups |
| `context_manager/stores/memory_store.py` | In-memory message store; `__bool__` returns `len(messages) > 0`; `restore()` is a no-op (not an abstract method) |
| `context_manager/strategies/yaml_structured_compress.py` | YAMLStructuredCompressor - unified YAML compression (aggressive/smart modes removed 2026-04-22); `strategies/__init__.py` re-exports CompressionStrategy; instrumented dump with timing/error logging; optional roundtrip validation controlled by `YAML_ROUNDTRIP_VALIDATE` env var; compression log includes YAML output size and token estimate (2026-04-25) |
| `context_manager/compat.py` | Legacy adapter for DCPOptimizerCompat |
| `context_manager/agent_context.py` | Multi-agent branching; shares EventBus with MemoryManager via DCPOptimizer |
| `RapidWrightMCP/server.py` | RapidWright MCP server |
| `VivadoMCP/vivado_mcp_server.py` | Vivado MCP server |

## 5. Data Flow Summary

```
User Input / Tool Result
         │
         ▼
add_message() ──────────────────────────────────────────────────┐
         │                                                     │
         ▼                                                     ▼
WorkingMemory                  CompressionContext
.add_message()                (built only when
 (no auto-compression)          _compress_context() called)
                                   │
                          ┌──────┴──────┐
                          ▼             ▼
                 dcp_optimizer      YAMLStructuredCompressor
                 ._compress_context()  .compress()
                          │             │
                          │             ▼
                          │    ┌─────────────────────────────┐
                          │    │  context.force_aggressive:   │
                          │    │  False → preserve_turns=20  │
                          │    │  True  → preserve_turns=3   │
                          │    └─────────────────────────────┘
                          │             │
                          └──────┬──────┘
                                 ▼
                   ┌─────────────────────────────────────┐
                   │  SEQUENTIAL COMPRESSION (inside MemoryManager._compress):      │
                   │  1. Separate system messages (protected)                       │
                   │  2. HistoricalMemory.add(summary)                             │
                   │  3. WorkingMemory.clear()                                      │
                   │  4. Add system + compressed non-system messages               │
                   │  5. EventBus.emit(CONTEXT_COMPRESSED)                          │
                   │  NOTE: System messages are NEVER compressed                    │
                   │  NOTE: YAMLStructuredCompressor budget calculation (system_tokens │
                   │        properly calculated before non-system budget)            │
                   │  NOTE: Unified YAML only (unified YAML strategy, aggressive/smart removed) │
└─────────────────────────────────────┘
```
