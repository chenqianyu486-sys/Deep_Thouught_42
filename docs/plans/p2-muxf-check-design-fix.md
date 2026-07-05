# P2 修复计划：MUXFTreeReorder 数据注入可见性 + check_design_status

## 背景

`dcp_optimizer_run-20260705_130916` 显示：
1. MUXFTreeReorder 执行了 4 轮却没有一次工具调用——LLM 花费了这些轮次尝试为 `critical_paths` 构建输入，但从未调用 `rapidwright_execute_muxf_tree_reorder_strategy`
2. 自动注入代码**已经存在于** [phase_execute.py:543-562](optimizer/nodes/subgraphs/phase_execute.py#L543-L562)，但 LLM 并不知道这一点
3. 在 EVALUATE 阶段，LLM 误读了 `check_design_status` 的输出（附录 A.1 bug 从未被修复）

## 子问题 1：MUXFTreeReorder FORMAT_GUARD 通知（方案 A）或关键路径自动刷新（方案 B，推荐）

### 方案 A（最小）：FORMAT_GUARD 通知

向 EXECUTE FORMAT_GUARD（[prepare_context.py](optimizer/nodes/prepare_context.py)）添加一个部分，告知 LLM 在调用组合逻辑策略工具之前不要提取数据：

```
AUTO-INJECTED STRATEGY DATA (DO NOT extract before calling execution tools):
  For netlist-modifying strategies (MUXFTreeReorder, CombinationalRebalance,
  LUTMUXFRepack, LUTCascade), critical_paths are automatically injected from
  verified state data. When one of these strategies is selected, simply call
  its execution tool — the system fills in critical_paths. Manual extraction
  via vivado_extract_critical_path_cells or design_data_read before tool
  invocation wastes rounds.
```

此方案添加了 4 个 token 行——非常小。

### 方案 B（更可靠）：MUXFTreeReorder 和类似策略的 EXECUTE 阶段关键路径实时刷新

与 ANALYZE/SELECT_STRATEGY 在阶段入口自动刷新 WNS 的做法类似（如果数据过时——约 0.5 秒），在 MUXFTreeReorder/LUTCascade/CombinationalRebalance/LUTMUXFRepack 的 EXECUTE 阶段入口自动刷新 `critical_paths`。这确保自动注入的数据始终是最新的，并且 LLM（在看到 `[fresh]` 仪表盘时）可以确信地直接调用工具。

`phase_execute.py` 中针对 EXECUTE 阶段入口的更改：
```python
# Auto-refresh stale critical paths for netlist-modifying strategies that
# need them (MUXFTreeReorder, LUTCascade, CombinationalRebalance, LUTMUXFRepack).
# These strategies' auto-inject mechanism fills critical_paths, but stale
# data causes the LLM to waste rounds on extraction before calling the tool.
_netlist_strategy_refresh_tools = {
    "rapidwright_execute_muxf_tree_reorder_strategy",
    "rapidwright_flatten_lut_cascade",
    "rapidwright_execute_combinational_rebalancing_strategy",
    "rapidwright_execute_lut_muxf_repack_strategy",
}
if strategy_name and strategy_tool in _netlist_strategy_refresh_tools:
    if state.timing.critical_paths_stale:
        await _auto_refresh_critical_paths(state, deps)
```

然后，`state.timing.critical_paths` 是 `[fresh]`，LLM 可以直接调用工具——无需手动提取。

**推荐：两个方案都实施。** 方案 A 告知 LLM 不需要做什么。方案 B 确保数据实际上是最新的（降低风险）。

## 子问题 2：修复 `check_design_status`（附录 A.1）

在 `VivadoMCP/vivado_mcp_server.py` 的第 2984-2998 行：

当前（有缺陷）：
```python
status_result = run_tcl_command("get_property STATUS [current_design]", timeout=timeout)
is_placed = ("place_design" in status_lower) or ("route_design" in status_lower)
is_routed = "route_design" in status_lower
```

修复：
```python
status_result = run_tcl_command("get_property STATUS [current_design]", timeout=timeout)
# IS_PLACED / IS_ROUTED are reliable even after open_checkpoint (STATUS may be
# empty for routed designs that were checkpointed and re-opened — A.1).
is_placed_result = run_tcl_command("get_property IS_PLACED [current_design]", timeout=5)
is_routed_result = run_tcl_command("get_property IS_ROUTED [current_design]", timeout=5)
is_placed = is_placed_result.strip() == "1"
is_routed = is_routed_result.strip() == "1"
```

同时修复第 543 行中同样使用 STATUS 的 `_update_design_state_from_vivado` 辅助函数。

## 子问题 3：MUXFTreeReorder `design_data_read` 避免系统告警

（次要——方案 B 后变为非必需。）

`design_data_read` 是一个不会修改的**内部**工具。系统在 EXECUTE 阶段禁止来自 LLM 的 `design_data_read` 调用（以阻止 LLM 浪费轮次），但……实际上，让我检查 MUXFTreeReorder EXECUTE 阶段是否允许 `design_data_read`。

从 tool_filter.py 查看 EXECUTE 工具白名单。如果 `design_data_read` 在白名单中，它将计入 `no_progress` 计数；如果不在，LLM 会在失败时收到 "tool not found"。无论哪种情况，自动注入 + FORMAT_GUARD 通知都能完全避免这个问题。

## 验证

1. `python3 -m pytest tests/test_context_engineering_fixes.py -q`——仍然通过（检查格式化防护更新后的包含/排除逻辑）
2. `python3 -m pytest optimizer/test_pure.py -q`——仍然通过（126 项）
3. `python3 skills/test_pblock_strategy.py`——仍然通过（34 项）
4. （可选）手动检查格式化防护输出：`python3 -c "from optimizer.nodes.prepare_context import build_phase_format_guard; from optimizer.pure.tool_filter import LoopPhase; print(build_phase_format_guard(LoopPhase.EXECUTE))" | grep -A3 AUTO-INJECTED`