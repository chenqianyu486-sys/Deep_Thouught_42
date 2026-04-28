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
│       ├── yaml_structured_compress.py  # YAML compression base class with iteration/WNS trend scoring
│       ├── planner_compress.py         # PlannerCompressor: token_budget=100K, preserve_turns=60, min_importance=0.1, max_chars_multiplier=1.0
│       └── worker_compress.py          # WorkerCompressor: token_budget=35K, preserve_turns=20, min_importance=0.35, max_chars_multiplier=0.5
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
MemoryManager._compress("yaml_structured", context, model_tier)
         │
         ├─ model_tier="planner" → PlannerCompressor (preserve_turns=60, min_importance=0.1)
         ├─ model_tier="worker" → WorkerCompressor (preserve_turns=20, min_importance=0.35)
         └─ model_tier=None → YAMLStructuredCompressor (default fallback)

YAMLStructuredCompressor (default):
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

### 2.4 WNS State Injection (Anti-Compression-Loss)

```
Before LLM API call:
api_messages = get_formatted_for_api()
         │
         ▼
_inject_wns_state_to_system_prompt()  ← Injects current/best WNS into system message
         │
         ├── Updates wns_ns in YAML metadata section (current_wns)
         ├── Updates clock_period_ns in YAML metadata section
         └── Appends/Updates "Current Optimization State:" block:
             - iteration, current_wns, best_wns, clock_period
             - input_dcp, best_checkpoint (with iteration info)
         │
         ▼
API call with updated system message (model always sees current state)
```

**Problem solved**: After context compression, the LLM may lose track of current WNS state.
**Solution**: System prompt is dynamically updated before each LLM call to ensure
the model always has access to current_wns, best_wns, and checkpoint information.

### 2.5 Tool Call WNS Tracking

```
WNS captured via 3 routes:
1. phys_opt auto-eval (regex WNS.*?([-\d.]+))
2. vivado_report_timing_summary (parse_timing_summary_static)
3. vivado_get_wns (direct float conversion)

Validation: _is_valid_wns()
- abs(wns) > clock_period * 10 → REJECT
- wns < -999 → REJECT (parsing error)
```

### 2.6 Compression Context

```python
CompressionContext(
    current_tokens, threshold_tokens, hard_limit_tokens,
    failed_strategies, tool_call_details,
    best_wns, initial_wns, current_wns, clock_period,
    iteration, force_aggressive, retrieved_history, agent_id,
    model_context_config, model_switch_detected, previous_model_tier  # Model-aware compression
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
  cost_hard_limit: $1.00 (combined planner+worker)
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
  cost_hard_limit: $1.00 (combined planner+worker, same as flash)
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
| `validate_dcps.py` | DCP validation (supports --vectors for custom vector count) |
| `context_manager/manager.py` | `_compress()` only uses "yaml_structured"; no auto-MESSAGE_ADDED subscribe |
| `context_manager/events.py` | EventBus with token-based unsubscribe |
| `context_manager/lightyaml.py` | YAML parser (pyyaml backend), FPGA signal names |
| `context_manager/estimator.py` | tiktoken token counting |
| `context_manager/strategies/yaml_structured_compress.py` | YAML compression base class with iteration-aware and WNS-trend scoring, O(1) message selection |
| `RapidWrightMCP/rapidwright_tools.py` | Device/tile caching for search_sites/get_tile_info |

## 7. DCP Validation Integration

```
Every N iterations (validation_interval=5): Validation (500 vectors) on intermediate checkpoint - failure triggers rollback + skip iteration
On is_done: Final full validation (Phase 1 + Phase 2, 10000 vectors)
```

Validation trigger conditions (all must be true):
- validation_enabled = True
- intermediate_dcp exists (not output_dcp, which is only written at completion)
- not is_done (skip on final iteration)
- iteration % validation_interval == 0 (run every 5 iterations by default)

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
| Long tool heartbeat | Tool calls running >60s print `[HEARTBEAT #N] Tool 'xxx' still running after Xs` every minute |
| Compression code duplication | PlannerCompressor/WorkerCompressor now inherit compress() from YAMLStructuredCompressor base class (~170 lines reduced) |
| Compression O(n²) lookup | Replaced O(n) list membership with O(1) set for selected message IDs |
| Iteration-aware scoring | Current iteration messages get 1.5x weight, previous iteration 1.2x |
| WNS trend scoring | Tool results boosted 1.2x when WNS improving, 1.3x when degrading |
| max_chars_multiplier | Planner=1.0 (full), Worker=0.5 (aggressive truncation) |
| Per-iteration validation | Fixed validation trigger: uses intermediate_dcp (not output_dcp), runs every 5 iterations (validation_interval), failure triggers rollback |
| Dead code cleanup | Removed unused `_run_phase1_validation()` method |
| EventBus memory leak | DCPOptimizer.cleanup() now unsubscribes EventBus handlers to prevent memory leak |
| ContextSnapshot.restore() | Now raises NotImplementedError (was no-op, now fail-fast) |
| WorkingMemory capacity check | add_message() now checks token limits and logs warnings |
| HistoricalMemory index | Changed from list to set for O(1) membership and consistent eviction |
| Tool call name preservation | Compression now preserves tool_call function names in YAML output |
| Dead config fields | Added DEPRECATED comments to unused recent_window, tool_result_truncate, relevance_threshold, age_based_decay |
| Fallback model_worker update | After successful fallback, model_worker is updated to current model so _select_model() checks correct exhausted status |

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

# Cost control
COST_HARD_LIMIT = 1.00              # USD hard limit (combined planner+worker)
```

## 12. 429 Rate Limit Fallback Mechanism

```
On 429 error:
1. Save rate_limited_model BEFORE reassignment (fixes log showing wrong model)
2. Mark rate_limited_model as exhausted
3. Try next fallback model from current tier's list (round-robin)
4. [FIX] Only mark next_fallback as exhausted when it ACTUALLY hits 429
5. [FIX] Clear last_exception on successful fallback to prevent stale exception
6. If all fallbacks exhausted → switch to model_planner
7. Clear BOTH exhausted sets (flash and pro) for clean slate with planner

_select_model() also checks if model_worker is exhausted:
- If model_worker in _exhausted_flash_fallbacks or _exhausted_pro_fallbacks → force planner
- Prevents immediately re-selecting a model that just hit 429
- [FIX] After successful fallback, model_worker is updated to current model so this check uses correct status

Key state variables:
- _flash_fallback_index / _pro_fallback_index: round-robin position
- _exhausted_flash_fallbacks / _exhausted_pro_fallbacks: track exhausted models

Model tier inference (_infer_model_tier):
- Uses exact matching for known models (pro: xiaomi/mimo-v2.5-pro, deepseek/deepseek-v4-pro, qwen/qwen3.6-plus)
- flash: deepseek/deepseek-v4-flash, qwen/qwen3.6-flash, xiaomi/mimo-v2-flash
- Generic fallback: "pro"/"planner" → pro tier, "flash"/"worker" → flash tier

Example log after fix:
"Rate limit on deepseek/deepseek-v4-flash, switching to fallback: qwen/qwen3.6-flash"
"HTTP Request: POST ... 200 OK" ← qwen/qwen3.6-flash succeeds, last_exception cleared
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

## 14. Exit Reason Tracking

When optimization ends, the reason is logged via `_is_done_reason` and printed to console as `[Exit reason: ...]`.

| Reason | Description |
|--------|-------------|
| `cost_limit` | Reached $1.00 USD hard limit (planner+worker combined) |
| `wns_target_met` | WNS >= 0.0 ns (timing converged) |
| `max_iterations_reached` | 5 consecutive iterations without improvement |
| `tool_round_limit` | 22 tool-calling rounds per iteration reached |
| `user_requested` | User typed `quit` to exit |

```python
# In get_completion(), exit reason is logged before return:
reason = self._is_done_reason or ("wns_target_met" if is_done and wns_target_met else "max_iterations_reached")
logger.info(f"get_completion exit: reason={reason}, is_done={is_done}, WNS={self.best_wns:.4f}")
print(f"[Exit reason: {reason}]")
```
```