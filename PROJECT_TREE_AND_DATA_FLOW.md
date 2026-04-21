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
│       └── aggressive_compress.py # AggressiveCompressionStrategy - heavy compression
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
│  │                        EventBus.emit(MESSAGE_ADDED)                      │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│         │                                                                     │
│         ▼                                                                     │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                  _check_compression(event) [SUBSCRIBED]                   │  │
│  │   - Returns early if _compression_strategy is None                      │  │
│  │   - If hard_limit exceeded → triggers aggressive compression immediately│  │
│  │   - If soft_threshold exceeded → triggers smart compression immediately │  │
│  │   Note: compression_type parameter passed to _compress() is not used;   │  │
│  │         actual strategy is determined by self._compression_strategy      │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│         │                                                                     │
│         ▼                                                                     │
│  (Later, before LLM call)                                                    │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                  dcp_optimizer._compress_context()                       │  │
│  │   - Sync state via _sync_state_to_memory_manager()                      │  │
│  │   - Build CompressionContext via _build_compression_context()            │  │
│  │   - Call _memory_manager._compress(context) [type param is ignored]    │  │
│  │     Note: the compression type string is passed but not used internally; │  │
│  │           the actual strategy comes from self._compression_strategy       │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│         │                                                                     │
│         ▼                                                                     │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                         _compress(context)                                │  │
│  │   1. Archive summary to HistoricalMemory                                 │  │
│  │   2. Clear WorkingMemory                                                  │  │
│  │   3. Repopulate with compressed messages                                  │  │
│  │   4. Emit CONTEXT_COMPRESSED event                                        │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
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
  │                  │      - get_context_for_model()
  └────────┬─────────┘      - estimate_tokens()
           │
           │  (when compression triggered by dcp_optimizer._compress_context())
           ▼
  ┌──────────────────┐
  │  Compression     │  ◄── Two strategies:
  │  Strategy        │      - SmartCompressionStrategy (soft threshold)
  │  .compress()     │      - AggressiveCompressionStrategy (hard limit)
  └────────┬─────────┘
           │
           ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                      SEQUENTIAL Operations:                              │
  │  1. Create summary from all messages                                    │
  │  2. HistoricalMemory.add(summary, importance=0.8)  ← Archive first       │
  │  3. WorkingMemory.clear()                        ← Then clear           │
  │  4. Add compressed messages back to store        ← Then repopulate      │
  │  5. EventBus.emit(CONTEXT_COMPRESSED)            ← Finally notify       │
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
  ├── MESSAGE_ADDED       → _check_compression() in MemoryManager (triggers compression when threshold exceeded)
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
```

## 4. File Responsibilities

| File | Responsibility |
|------|---------------|
| `dcp_optimizer.py` | Main agent: LLM orchestration, tool calls, model selection; owns shared EventBus + AgentContextManager |
| `validate_dcps.py` | DCP equivalence validation (Phase1: structural, Phase2: simulation) |
| `context_manager/manager.py` | Central memory orchestration, compression decisions |
| `context_manager/events.py` | Event bus: subscribe/unsubscribe/emit; shared by MemoryManager and AgentContextManager |
| `context_manager/estimator.py` | Token estimation using content-type-aware method (Chinese: 1.5 chars/token, English: 3.5, Code: 2.5, Digits: 4.0, Whitespace: 5.0); `estimate_context_complexity` integrated into `DCPOptimizer._estimate_context_complexity` for model routing |
| `context_manager/memory/working_memory.py` | Short-term message storage |
| `context_manager/memory/historical_memory.py` | Long-term archive with retrieval |
| `context_manager/stores/memory_store.py` | In-memory message store implementation |
| `context_manager/strategies/*.py` | Compression algorithms |
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
EventBus.emit(MESSAGE_ADDED)                          CompressionContext
         │                                                     │
         ▼                                                     ▼
_check_compression()                               (created but used
 triggers compression when threshold exceeded)     only by dcp_optimizer)
         │                                                     │
         │                                            ┌──────┴──────┐
         │                                            ▼             ▼
         │                                   dcp_optimizer      Compression
         │                                   ._compress_context() Strategy.compress()
         │                                            │             │
         │                                            └──────┬──────┘
         │                                                   │
         │                                                   ▼
         │                                   ┌─────────────────────────────────────┐
         │                                   │     SEQUENTIAL COMPRESSION:       │
         │                                   │  1. HistoricalMemory.add(summary)   │
         │                                   │  2. WorkingMemory.clear()          │
         │                                   │  3. Add compressed msgs back        │
         │                                   │  4. EventBus.emit(CONTEXT_COMPRESSED)
         │                                   └─────────────────────────────────────┘
```
