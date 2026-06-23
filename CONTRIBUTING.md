# Contributing

## Workflow

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Run the test suite: `make run_test_v2 DCP=demo.dcp`
4. Commit your changes: `git commit -m 'Add your feature'`
5. Push and open a Pull Request

## Test Modes

```bash
# Full V2 test (tools + skills + place/route)
make run_test_v2 DCP=demo_corundum_25g_misses_timing.dcp

# Skill-only test (fast, no place/route)
make run_skill_test_v2 DCP=demo_corundum_25g_misses_timing.dcp

# Unit tests (framework, no Vivado required)
python -m pytest skills/test_skill_framework.py -v
python -m pytest optimizer/test_graph.py -v
```

## Code Style

- **Simplicity First**: Minimum code that solves the problem. No speculative abstractions.
- **Surgical Changes**: Touch only what you must. Remove imports/variables/functions your changes made unused.
- **Pure Functions**: Wrap internal APIs with pure functions in `optimizer/pure/`, don't call directly from nodes.
- **No XML/YAML Fallback**: V2 only uses native LLM tool calls.

See [CLAUDE.md](CLAUDE.md) for full guidelines.

## Documentation

- [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md) — Module-level project structure and high-level data flow
- [architecture.md](architecture.md) — Implementation details (migration mapping, compression pipeline, message flow, SKILL_CHAIN, flow_control)
- [README.md](README.md) (中文) / [README_EN.md](README_EN.md) (English) — Getting started, architecture overview, design principles

## Adding New Strategies or Tools

When adding new strategies or tools, update the following locations:

| Addition | Update Required |
|----------|-----------------|
| New analysis tool | `PROTECTED_ANALYSIS_TOOLS` in `context_manager/strategies/yaml_structured_compress.py` |
| Dashboard-refreshing tool | `DASHBOARD_REFRESH_MAP` in `optimizer/pure/constants.py` |
| New strategy | `_is_failed_strategy_tool_result()` + `infer_strategy_from_tools()` in `optimizer/pure/iteration_logic.py` |
| Post-exec evaluation tool | `POST_EVAL_TOOLS` in `optimizer/nodes/subgraphs/llm_tool_loop.py` |
| Chained skill | `SKILL_CHAIN_ACTIONS` in `optimizer/pure/constants.py` |
| Skill timeouts | Three places: `@skill` decorator, JSON descriptor, test call timeout |

## Prerequisites

- Python 3.10+
- Vivado 2024.1+
- Java 11+ (for RapidWright)
- OpenRouter API key (for LLM access)
