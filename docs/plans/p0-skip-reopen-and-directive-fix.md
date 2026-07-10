# P0 致命问题修复计划：skip-reopen 基线污染 + 非法 directive

> 依据：`dcp_optimizer_run-20260711_015650/PROBLEM_REPORT.md` 的 P0-1、P0-2
> 目标：修复两个 P0 致命问题并增强鲁棒性，使后续策略基于正确基线决策、消除 directive 瞬时失败。

---

## 一、P0-1：skip-reopen 基线污染（dirty 标志方案）

### 根因
`_reload_baseline_on_switch` 的 skip 判定（[phase_execute.py:188](optimizer/nodes/subgraphs/phase_execute.py#L188)）仅比较 `current_dcp_path == 目标路径`。但 `current_dcp_path` 只在「成功写 best_checkpoint」或「显式 reopen」时更新。失败/无改善策略仍修改了 Vivado 内存设计，`current_dcp_path` 不变 → skip-reopen 错误跳过 → 读取脏内存的 WNS（-0.602 而非真实 -0.542），污染传播至后续所有策略。

### 方案（报告推荐 B：dirty 标志，更稳健）
新增 `live_design_dirty` 标志，语义：**Vivado 内存的时序相关状态是否已偏离 `current_dcp_path` 指向的文件**。skip-reopen 需同时满足「路径匹配」且「not dirty」。

### 编辑点
1. **[state.py:475](optimizer/state.py#L475)** `ControlState`：新增 `live_design_dirty: bool = False`。
2. **[phase_execute.py:186-202](optimizer/nodes/subgraphs/phase_execute.py#L186-L202)** `_reload_baseline_on_switch`：
   - skip 条件追加 `and not state.control.live_design_dirty`；当 dirty=True 时强制 reopen 并打印原因日志。
   - reopen 成功后（L202）置 `live_design_dirty = False`。
3. **[phase_execute.py:918-942](optimizer/nodes/subgraphs/phase_execute.py#L918-L942)** 主循环工具分发后：
   - 复用现有 `_design_changed` 检测。`_design_changed` 且非 `vivado_open_checkpoint` → `live_design_dirty = True`。
   - `tool_name == "vivado_open_checkpoint"` → 置 dirty=False，并同步 `current_dcp_path`（主循环原本漏更新此指针）。
4. **[phase_execute.py:2235-2244](optimizer/nodes/subgraphs/phase_execute.py#L2235-L2244)** chain 循环：`_track_wns_from_result` 之后追加 dirty 跟踪——`target_tool == "vivado_open_checkpoint"` → dirty=False（指针已在 L2239 更新）；`target_tool in DESIGN_MODIFICATION_TOOLS` → dirty=True。
5. **[phase_execute.py:1651](optimizer/nodes/subgraphs/phase_execute.py#L1651)** `_save_best_checkpoint`：写 best 后置 dirty=False（内存已与 best_checkpoint 文件一致）。
6. **[phase_execute.py:2070](optimizer/nodes/subgraphs/phase_execute.py#L2070)** `_restore_pre_chain_checkpoint`：reopen 后置 dirty=False。
7. **[rollback.py:57](optimizer/nodes/rollback.py#L57)**、**[init_analysis.py:144](optimizer/nodes/init_analysis.py#L144)**、**[save_output.py:92](optimizer/nodes/save_output.py#L92)**：reopen/write 后各置 dirty=False。

### 正确性验证（场景推演）
- iter1 PBLOCK 成功 → `_save_best_checkpoint` → dirty=False, path=best(-0.542)。
- 下个策略 Fanout 重入：path 匹配且 dirty=False → skip reopen ✓（内存确为 best）。
- Fanout chain 跑 place/route → dirty=True；无改善 → 不写 best → dirty 保持 True。
- 下个策略 LUTCascade 重入：path 匹配但 dirty=True → **强制 reopen** ✓（修复点），重载 best(-0.542)，基线正确。

### 边界说明
- pblock_tight 清理（[phase_execute.py:319](optimizer/nodes/subgraphs/phase_execute.py#L319)）是约束删除，不改布局/布线几何，WNS 不变；且它是直接 `call_tool_fn` 不走主循环 `_design_changed` 分支，故不置 dirty——这符合「时序相关状态未偏离」的语义，不会误触发 reopen。
- chain 失败时 `_restore_pre_chain_checkpoint` 已置 dirty=False，与现有回滚语义一致。

---

## 二、P0-2：非法 directive（白名单收紧 + 动态回退鲁棒性增强）

### 根因
1. `Congestion_Explore` 写死在 [strategy_library.py:343](strategy_library.py#L343)；它是 Vivado **策略预设名**而非 route_design `-directive`，被 2025.1 以 18-641 拒绝。
2. MCP 白名单 `ROUTE_SAFE_DIRECTIVES`/`PLACE_SAFE_DIRECTIVES` 自身含非法项（`Congestion_Explore`、`Congestion_NetDelay_*`、`NetDelay_high/medium/low`），本意 defense-in-depth 反成非法来源。
3. 无法离线运行 Vivado 导出精确合法列表 → 静态白名单可能再次漂移。

### 方案（静态收紧 + 动态回退双保险）
**A. 静态收紧**：移除已确认非法/误分类项。
**B. 动态回退**（鲁棒性核心）：place/route handler 检测到 Vivado 18-641「not a recognized directive」时，自动用无 directive 的默认命令重试一次，并把回退事实写回结果。即使白名单未来再漂移，策略也不再瞬时失败。

### 编辑点
1. **[vivado_mcp_server.py:87-100](VivadoMCP/vivado_mcp_server.py#L87-L100)** `PLACE_SAFE_DIRECTIVES`：移除 `NetDelay_high`、`NetDelay_medium`、`NetDelay_low`（报告确认 2025.1 拒绝）。保留 `Performance_NetDelay_*`（文档合法，漂移由动态回退兜底）。
2. **[vivado_mcp_server.py:103-112](VivadoMCP/vivado_mcp_server.py#L103-L112)** `ROUTE_SAFE_DIRECTIVES`：移除 `Congestion_Explore`、`Congestion_NetDelay_high/medium/low`（策略预设名误入 route 白名单）。
3. **[vivado_mcp_server.py:2501-2534](VivadoMCP/vivado_mcp_server.py#L2501-L2534)** `place_design` handler：抽取 directive 到变量；`run_tcl_command` 后若 `_is_unrecognized_directive_error(output)` 且原 directive 非空 → 用 `place_design`（无 directive）重试一次，结果前置 `[FALLBACK]` 说明。
4. **[vivado_mcp_server.py:2565-2596](VivadoMCP/vivado_mcp_server.py#L2565-L2596)** `route_design` handler：同上动态回退。
5. 新增模块级纯函数 `_is_unrecognized_directive_error(output: str) -> bool`：匹配 `18-641` 与 `not a recognized directive`（可单测）。
6. **[vivado_mcp_server.py:1853](VivadoMCP/vivado_mcp_server.py#L1853)** `route_design` 工具描述：删除 `Congestion_Explore/_NetDelay_*` 宣传。
7. **[strategy_library.py:343](strategy_library.py#L343)** CongestionRouteExplore 的 route directive：`Congestion_Explore` → `AlternateRoutability`（拥塞/可布线性主题最契合的合法 route directive；动态回退兜底任何不确定性）。
8. **[prepare_context.py:200,207](optimizer/nodes/prepare_context.py#L200)** system prompt 指引：删除 place 的 `NetDelay_high/medium/low`、route 的 `Congestion_Explore / Congestion_NetDelay_*`。
9. **[RapidWrightMCP/server.py](RapidWrightMCP/server.py)** 8 处 route_directive 描述（L730/764/806/871/929/983/1058/1125）：删除 `Congestion_Explore, Congestion_NetDelay_high/medium/low`。

### 设计决策
- **为何 `AlternateRoutability` 而非 `Explore`**：`PlaceRouteDirectiveExplore` 已用 `Explore`；CongestionRouteExplore 用 `AlternateRoutability` 区分且更贴合拥塞主题。两者均为标准合法 route directive，动态回退兜底。
- **为何保留 `Performance_NetDelay_*`**：文档列为合法；run-20260710_002051 的 `Performance_NetDelay_high` 失败证据模糊（可能是设计状态相关），动态回退已能处理，不必激进移除丧失有用 directive。
- **动态回退只针对 18-641**：真实 place/route 失败（如设计无法布局）不触发回退，避免无谓重试浪费时间。

---

## 三、测试

新增 `tests/test_p0_robustness_fixes.py`：
- **P0-1**：(a) `live_design_dirty` 默认 False；(b) 模拟 chain 工具修改后 dirty=True 时，`_reload_baseline_on_switch` 的 skip 条件不成立（用纯函数化判定或直接断言状态字段）；(c) reopen/best_save 后 dirty 复位为 False。
- **P0-2**：(a) 断言 `NetDelay_*` 不在 `PLACE_SAFE_DIRECTIVES`、`Congestion_*` 不在 `ROUTE_SAFE_DIRECTIVES`；(b) `_is_unrecognized_directive_error` 对 18-641 返回 True、对普通 ERROR 返回 False；(c) `strategy_library` CongestionRouteExplore 的 route directive == `AlternateRoutability`。

> MCP handler 的完整重试行为依赖 `run_tcl_command`（模块级、连 Vivado），单测聚焦可纯函数化的检测逻辑与静态集合，与现有测试风格一致（参考 `test_context_engineering_fixes.py`）。

---

## 四、执行顺序与验证

1. P0-1 state.py 加字段 → 验证：`python -c "from optimizer.state import OptimizerState; s=OptimizerState(); assert s.control.live_design_dirty is False"`
2. P0-1 phase_execute.py + 三个 node 文件的 dirty 置位/复位 → 验证：跑相关单测。
3. P0-2 vivado_mcp_server 白名单 + 动态回退 + 描述 → 验证：`_is_unrecognized_directive_error` 单测 + 白名单断言。
4. P0-2 strategy_library + prepare_context + RapidWrightMCP 描述 → 验证：策略 directive 断言。
5. 全量回归：`python -m pytest tests/ -q`（WSL 内）。
6. 同步更新文档：README.md / PROJECT_TREE_AND_DATA_FLOW.md / architecture.md（记录 dirty 标志不变量与 directive 动态回退）。

---

## 五、不改动的范围（surgical）
- 不重构 `_reload_baseline_on_switch` 其余逻辑、不改 pblock_tight 清理。
- 不删除既有 dead code、不改无关注释格式。
- 不触碰 P1/P2 问题（超出本次「两个 P0」范围）。
- `Performance_NetDelay_*` 暂保留（动态回退兜底）。
