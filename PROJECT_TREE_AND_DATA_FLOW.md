# FPL26 Optimization Contest - Project Structure & Data Flow

## 1. Project Tree

```
fpl26_optimization_contest/
├── dcp_optimizer.py              # Main agent: LLM orchestration, model selection, compression trigger
├── config_loader.py             # Model configuration loader (singleton)
├── model_config.yaml             # Model tier and fallback configuration
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
│       └── yaml_structured_compress.py  # YAML compression, preserve_turns from model_config (40/60)
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
    - **429 Fallback**: Per-tier fallback model lists with round-robin and exhaustion tracking
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
    - force_aggressive=False → preserve_turns from model_config, min_threshold from model_config
    - force_aggressive=True  → preserve_turns=10, min_importance=0.7
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
  preserve_turns: 40 (normal) / 10 (aggressive)
  min_importance: 0.15 (normal) / 0.7 (aggressive)
  fallback_models:  # 429 rate limit fallback (priority order)
    - "qwen/qwen3.6-flash"
    - "xiaomi/mimo-v2-flash"

# Pro (Planner): xiaomi/mimo-v2.5-pro, 1M max
pro:
  soft_threshold: 120K
  hard_limit: 300K
  token_budget: 100K
  preserve_turns: 60 (normal) / 10 (aggressive)
  min_importance: 0.1 (normal) / 0.7 (aggressive)
  fallback_models:  # 429 rate limit fallback (priority order)
    - "deepseek/deepseek-v4-pro"
    - "qwen/qwen3.6-plus"
```

## 6. File Responsibilities

| File | Responsibility |
|------|-----------------|
| `dcp_optimizer.py` | Main agent; owns EventBus + AgentContextManager; single compression trigger; `latest_wns` cache; file handles via exit_stack.callback() |
| `config_loader.py` | ModelConfigLoader singleton; loads model_config.yaml; provides flash/pro config including fallback_models |
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
| Tool round limit | Increased from 15 to 22 per iteration |
| Iteration checkpoint check | Mandatory checkpoint save + get_wns check before proceeding to next iteration |
| WNS regression rollback | If WNS < 0 and worse than best, auto-rollback to best checkpoint; completion check uses `latest_wns` not `best_wns` |
| 429 rate limit fallback | Separate fallback model lists per tier; correct log shows rate-limited model; mark both original and fallback as exhausted; _select_model() filters exhausted models |

## 10. Iteration Exit Conditions

```
Iteration proceeds to next round if ALL conditions met:
1. iteration < 50 (safety limit)
2. WNS < 0.0 ns (not timing-converged)
3. global_no_improvement < 5 (not at hard limit)
4. Tool rounds <= 22 (max tool-calling rounds per iteration)
5. [NEW] Checkpoint saved successfully (via _save_intermediate_checkpoint)
6. [NEW] get_wns returns valid WNS value

Iteration ends when:
- is_done = (wns_target_met OR max_iterations_reached)
- Checkpoint + get_wns check FAILED → iteration skipped (continue, no counter update)

Completion judgment uses latest_wns (current state), NOT best_wns (historical best)
WNS regression (WNS < 0 and worse than best) triggers automatic rollback to best checkpoint
```

## 11. Key Constants

```python
# From model_config.yaml
# Flash (Worker): deepseek/deepseek-v4-flash, 500K context
FLASH_SOFT_THRESHOLD = 40K
FLASH_HARD_LIMIT = 100K
FLASH_FALLBACK_MODELS = ["qwen/qwen3.6-flash", "xiaomi/mimo-v2-flash"]
# Pro (Planner): xiaomi/mimo-v2.5-pro, 1M context
PRO_SOFT_THRESHOLD = 120K
PRO_HARD_LIMIT = 300K
PRO_FALLBACK_MODELS = ["deepseek/deepseek-v4-pro", "qwen/qwen3.6-plus"]

# Iteration control
MAX_TOOL_ROUNDS_PER_ITERATION = 22  # Max tool-calling rounds per iteration
GLOBAL_NO_IMPROVEMENT_LIMIT = 5      # Hard limit for consecutive no-improvement
WNS_TARGET_THRESHOLD = 0.0           # WNS target (0.0 ns = timing converged)
```

## 12. 429 Rate Limit Fallback Mechanism

```
On 429 error:
1. Save rate_limited_model BEFORE reassignment (fixes log showing wrong model)
2. Mark rate_limited_model as exhausted (original model, not just fallback models)
3. Try next fallback model from current tier's list (round-robin)
4. Mark next_fallback as exhausted too
5. If all fallbacks exhausted → switch to model_planner
6. Clear BOTH exhausted sets (flash and pro) for clean slate with planner

_select_model() also checks if model_worker is exhausted:
- If model_worker in _exhausted_flash_fallbacks or _exhausted_pro_fallbacks → force planner
- Prevents immediately re-selecting a model that just hit 429

Key state variables:
- _flash_fallback_index / _pro_fallback_index: round-robin position
- _exhausted_flash_fallbacks / _exhausted_pro_fallbacks: track exhausted models

Model tier inference (_infer_model_tier):
- Uses exact matching for known models (pro: xiaomi/mimo-v2.5-pro, deepseek/deepseek-v4-pro, qwen/qwen3.6-plus)
- flash: deepseek/deepseek-v4-flash, qwen/qwen3.6-flash, xiaomi/mimo-v2-flash
- Generic fallback: "pro"/"planner" → pro tier, "flash"/"worker" → flash tier

Example log after fix:
"Rate limit on deepseek/deepseek-v4-flash, switching to fallback: qwen/qwen3.6-flash"
```

## 13. Console Exit Intervention

User can type `quit` in the console to request graceful exit without killing the process.

```
Implementation:
- _user_exit_requested: threading.Event flag
- _start_console_reader(): daemon thread reading stdin for "quit"
- _check_exit_requested(): returns True if exit was requested

Exit checkpoints:
- optimize() while loop: checks before iteration starts → saves checkpoint + summary
- get_completion() tool_round loop: checks between tool rounds → breaks loop gracefully
```

See [docs/CONSOLE_EXIT.md](docs/CONSOLE_EXIT.md) for usage details.
```