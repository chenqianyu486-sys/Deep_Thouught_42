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
│   ├── interfaces.py             # Core interfaces & data classes
│   ├── manager.py                # MemoryManager - central orchestration
│   ├── estimator.py              # ContextEstimator - token counting
│   ├── events.py                 # EventBus - event system
│   ├── compat.py                 # DCPOptimizerCompat - legacy adapter
│   ├── agent_context.py          # AgentContextManager - multi-agent branching
│   ├── formatters.py             # XMLMessageFormatter, XMLResponseParser - XML formatting for LLM
│   │
│   ├── memory/
│   │   ├── working_memory.py     # WorkingMemory - short-term context
│   │   └── historical_memory.py  # HistoricalMemory - long-term storage
│   │
│   ├── stores/
│   │   └── memory_store.py       # InMemoryContextStore - message storage
│   │
│   └── strategies/
│       ├── base.py               # BaseCompressionStrategy - abstract base
│       ├── smart_compress.py     # SmartCompressionStrategy - light compression
│       ├── aggressive_compress.py # AggressiveCompressionStrategy - heavy compression
│       └── xml_structured_compress.py # XMLStructuredCompressor - XML format compression
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
  │  Compression     │  ◄── Three strategies, selected by compression_type:
  │  Strategy        │      - SmartCompressionStrategy ("smart")
  │  .compress()     │      - AggressiveCompressionStrategy ("aggressive")
  │                  │      - XMLStructuredCompressor ("xml_structured")
  │                  │  NOTE: System messages are NEVER compressed
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
  │   Model Selection Logic:                                                 │
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
  │   - WORKER: xiaomi/mimo-v2-flash (200K context, fast execution)         │
  │                                                                          │
  │   Context Budget:                                                        │
  │   - WORKER_CONTEXT_WARN_TOKENS = 120K (60%) → bias toward PLANNER        │
  │   - WORKER_CONTEXT_FORCE_TOKENS = 170K (85%) → hard override to PLANNER  │
  │                                                                          │
  │   Model Switching:                                                        │
  │   - Worker consecutive successes → downgrade to smaller model             │
  │   - Worker failures累积 → upgrade to Planner                            │
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
  └── _event_history: list[ContextEvent]

  Methods:
  ├── subscribe(event_type, handler)           # Register handler for event type
  ├── unsubscribe(event_type, handler)        # Unregister handler (prevents memory leaks)
  ├── subscribe_global(handler)               # Register handler for all events
  ├── unsubscribe_global(handler)             # Unregister global handler
  ├── emit(event)                             # Fire event synchronously
  └── get_history(event_type, limit)          # Retrieve recent events

  Event Types:
  ├── MESSAGE_ADDED       → (DISABLED) Previously triggered auto-compression; now disabled.
  │                        Compression is exclusively triggered by DCPOptimizer._compress_context()
  │                        (single explicit trigger point - eliminates dual-trigger conflicts)
  ├── CONTEXT_COMPRESSED  → Listeners notified after compression
  ├── LAYER_PROMOTED     → Emitted by HistoricalMemory.add() after archiving an entry
  ├── LAYER_ARCHIVED     → (Defined but never emitted - no callers exist)
  ├── BRANCH_CREATED      → New agent branch created (AgentContextManager)
  ├── BRANCH_MERGED       → Branch merged into parent (AgentContextManager)
  ├── SNAPSHOT_CREATED    → (Defined but never emitted - no callers exist)
  └── RETRIEVAL_COMPLETED → (Defined but never emitted - no callers exist)

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

### 2.7 Compression Flow (Single Trigger Point)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    COMPRESSION FLOW - SINGLE TRIGGER POINT                  │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    DCPOptimizer._compress_context()                     │
  │                                                                        │
  │   Entry Point: Called exclusively before LLM API calls                 │
  │   (No automatic triggering - fully controllable timing)                 │
  │                                                                        │
  │   Steps:                                                                │
  │   1. _sync_state_to_memory_manager()   ← Sync WNS, iteration, etc.   │
  │   2. _estimate_tokens()                 ← Calculate current token count │
  │   3. Build CompressionContext           ← Include tool_call_details,   │
  │                                           failed_strategies, etc.       │
  │   4. MemoryManager._compress(type)     ← "aggressive" or "smart"     │
  │   5. Return                             ← Done (no extra steps)       │
  └──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    MemoryManager._compress(type, context)                │
  │                                                                        │
  │   Steps (inside this method, single atomic operation):                   │
  │   1. Select strategy (AggressiveCompression or SmartCompression)        │
  │   2. strategy.compress()           ← Returns compressed message list   │
  │   3. HistoricalMemory.add(summary) ← Archive before clearing           │
  │   4. WorkingMemory.clear()         ← Clear all messages                │
  │   5. Add compressed messages back  ← Repopulate with compressed msgs  │
  │   6. EventBus.emit(CONTEXT_COMPRESSED) ← Notify listeners              │
  │                                                                        │
  │   NOTE: No additional message replacement needed after this returns     │
  └──────────────────────────────────────────────────────────────────────────┘

  Key Design Decisions:
  ├── Single Trigger: Compression ONLY via DCPOptimizer._compress_context()
  ├── No Auto-Trigger: MESSAGE_ADDED auto-subscription is DISABLED
  ├── No Redundant Ops: Messages already replaced inside _compress()
  ├── No Re-add Loop: _pull_compressed_messages_from_memory_manager() removed
  └── XML Compression: "xml_structured" type now supported in _compress() (was previously unreachable)
```

### 2.8 XML Formatting Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         XML Formatting & Parsing                             │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                     XMLMessageFormatter.format_for_llm()                    │
  │  (formatters.py)                                                         │
  │                                                                          │
  │  Input: list[Message]                                                    │
  │  Output: XML string with structure:                                      │
  │                                                                          │
  │  <llm_context>                                                           │
  │    <meta><token_budget/><format/>xml</format></meta>                    │
  │    <system_section><system_message><content/></content></system_message> │
  │    <conversation>                                                        │
  │      <turn role="" type=""><content/><metadata/></turn>                  │
  │    </conversation>                                                      │
  │    <output_format><instruction/></instruction></output_format>           │
  │  </llm_context>                                                          │
  └──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ (LLM Response)
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                     XMLResponseParser.parse()                             │
  │  (formatters.py)                                                         │
  │                                                                          │
  │  Input: XML string from LLM                                              │
  │  Output: ParsedResponse { content, tool_calls[], error }                  │
  │                                                                          │
  │  Supports:                                                               │
  │  <response>                                                              │
  │    <content>text response</content>                                      │
  │    <tool_calls><tool_call name="" status="">                              │
  │      <parameters/></parameters></tool_call></tool_calls>                 │
  │    <error><description/></error>                                        │
  │  </response>                                                             │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 2.9 XML Structured Compression Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    XML Structured Compression Flow                           │
│                    (context_manager/strategies/xml_structured_compress.py)   │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────────────┐
  │                  messages_to_xml(messages, context)                       │
  │                                                                          │
  │  Enhanced XML output with FPGA design context:                          │
  │                                                                          │
  │  <context>                                                               │
  │    <meta><token_count/><message_count/></meta>                          │
  │    <design_state>                                                        │
  │      <timing clock_period="" initial_wns="" best_wns="" current_wns=""/>│
  │      <iteration/>                                                        │
  │      <blocked_strategies>                                                │
  │        <strategy status="failed">route_design -directive Aggressive</strategy>
  │      </blocked_strategies>                                               │
  │    </design_state>                                                       │
  │    <system_messages>                                                     │
  │      <system_message priority="critical"><content/></system_message>     │
  │    </system_messages>                                                    │
  │    <conversation>                                                        │
  │      <turn index="" role="" type="">                                     │
  │        <topics><topic name="timing" score="2"/></topics>                 │
  │        <content/>                                                        │
  │        <importance/>                                                      │
  │      </turn>                                                             │
  │    </conversation>                                                       │
  │  </context>                                                              │
  └──────────────────────────────────────────────────────────────────────────┘

  Key Features:
  ├── FPGA design state section with timing metrics (WNS, TNS, clock_period)
  ├── Blocked strategies tracking (failed approaches to avoid)
  ├── Topic classification per message (placement/routing/timing)
  ├── Importance scoring preserved
  └── TopicClassifier and ImportanceScorer integrated
```

## 3. Key Data Classes

### 3.1 Core Interfaces (interfaces.py)

```python
Message             # role, content, name, tool_calls, tool_call_id, metadata
CompressionContext  # current_tokens, threshold_tokens, failed_strategies, 
                    # tool_call_details, best_wns, initial_wns, iteration
ContextEvent        # event_type, timestamp, data, source_agent_id
ContextSnapshot     # timestamp, layer, message_count, token_estimate, 
                    # agent_id, parent_snapshot_id
HistoricalEntry     # id, timestamp, content, importance_score, task_type,
                    # agent_id, tags, embedding
```

### 3.2 Configuration Classes

```python
MemoryManagerConfig       # working_config, historical_config,
                          # soft_threshold (80K), hard_limit (150K)

WorkingMemoryConfig       # max_tokens (80K), hard_limit_tokens (150K),
                          # recent_window (20), tool_result_truncate (30K)

HistoricalMemoryConfig    # max_entries (10K), relevance_threshold (0.5),
                          # age_based_decay (0.95)

XMLCompressionConfig      # token_budget (80K), preserve_turns (20),
                          # min_importance_threshold (0.3)
                          # Used by XMLStructuredCompressor
```

### 3.3 Formatter Classes (formatters.py)

```python
ToolCall              # name, parameters, status (from XML parsed response)
ParsedResponse        # content, tool_calls[], error (parsed LLM XML response)
XMLMessageFormatter   # format_for_llm(messages, token_budget) -> XML string
XMLResponseParser     # parse(text) -> ParsedResponse
```

## 4. File Responsibilities

| File | Responsibility |
|------|-----------------|
| `dcp_optimizer.py` | Main agent: LLM orchestration, tool calls, model selection; owns shared EventBus + AgentContextManager; triggers compression via `_compress_context()` (single compression trigger point) |
| `validate_dcps.py` | DCP equivalence validation (Phase1: structural, Phase2: simulation) |
| `context_manager/manager.py` | Central memory orchestration; `_compress()` selects strategy by `compression_type` param (supports "aggressive", "smart", "xml_structured"); `replace_all_messages()` for batch operations; no auto-subscribes to MESSAGE_ADDED |
| `context_manager/events.py` | Event bus: subscribe/unsubscribe/emit; shared by MemoryManager and AgentContextManager |
| `context_manager/formatters.py` | XMLMessageFormatter (formats messages as XML for LLM), XMLResponseParser (parses LLM XML responses) |
| `context_manager/estimator.py` | Token estimation using content-type-aware method (Chinese: 1.5 chars/token, English: 3.5, Code: 2.5, Digits: 4.0, Whitespace: 5.0); `estimate_context_complexity` integrated into `DCPOptimizer._estimate_context_complexity` for model routing |
| `context_manager/strategies/xml_structured_compress.py` | XMLStructuredCompressor with FPGA-aware XML output; includes design_state (timing, WNS, blocked strategies), topic classification, importance scoring; budget calculation fixed (system tokens properly subtracted before non-system message budget) |
| `context_manager/memory/working_memory.py` | Short-term message storage |
| `context_manager/memory/historical_memory.py` | Long-term archive with retrieval |
| `context_manager/stores/memory_store.py` | In-memory message store implementation |
| `context_manager/strategies/*.py` | Compression algorithms; smart_compress, aggressive_compress, xml_structured_compress |
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
                 dcp_optimizer      Compression
                 ._compress_context() Strategy.compress()
                          │             │
                          │             ▼
                          │    ┌─────────────────────────────┐
                          │    │  _compress(type) selects:    │
                          │    │  "aggressive" → Aggressive  │
                          │    │  "smart" → Smart            │
                          │    │  "xml_structured" → XML      │
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
                   │  NOTE: XMLStructuredCompressor budget bug fixed (system_tokens │
                   │        properly calculated before non-system budget)            │
└─────────────────────────────────────┘
```
