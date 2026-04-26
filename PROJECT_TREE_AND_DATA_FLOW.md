# FPL26 Optimization Contest - Project Structure & Data Flow

## 1. Project Tree

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # Main agent: LLM orchestration, model selection, compression trigger
├── validate_dcps.py               # DCP equivalence validator (Phase1 structural, Phase2 simulation)
├── SYSTEM_PROMPT.TXT             # System prompt
├── requirements.txt
│
├── context_manager/               # Memory management module
│   ├── manager.py                # MemoryManager - central orchestration, single _compress() trigger
│   ├── estimator.py              # TokenEstimator (tiktoken)
│   ├── events.py                 # EventBus - subscribe/unsubscribe (ref + token-based)
│   ├── lightyaml.py              # YAML parser (pyyaml backend)
│   ├── interfaces.py             # Core data classes (Message, CompressionContext, etc.)
│   ├── agent_context.py          # AgentContextManager - multi-agent branching
│   ├── compat.py                 # DCPOptimizerCompat - legacy adapter
│   ├── memory/
│   │   ├── working_memory.py     # Short-term storage
│   │   └── historical_memory.py  # Long-term archive with retrieval
│   ├── stores/
│   │   └── memory_store.py       # InMemoryContextStore
│   └── strategies/
│       └── yaml_structured_compress.py  # YAML compression (preserve_turns=20 or 3 via force_aggressive)
│
├── RapidWrightMCP/               # RapidWright MCP server
│   ├── server.py
│   └── rapidwright_tools.py      # Tool wrappers with device/tile caching
│
├── VivadoMCP/
│   └── vivado_mcp_server.py      # Vivado MCP server
│
└── RapidWright/                  # RapidWright SDK (vendor)
```

## 2. Core Data Flow

### 2.1 Agent Orchestration

```
DCPOptimizer (dcp_optimizer.py)
├── RapidWright MCP Session
├── Vivado MCP Session
├── MemoryManager (shared EventBus)
├── AgentContextManager (shared EventBus)
└── Model Selection: PLANNER vs WORKER
    - 8-dimension weighted scoring
    - PLANNER: xiaomi/mimo-v2.5-pro (1M context, complex reasoning)
    - WORKER: deepseek/deepseek-v4-flash (500K context, fast execution)
    - Threshold: margin of 2 for PLANNER, margin of 1 for WORKER, default=PLANNER
    - **Intra-iteration switching**: When task category changes (INFORMATION↔OPTIMIZATION) during tool execution loop, model is re-evaluated and switched if needed
```

### 2.2 Message Flow

```
add_message(role, content)
         │
         ▼
WorkingMemory.add_message()  # No auto-compression
         │
         ▼
DCPOptimizer._compress_context()  ← SINGLE trigger point (before LLM call)
         │
         ▼
MemoryManager._compress("yaml_structured", context)
         │
         ▼
YAMLStructuredCompressor.compress()
    - force_aggressive=False → preserve_turns=20, min_threshold=0.3
    - force_aggressive=True  → preserve_turns=3, min_threshold=0.8
```

### 2.3 Sequential Compression (inside MemoryManager._compress)

```
1. Separate system messages (protected)
2. HistoricalMemory.add(summary, importance=0.8)  ← Archive first
3. WorkingMemory.clear()                          ← Then clear
4. Add system + compressed non-system messages    ← Repopulate
5. EventBus.emit(CONTEXT_COMPRESSED)
```

### 2.4 Tool Call WNS Tracking

```
WNS captured via 3 routes:
1. phys_opt auto-eval (regex WNS.*?([-\d.]+))
2. vivado_report_timing_summary (parse_timing_summary_static)
3. vivado_get_wns (direct float conversion)

Validation: _is_valid_wns()
- abs(wns) > clock_period * 10 → REJECT
- wns < -999 → REJECT (parsing error)
```

### 2.5 Compression Context

```python
CompressionContext(
    current_tokens, threshold_tokens, hard_limit_tokens,
    failed_strategies, tool_call_details,
    best_wns, initial_wns, current_wns, clock_period,
    iteration, force_aggressive, retrieved_history, agent_id
)
```

## 3. Event System

```python
EventBus (events.py)
├── subscribe(event_type, handler) → token
├── subscribe_global(handler) → token
├── unsubscribe_by_token(token)
├── unsubscribe_global_by_token(token)
└── emit(event)

EventTypes:
├── CONTEXT_COMPRESSED   # After compression (data: original_tokens, compressed_tokens, ratio)
├── LAYER_PROMOTED       # After HistoricalMemory.add()
├── BRANCH_CREATED       # New agent branch
└── BRANCH_MERGED        # Branch merge
```

## 4. Key Interfaces

```python
MemoryLayer enum    # WORKING, HISTORICAL, ARCHIVE
MessageRole enum    # SYSTEM, USER, ASSISTANT, TOOL

Message( role, content, name, tool_calls, tool_call_id, metadata )
CompressionContext( ... )
HistoricalEntry( id, timestamp, content, importance_score, task_type, agent_id, tags, embedding )
RetrievalQuery( text, task_type, time_range, min_importance, limit, agent_id )
```

## 5. Configuration (from model_config.yaml)

```yaml
# Flash (Worker): deepseek/deepseek-v4-flash, 500K max
flash:
  soft_threshold: 40K
  hard_limit: 100K
  token_budget: 35K
  preserve_turns: 25 (normal) / 5 (aggressive)
  min_importance: 0.15 (normal) / 0.7 (aggressive)

# Pro (Planner): xiaomi/mimo-v2.5-pro, 1M max
pro:
  soft_threshold: 120K
  hard_limit: 300K
  token_budget: 100K
  preserve_turns: 40 (normal) / 5 (aggressive)
  min_importance: 0.1 (normal) / 0.7 (aggressive)
```

## 6. File Responsibilities

| File | Responsibility |
|------|-----------------|
| `dcp_optimizer.py` | Main agent; owns EventBus + AgentContextManager; single compression trigger; `latest_wns` cache; file handles via exit_stack.callback() |
| `SYSTEM_PROMPT.TXT` | System prompt with YAML format docs |
| `validate_dcps.py` | DCP validation (--phase1-only for intermediate) |
| `context_manager/manager.py` | `_compress()` only uses "yaml_structured"; no auto-MESSAGE_ADDED subscribe |
| `context_manager/events.py` | EventBus with token-based unsubscribe |
| `context_manager/lightyaml.py` | YAML parser (pyyaml backend), FPGA signal names |
| `context_manager/estimator.py` | tiktoken token counting |
| `context_manager/strategies/yaml_structured_compress.py` | YAML compression, roundtrip validation via YAML_ROUNDTRIP_VALIDATE env var |
| `RapidWrightMCP/rapidwright_tools.py` | Device/tile caching for search_sites/get_tile_info |

## 7. DCP Validation Integration

```
Every N iterations (default 5): Phase 1 structural check
On is_done: Phase 1 + Phase 2 (full simulation)
Phase 1 failure → warning to LLM context (non-blocking)
```

## 8. Prompt Logger

```
Output: fpl26-prompts.log
Format: Header + messages + separator
Truncation: >5000 chars → first 2500 + "..." + last 2500
```

## 9. Recent Improvements

| Issue | Fix |
|-------|-----|
| File handle leak | exit_stack.callback() for guaranteed cleanup |
| Bare except | Changed to `except Exception` |
| Routing detection | `ROUTING_FAILURE_PHRASES` constant + `_is_routing_failure()` |
| WNS lookup | O(1) via `latest_wns` cache |
| -0.0 normalization | vivado_mcp_server.py normalizes to +0.0 |
| RapidWright caching | device_sites_cache + tile_info_cache |
| Device context | proactive injection via device_topology in initial analysis |
| Intra-iteration model switch | Re-evaluate model when task category changes during tool execution loop |

## 10. Key Constants

```python
# From model_config.yaml
# Flash (Worker): deepseek/deepseek-v4-flash, 500K context
FLASH_SOFT_THRESHOLD = 40K
FLASH_HARD_LIMIT = 100K
# Pro (Planner): xiaomi/mimo-v2.5-pro, 1M context
PRO_SOFT_THRESHOLD = 120K
PRO_HARD_LIMIT = 300K
```