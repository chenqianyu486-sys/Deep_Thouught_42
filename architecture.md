# FPL26 优化竞赛 — 架构技术细节

> **读者对象：** 深度贡献者和调试者。本文档包含所有实现级技术细节——压缩管线、消息流、SKILL_CHAIN 自动链、flow_control 信号处理、验证守卫等。高层架构概览和模块结构请见 [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md)，快速上手见 [README.md](README.md)。

---

## 1. 核心数据流（消息管线）

### 1.1 消息压缩与注入管线

```
add_message(role, content) → WorkingMemory.add_message()  # 无自动压缩
  ↓ (token超阈值) → MemoryManager._compress("yaml_structured", ...)
  → YAMLStructuredCompressor: 归档→清空→YAML摘要→保留最近6条原始role

_prepare_api_messages() 每轮LLM调用前:
  1. get_formatted_for_api() → 从MemoryManager获取消息列表
  2. _auto_compact_messages() ← 轻量去重(重复REFLECTION/REPETITION/FORMAT→仅最新; 连续同名工具→仅最后)
  3. 增强系统提示词(scenario hint + skill catalog)
  4. 注入迭代交接提示词
  5. inject_merged_dashboard(): 7-module StateSpace 仪表板（phase-aware，含 field_freshness）
  → LLM API Call
```

压缩参数（来自 [model_config.yaml](model_config.yaml)，Worker 250K/Planner 1M max）:
- 正常模式: preserve_turns=40/min_importance=0.15/preserve_role_turns=6 (worker), preserve_turns=60/min_importance=0.1/preserve_role_turns=6 (planner)
- 激进模式(hard_limit触发): preserve_turns=25(worker)/40(planner), min_importance=0.35(worker)/0.25(planner)
- system消息始终保护
- preserve_role_turns=6: 最近6条消息保留原始API role（user/assistant/tool），不塞进YAML
- 两轮预算分配: 60%高重要性 + 40%中等重要性
- preserve_turns预留预算: ~1500 tokens/turn, 最多10K
- 工具调用保留参数（最多5个）

### 1.2 顺序压缩流程

```
1. 分离system消息（受保护）
2. HistoricalMemory.add(summary, importance=0.8)  ← 先归档
3. WorkingMemory.clear()                        ← 再清空
4. 添加system + YAML摘要（旧消息）              ← YAML压缩
5. 添加最近 preserve_role_turns=6 条消息        ← 保留原始 role（user/assistant/tool）
    注：`getattr(self, 'preserve_turns', 3)`，回退值硬编码为 3
```

### 1.3 时序报告智能截断（5项改进）

1. **动态预算**: 根据模型层级的 token_budget 动态分配时序报告空间
2. **阈值过滤**: 只保留 slack 低于阈值的路径
3. **起终点成对**: 截断时保持 startpoint/endpoint 配对完整
4. **时钟域分组**: 按 path_group 分组压缩，避免混合
5. **回退保护**: 解析失败时安全回退到原始文本

**过时时序报告替换**: 迭代 < current_iteration-1 的长时序报告 → `[Outdated timing report from iteration N]`

**失败策略工具消息提前压缩**: 已知失败策略的工具结果不受迭代年龄限制，直接压缩为 `[SYSTEM COMPRESSED TOOL: name (iteration N)]` 标记

**反"鬼打墙"机制**（2026-05 新增）：
- 受保护工具列表 `PROTECTED_ANALYSIS_TOOLS`（frozenset）：分析型工具不被压缩为标记，保留完整 YAML 摘要
- 标记格式改为 `[SYSTEM COMPRESSED TOOL: ...]`，明确标注系统主动压缩而非截断
- 压缩发生后注入 `SYSTEM NOTICE: ...` 通知消息

### 1.4 WNS/TNS 状态注入机制

```
API调用前 → _inject_wns_state_to_system_prompt()
    - 追加数据驱动scenario hint（avg_distance>70 → "distributed"场景 → PBLOCK推荐）
    - ~~追加analysis skill guide（get_skill_guide()，一次性注入含"Skill Catalog"标记）~~（已移除，SKILL_GUIDANCE 死代码清理）
    → 仅处理静态上下文增强，不再注入WNS状态

WNS动态状态 → 已迁移至 _inject_dashboard_at_end()，作为 user message 注入
```

### 1.5 Agent 上下文快照注入机制

```
V2 tool loop 每轮 LLM 调用前 → inject_pinned_cell_registry() + inject_merged_dashboard()
    ↓
inject_pinned_cell_registry(api_messages, state):       # Pinned 层（L2，2026-06 新增）
    1. build_registry_snapshot_yaml(state.entity_registry) → [CELL REGISTRY] YAML
    2. 移除已有 [CELL REGISTRY] header 消息（幂等，防累积）
    3. 插入为 system 消息之后的独立 user 消息
    → 不进入 MessageStore，天然抗压缩；每轮从 state.entity_registry 重建
    ↓
inject_merged_dashboard(api_messages, state, phase):    # Dynamic 层（L3）
    1. build_state_space(state) → 7-module StateSpace
    2. format_state_space_for_llm() → YAML 仪表板（phase-aware 模块过滤）
    3. inject_context_snapshot_at_end(api_messages, snapshot):
       a. 查找已有 Dashboard header → 移除旧版
       b. 追加为最后一条 user 消息（最大注意力权重）
    → 每次 API 调用最多一条快照消息，零残留
```

Dashboard 7 模块 StateSpace 的详细格式、字段映射和 phase-aware filtering 见 [PROJECT_TREE_AND_DATA_FLOW.md §3.2](PROJECT_TREE_AND_DATA_FLOW.md)。

**关键设计要点（架构实现层面）**：
- **LLM 防歧义注解**: `_annotated_list()` / `_annotated_val()` 辅助函数确保 [] 和 N/A 都携带机器可读原因
- **类型契约**: Dashboard 严格区分 `None`（未分析）与 `[]`/`0`（已分析但为零）
- **无持久化**: Dashboard 快照不进入 MessageStore，完全绕过压缩系统，每次 API 调用从当前状态重建；全量设计数据通过 `DesignDataManager` 持久化到 `{run_dir}/design_data/`（2026-07 新增），LLM 通过 `design_data_read` 内部工具访问
- **截断透明化（2026-07 新增）**: Dashboard 在被截断的模块后添加未显示数据的聚合统计（`unshown_path_stats`、`unshown_hotspots`、`unshown_high_fanout_nets`），末尾添加 `truncation_advisory` 区段列出截断量与存取指南；聚合统计基于全量 `critical_paths` 纯函数计算，零 MCP 开销，均带 `[fresh]`/`[stale]` 新鲜度标注
- **无策略引导**（2026-06）：`design_delay_profile` 不再附带 `strategy_hint`，`_append_architecture_hints()` 重命名为 `_append_architecture_insights()`，仅输出纯数据描述
- **设计状态标注（DesignState 枚举）**: 从 `report_timing_summary` 的 `Design State` 字段解析，设置 `state.timing.design_state` 为 `DesignState.UNPLACED`（未布局） / `PLACED`（仅布局） / `ROUTED`（已布线）。Dashboard M1 根据状态显示不同粒度的警告：UNPLACED→"WNS based on wireload estimates"，PLACED→"WNS based on estimated routing delays"。非 ROUTED 状态时 Level 1 RW 预检查自动跳过。
  - **解析失败保留策略（2026-07，P1 修复）**: `parse_design_state()` 在 `Design State` 字段缺失时返回 `None` 而非默认 `UNPLACED`，调用方保留上次已知状态。`vivado_route_design`/`vivado_physopt_and_route` 执行后显式置 `ROUTED`（它们必然产生已布线结果），`vivado_phys_opt_design` 保留原状态。修复了 `physopt_and_route` 后误翻转为 UNPLACED、对真实布线后 WNS 误报"线负载估计"的灾难性 bug（见 run-20260703_142810）。Dashboard 警告加守卫：`design_state=UNPLACED` 但存在真实 `wns_setup` 时降级为温和提示，不再误报线负载。
- **比赛时钟处理**: `init_analysis` 通过 `get_clocks` + `get_property PERIOD` 提取时钟周期，用于 Fmax 计算（`Fmax = 1000 / (period - WNS)`）。Dashboard Module 5 的时钟名从 `state.timing.critical_paths[0].clock.source_clock` 动态提取（2026-07 修复，此前硬编码 `clk_fpl26contest`），无 critical_paths 时回退到该默认名。
- **`do_not_repeat` 推导**: 从 `state.iteration.tools_used` 聚合被调用 > 3 次且 WNS delta < 0.01ns 的工具，最多 5 条
- **`strategy_catalog` 排除机制（2026-07 更新）**: `strategy_ineffective`（TTL=1）、`no_improvement`（TTL=3）、`strategy_not_applicable`（TTL=5）在 catalog 中标为 `[BLOCKED]` 占位符（含剩余轮数/原因）；`tool_error` 完全移出 catalog（无 TTL，可立即重试）。排除逻辑在 `inject_merged_dashboard()` 中拆分 hard-exclude vs blocked 两组。
- **`field_freshness` 逐字段新鲜度追踪**: `refreshed_fields: set[str]` 升级为 `field_freshness: dict[str, str]`，为每个Dashboard字段独立追踪 `"fresh"`/`"stale"` 状态。`init_analysis` 完成后全部初始化为 `fresh`；工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段为 `fresh`；设计修改工具（`DESIGN_MODIFICATION_TOOLS` 共23个，2026-06-27 补充5个缺失工具）执行后全部降级为 `stale`（EXECUTE 和 EVALUATE 两阶段均处理）。Dashboard 中每个值后显示 `[fresh]`/`[stale]` 标记，供LLM决策是否信任。
- **TTL 机制（按原因分级，2026-07 重构）**: `_ttl_for_reason()` 函数统一计算各失败原因的冷却期（`blocked_until_iter` 字段）：`strategy_ineffective`→1 轮、`strategy_not_applicable`→5 轮、`no_improvement`→3 轮后自动解封；`tool_error`→无 TTL（`blocked_until_iter=current`，立即重试）。`record_strategy_failure` 去重时刷新 `blocked_until_iter`。
- **策略选择质量修复（2026-07-10，run-20260710_132555 复盘）**:
  - `strategy_not_applicable` TTL 从 2 提升到 `STRATEGY_NOT_APPLICABLE_TTL=5`：结构性不适用（技能无可用目标）比 `no_improvement`（跑了无收益）信号更强，冷却应更长。此前 MUXFTreeReorder 在 iter1 失败（TTL=2）后于 iter3 被重试并以同样方式失败。
  - `cell_type_chain` 优先使用 `PathNode.cell_type`（真实 Vivado 类型）而非名称启发式：MUXF7/MUXF8 cell 名为 `*_reg[..]_i_*`（匹配 LUT 的 `_i_` 模式），启发式误标为 LUT，导致 dashboard chain 与 CELL REGISTRY 自相矛盾，误导 MUXF 策略选择。
  - NetSwap 依赖工具暴露：`rapidwright_execute_net_swapping` 需 `rapidwright_analyze_net_swapping` 产出 candidates，但后者此前未在任何阶段暴露。新增 `STRATEGY_DEPENDENCY_TOOLS` 映射，EXECUTE 阶段为 NetSwap 额外暴露 analyze 工具；catalog trigger 同步提示两步调用。

---

## 2. 状态管理

### 2.1 动态 Critical Path 管理

**数据结构**（[optimizer/state.py](optimizer/state.py)）：

```python
@dataclass
class PathNode:
    """单节点（cell 或 net）及其逐点延迟分解。D1 上下文改进。"""
    kind: str = ""              # "cell" | "net"
    name: str = ""
    cell_type: str = ""         # LUT6/CARRY8/FDRE/MUXF7/...
    location: str = ""          # SLICE_X91Y106 / DSP48E2_X10Y46 (cell only)
    incr_delay: Optional[float] = None   # 增量延迟 (ns) — 关键诊断字段
    cumul_delay: Optional[float] = None  # 累计到达时间 (ns)
    fanout: Optional[int] = None         # net fanout (net only)
    net_status: str = ""                 # "routed" | "unset" (net only)

@dataclass
class ClockDomainInfo:
    """D2: 单条时序路径的时钟域上下文。"""
    source_clock: str = ""
    dest_clock: str = ""
    path_group: str = ""
    clock_skew: Optional[float] = None
    clock_uncertainty: Optional[float] = None
    is_cross_clock: bool = False

@dataclass
class CriticalPathEntry:
    cells: list[str]
    path_length: int = 0
    iteration: int = 0
    slack: Optional[float] = None
    logic_delay: Optional[float] = None
    net_delay: Optional[float] = None
    levels: Optional[int] = None
    nodes: list[PathNode] = field(default_factory=list)      # D1
    startpoint: str = ""
    endpoint_pin: str = ""
    top_delay_nodes: list[PathNode] = field(default_factory=list)  # top-3 by incr_delay
    clock: ClockDomainInfo = field(default_factory=ClockDomainInfo)  # D2
```

**D1/D2 上下文**: D1 保留每 cell 的 `incr_delay`/`cell_type`/`location`；D2 解析 `Clock Path Skew`/`Clock Uncertainty`/`Source`/`Destination`/`Path Group` 替代旧字符串猜测。

**ViolationSummary**: EXECUTE/EVALUATE 使用紧凑摘要(Module 2b, ~200-300 tokens)替代展开 ~3000+tokens。

**双重更新**: 被动(LLM调`vivado_extract_critical_path_cells`) + 主动(phys_opt/place/route后设`stale`, 自动调用`_auto_refresh_critical_paths()`)。

**纯函数** ([critical_path.py](optimizer/pure/critical_path.py) + [entities.py](optimizer/pure/entities.py)): `parse_critical_path_cells()` / `update_critical_paths()` / `format_critical_paths_snapshot` / `validate_critical_path_data()` / `heuristic_cell_type()`。细胞名验证 `is_valid_cell_name()` 的 SSOT 实现已迁移至 `entities.py`（`critical_path.py` 通过 re-export 保持向后兼容）；拒绝 `pblock_*`/`SLICE_X*Y*` 等非细胞模式，>50% 无效整条跳过。`update_critical_paths()` 解析后同步写入 `state.entity_registry`（SSOT + Pinned 层）。

**展示位置**:
| 位置 | 来源 | 限制 |
|------|------|------|
| Context Dashboard | `format_state_space_for_llm()` | top 8, 6 cells/path |
| Planner Handoff | `_generate_planner_handoff()` | top 5, 6 cells/path |
| Worker Handoff | `_generate_worker_handoff()` | top 3, 6 cells/path |

### 2.2 关键信息保护（实现机制表）

| 类型 | 存储位置 | 保护机制 |
|------|----------|----------|
| System消息 | Working memory（受保护） | 压缩前分离，始终前置 |
| WNS/TNS/策略状态 | 上下文Dashboard（user message，独立于压缩系统） | 每 API 调用前通过 `format_state_space_for_llm()` → `inject_merged_dashboard()` 注入 |
| 失败策略 | CompressionContext | 存入YAML输出；`record_failure()` 在8个检测点被调用 |
| Tool调用摘要 | state.iteration.tools_used | 工具名直接追加到 state |
| 最近N轮消息 | Working memory（role保留） | preserve_role_turns=6 |
| report_step_state 格式 | User message + System prompt 头部 | 双重提醒 |
| 工具缓存 | `state.context.tool_cache` | 同 phase 内相同参数自动命中；执行工具后 `clear()` |
| 工具调用频率限制 | `state.context.tool_phase_call_counts` | 超限返回 `[RATE LIMITED]` |
| 动态 Critical Path | `state.timing.critical_paths` | 被动更新 + 主动更新 |
| 工具重复检测 | DCPOptimizer._recent_tools（滑动窗口） | >=3次相同工具+总WNS变化<0.05ns → REPETITION DETECTED |
| 周期反思 | get_completion() 内嵌 | 每8个 tool_round 注入 REFLECTION CHECKPOINT |
| DCP 身份 | EXECUTE 阶段从白名单移除 `vivado_open_checkpoint` | 确保不重新打开 DCP |
| TCL 关键路径提取拦截 | `tool_router.py:vivado_run_tcl` 内容匹配 | 检测 `get_timing_paths`+`get_cells` 模式，返回 `[AUTO-GUIDANCE]` |
| PBLOCK 数据质量提前退出 | `phase_execute.py` | `critical_path_cells` 全部过滤时跳过 MCP，记录 `data_quality_error` |
| 冷却逻辑分层 | `phase_evaluate.py` | 区分策略工具错误(`STRATEGY_TOOL_NAMES`) vs 辅助工具错误 |
| 策略目录分层暴露 | `inject_merged_dashboard()` | `strategy_ineffective`（TTL=1）+ `no_improvement`（TTL=3）+ `strategy_not_applicable`（TTL=5）标为 `[BLOCKED]` 占位符；`tool_error` 完全移出；`get_strategy_catalog(blocked_strategies=...)` 渲染 |
| 空结果模式匹配 | `iteration_end.py` | `optimized_count: 0` → `tool_error`（可重试）非 `strategy_ineffective`（永久排除） |
| Improvement 阈值（三档语义）| `STRATEGY_IMPROVEMENT_EPSILON_NS=0.050` | `delta>0` 不冷却（best_checkpoint 已保存）；`delta>0.050` 重置无进展计数；`delta≤0` 计无进展。边际正收益 (0,0.050] 不惩罚 |

---

## 3. Skill 框架实现细节

### 3.1 三层加载机制

1. **Discovery**: `lazy_loader._ensure_index()` regex 扫描 `skills/*.py` → `name→module` 映射；`SkillRegistry.discover_all()` 读取 `descriptors/*.json` → 供 LLM 路由匹配
2. **Activation**（按需 import）: `SkillRegistry.get(name)` → `lazy_loader.activate(name)` → `importlib.import_module()` → `@skill` 装饰器触发注册
3. **Execution**: `skill.execute_with_telemetry()` → 幂等性检查 → Heartbeat(30s) → 协程安全网 → telemetry 记录 → `SkillResult`

**关键变更**: `skills/__init__.py` 启动 0 导入；JSON descriptor 由 `@skill` 装饰器自动导出；所有 `get()` 调用不变，内部触发懒加载。

### 3.2 Skill 超时三层模型

每 Skill 有三层超时：**@skill decorator**（心跳阈值）→ **JSON descriptor** `defaultMs/maxMs` → **asyncio.wait_for**（实际截止时间）。典型值：分析型 30-60s，执行型 120-300s（PBLOCK/Fanout/CongestionSpreading 可达 300s）。完整映射见 `skills/*.py` + `skills/descriptors/*.json`。

### 3.3 SKILL_CHAIN_ACTIONS 自动工具链执行

```python
SKILL_CHAIN_ACTIONS: dict[str, list[dict]] = {
    "rapidwright_execute_pblock_strategy": [
        # 局部 unplace：仅关键路径 cell（2026-07-04 改造，原为全局 place_design -unplace）
        {"tool": "vivado_unplace_cells",
         "args_from_skill": {"cells": "critical_path_cells"}},
        {"tool": "vivado_create_and_apply_pblock",
         "args_from_skill": {
             "pblock_name": "pblock_name",
             "ranges": "pblock_ranges",
             "is_soft": "is_soft_recommended",
             "cells": "critical_path_cells",   # 局部 pblock：只约束关键 cell
         }},
        {"tool": "vivado_place_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "place_directive"}},
        {"tool": "vivado_route_design", "args": {"directive": "Explore"},
         "args_from_skill": {"directive": "route_directive"}},
        {"tool": "vivado_report_timing_summary", "args": {}},
    ],
}
```

**route_design 路由复用**（2026-07-05 修订）：Vivado `route_design` 无 `-reuse` 选项，对未变更网线自动保留布线，无需任何 flag。早期版本在 chain config 设 `reuse: True` 并由 `phase_execute.py` 的 route-reuse guard 发射 `-reuse`，被 Vivado 以 `Unknown option '-reuse'` 拒绝，导致 PBLOCK/physopt 链在 route 步必然失败回滚。现已移除全部 `reuse` 配置、guard 与 MCP `reuse` 参数（`test_no_chain_sets_reuse_flag` 覆盖）。

**执行流程**：
```
Skill execute() 返回 SkillResult(success=True, data={...})
         ↓
llm_tool_loop._execute_chain_actions()
         ↓
遍历 SKILL_CHAIN_ACTIONS[tool_name]:
  ├── 从 skill_result_data 解析 args_from_skill 参数
  ├── 调用 call_tool_fn() 执行 Vivado MCP 工具
  ├── summarize_tool_result() + add_message("user", "[AUTO-CHAIN] ...")
  ├── _track_wns_from_result() 更新 WNS 状态
  └── state.iteration.tools_used.append(target_tool)
         ↓
链式执行完成 → LLM 在下一轮对话中看到 [AUTO-CHAIN] 结果
```

**与 `POST_EVAL_TOOLS` 的区别**：
| 机制 | 触发 | 执行内容 |
|------|------|---------|
| `POST_EVAL_TOOLS` | 指定工具执行后 | 仅 `report_timing_summary`（单一评估） |
| `SKILL_CHAIN_ACTIONS` | 指定 Skill 返回后 | 完整工具链（多步串联，含参数传递） |

**关键设计**：
- `args_from_skill`: 参数从 Skill 返回值中动态提取
- Chain 步骤状态检测: 每个 chain 步骤检查 `"error"` 键，有错则中断 chain
- 错误恢复: 链式动作开始前保存快照，单步失败时自动恢复
- Critical path stale 标记: `vivado_place_design` 和 `vivado_create_and_apply_pblock` 执行后自动标记 `critical_paths_stale = True`
- 空结果 chain-skip 守卫: 执行 P&R 链前 `should_skip_chain_for_empty_result` 检测 Skill 是否产出可执行数据，`status in ("skipped","no_action","unchanged")` 或既无 `optimized_cells`/`critical_paths` 也无 plan 风格 `steps` 时跳过链（避免 ~17s 无效回滚）。plan 风格输出（`status:"ready"` + 非空 `steps`）视为有数据（见 §15.10）

### 3.4 关键路径感知的 PBLOCK 区域选择

**动机**：PBLOCK 区域选择原仅基于资源容量。区域评分函数中距离权重仅 `0.001`，导致区域选择完全由"最小宽度"决定，忽略与关键路径的邻近度。

**方案**：
1. **数据源**: `phase_execute.py` 自动从 `state.timing.critical_paths` 提取 cell 名列表，注入到工具参数
2. **质心计算**: `smart_region_search._compute_critical_path_center_from_cells()` 通过 RapidWright API 查找各 cell 的 tile 坐标，取算术平均
3. **评分函数**: `_score(width, dist) = width + dist * distance_weight_factor`（默认 `distance_weight_factor=0.3`）
4. **参考点优先级**: 关键路径 cell 质心 > 显式传入坐标 > 全局 cell 质心

**自动注入逻辑（始终覆盖，2026-06 统一为注册表过滤）**:
```python
if tool_name in ("rapidwright_execute_pblock_strategy", "rapidwright_analyze_pblock_region"):
    if state.timing.critical_paths or state.entity_registry.cells:
        # 统一使用 extract_registry_cells_for_inject()（entities.py SSOT）
        cells = extract_registry_cells_for_inject(state.entity_registry, state.timing.critical_paths)
        tool_args["critical_path_cells"] = cells  # 始终覆盖 LLM 提供的数据
```
> **数据完整性保护（2026-06）**: LLM 可能通过 TCL 提取含扇出分支的污染数据。上述注入已改为**始终覆盖** LLM 提供的参数，并记录 warning 日志 + 向 LLM 上下文注入 `[DATA INTEGRITY]` 警告。同一机制也应用于 `critical_paths` 参数（CombinationalRebalance / LUTMUXFRepack / MUXFTreeReorder / LUTCascade 策略）。**2026-06 增强**：注入逻辑统一改用 `extract_registry_cells_for_inject()`（注册表 + critical paths），替换原 pblock 子串过滤与零过滤的不一致实现；`rapidwright_optimize_cell_placement` 新增同源 auto-inject（详见 §12.4）。

> **区域尺寸绑定 cell 化（2026-07-05）**：上述注入的 `critical_path_cells` 不仅用于质心定位，还是 pblock 的**绑定对象**（chain 仅 `add_cells_to_pblock(cells=critical_path_cells)`，见 [constants.py](optimizer/pure/constants.py) `SKILL_CHAIN_ACTIONS`）。但 `generate_pblock_plan` 原先用 `target_lut_count`（全设计 LUT）× multiplier 计算区域容量，导致「能装下整个设计的大区域 + 只绑 50 个 cell + is_soft=True」的零约束空壳（见 `dcp_optimizer_run-20260705_130916`：63652-LUT 区域仅绑 50 cell，+0.049ns 实为 P&R 噪声）。现改为：当 `critical_path_cells` 可解析（≥50% 匹配）时，`_estimate_bound_cell_resources()` 按 cell 类型（`LUT*`→luts、`FD*`→ffs、`MUXF*`→luts、`DSP*`→dsps、`RAMB*`→brams）求和，区域尺寸 = 绑定 cell 资源 × multiplier；`utilization_density` 改为**绑定 cell 占区域容量的真实密度**（不再是全设计/区域）；`is_soft` 随之基于真实密度判定（绑定 cell 少 → 低密度 → 硬 pblock `IS_SOFT=0`，真正起约束作用）。不可解析时回退全设计尺寸（`sizing_basis="whole_design"`）。新增 result 字段 `sizing_basis`/`bound_resources`/`bound_cell_count` 透出尺寸依据；`next_steps` 文案对齐为 `unplace_cells(cells=critical_path_cells)`。`adaptive_multiplier` 仍用 `target_lut_count`（全设计）做 small/medium/large 分类——这是全设计属性。

### 3.5 Auto-chain Directive Tuning

The Vivado place-and-route auto-chain mechanism now supports LLM-tunable place/route directives. Two changes were implemented:

**Bug fix — opt/physopt directive passthrough**: `_strategy_plan_to_dict` in `RapidWrightMCP/rapidwright_tools.py` previously only emitted `directive`/`retarget` nested under `analysis_summary`, but the chain executor (`SKILL_CHAIN_ACTIONS` in `optimizer/pure/constants.py`) performs flat top-level key lookup — so the LLM's chosen `opt_design`/`phys_opt_design` directive was silently lost and always fell back to `"Explore"`. Fixed by flattening `directive`, `retarget`, `place_directive`, `route_directive` from `analysis_summary` to the top level in `_strategy_plan_to_dict`.

**New feature — LLM-tunable place/route directives**: The eight skill wrappers (pblock, physopt, opt_design, combinational_rebalancing, lut_muxf_repack, muxf_tree_reorder, fanout, flatten_lut_cascade) now accept optional `place_directive`/`route_directive` arguments. A new helper `_attach_chain_directives()` echoes them into the skill result JSON at top level. The chain steps in `SKILL_CHAIN_ACTIONS` gained `args_from_skill: {"directive": "place_directive"}` / `{"directive": "route_directive"}` mappings, so the LLM's value overrides the hardcoded `"Explore"` when present, and `"Explore"` remains the fallback when omitted. The LLM is guided via a new `"PLACE/ROUTE DIRECTIVE TUNING"` section in the EXECUTE phase prompt (`optimizer/nodes/prepare_context.py`). Safe directive whitelists (`PLACE_SAFE_DIRECTIVES`/`ROUTE_SAFE_DIRECTIVES` in `VivadoMCP/vivado_mcp_server.py`) are enforced at the MCP server level.

**Design choice**: "full freedom + safety fallback" — the LLM can freely pick any whitelisted directive per call; when it omits, the chain falls back to `Explore`. The `register_retiming` chain was skipped (FORBIDDEN — breaks cycle-exact equivalence).

**Tier-2 strategy-default fallback (NEW)**: When the LLM omits the `place_directive`/`route_directive` parameters, the chain executor (`_execute_chain_actions` in `optimizer/nodes/subgraphs/phase_execute.py`) now consults `STRATEGY_DEFAULT_DIRECTIVES` in `optimizer/pure/constants.py` before falling back to hardcoded `"Explore"`. This dict maps each strategy's chain key to a `(place_default, route_default)` tuple drawn from the `PR_DIRECTIVE_COMBINATIONS` scenario catalog, matching each strategy's typical bottleneck. For example: `opt_design`/`combinational_rebalancing`/`flatten_lut_cascade` → `("ExtraTimingOpt", "NoTimingRelaxation")` for logic-depth-limited designs; `physopt`/`muxf_tree` → `("Explore", "Explore")`; `pblock`/`fanout` → route-only `(None, "NoTimingRelaxation")`. A guard — `"args_from_skill" in step` — prevents the special pblock "unplace" step (`directive: "unplace"` with no `args_from_skill`) from being overridden. `place=None` indicates that strategy's chain has no `place_design` step. This activates the previously-dormant `PR_DIRECTIVE_COMBINATIONS` catalog as a live mechanism, establishing a three-tier precedence: LLM override > strategy default > hardcoded `"Explore"`.

---

## 4. 迭代控制实现细节

### 4.1 迭代边界模型切换

**机制**:
- `iteration_end_node` 调用 `_select_model()` 决定下一迭代模型，存入 `_next_iteration_model`
- 下一迭代 `get_completion()` 开头直接使用预定模型
- 交接提示词迭代结束时生成，模型分层专属

**交接提示词**:
- **Planner** (~600-1000 tokens): EXIT REASON → CONTINUATION DIRECTIVE → ITERATION TRAJECTORY → CURRENT STATE → CRITICAL PATHS (top 5) → NEXT GOAL → LAST ITERATION TOOLS → FAILED STRATEGIES → RECOMMENDED SKILL → STAGNATION → SKILL INVOCATIONS → INCOMING MODEL
- **Worker** (~300-500 tokens): CONTINUATION → EXIT LABEL → RECENT TRAJECTORY (last 3) → STATE → CRITICAL PATHS (top 3) → GOAL → LAST TOOLS → AVOID → RECOMMENDED SKILL → STAGNATION → SKILL INVOCATIONS
- **首次迭代**: 注入 `**FIRST ITERATION** - Begin with initial design analysis...`

**辅助数据**: `_iteration_narratives[]`（最多 20 条）、`_build_tool_effect_summary()`（最近 8 条）、`_build_failed_strategy_summary()`（最近 5 条）

### 4.2 Step 状态追踪（`report_step_state` Tool）

每轮 LLM 响应到达后，先从 `message.tool_calls` 中提取 `report_step_state` 参数，flow_control 优先于工具执行。

**流程**：
```
LLM response arrives
  ↓
1. 扫描 message.tool_calls 寻找 report_step_state
   ↓ 找到
   解析 JSON arguments → StepState
   将 report_step_state 从 tool_calls 中移除（不进入工具执行）
   ↓ 未找到
   StepState 保持为空（flow_control=None，纯文本视为 CONTINUE）
   ↓
2. 如果 flow_control ∈ {DONE, SWITCH_STRATEGY, NEXT_ITERATION}
   → 跳过工具执行，跳转到 flow_control 处理
   else if tool_calls → 正常执行工具
   else （纯文本） → 现有纯文本处理逻辑
```

**StepState 数据结构**：
```python
@dataclass
class StepState:
    step_id: Optional[int] = None
    result_status: Optional[str] = None
    flow_control: Optional[str] = None
    has_tool_calls: bool = False
    raw_content: str = ""
    strategy_phase: Optional[str] = None
    strategy_name: Optional[str] = None
```

### 4.3 Flow Control 信号处理

所有信号通过 `record_flow_signal()` 录制到 `state.context.flow_control_log`。控制台统一 `[FC]` 日志格式。

**行为矩阵**：
| 场景 | 行为 |
|------|------|
| `flow_control: ANALYZE_DONE` | 切换到 SELECT_STRATEGY |
| `flow_control: EXEC_DONE` | 切换到 EVALUATE |
| `flow_control: DONE`，WNS<0 | 进入下一迭代 |
| `flow_control: DONE`，WNS>=0 | 退出优化 |
| `flow_control: SWITCH_STRATEGY` (EVALUATE) | 多策略循环回 SELECT_STRATEGY（最多 5 轮/迭代） |
| `flow_control: NEXT_ITERATION` (EVALUATE) | 结束迭代 + 不记录失败 |
| `flow_control: CONTINUE` (EVALUATE) | 回到 ANALYZE |
| `flow_control: ROLLBACK` (EVALUATE) | 回滚到最佳 checkpoint |
| `detect_rollback_needed()` | WNS 退化时自动设 done_reason=rollback |
| 无 tool_calls，无 DONE 信号 | 继续循环（纯文本处理）|
| `ANALYZE_DONE` (EVALUATE 误发) | 映射为 SWITCH_STRATEGY（冷却停滞策略 + 推进） |
| `EXEC_DONE` (EVALUATE 误发) | 映射为 NEXT_ITERATION |
| `flow_control: EXHAUSTED` (任意阶段) | 设 `is_done=True`/`done_reason=strategies_exhausted`，终止优化（2026-07-09 修复：EXECUTE 中 EXHAUSTED 此前仅 break 出循环，落入 EVALUATE 后被 no-progress 强制 SWITCH，忽略 LLM"策略穷尽"判定；现在三处入口 EXECUTE/SELECT_STRATEGY/EVALUATE 均设 is_done） |

### 4.4 失败策略追踪

**`record_failure()` 的触发点**：

| 触发点 | 记录的 reason |
|--------|-------------|
| SWITCH_STRATEGY 处理 | `strategy_ineffective` |
| 工具调用超时 | `tool_error` |
| 工具调用异常 | `tool_error` |
| 工具结果含错误 | `tool_error` |
| PBLOCK validation_failed | `execution_failure` |
| Fanout后评估缺失 | `execution_failure` |
| 路由失败 | `execution_failure` |
| 策略中断检测 | `execution_failure` |
| 工具返回空结果 | `tool_error` |

**`_EMPTY_RESULT_PATTERNS` 空结果模式匹配**：当 LLM 调用 `SWITCH_STRATEGY` 且工具输出包含 `"0 candidates"` / `"no candidates"` / `"optimized_count": 0` 等模式时，归类为 `tool_error`（无 TTL，立即重试）而非 `strategy_ineffective`（1 轮冷却）。

**按原因分级 TTL（2026-07 `_ttl_for_reason()` 函数统一计算）**：
- `reason="strategy_ineffective"` → `blocked_until_iter = current + 1`（1 轮冷却）
- `reason="strategy_not_applicable"` → `blocked_until_iter = current + 5`（5 轮冷却）
- `reason="no_improvement"` → `blocked_until_iter = current + 3`（3 轮冷却）
- `reason ∈ {tool_error, execution_failure, unknown}` → `blocked_until_iter = current`（无 TTL，立即重试）

**冷却逻辑分层**（`_cool_down_current_strategy_if_stalled()`）：
1. 定义 `STRATEGY_TOOL_NAMES` 常量，包含 16 个策略执行工具
2. 检查 `state.iteration.tool_errors` 中的错误工具名是否在 `STRATEGY_TOOL_NAMES` 中
3. **策略工具本身失败** → 策略未获公平执行机会 → **跳过冷却**
4. **仅辅助工具失败** → 策略已执行 → **应用冷却**
5. Improvement 阈值从 0.001ns 提升至 **0.050ns**

**`_infer_strategy_from_tools()` 策略推断映射**：
- PBLOCK → 工具名含 `pblock`
- PhysOpt → 工具名含 `phys_opt`
- Fanout → 工具名含 `fanout` 或 `optimize_fanout`
- CongestionSpreading → 工具名含 `congestion_spread`
- PlaceRoute → 工具名含 `place_design` 或 `route_design`

**历史黑洞修复**（2026-07-09）：迭代内 SWITCH_STRATEGY 直接回 SELECT_STRATEGY，**绕过 iteration_end**，故中途切换的无改进策略从不进入 `failed_strategies`，LLM 在 `strategy_outcomes` 中看不到已试策略（iter2 实测 6 个策略仅 1 个可见）。现在 `_handle_switch_strategy` 在切换时记录当前策略：若未已记录且未成功（不在 `optimization_history`），按 `delta≤0` 记 `no_improvement`、`delta=None` 记 `tool_error`。仅**新增**记录，不覆盖 EXECUTE 已记的更具体分类。

**失败分类不覆盖**（2026-07-09）：`record_strategy_failure` 去重时，若新分类的 TTL **低于**现有分类（即试图把 EXECUTE 的语义化冷却降级为 `tool_error` 立即可重试），**保留**现有更严格分类。例如 EXECUTE 记 `strategy_not_applicable`（TTL=5），iteration_end 空结果重扫试图改记 `tool_error`（TTL=0），保留 `strategy_not_applicable`。仅当新 TTL ≥ 现有 TTL（同等或更严格）时才覆写并刷新 TTL。

**失败归因**（2026-07-09）：iteration_end 失败记录的 `tool` 字段取 `get_strategy_primary_tool(strategy)`（策略主执行工具），而非 `tools_used[:3]`（跨阶段、跨策略循环累积列表，曾把 CellReplication 的 `vivado_physopt_and_route` 错归到 Fanout 名下）。无策略映射时回退 `tools_used[:3]`。

### 4.4.1 策略自动阻断（Post-eval UNCHANGED/REGRESSED，2026-07）

`phase_execute.py` 的 `_post_eval_hook()` 和直接工具评估段中，当 post-eval verdict 为 `UNCHANGED` 或 `REGRESSED` 时，自动将当前策略加入 `state.iteration.blocked_strategies`，阻止本迭代内同一策略的重复执行：

```python
if verdict in ("UNCHANGED", "REGRESSED"):
    if state.strategy.current_strategy not in state.iteration.blocked_strategies:
        state.iteration.blocked_strategies.append(state.strategy.current_strategy)
```

**设计意图**：策略执行后 WNS 无改善或倒退 → 继续使用该策略预计不会带来益处。自动阻断替代 LLM 的判断，减少因 LLM 重复无效策略导致的工具调用浪费。

### 4.5 report_step_state 格式提醒（双重提醒）

**提醒 1 — System Message（每 phase 注入，phase-specific）**: FORMAT_GUARD 由 `build_phase_format_guard(phase)` 动态生成 BASE + per-phase addendum，在 `inject_merged_dashboard()` 中注入为 system message（幂等 marker 去重）。包含输出格式、EXECUTE 工具映射（仅 EXECUTE phase）、DESIGN CONSISTENCY 要求、CELL NAME CONTRACT。EVALUATE addendum 新增决策指引表（2026-07），预编码各 verdict 对应的推荐 flow_control 信号（IMPROVED→CONTINUE/SWITCH, UNCHANGED→SWITCH, REGRESSED→SWITCH/ROLLBACK），减少 LLM 决策迷茫。

**提醒 2 — 工具 schema 自描述**: `report_step_state` 工具的 parameters 定义包含完整描述，`filter_tools_for_phase()` 按阶段动态 patch flow_control enum。

### 4.6 工具重复检测器

```python
_recent_tools: list[tuple[str, float]]  # 滑动窗口 (tool_name, wns)，最多5条
```
**检测条件**: 连续 >=3 次相同工具 + WNS 总变化 < 0.05ns
**触发时行为**: 注入 REPETITION DETECTED 警告 user 消息，清空窗口，关联触发周期反思。

### 4.7 周期反思触发器

每 8 个 `tool_round` 注入 REFLECTION CHECKPOINT，要求 LLM 显式 justify CONTINUE vs SWITCH_STRATEGY。重复检测器触发时会跳过周期等待，立即注入反思。

### 4.8 收益递减自动检测

`iteration_end.py` 中的 `_check_diminishing_returns()`:
- 检测同一策略最近 2+ 次使用且每次 |delta| < 0.020ns
- 记录为 `reason="no_improvement"`（非 `strategy_ineffective`）——策略不被永久排除
- 信号出现在 handoff 轨迹中，引导 LLM 远离已耗尽的策略

### 4.9 空响应早期终止（Ghost Loop 防护）

DeepSeek V4 Flash 在长上下文场景下可能产生"沉默退化"——65% 的调用返回 0 字符。

- `ContextState.consecutive_empty_responses` 计数器
- 阈值: ANALYZE/EXECUTE/EVALUATE = 3, SELECT_STRATEGY = 2
- 达到阈值时记录 `SYSTEM_EXIT` 信号并强制退出阶段
- 非空响应时重置为 0，阶段入口时无条件重置

### 4.10 多策略循环

`MAX_STRATEGY_CYCLES=5`。EVALUATE 的 `SWITCH_STRATEGY` 信号触发循环回 SELECT_STRATEGY（跳过 ANALYZE）。失败策略通过 TTL 机制按原因分级（`strategy_ineffective` 1 轮 / `strategy_not_applicable` 5 轮 / `no_improvement` 3 轮后自动解封；`tool_error` 无 TTL），而非永久阻止。

### 4.11 连续无进展自动检测（2026-07）

EVALUATE 阶段 `consecutive_no_progress` 计数器。每次评估计算当前策略 WNS delta（与进入 EXECUTE 时相比），三档语义：

- `delta > STRATEGY_IMPROVEMENT_EPSILON_NS`（0.050ns，显著收益）→ 重置为 0
- `delta ≤ 0`（真无改善）→ `+= 1`，计数 >= 3 强制切换
- `0 < delta ≤ 0.050`（边际正收益，best_checkpoint 已保存）→ **既不重置也不递增**

```python
if state.context.consecutive_no_progress >= 3:
    flow_signal = "SWITCH_STRATEGY"  # 强制切换，不等 LLM 决策
```

触发后记录日志警告 `"[EVALUATE] 3 consecutive no-progress evaluations — forcing SWITCH_STRATEGY"`。

**冷却判定同步**（`_cool_down_current_strategy_if_stalled`）：以 `delta > 0` 跳过冷却，与计数器三档对齐。2026-07-04 修复：此前冷却用 `delta > 0.050` 而 best_保存用 `>0`，导致 PBLOCK +0.049ns 改进被存为 best 却被冷却（"见好就收"矛盾）。现在任何正收益都不冷却，EPSILON 仅用于"显著收益"判定（重置计数）。

**每迭代重置**（2026-07-09 修复）：`consecutive_no_progress` 在 `iteration_start` 重置为 0（与 `tool_errors`/`tools_used`/`blocked_strategies` 一同清理）。此前该计数器跨迭代持久，导致 iter N 的停滞计数带入 iter N+1 并无限递增（实测 3→4→…→11），每次 eval≥3 都强制 SWITCH 却无升级终止路径，直到时间预算耗尽。重置后该计数器仅门控迭代内的策略切换；跨迭代平台期终止由 `global_no_improvement` + `GLOBAL_NO_IMPROVEMENT_LIMIT`（check_exit）独立处理。

### 4.12 优化历史追踪（2026-07）

**数据结构**（`optimizer/state.py` 新增 `OptimizationAppliedRecord`）：

```python
@dataclass
class OptimizationAppliedRecord:
    strategy: str = ""
    params: str = ""           # 短参摘要（200字截断）
    wns_before: float = 0.0
    wns_after: float = 0.0
    iteration: int = 0
    checkpoint_path: str = ""
```

**触发时机**：`_save_best_checkpoint()` 每次保存新的 `best_checkpoint.dcp` 时追加记录到 `state.context.optimization_history`：

```python
state.context.optimization_history.append(OptimizationAppliedRecord(
    strategy=state.strategy.current_strategy,
    params="",
    wns_before=_current_strategy_baseline_wns(state),  # 本策略进入 EXECUTE 时的 best_wns
    wns_after=state.timing.best_wns,
    iteration=state.iteration.current,
    checkpoint_path=str(state.control.best_checkpoint_path),
))
```

**每策略基线**（2026-07-09 修复）：`wns_before` 取 `_current_strategy_baseline_wns(state)`——当前策略进入 EXECUTE 时记录的 `PhaseEntry.best_wns_at_entry`（与 cooldown 的 `_strategy_wns_delta_since_entry` 同源），无匹配条目时回退 `prev_best_wns`。此前用 `prev_best_wns`（迭代开始时冻结一次），导致同迭代内后续策略的 delta 全部以迭代起始 WNS 为基准（PhysOpt 记 +0.335ns 而实际仅 +0.012ns），误导 LLM 高估策略贡献。

**消费方**：
- **Handoff**（`pure/handoff.py`）：`_format_optimization_history()` 在 planner/worker handoff 末尾追加 `APPLIED OPTIMIZATIONS` 段落
- **Dashboard**（`pure/state_space.py`）：`applied_optimizations` 独立段落，列出每条记录的 `strategy: WNS_before→WNS_after (iter N)`

### 4.13 设计指纹缓存保留（2026-07）

**动机**：EVALUATE→CONTINUE→ANALYZE 循环中，若设计未被修改，跨阶段清理 tool cache 会导致已缓存的时序数据不必要丢失，增加重复 MCP 调用。

**机制**（`nodes/subgraphs/phase_handoff.py`）：

```python
# 模块级变量追踪上次指纹
_last_design_fingerprint: str | None = None

if design_fingerprint is not None and design_fingerprint == _last_design_fingerprint:
    # 设计未变更 → 保留缓存
    pass  # tool_cache 不被清除
else:
    tool_cache.clear()
    _last_design_fingerprint = design_fingerprint
```

所有四阶段（ANALYZE、SELECT_STRATEGY、EXECUTE、EVALUATE）的 `transition_phase()` 调用均传入 `design_fingerprint=str(state.control.best_checkpoint_path)`。向后兼容：`design_fingerprint=None`（旧调用方）保持原有的始终清除行为。

### 4.14 迭代开始 Checkpoint（2026-07）

`iteration_start_node` 在每个迭代开始时自动保存当前设计状态到 `iteration_{iter}_start.dcp`，作为 `_reload_baseline_on_switch` 的回退基线。

**拷贝优化（2026-07-04）**：当 `best_checkpoint.dcp` 已存在于磁盘时（表明 Vivado 内存未发生变化），直接通过 `shutil.copy2` 拷贝文件，而非调用 Vivado `write_checkpoint` 序列化。因 `best_checkpoint.dcp` 始终精确反映 Vivado 当前内存状态，且两次 checkpoint 写入间无任何工具调用修改设计，拷贝结果与 Vivado 序列化等价，但耗时从 ~2.5s 降至 ~0.01s。`_ensure_iteration_start_checkpoint` 中采用相同逻辑。

```python
iter_ckpt = state.control.run_dir / f"iteration_{state.iteration.current}_start.dcp"
best = state.control.best_checkpoint_path
if best is not None and best.exists():
    shutil.copy2(str(best), str(iter_ckpt))  # 节省 ~2.5s Vivado 序列化
else:
    await call_tool_fn("vivado_write_checkpoint", ...)
state.control.iteration_checkpoints.append((state.iteration.current, iter_ckpt))
```

**加载优先级**（在 `_reload_baseline_on_switch` 中）：
1. `state.control.best_checkpoint_path`（最佳 checkpoint，优先）
2. `iteration_{iter}_start.dcp`（迭代开始快照，回退）

当 `best_checkpoint_path` 不存在或已被删除时，fallback 到迭代开始 DCP，确保策略切换加载的基线始终包含当前迭代的所有修改。

### 4.15 检查点重载跳过优化（2026-07）

`_reload_baseline_on_switch()` 中每次策略切换都会调用 `vivado_open_checkpoint`（约 27 秒）。对于连续执行多个策略的迭代，同一最佳检查点被反复重载。

优化：在调用 `vivado_open_checkpoint` 前检查 `state.control.current_dcp_path` 是否已匹配目标检查点路径。若已匹配（`str(state.control.current_dcp_path) == str(iter_ckpt.resolve())`），跳过重新加载，仅刷新时序报告。`current_dcp_path` 在 `vivado_open_checkpoint` 成功执行后更新，auto-chain 中同样更新（`phase_execute.py` L1869-1871）。每次跳过节省约 27 秒。

**Bug 修复（2026-07-04）**：`_save_best_checkpoint()` 通过 Vivado 成功写入 `best_checkpoint.dcp` 后，仅更新了 `best_checkpoint_path` 但未同步更新 `current_dcp_path`。导致后续策略切换时 `_reload_baseline_on_switch` 路径比较失效——Vivado 内存实际已持有 best checkpoint 内容，但系统误认为设计指针仍指向 iteration start DCP，触发无意义重载（~15s 浪费）。修复：在 `_save_best_checkpoint` 中增加 `state.control.current_dcp_path = ckpt_path.resolve()`。

---

### 4.16 skip-reopen 脏设计守卫（P0-1，2026-07-11）

`_reload_baseline_on_switch()` 的跳过重载优化（4.15）原仅比较 `current_dcp_path == 目标路径`。但 `current_dcp_path` 只在「成功写 best_checkpoint」或「显式 reopen」时更新--失败/无改善策略仍会修改 Vivado 内存（place/route/opt），却不更新该指针，于是「路径匹配」成立而内存已脏，跳过重载会读取脏设计的错误 WNS，污染后续所有策略基线（run-20260711_015650：报告 -0.602 而非真实 best -0.542）。

修复：新增 `ControlState.live_design_dirty` 标志，语义为「Vivado 内存的时序相关状态已偏离 `current_dcp_path` 指向的文件」。跳过重载判定抽取为纯函数 `should_skip_reopen(current_dcp_path, target, live_design_dirty)`（`optimizer/pure/execute_contracts.py`），需同时满足「路径匹配」且「`not live_design_dirty`」方可跳过。

置位规则：
- 任何 DESIGN_MODIFICATION_TOOLS 调用（主循环 + auto-chain 循环）成功后置 `live_design_dirty=True`；`vivado_open_checkpoint` 成功后置 False 并同步 `current_dcp_path`。
- `_save_best_checkpoint` 写 best 后置 False（内存已与 best_checkpoint 文件一致）；`_restore_pre_chain_checkpoint`、`rollback`、`init_analysis`、`save_output` 重载/写入后均置 False。

场景：iter1 PBLOCK 成功写 best（dirty=False）-> 后续策略 chain 跑 P&R（dirty=True）-> 无改善不写 best -> 下次策略切换路径匹配但 dirty=True -> 强制 reopen，基线 WNS 正确。

## 5. 工具输出摘要化 + 历史自动裁剪

### 5.1 动机

phys_opt_design / route_design 等工具的原始 Vivado 日志单次可达 25k+ 字符，导致模型注意力被稀释。

### 5.2 实现

**1. `_summarize_tool_result()`** — 每个工具调用返回时自动提取结构化 YAML 摘要：

```yaml
# 大输出（timing report）
tool_result:
  tool: vivado_phys_opt_design
  summary: "WNS: -0.939, TNS: -834.718, Failing endpoints: 1529"
  key_details:
    wns: -0.939
    wns_delta: +0.039
    tns: -834.718
    failing_endpoints: 1529
  status: completed
  raw_output_truncated: true
  raw_output_chars: 45231

# 小型输出（<3KB，非 timing）
tool_result:
  tool: vivado_get_wns
  summary: "WNS: -0.352"
  status: completed
  raw_output_truncated: false
  raw_output_chars: 84
  raw_output: |
    -0.352
```

**小输出错误状态修复（2026-07-09）**：`summarize_tool_result()` 的小输出绕过（<3KB 非 timing）此前硬编码 `status: completed`，且位于错误检测逻辑之前返回，导致小型 JSON 错误响应（如 `{"error": "Directive '...' is not a recognized directive"}`）被误标为 completed。修复：将通用 error/fail 检测移至绕过之前，绕过分支使用已计算的 `status`（错误时为 `error`）。同时 `phase_execute` 的 `tool_errors` 追加改为存 `tool_result.error` 原始错误文本（非 summary），保留真实失败原因供失败分类与 LLM 反馈。

**工具错误 ERROR 级日志（2026-07-09）**：`tool_router.call_tool` 检测到 MCP 错误响应（`is_mcp_error_response=True`）时，新增 `logger.error("[TOOL_ERROR] ...")` 含错误原文前 500 字符。此前仅 `logger.info` 记录布尔 `error=True`，而 `fpl26-error.log` handler 只收 ERROR 级，导致该日志保持空白、Vivado 应用错误（directive 不识别、place_design 失败）不可见。

**2. `_raw_tool_outputs`**: FIFO 淘汰（最多 50 条），仅当 LLM 调 `vivado_get_raw_tool_output` 时返回。键格式为 `(iteration, phase, tool_round, tool_name)`（2026-06-27 修复：之前为 `(iteration, tool_round)`，ANALYZE/EXECUTE 阶段键冲突导致结果互相覆盖）。

**3. 压缩阶段旧消息裁剪**: `_compress_outdated_tool_results()` — 迭代差 > 2 的工具消息替换为 `[COMPRESSED: {tool} iter={N} | {summary}]`。

### 5.3 压缩后角色保留

```python
api_messages = [
  system("SYSTEM_PROMPT + WNS state"),
  system("YAML compressed OLDER messages"),
  user("..."),                               # ← 保留role
  assistant("...", tool_calls=[...]),        # ← 保留role
  tool("..."),                               # ← 保留role
]
```

### 5.4 设计数据持久化（2026-07 新增）

`DesignDataManager`（`optimizer/pure/design_data.py`）在每次 dashboard 构建和工具调用时，将全量设计数据持久化为 `{run_dir}/design_data/iteration_{N}/` 下的结构化 JSON 文件：

- **`store_raw_output()`**: 每次工具调用后，将原始输出持久化到 `tool_output_{name}_{round}.json`，含 `_meta` 元数据（时间戳、迭代、阶段、原始字符数）
- **`store_snapshot()`**: 新迭代首次 dashboard 构建时，将全量 `critical_paths`、`high_fanout_nets`、`congestion_data`、`route_status`、`design_info` 分别写入独立 JSON 文件。每个文件的 `_meta` 中包含对应的 `field_freshness` 状态，供 LLM 判断数据是否过期
- **`read_design_data()`**: 通过 `design_data_read` 内部工具读取持久化文件，返回结构化 JSON（含 `total_records`、`size`、`meta`）
- **`list_available_data()`** / **`list_all_iterations()`**: 列出可用数据

**持久化触发点**：
1. `inject_merged_dashboard()`（`context_snapshot.py`）：新迭代首次调用时触发全量快照
2. `phase_analyze.py` / `phase_execute.py` / `phase_evaluate.py`：每次工具调用后触发原始输出持久化

**消费者**: LLM 通过 `design_data_read(iteration=N, data_type='critical_paths')` 内部工具访问（注册在全局 `_NO_CACHE_TOOLS` 中，4 个 phase 白名单均包含）。不调用 Vivado/RapidWright，零延迟。

---

## 6. DCP 验证实现细节

**逻辑等价性是优化过程的硬约束**。

```
验证策略:
├── 每5次迭代: 中间 checkpoint 验证（500 向量）
├── 完成时: 完整验证（Phase1 + Phase2, 默认 200 向量，可通过 --vectors 调整）
└── 条件: validation_enabled AND intermediate_dcp存在 AND 非完成态 AND iteration%5==0
```

**Phase 1 — 结构对比（RapidWright）**: 比较 golden 和 revised DCP 的 EDIF 网表结构。始终执行（不再提供 `--skip-structural` 标志）。

**Phase 2 — 功能仿真（Vivado xsim）**: 从 golden 和 revised DCP 分别导出 Verilog 仿真模型，生成 LFSR 驱动的随机测试激励。默认先运行 100 向量 precheck，再运行 200 向量完整验证。

### 6.1 EXECUTE 阶段 no-progress 检测优化

- `NO_PROGRESS_LIMIT`: 12 → 4（节省 ~20s 空转时间）
- 新增 `_pending_tool_count` 防止长执行工具（place_design 1800s, route_design 1800s）误触发
- Post-eval hook 返回 UNCHANGED 后注入 `[GUIDANCE]` 消息

### 6.2 vivado_run_tcl 速率限制强化

- `PHASE_TOOL_RATE_LIMITS["vivado_run_tcl"]`: 5 → 2
- RATE LIMITED 消息引导使用 Dashboard 数据

### 6.3 重定时策略排除机制

4 层排除策略：
1. **`STRATEGY_VALIDATION_SAFE` 字典**（strategy_library.py）: RegisterRetiming 等映射为 False
2. **工具白名单移除**（tool_filter.py）: 从 `INDEPENDENT_RAPIDWRIGHT_TOOLS` 和 `PHASE_TOOLS[EXECUTE]` 中移除重定时工具
3. **策略→工具映射移除**（constants.py）: 从 `STRATEGY_MAP` 中移除映射（原 `EXECUTE_STRATEGY_TOOL_MAP` 已合并到统一 `STRATEGY_MAP`）
4. **静态提示词**（SYSTEM_PROMPT.TXT）: 说明为何排除

策略定义和 RapidWright 工具实现**保留**在代码库中，仅切断 LLM 的暴露路径。

### 6.4 Pre-placement 逻辑优化（opt_design）策略

**架构**:
```
LLM calls rapidwright_opt_design_strategy (RapidWright skill)
  └─► SKILL_CHAIN_ACTIONS triggers:
       vivado_opt_design (directive=Explore, retarget=True)
       → vivado_place_design → vivado_route_design
       → vivado_report_timing_summary → vivado_extract_critical_path_cells
```

**安全分析**: opt_design 无 retiming 选项 → 不需要 retiming 安全守卫。所有 directive 都是纯逻辑优化（无物理层面副作用）。

### 6.5 策略别名映射

`STRATEGY_MAP` 新增 `"LogicOptimization": StrategyEntry("opt_design_strategy", "rapidwright_execute_opt_design_strategy")`。FORMAT_GUARD EXECUTE addendum 策略列表同步添加。`_STRATEGY_MAPPING_LINES` 自动生成。

### 6.6 Dashboard 数据新鲜度与工具 rate limit

**字段级新鲜度追踪**: `field_freshness: dict[str, str]`（2026-06-24 新增，替代 `refreshed_fields: set[str]`）。每个 Dashboard 数据字段独立标注 `[fresh]`/`[stale]`。初始化全部 `fresh`，工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段，设计修改工具（`DESIGN_MODIFICATION_TOOLS`）执行后全部降级为 `stale`。EXECUTE 和 EVALUATE 两阶段对称处理（2026-06-27 修复：EVALUATE 之前缺失 stale 标记逻辑，造成 false-fresh）。

**截断透明化新鲜度（2026-07 新增）**: `unshown_path_stats` 区段顶部显示 `unshown_freshness: stale/fresh` 行（基于 `critical_paths_stale` + `_tag('critical_path_cells')`）；`unshown_hotspots` 和 `unshown_high_fanout_nets` 行尾带 `_tag('congestion_data')`/`_tag('high_fanout_nets')`；`truncation_advisory` 区段包含 `freshness: stale_fields=[...] | all_fields_fresh` 全局状态行。截断透明化新增的三个 section 均与已有新鲜度系统完全集成。

**新鲜度契约对齐**（2026-07-09 修复，run-20260709_123409 分析）：
- **`_reload_baseline_on_switch`**（策略重入时刷新 WNS）此前刷新 WNS 后又把全部 `field_freshness` 标为 `stale`，造成"值正确但标签为 stale"的反模式，诱导 LLM 浪费轮次重复 `vivado_report_timing_summary`。现刷新成功（`baseline_wns is not None`）后调用 `_mark_timing_fresh` 将 `timing_summary`/`cdc_paths` 重新标为 fresh，其余字段（critical_paths 等）保持 stale（反映前序策略的设计状态）。
- **STALE DATA HANDLING 指令改写**（`prepare_context.py` BASE_FORMAT_GUARD）：原指令要求 LLM "stale WNS/TNS 必须手动刷新"，但框架已在 ANALYZE/SELECT_STRATEGY 入口及策略重入时自动刷新 WNS，4 个网表策略 EXECUTE 入口自动刷新 critical_paths，pblock/combinational 工具自动注入 verified cells/paths（附 `[DATA INTEGRITY]` 通知）。新指令如实描述自动刷新范围，仅要求 LLM 在框架未自动刷新且 cell-targeting 需要时手动刷新，消除"规则要求手动刷新 / 框架已自动刷新"的双向矛盾。
- **CELL NAME CONTRACT 补充**：声明执行工具会自动注入 verified state data 并在覆盖 LLM 输入时发 `[DATA INTEGRITY]` 通知，对齐"[CELL REGISTRY] canonical"声明与"框架强制覆盖"的实际行为。
- **`truncation_advisory` 诚实化**：注明 `design_data_read` 返回持久化快照（不调用 Vivado/RapidWright），stale 字段的持久化值可能滞后于实时设计，需用提取工具刷新。

**工具调用频率限制**（`PHASE_TOOL_RATE_LIMITS` 补强层，反应式拦截冗余调用）：
- `rapidwright_search_cells`: 3
- `vivado_run_tcl`: 2
- `vivado_write_checkpoint`: 3
- `rapidwright_analyze_net_detour`: 2
- `vivado_get_cached_high_fanout_nets`: 2
- `vivado_check_design_status`: 3

---

## 7. 未布局 DCP 防护机制（四层防护）

### Layer 1: save_output 保存前检查
1. 查询 `get_property STATUS [current_design]`
2. 回退到 `report_timing_summary` 的 `Design State` 字段（routed / placed / optimized）
3. 若非 `routed`: 尝试从 best_checkpoint 恢复；若仍需修复→自动执行 place_design + route_design
4. 写入后再次验证，若非 `routed` 则记录 WARNING

### Layer 2: 虚假正 WNS 检测
- `_post_eval_hook` / `_track_wns_from_result`: 检查 `Design State`，若非 `Routed` 追加 `[WARNING: design not routed]`
- Place-only WNS 检查: 若为 `Optimized`（未布局），跳过 WNS 检查（使用估计延迟，虚假乐观）

### Layer 3: unplace 自动回滚
1. 追踪 `vivado_place_design(directive="unplace")` 调用
2. 保存 pre-unplace checkpoint 到 `run_dir/pre_unplace_{iter}_{round}.dcp`（原 `/tmp/`，2026-07-04 迁移至 `run_dir` 消除并发覆盖风险）
3. 后续 `vivado_place_design`（非 unplace）清除标志
4. 阶段退出时若标志仍为 True → 从 pre-unplace checkpoint 恢复 + 刷新 WNS/TNS/FE

> **PBLOCK 链改用局部 unplace**（2026-07-04）：PBLOCK auto-chain step1 由全局 `place_design -unplace` 改为 `vivado_unplace_cells(cells=critical_path_cells)`，仅 unplace 关键路径 cell，其余设计布局/布线保持不变（增量 P&R）。此 Layer 3 全局 unplace 回滚仍对 LLM 直接调用 `place_design -unplace` 生效；PBLOCK 链的失败由 `_execute_chain_actions` 的 pre-chain checkpoint（`pre_chain_pblock.dcp`）回滚覆盖。

### Layer 4: Vivado 执行工具错误检测
- VivadoMCP 检测 `^ERROR: [` 文本模式，返回 JSON `{"error": "..."}`
- `phase_execute.py` 检查 JSON `error` 键 + 文本 `ERROR: [` 模式
- 任一检测到错误: 中止链 + 从 pre-chain checkpoint 恢复（`run_dir/pre_chain_pblock.dcp`，2026-07-04 从 `/tmp/` 迁移至 `run_dir`）

---

## 8. 重定时安全守卫（phys_opt / place_design / route_design）

阻止 `AlternateFlowWithRetiming`、`AddRetime` 等指令（双层防护）；place_design/route_design 新增 PLACE_SAFE_DIRECTIVES/ROUTE_SAFE_DIRECTIVES 安全指令白名单

**VivadoMCP 服务端守卫**:
```python
BLOCKED_DIRECTIVES = {"AlternateFlowWithRetiming", "AddRetime"}
BLOCKED_BOOL_OPTIONS = {"retime", "interconnect_retime"}
SAFE_DIRECTIVES = {"Default", "Explore", "AggressiveExplore", ...}

# place_design/route_design 指令白名单（2026-06 新增）
# 实际定义见 VivadoMCP/vivado_mcp_server.py L87-L112
PLACE_SAFE_DIRECTIVES = {"Default", "Explore", "ExtraTimingOpt", "WLBlockPlacement",
    "ExtraPostPlacementOpt", "AltSpreadLogic_high", "AltSpreadLogic_medium",
    "AltSpreadLogic_low", "SpreadLogic_high", "SpreadLogic_medium", "SpreadLogic_low",
    "EarlyBlockPlacement", "LateBlockPlacement", "SSI_SpreadLogic_high", "SSI_SpreadLogic_low",
    "Quick", "RuntimeOptimized", "FlowQuick", "FlowRuntimeOptimized",
    "Congestion_Default", "Congestion_SpreadLogic_high", "Congestion_SpreadLogic_medium",
    "Congestion_SpreadLogic_low", "Area_Explore", "Area_ExploreWithRemap",
    "Area_ExploreSequentialArea", "Performance_Explore", "Performance_ExplorePostRoute",
    "Performance_ExtraTimingOpt", "Performance_NetDelay_high", "Performance_NetDelay_medium",
    "Performance_NetDelay_low", "Performance_RefinePlacement", "Performance_WLBlockPlacement",
}
ROUTE_SAFE_DIRECTIVES = {"Default", "Explore", "AggressiveExplore", "HigherDelayCost", "LowerDelayCost",
    "NoTimingRelaxation", "RuntimeOptimized", "Quick", "FlowQuick",
    "FlowRuntimeOptimized", "Performance_Explore", "Performance_NetDelay_high",
    "Performance_NetDelay_medium", "Performance_NetDelay_low",
    "Performance_RefinePlacement", "Performance_WLBlockPlacement",
    "SSI_Explore", "SSI_Quick",
    "Area_Default", "Area_Explore", "AlternateRoutability",
}
```

**call_tool 入口守卫**: 检查 directive 参数和 retime/interconnect_retime 布尔选项。place_design/route_design 指令需在 SAFE_DIRECTIVES 白名单中方可执行。

### 8.1 指令黑名单 + 自动回退（2026-07 新增）

`KNOWN_BROKEN_DIRECTIVES: frozenset[str]`（定义在 `optimizer/pure/constants.py`）记录因许可/环境限制已知失败的指令。当前黑名单：
- `Performance_ExtraTimingOpt` — 需要 Extra Timing license，竞赛环境不可用

在 `phase_execute.py` 的 `_execute_chain_actions()` 中，指令解析（Tier-1 LLM 提供 + Tier-2 策略默认）后增加黑名单检查：若 `args["directive"]` 在黑名单中，静默回退到 `STRATEGY_DEFAULT_DIRECTIVES` 中对应策略的默认指令，并记录 WARNING 日志。回退优先级：place→`place_def`，route→`route_def`；若无对应回退值则移除 `directive` 参数，让 Vivado 使用默认值。每次回退避免约 17 秒的失败 P&R 循环。

**MCP 层 directive 动态回退（P0-2，2026-07-11）**：`VivadoMCP/vivado_mcp_server.py` 的 `place_design`/`route_design` handler 在 Vivado 返回 Constraints 18-641（directive not recognized）时，自动用无 directive 的默认命令重试一次，并在结果前缀 `[FALLBACK]` 说明。检测函数 `_is_unrecognized_directive_error(output)` 匹配 `18-641` 与 `not a recognized directive`。即使白名单未来再漂移，策略也不再瞬时失败。同时收紧白名单：移除 place 的 `NetDelay_high/medium/low` 与 route 的 `Congestion_Explore`/`Congestion_NetDelay_*`（后者是 Vivado 策略预设名而非 route_design -directive，被 2025.1 以 18-641 拒绝）。`strategy_library.py` 的 CongestionRouteExplore route directive 由 `Congestion_Explore` 改为合法的 `AlternateRoutability`。

---

## 9. 心跳日志系统

```
tool call 入口:
├── DCPOptimizer.call_tool()           # 主要入口
├── FPGAOptimizerTest.call_vivado_tool()    # 测试模式
└── FPGAOptimizerTest.call_rapidwright_tool() # 测试模式

心跳机制 (_start_tool_heartbeat):
- 每60秒打印 [HEARTBEAT #{n}] Tool '{name}' still running after {elapsed}s
- 工具完成时打印 [TOOL_COMPLETE] '{name}' completed in {elapsed}s (heartbeats: {n})
```

---

## 10. 重要常量

```python
WORKER_HARD_LIMIT = 220K, WORKER_TOKEN_BUDGET = 200K
PLANNER_HARD_LIMIT = 600K, PLANNER_TOKEN_BUDGET = 500K

# Dashboard 新鲜度追踪 (工具→影响字段映射, 2026-06 新增 vivado_report_qor_suggestions/vivado_report_high_fanout_nets)
DASHBOARD_REFRESH_MAP: dict[str, frozenset[str]] = {
    "vivado_report_utilization_for_pblock": {"resource_utilization"},
    "vivado_get_critical_high_fanout_nets": {"high_fanout_nets"},
    "rapidwright_analyze_critical_path_spread": {"critical_path_spread"},
    "vivado_report_qor_suggestions": {"qor_suggestions"},
    "vivado_report_high_fanout_nets": {"high_fanout_nets"},
}

# init_analysis 自适应超时
DESIGN_SIZE_BINS = [(0, 50000, 1.0), (50000, 150000, 1.5), (150000, 2**31, 3.0)]
MAX_TIMEOUT = 900.0

# 冷却逻辑 Improvement 阈值 (0.001 → 0.050, 2026-06-23)
# 2026-07-04: 三档语义——delta>0 不冷却；delta>0.050 重置无进展计数；delta≤0 计无进展
STRATEGY_IMPROVEMENT_EPSILON_NS: float = 0.050

# 策略工具名称集合（用于冷却逻辑分层：策略工具本身失败→跳过冷却，仅辅助工具失败→应用冷却）
STRATEGY_TOOL_NAMES: frozenset[str] = frozenset({
    "rapidwright_execute_pblock_strategy",
    "rapidwright_execute_muxf_tree_reorder_strategy",
    "rapidwright_execute_fanout_strategy",
    "rapidwright_execute_congestion_spreading",
    "rapidwright_optimize_pin_swapping",
    "rapidwright_flatten_lut_cascade",
    "rapidwright_replicate_critical_cells",
    "rapidwright_execute_register_retiming",
    "rapidwright_execute_lut_muxf_repack_strategy",
    "rapidwright_execute_combinational_rebalancing_strategy",
    "rapidwright_execute_net_swapping",
    "rapidwright_optimize_cell_placement",
    "rapidwright_optimize_lut_input_cone",
    "vivado_physopt_and_route", "vivado_phys_opt_design", "vivado_opt_design",
})

# 设计修改工具集合（执行后所有 field_freshness 降级为 stale）
DESIGN_MODIFICATION_TOOLS: frozenset[str] = frozenset({
    "vivado_phys_opt_design", "vivado_route_design", "vivado_place_design",
    "vivado_create_and_apply_pblock", "vivado_physopt_and_route",
    "rapidwright_execute_pblock_strategy", "rapidwright_execute_fanout_strategy",
    "rapidwright_optimize_pin_swapping", "rapidwright_flatten_lut_cascade",
    "rapidwright_replicate_critical_cells", "rapidwright_execute_congestion_spreading",
    "rapidwright_execute_register_retiming", "rapidwright_execute_net_swapping",
    "rapidwright_execute_opt_design_strategy",
    "rapidwright_execute_combinational_rebalancing_strategy",
    "rapidwright_execute_lut_muxf_repack_strategy",
    "rapidwright_execute_muxf_tree_reorder_strategy", "rapidwright_smart_retiming",
    # 补充的独立 RW 工具（2026-06-27）
    "rapidwright_optimize_cell_placement",
    "rapidwright_optimize_lut_input_cone",
    "rapidwright_optimize_fanout_batch",
    "rapidwright_execute_physopt_strategy",
    # Vivado 工具
    "vivado_opt_design",
})

# 迭代控制常量
MAX_TOOL_ROUNDS_PER_ITERATION = 80
GLOBAL_NO_IMPROVEMENT_LIMIT = 3
WNS_TARGET_THRESHOLD = 0.0

# 失败策略 TTL（2026-07 `_ttl_for_reason()` 统一计算）
# strategy_ineffective → current + 1
# strategy_not_applicable → current + 5
# no_improvement → current + 3
# tool_error / execution_failure / unknown → current（无 TTL）

# 连续无进展检测阈值（2026-07）
CONSECUTIVE_NO_PROGRESS_LIMIT = 3
```

---

## 11. 上下文工程：弱引导设计（2026-06 新增）

### 原则与实现

| 原则 | 实现 |
|------|------|
| 描述问题，非处方方案 | `safety_constraints` → `known_risks`（"observed" 替代 "MUST"） |
| 信任 LLM 领域知识 | 移除 `strategy_implications`、`selection_guide`、`architecture_overview` 解释（2026-06-27 已落实：SYSTEM_PROMPT.TXT 中移除 STRATEGY SELECTION GUIDANCE、architecture_overview、iteration 阈值、workflow 处方） |
| 单一信息源 | FORMAT_GUARD 不再重复工具 schema 和 lifecycle 描述 |
| 减少认知负担 | 信号从 9 → 7（移除 RETRY、合并 RESELECT→SWITCH） |
| 自动化替代指令 | 移除 EXECUTE CONSTRAINT 和 post_actions（auto-chain 已覆盖） |
| 编码领域知识 | 16 种策略带有触发条件 |

### 上下文注入层次

1. **SYSTEM_PROMPT.TXT** (~75行): 角色定义 + known_risks(事实描述) + 策略目录
2. **FORMAT_GUARD** (~4000 字符, 一次性): 行为要求 + EXECUTE工具映射 + 格式禁令 + **CELL NAME CONTRACT**（2026-06 新增：指引 LLM 使用 [CELL REGISTRY]、禁止 device site / bare type 名、说明富错误反馈机制）
3. **Dashboard** (每轮动态重建): 纯数据无判断标签 + phase-aware过滤 + 最后一条user消息注入
4. **Tool schema** (按阶段过滤): `filter_tools_for_phase()` 深拷贝+动态patch flow_control enum
5. **Runtime nudge** (按需): 事实描述("Current WNS: X")，非处方

### 信号体系

### 信号体系

| 信号 | 阶段 | 语义 |
|------|------|------|
| `ANALYZE_DONE` | ANALYZE | 分析完成，进入策略选择 |
| `EXEC_DONE` | EXECUTE | 执行完成，进入评估 |
| `CONTINUE` | ANALYZE/EXECUTE | 继续当前阶段 |
| `NEXT_ITERATION` | EVALUATE | 显著改善，进入下一轮 |
| `SWITCH_STRATEGY` | EVALUATE | 策略失败/尝试另一策略 |
| `DONE` | EVALUATE | WNS >= 0 |
| `ROLLBACK` | EVALUATE | 回滚到最佳 checkpoint |
| `EXHAUSTED` | EVALUATE | 所有策略已耗尽 |

`filter_tools_for_phase()` 对 `report_step_state` 工具定义做深拷贝 + 按阶段 patch `flow_control` enum，LLM 只看到当前阶段的合法信号。

### 上下文注入层次（分层上下文管理，2026-07 增强）

显式五层注入架构，每层有明确的生命周期与注意力权重策略：

| 层 | 生命周期 | 内容 | 注入位置 |
|------|---------|------|---------|
| L0 STATIC | 不变（top-level `system` 参数） | SYSTEM_PROMPT.TXT（角色/规则/启发式，~110行） | `extra_body[system]`，provider 可缓存 |
| L1 FORMAT_GUARD | 每 phase 重建（system message） | BASE 格式要求 + Cell Name Contract + 设计一致性 + per-phase addendum（tool 可用性/PBLOCK 行为/EVALUATE 决策指引等） | 首个 system message 之后，幂等 marker 去重 |
| L2 PINNED | 每轮重建，绕过压缩 | **CellNameRegistry 快照**（canonical cell 名 + 模块索引 + stale/fresh 标记 + iter 版本号） | system 之后，独立 user 消息 |
| L3 DYNAMIC | 每轮重建，phase-aware | Dashboard 7-module StateSpace（非 EXECUTE/EVALUATE 阶段抑制 current_strategy；时钟名从 critical_paths 提取；模块尾部含 `unshown_path_stats`/`unshown_hotspots`/`unshown_high_fanout_nets` 截断聚合统计；末尾 `truncation_advisory` 列出截断量与 `design_data_read` 存取指南） | 最后一条 user 消息 |
| L4 EPHEMERAL | 受压缩管理（preserve_role_turns=6） | 最近对话轮次 + 压缩后 YAML 历史 | 消息列表主体 |

**关键修复（2026-07）**：
- **P0**：`transition_phase` 现恢复全部 system messages（此前仅恢复首个，导致 FORMAT_GUARD/handoff/budget 在首次 phase 切换后丢失）。
- **P1/P7**：FORMAT_GUARD 从一次性注入改为每 phase 按 `build_phase_guard(phase)` 动态生成，ANALYZE 阶段不再看到 EXECUTE-only 的 PBLOCK 行为指令。注入点集中在 `inject_merged_dashboard()`，非散布 4 个 phase 文件。
- **P3**：`format_state_space_for_llm` 在非 EXECUTE/EVALUATE 阶段抑制 `current_strategy`，防止上一迭代残留误导。
- **P5**：Module 5 时钟名从 `critical_paths[0].clock.source_clock` 提取，不再硬编码 `clk_fpl26contest`。
- **P6**：Cell Registry 附带 `stale`/`fresh` 标记和 `iter=N` 版本号。
- **P4**：`compact_tool_summary()` 作为共享函数统一 dashboard/handoff 的 tool result 摘要，优先 JSON 解析。
- **P8 — STALE DATA HANDLING 指令**：`BASE_FORMAT_GUARD` 新增完整章节，明确指示 LLM：WNS/TNS 标记 `[stale]` 必须先调用 `vivado_report_timing_summary` 刷新再评估；Critical paths 标记 `[stale]` 必须先调用 `vivado_extract_critical_path_cells` 再执行 cell-targeting 操作。
- **P9 — 阶段入口自动 WNS 刷新**：ANALYZE 和 SELECT_STRATEGY 阶段入口自动检查 `field_freshness["timing_summary"] == "stale"`，若是则自动调用 `vivado_report_timing_summary` 刷新 WNS/TNS/FE 并置为 `fresh`。无需 LLM 介入，消除 50+ 轮连续 stale 数据决策风险。
- **P10 — Strategy Outcome Table**：Dashboard 末尾新增 `strategy_outcomes:` YAML 区块，含 `successful`（从 `optimization_history` 读取，含 WNS delta）和 `failed`（从 `failed_strategies` 读取，按策略去重显示最新记录）两节。每轮可见，消除策略重复选择。
- **P11 — 策略-工具映射恢复**：`_STRATEGY_MAPPING_LINES` 从 EXECUTE 独占扩展为 SELECT_STRATEGY 阶段也可见，LLM 选择策略前即可验证工具是否存在。

**Pinned 层（L2）是关键**：LLM 在 EXECUTE 阶段调用 `optimize_cell_placement` 等工具时，不再依赖"几轮前看过的、已被压缩的工具输出"来回忆 cell 名，而是直接看到 Pinned 层注册表提供的规范化名。把"记忆重建"降级为"复制粘贴"，从根上消除 cell 名幻觉。Pinned 层由 `inject_pinned_cell_registry()` 每轮移除并重新插入（幂等，不累积），不进入 MessageStore，天然抗压缩。

---

## 12. 实体注册表与 cell 名边界校验（2026-06 新增）

### 12.1 根因：cell 名错误的上下文工程缺口

cell 实例名（如 `u_core/u_alu/lut1`）在原架构中只出现在三个不稳定来源：
- 工具原始输出（被 `summarize_tool_result` 压缩为摘要，cell 名丢失）
- Dashboard Module 2（仅 ANALYZE/SELECT_STRATEGY 可见，固定截断 top 8 路径）
- LLM 对话记忆（更早的 cell 名被 YAML 化或标记化）

结果：LLM 在 EXECUTE 阶段调用 cell-targeting 工具时只能凭"记忆重建" cell 名 → 拼写错误、截断、幻觉。

### 12.2 EntityRegistry（单一事实来源 + Pinned 上下文）

`optimizer/pure/entities.py` 定义 `EntityRegistry` dataclass，挂在 `OptimizerState.entity_registry`：

```
EntityRegistry
├── cells: dict[str, CellRef]      # canonical_name → {type, location, source_path_idx, last_seen_iter}
├── by_module: dict[str, set[str]] # 模块索引（第二路径段）
└── snapshot_version: int          # 设计变更后自增，触发 LLM 侧失效
```

**数据流**：
- `vivado_extract_critical_path_cells` 返回 → `update_critical_paths()` 解析 → 同步写入 `entity_registry`（`register_cells_from_entries`）
- `rapidwright_search_cells` 返回 → `sync_search_cells_result()` 同步写入注册表
- 设计修改工具（`DESIGN_MODIFICATION_TOOLS`）执行后 → `mark_stale()`（`snapshot_version += 1`），标记注册表 stale（EXECUTE 和 EVALUATE 两阶段均处理）
- rollback 后 → `entity_registry.clear()`（2026-06-27 新增：checkpoint 恢复后 cell 拓扑可能变化，旧名必须重新获取）
- LLM 调用工具时，工具参数中的 cell 名 → `validate_and_sanitize_cell_args()` 与注册表比对

**关键设计**：注册表不进入 MessageStore，而是和 Dashboard 一样每次从 state 重建注入（Pinned 层）—— 天然抗压缩。

### 12.3 边界校验层（tool_router）

`tool_router.call_tool` 在 LLM→MCP 咽喉处增加 `validate_and_sanitize_cell_args()`（部分放行+警告策略；2026-06-27 新增 `allow_unverified` 参数，设计修改工具强制 `allow_unverified=False`）：

| 情况 | 处置 |
|------|------|
| 格式合法 + 在注册表 | 放行（accepted） |
| 格式合法 + 不在注册表 | `allow_unverified=True`（默认）→ 标记 `[UNVERIFIED]` 放行；`allow_unverified=False`（设计修改工具）→ 剔除（2026-06-27 新增严格模式） |
| 格式非法（device site / bare type / pblock label） | 剔除并记录（rejected） |
| 全部非法 | 返回结构化富错误，**不调用 MCP** |

**富错误反馈协议**（教会 LLM 纠正，而非静默失败）：
```json
{
  "tool": "rapidwright_optimize_cell_placement",
  "status": "rejected",
  "reason": "invalid_cell_names",
  "invalid_names": ["SLICE_X38Y277"],
  "rejection_reasons": [{"name": "SLICE_X38Y277", "reason": "device_site"}],
  "suggested_canonical_names": ["u_core/u_alu/lut1", "u_core/u_alu/lut2"],
  "guidance": "Cell names must be hierarchical paths (contain '/')... Use names from the [CELL REGISTRY] section..."
}
```

校验函数 `_is_valid_cell_name()` 从 `critical_path.py` 提取到 `entities.py`（SSOT），router、parse、MCP 共享同一份校验逻辑。`critical_path.py` 通过 re-export 保持向后兼容。

### 12.4 auto-inject 策略统一

`phase_execute.py` 的"用 state 数据覆盖 LLM 参数"机制统一使用注册表过滤：

| 工具 | auto-inject | 来源 |
|------|------------|------|
| `rapidwright_execute_pblock_strategy` | ✅ | `extract_registry_cells_for_inject()`（注册表 + critical paths） |
| `rapidwright_flatten_lut_cascade` | ✅ | state.timing.critical_paths |
| `rapidwright_execute_combinational_rebalancing_strategy` | ✅ | state.timing.critical_paths |
| `rapidwright_execute_lut_muxf_repack_strategy` | ✅ | state.timing.critical_paths |
| `rapidwright_execute_muxf_tree_reorder_strategy` | ✅ | state.timing.critical_paths |
| `rapidwright_optimize_cell_placement` | ✅ **新增** | `extract_registry_cells_for_inject()`（注册表 + critical paths） |
| `rapidwright_optimize_lut_input_cone` | router 校验 | pin 格式校验 |

`extract_registry_cells_for_inject()` 优先取 critical path 上的 cell，再用注册表最近见过的 cell 回填，统一了 pblock 子串过滤与零过滤的不一致。

### 12.5 工具 schema 增强（契约即文档）

所有接收 cell 名的 MCP 工具，在 JSON schema 中补充格式契约：
- `items.pattern`: `^.+/.+$`（强制层级分隔符）
- `description`: 明确说明 device site / bare type 名非法，指引 [CELL REGISTRY]
- `examples`: 给出合法 cell 名示例

涉及参数：`cell_names`、`critical_path_cells`、`critical_paths`（含 dict 格式的 `cells`/`name` 子项）、`hierarchical_input_pins`。

### 12.6 涉及文件

| 文件 | 变更 |
|------|------|
| `optimizer/pure/entities.py` | **新增**：EntityRegistry + 校验 + 富错误 + Pinned 渲染 + auto-inject 提取 |
| `optimizer/state.py` | `OptimizerState` 新增 `entity_registry` 字段 |
| `optimizer/pure/critical_path.py` | `_is_valid_cell_name` re-export；`update_critical_paths` 同步注册表 |
| `optimizer/pure/tool_router.py` | `call_tool` 增加 `entity_registry` 参数 + 边界校验 + `run_dir` 参数 + `design_data_read`/`design_data_list_snapshots` 内部工具（2026-07） |
| `optimizer/pure/context_snapshot.py` | `inject_pinned_cell_registry()` + `extract_system_message()` 共享函数 + `_inject_phase_guard()` per-phase 注入 + DesignDataManager 全量数据持久化触发 + `format_state_space_for_llm` 传递截断参数（2026-07） |
| `optimizer/pure/state_space.py` | 添加 `unshown_path_stats`/`unshown_hotspots`/`unshown_high_fanout_nets` 截断聚合统计 + `truncation_advisory` 区段 + 所有新增字段 `[fresh]`/`[stale]` 标注（2026-07） |
| `optimizer/pure/design_data.py` | **新增**：DesignDataManager 设计数据持久化（store_raw_output/store_snapshot/read_design_data）+ 纯函数 compute_unshown_path_stats/compute_unshown_hotspot_stats（2026-07） |
| `optimizer/pure/tool_summary.py` | 新增 `design_data_read`/`design_data_list_snapshots` 工具摘要分支（2026-07） |
| `optimizer/pure/tool_filter.py` | 新增 `design_data_read`/`design_data_list_snapshots` 到所有 4 阶段白名单（2026-07） |
| `optimizer/pure/constants.py` | 新增 `DESIGN_DATA_DIR`/`DESIGN_DATA_MAX_FILES` 常量（2026-07） |
| `optimizer/state.py` | 新增 `DesignDataState` dataclass，挂载到 `ContextState.design_data`（2026-07） |
| `optimizer/nodes/prepare_context.py` | FORMAT_GUARD 拆分为 `BASE_FORMAT_GUARD` + `_PHASE_GUIDES` + `build_phase_format_guard()`；移除一次性注入逻辑 |
| `optimizer/nodes/subgraphs/phase_*.py` | 4 阶段 `_call_phase_llm` 调用 Pinned 注入 + 传 registry + `call_tool_fn` 新增 `run_dir` 参数 + 工具输出持久化钩子（2026-07） |
| `RapidWrightMCP/server.py` | cell 名参数 schema 增强（pattern/description/examples） |
| `dashboard/serializer.py` | 无需改动（`dataclasses.asdict` + `_make_json_safe` 自动处理 `entity_registry`，含 set→sorted list 转换） |

---

## 13. Tool 描述增强模式（2026-05 新增）

1. **LIMITATIONS / Contraindications**: 工具 description 标注不适用场景
2. **RESULT INTERPRETATION**: 指导正确理解工具返回值
3. **STRATEGY INTERACTION WARNING**: 策略交互警告
4. **`llm_hint` 运行时注入**: 当工具返回异常结果时注入提示
5. **SYSTEM_PROMPT.TXT 策略排序约束**: 策略目录的排列顺序规则
6. **~~strategy_library.py SKILL_GUIDANCE 增强字段~~**（已移除，死代码清理）

---

## 14. 工具链优化（2026-06 新增）

本组变更围绕工具链安全性、策略覆盖面和编译流程效率进行系统性增强。

| 编号 | 类别 | 变更 | 说明 |
|------|------|------|------|
| A1 | 安全 | place_design/route_design 白名单 | 新增 `PLACE_SAFE_DIRECTIVES` / `ROUTE_SAFE_DIRECTIVES` 安全指令白名单，阻止不安全 directive（详见 §8） |
| A2 | 安全 | AddRemap 加入 OPT_SAFE_DIRECTIVES | `vivado_opt_design` 的 `AddRemap` 指令加入安全白名单，允许 LLM 在逻辑优化阶段使用重映射 |
| A3 | 策略 | PlaceRouteDirectiveExplore + CongestionRouteExplore | 新增两种布线/拥塞相关策略，扩展策略库覆盖范围 |
| B1 | 工具 | report_qor_suggestions 专用工具 | 封装 Vivado `report_qor_suggestions` 为独立 MCP 工具，供 LLM 获取优化建议（Dashboard 字段: qor_suggestions） |
| B2 | 工具 | report_high_fanout_nets 专用工具 | 封装 Vivado `report_high_fanout_nets` 为独立 MCP 工具，供 LLM 诊断高扇出网络（Dashboard 字段: high_fanout_nets） |
| C2 | 编译 | set_incremental_checkpoint 增量编译支持 | 新增 `set_incremental_checkpoint` 工具，支持增量编译流程以加速多轮迭代 |
| C3 | 编译 | RWRoute 环境变量开关 | 提供环境变量控制 RapidWright 路由引擎（RWRoute）的启用/禁用，灵活适配不同设计场景 |

---

## 15. 已知数据质量缺陷（2026-07-04 运行日志分析发现）

本节记录运行时日志交叉分析（`dcp_optimizer_run-20260704_085355`）发现的数据质量问题，这些缺陷会导致 LLM 在策略决策时被错误数据误导。

### 15.1 `check_design_status` 对 routed 设计返回 `is_routed=false`

**现象**：`vivado_check_design_status` 返回 `is_placed=false, is_routed=false`，即使设计已完成布局布线。

**根因**：该工具通过 `get_property STATUS [current_design]` 获取状态（`VivadoMCP/vivado_mcp_server.py:2988-3022`）。在 `open_checkpoint` 加载 DCP 后，Vivado 2025.1 的 `STATUS` 属性返回**空字符串**。C1 修复曾假设 `IS_PLACED`/`IS_ROUTED` 属性可靠并作为回退，但日志证伪：`open_checkpoint` 后 `IS_PLACED`/`IS_ROUTED` 同样返回空字符串，回退到 STATUS 字符串匹配也为空，最终 `is_placed=false, is_routed=false, status="Unknown"`。

**影响**：LLM 看到的 `is_routed=false` 与真实状态完全矛盾，导致对已 routed 设计重新 unplace+place+route（浪费 ~11 分钟且时序退化）。

**修复**：当 `IS_PLACED`/`IS_ROUTED` 均返回空时，调用 `report_route_status -return_string` 解析实际网线布线状态兜底（`# of fully routed nets > 0` 且 `# of nets with routing errors == 0` => `is_routed=true, is_placed=true`；`# of routable nets > 0` => `is_placed=true`）。`report_route_status` 直接遍历网线路由状态，不依赖元数据属性，在 Vivado 2025.1 上可靠。

### 15.2 高扇出网线扫描阈值过高（init_analysis 使用 min_fanout=100）

**现象**：`init_analysis.py:230-237` 以 `min_fanout=100` 调用 `vivado_get_critical_high_fanout_nets`，`parse_high_fanout_nets()`（`timing.py:175-212`）解析格式化表格时返回 0 条记录，而实际设计存在 fanout >= 100 的网线（如 `M0w[19]` 扇出 259，`M1w[47]` 扇出 164）。

LLM 在 ANALYZE 阶段以 `min_fanout=50` 重查则正确返回 34 条。

**根因**：`get_critical_high_fanout_nets` 内部基于 `report_timing -return_string` 文本正则解析。第一次调用时函数内部确有数据（Vivado MCP 日志显示执行了 26 次父网线名解析），但 `parse_high_fanout_nets` 的表格行格式匹配严格（要求 `line.split()` 恰好 3 部分），父网线名中的特殊字符可能导致解析跳过。

**修复方向**：阈值降至 `min_fanout=50`；`parse_high_fanout_nets` 增加对异常网线名的容错。

### 15.3 Module 3（物理与拥塞指标）在策略切换后从 Dashboard 消失

**现象**：ANALYZE 阶段的 Dashboard（`state_space.py` 构建的 Module 3）包含完整的拥塞/高扇出/路由状态。但 EVALUATE 和 SELECT_STRATEGY #2 阶段的 Dashboard **完全没有 Module 3**。

**影响**：LLM 在第二次策略选择时"忘记"了自己在 ANALYZE 阶段发现的 34 条高扇出网线，认为 "Fanout: Need to check fanout first"（自相矛盾）。

分析表明这与 `format_state_space_for_llm()` 的 phase-aware 过滤逻辑有关——部分阶段会剔除被认为"不相关"的模块，但 PHYSICAL_CONGESTION 模块对策略决策始终相关。

**修复方向**：Module 3 在所有阶段保留结构，stale 字段标注 `[stale]` 而非移除。

### 15.4 拥塞工具摘要缺失

**现象**：`tool_summary.py` 对 `rapidwright_analyze_congestion` 的 `compact_tool_summary` 生成为 `{`（空 JSON 前缀），因为该 JSON 输出缺少 `message` 顶层键。而实际原始数据包含 10 个拥塞列和 `has_congestion_issues: true`。

**影响**：LLM 误认为拥塞不严重，排除了 CongestionSpreading 和 CongestionRouteExplore 策略。

**修复方向**：`tool_summary.py` 对 `rapidwright_analyze_*` 工具增加结构化摘要（提取 congested_columns_count/severity 等顶层键）。

### 15.5 路由状态 `total_nets=0` 解析错误（2026-07-04 已修复）

**现象**：`vivado_report_route_status` 返回 `{"total_nets": 0, "routed_nets": 37081}`——`total_nets` 解析为 0，`routed_nets` 也是错的（37081 实为 logical nets，非 fully-routed）。

**根因（两层）**：
1. **JSON 包装未解包**：MCP 工具 `vivado_report_route_status`（`vivado_mcp_server.py:2448`）把真实 Vivado 输出塞进 `raw_report` 字段返回 JSON。`parse_route_status()` 收到的是这段 JSON 而非裸文本，`raw_report` 内的换行被转义为 `\n`，`split('\n')` 后整段报告变成**一整行**。
2. **子串匹配与真实标签不符**：真实 Vivado 格式为 `# of logical nets` / `# of fully routed nets` / `# of nets with routing errors`（见 `dcp_optimizer_run-20260704_085355/vivado.log:11663`），而解析器找 `'total nets'`/`'nets total'` → 永不命中 → `total_nets=0`；`'routed' in s` 命中那一整行（含 "fully routed"），`re.search` 抓到行内第一个数字 37081（logical nets），`routed_nets` 也错。

**修复**（`optimizer/pure/timing.py:284`）：解析前先解包 JSON `raw_report`；按真实标签 `# of logical nets` / `# of fully routed nets` / `# of nets with routing errors` / `# of routable nets` / `# of nets not needing routing` 匹配。

**连带修复**（`optimizer/nodes/subgraphs/phase_execute.py` route-reuse guard）：guard 原查 `total_nets > 0`，但 `total_nets` = logical nets（设计里总存在，与是否布线无关）。修复前靠 bug（=0）让 guard 永远回退常规布线；修好 parser 后 `total_nets` 恒 >0 会让 guard 变成永真死代码、对未布线设计也启用 `-reuse`。改查 `routed_nets > 0`（真正表示"有先验布线"），与 guard 注释 "only if design has prior routing" 语义一致。`optimizer/pure/constants.py:425` 注释同步。

> **2026-07-05 更新**：上述 route-reuse guard 与全部 `reuse: True` 配置已彻底移除——Vivado `route_design` 根本不接受 `-reuse` 选项（实测 `Unknown option '-reuse'`），路由复用由 Vivado 默认行为自动完成。详见上节"route_design 路由复用"。

**验证**：`TestParseRouteStatus` 5 项单测（真实格式 / JSON 包装 / 未布线设计 / 空输入 / 千分位逗号）。全量 434 项通过。

### 15.6 定位文件总览

第一轮（2026-07-04 日志分析）与第二轮（DCP 客观数据准确度审计）修复状态：

| 文件 | 缺陷 | 优先级 | 状态 |
|------|------|--------|------|
| `VivadoMCP/vivado_mcp_server.py` | `check_design_status` 对 routed 设计返回 `is_routed=false`（`STATUS`/`IS_PLACED`/`IS_ROUTED` 在 `open_checkpoint` 后均返回空） | P3 | ✅ 已修（`report_route_status -return_string` 文本解析兜底；C1 的 `IS_PLACED`/`IS_ROUTED` 回退已被日志证伪） |
| `optimizer/nodes/init_analysis.py` | 高扇出扫描阈值 `min_fanout=100` 过高 | P0 | ✅ 已修（C2：降至 50） |
| `optimizer/pure/timing.py` | `parse_high_fanout_nets` `net_name=parts[2]` 截断多 token 网名 | P1 | ✅ 已修（C2：`" ".join(parts[2:])`） |
| `optimizer/pure/state_space.py` | Module 3 在 EXECUTE/EVALUATE 阶段被过滤（SELECT_STRATEGY 实际显示） | P2 | ✅ 已修（M1：EXECUTE/EVALUATE 保留 physical_congestion） |
| `optimizer/pure/tool_summary.py` | `design_data_read`/`list_snapshots` 分支 NameError 被静默吞掉 | P1 | ✅ 已修（M4：累加器初始化前移） |
| `optimizer/pure/timing.py:284` | `parse_route_status` 对 `total_nets` 的解析定位偏移 | P2 | ✅ 已修（§15.5） |
| `optimizer/pure/timing.py` | `parse_pvt_corner` 未测得时返回硬编码 `slow_0p95v_85c`（伪造测量值） | P0 | ✅ 已修（C4：返回 None + Dashboard N/A） |
| `optimizer/pure/critical_path.py` | `validate_critical_path_data` FF 计数恒零（`k.startswith("FD")` 与 `"FF"` 返回值不匹配） | P1 | ✅ 已修（C5：`k in ("FF","FF_REPLICA")`） |
| `optimizer/pure/entities.py` | `is_valid_cell_name` `"pblock" in name` 误拒 `u_core/pblock_controller/inst` | P3 | ✅ 已修（C6：仅检查 leaf 段） |
| `optimizer/pure/constants.py` | `vivado_run_tcl` 不在 `DESIGN_MODIFICATION_TOOLS` → TCL 改设计后 field_freshness 不降级 | P0 | ✅ 已修（F1：`is_modifying_tcl` 内容检测） |
| `optimizer/pure/constants.py` | `rapidwright_analyze_congestion` 不在 `DASHBOARD_REFRESH_MAP` | P2 | ✅ 已修（F3） |
| `optimizer/pure/constants.py` | `vivado_unplace_cells` 不在 `DESIGN_MODIFICATION_TOOLS` | P3 | ✅ 已修（F6） |
| `optimizer/nodes/subgraphs/phase_analyze.py`、`phase_select_strategy.py` | 自动刷新只置 `timing_summary=fresh`，遗漏 `cdc_paths` | P2 | ✅ 已修（F2：按 `DASHBOARD_REFRESH_MAP` 批量置 fresh） |
| `optimizer/nodes/subgraphs/phase_execute.py` | `_auto_refresh_critical_paths` 置 `critical_paths_stale=False` 不同步 `field_freshness` → `stale=false [stale]` 矛盾 | P1 | ✅ 已修（F4/F5：同步两套系统） |
| `optimizer/pure/state_space.py` | `delta_wns` 跨阶段/迭代残留，无抑制无标签 | P1 | ✅ 已修（F8：非 EXECUTE/EVALUATE 置 None） |
| `optimizer/pure/state_space.py` | `bram/dsp_utilization`、`ths_hold` 缺 `[fresh]`/`[stale]` 标签 | P1 | ✅ 已修（M2） |
| `optimizer/pure/state_space.py`、`state.py` | `total_control_sets`/`false_paths_count`/`multicycle_paths_count` 零值歧义 | P2 | ✅ 已修（M3：`None` + `_annotated_val`） |
| `SYSTEM_PROMPT.TXT` | 指示 LLM 信任 `vivado_get_cached_high_fanout_nets` 为 "canonical source"（缓存实为空） | P0 | ✅ 已修（M5：改为可空 + 刷新指引） |
| `optimizer/pure/design_data.py` | 持久化文件名 `tool_output_{name}_{round}.json` 缺 phase → 跨阶段覆盖 | P2 | ✅ 已修（M6：加 phase） |
| `optimizer/pure/design_data.py` | `_enforce_file_limit` 清理范围含快照数据文件 | P3 | ✅ 已修（M7：仅 `tool_output_*`） |
| `optimizer/pure/design_data.py` | `read_design_data` 回传的 `_meta` fresh 标签是持久化时刻的，无过期警示 | P2 | ✅ 已修（M9：`freshness_caveat` 字段） |
| `optimizer/nodes/prepare_context.py` | FORMAT_GUARD 称 "Trust `[fresh]` data" 过度承诺 | P3 | ✅ 已修（F7：改为"无修改记录"语义） |
| `VivadoMCP/vivado_mcp_server.py` | `report_route_status` envelope 硬编码 `route_errors:0`/`unrouted_nets:0` | P3 | ✅ 已修（C7：置 None，纯函数为唯一来源） |

第三轮（2026-07-05 上下文管理数据准确度/新鲜度审计，基于 `dcp_optimizer_run-20260705_092133`）：

| 文件 | 缺陷 | 优先级 | 状态 |
|------|------|--------|------|
| `VivadoMCP/vivado_mcp_server.py`、`optimizer/pure/constants.py`、`phase_execute.py`、3 个 skill | `route_design -reuse` 是 Vivado 非法选项（`Unknown option '-reuse'`），PBLOCK/physopt 链在 route 步必然失败回滚 | P0 | ✅ 已修（移除 `reuse` 参数/配置/guard/测试，Vivado 默认自动复用布线） |
| `optimizer/nodes/subgraphs/phase_execute.py`、`phase_handoff.py` | auto-chain 失败回滚后 Previous Phase Summary 仍报 "completed"，failed_strategies reason 误为 `strategy_ineffective` | P0 | ✅ 已修（chain 失败写入 `tool_errors` → `_determine_failure_reason` 返回 `tool_error`；handoff 显式 `Outcome: failed_restored`） |
| `optimizer/nodes/subgraphs/phase_execute.py` | post-eval 从 JSON/report 拿到当前 WNS 后未同步 `field_freshness[timing_summary]`，dashboard 显示 `wns_setup: -0.939 [stale]`（值新标签旧） | P1 | ✅ 已修（`_mark_timing_fresh` 在 physopt JSON/post_eval/auto-rollback/chain-restore 4 处同步） |
| `optimizer/pure/design_data.py`、`context_snapshot.py` | snapshot 在 EXECUTE 入口继承 pre-modification stale 标记，但 critical_paths 数据是刚 re-extract 的当前值（"数据新标签旧"） | P1 | ✅ 已修（snapshot.json + critical_paths.json meta 增 `data_currency`/`extraction_iteration`/`data_is_current`） |
| `optimizer/pure/entities.py` | Cell Registry 跨提取累积不剪枝，旧路径 cell（如 `[41]`）与新路径 cell（如 `[40]`）混排，LLM 难辨当前优先 | P2 | ✅ 已修（YAML 增 `current_extraction` 汇总行 + 历史 cell `iter=M` 注记） |
| `optimizer/state.py`、`state_space.py`、5 个 phase 节点、`rollback.py` | `critical_paths_stale=true (place/route changed)` 标签对 `open_checkpoint` 重载/`strategy switch`/`rollback` 均误标为 "place/route changed" | P2 | ✅ 已修（`critical_paths_stale_reason` 字段区分 4 种原因并渲染） |
| `optimizer/nodes/subgraphs/phase_analyze.py` | `resource_utilization` 全程 stale 未 auto-refresh，LLM 决策无当前 LUT/FF/DSP 数据 | P3 | ✅ 已修（ANALYZE 入口 `vivado_report_utilization_for_pblock` auto-refresh） |
| `RapidWrightMCP/server.py` | `execute_pblock_strategy` 硬编码 `resource_multiplier=2.0`（"FORCED"），LLM 传入的 multiplier 被无视、调参失效（日志中 1.2→2.0 的根源） | P0 | ✅ 已修（改为 `arguments.get("resource_multiplier", 2.0)`，默认保留 2.0 但尊重 LLM 覆盖；schema default 同步 2.0） |
| `optimizer/nodes/subgraphs/phase_evaluate.py` | chain 失败回滚后 `delta=0.0`（非 None），`_cool_down_current_strategy_if_stalled` 误判"策略无效"冷却；PBLOCK 因 `route_design` 失败被错误阻塞（与上行 handoff 层修复互补，handoff 归因对但 EVALUATE 仍冷却） | P0 | ✅ 已修（C1：`delta<=0` 分支检测 `tool_errors` 中 `chain=True` 记录，跳过冷却保持 retriable） |

**第三轮补充：LLM 可调参与上下文质量改进**（基于同一日志，bug 修复之外追加的接口增强，让 LLM 能有效调参并拿到充足、准确、客观的上下文与反馈）：

| 文件 | 改动 | 类别 |
|------|------|------|
| `skills/pblock_strategy.py` | 返回 dict 增 `utilization_density`/`density_warning`/`capacity_basis`/`input_multiplier`/`final_multiplier`/`multiplier_transform`/`region_selection_reason`；`_build_advice_sufficient` 按 density 分级（>90% 拥塞警告，不再无脑 "safely proceed"）；message 标注 multiplier 变换与 density | 上下文 |
| `skills/pblock_strategy.py`、`RapidWrightMCP/rapidwright_tools.py`、`server.py` | 新增 `max_utilization_density` 参数（默认 0.90），LLM 可控区域余量；density 超限返回 `density_warning=true`，advice 警告 | 可调参 |
| `optimizer/nodes/subgraphs/phase_execute.py` | critical_path_cells 覆盖时 `add_message` 增强：列出 cells 预览 + 覆盖原因（data quality guard），告知 LLM 其输入被覆盖 | 反馈 |

> **文档订正**：§15.2 原"parser 要求 `line.split()` 恰好 3 部分"有误，实为 `>= 3`，真正脆弱点是 `net_name=parts[2]` 只取首 token。§15.3 原"EVALUATE/SELECT_STRATEGY 阶段被过滤"有误——SELECT_STRATEGY 实际显示 Module 3，仅 EXECUTE/EVALUATE 隐藏。§15.4 原"`rapidwright_analyze_congestion` 摘要为 `{`"在当前代码不成立（`compact_tool_summary` 已有专用 handler 产出 `global_score=..., severity=...`），真实受影响的是 `design_data_read` 分支 NameError（M4）。

### 15.7 三条系统性根因

1. **双陈旧度系统漂移**（F4/F5）：`critical_paths_stale` 布尔与 `field_freshness["critical_path_cells"]` 字典独立更新必然漂移。修复：`_auto_refresh_critical_paths` 同步两套；未来可合并为字典 SSOT。
2. **静默零/空解析范式**（C2/C4/C8/M3）：多个 parser 在失败时返回与真实零无法区分的值，甚至伪造默认值（C4 PVT）。修复：失败返回 `None` + Dashboard `_annotated_val` 标注原因（`parse_resource_utilization` 为范例）。
3. **内存修复未同步磁盘**（M6）：`raw_tool_outputs` 内存键已修为 `(iteration, phase, round)` 三元，但 `DesignDataManager.store_raw_output` 文件名仍二元。修复：文件名加 phase。

### 15.8 第四轮：迭代间 handoff 归因与解析/状态/格式守卫修复（2026-07-09）

基于迭代间 handoff 与运行日志交叉审查发现的 4 个缺陷，仅做核心修复（不含四阶段统一、`_validate_phase_result` 控制流改动等增强项）：

| 文件 | 缺陷 | 修复 |
|------|------|------|
| `optimizer/pure/handoff.py` | TRAJECTORY 行将迭代净增益错误归因给最后选中但无效的策略（`narratives` 每迭代一条，策略名取迭代结束时的 `current_strategy`，delta 取迭代级净增量） | `_format_trajectory_brief` 改为同时接收 `optimization_history`，按 iteration 对齐：有生效记录则渲染 per-strategy delta（各策略真实贡献），无则回退 narrative 标签保留"试过 X 无效"可见性 |
| `VivadoMCP/vivado_mcp_server.py` | `place_design -unplace` 后关键路径 cell 行正则硬编码 Location 列，unplace 后 Location 为空导致三条 cell 正则全失配，返回 10 条路径但 cells 全空污染下游 | `RE_CELL_LINE`/`RE_CELL_LINE_BARE` 的 Location 改为可选 `(?:...)?`；追加条件收紧为 `len(cell_names)>=2`（不再 `or len(nodes)>=2`） |
| `optimizer/pure/execute_contracts.py`、`optimizer/nodes/subgraphs/phase_select_strategy.py`、`optimizer/state.py` | FORMAT_GUARD 违规（LLM 返回纯文本但未调 `report_step_state`）未被校验重试，违规消息写入历史导致下一轮策略偏移；`consecutive_empty_responses` "双重为空"条件使有文本无工具的违规重置计数 | 新增 `detect_format_guard_violation` 纯函数；`extract_step_state` 提前到 `add_message` 之前，违规时跳过写入、注入强提示、重试（限 2 次，不消耗 `tool_round`）；新增 `consecutive_no_tool_call` 计数器，"无工具调用"即计数并退出 |
| `VivadoMCP/vivado_mcp_server.py` | `check_design_status` 对已 routed DCP 返回 `Unknown/false/false`（`STATUS`/`IS_PLACED`/`IS_ROUTED` 在 `open_checkpoint` 后均返回空），误导 LLM 重新 place+route | 见 §15.1：`report_route_status -return_string` 文本解析兜底 |

### 15.9 第五轮：unplaced 设计污染 best_checkpoint 与级联崩溃修复（2026-07-10）

基于 `dcp_optimizer_run-20260710_002051` 四类日志（vivado/prompts/llm_response/optimization）交叉分析发现的 5 个缺陷。核心事故：`PlaceRouteDirectiveExplore` 用非法 directive（`Performance_NetDelay_high`）调用 `place_design` 失败后设计残留未完全布局，`physopt_and_route` 内部 `phys_opt_design`/`route_design` 双双失败（`Found unplaced instances`），但其 JSON 仍带 `post_optimization.wns=-0.003`（wireload 估算），被采信为 best 并覆盖了合法的 `-0.465` routed 检查点；最终输出 DCP 实为未布线设计，报告的 `-0.003` 是无效估算。`save_output` 的 `get_property STATUS` 因 DCP 内 STATUS 属性粘性误判为 "Routed"，未触发修复。

| 文件 | 缺陷 | 修复 |
|------|------|------|
| `optimizer/pure/tool_router.py`、`optimizer/nodes/subgraphs/phase_execute.py` | `_save_best_checkpoint` 写盘前不校验 routed，unplaced 估算 WNS 覆盖合法 best（Bug #1 核心） | 新增 `verify_design_routed`（调 `vivado_check_design_status`，走 §15.1 的 `report_route_status` 兜底，非粘性 STATUS）；`_save_best_checkpoint` 写盘前校验 `is_routed`，非 routed 拒绝保存并清 `needs_save` |
| `optimizer/nodes/subgraphs/phase_execute.py` | `physopt_and_route` JSON post-eval 路径在 `tool_result.ok=False`（partial/error）时仍用 `post_optimization.wns` 推进 `best_wns`/`needs_save`；`_track_wns_from_result` 无条件对 physopt 置 `design_state=ROUTED` | best 推进门禁 `metrics is not None and tool_result.ok`；physopt 仅在结果无 `error`/非 `partial` 时置 ROUTED，否则降级 UNPLACED |
| `optimizer/nodes/subgraphs/phase_execute.py` | `_post_eval_hook` 对非 routed 报告仍更新 `latest_wns` 并推进 `best_wns`（与 `_track_wns_from_result` 的门禁不一致） | `design_state != ROUTED` 时丢弃 WNS、保留上一已知值并返回 None（与 `_track_wns_from_result:1527` 门禁对齐） |
| `optimizer/nodes/save_output.py` | 交付前用 `get_property STATUS`+正则推断设计态，粘性 STATUS 把未布线 DCP 误判 "Routed"，未触发紧急 place+route | 改用 `_check_routed_state`（`vivado_check_design_status` 的 `is_placed`/`is_routed`）；非 routed 先 restore best 再紧急 place+route，修后仍非 routed 则报错；移除死函数 `_classify_design_state` 及 `re` 导入 |
| `optimizer/nodes/subgraphs/phase_execute.py` | `unplaced_without_replace` 仅按 directive 字符串复位，失败的 `place_design`（非法 directive）也清标志 -> phase 退出自动回滚未触发 | 仅在 `not tool_result.error` 时清标志，失败的重布局保持标志使自动回滚生效 |
| `optimizer/nodes/subgraphs/phase_select_strategy.py` | "Auto-refresh stale WNS" 直接把非 routed 报告的 WNS 写入 `latest_wns`（`-0.003` 估算首泄点） | `parse_design_state` 判定非 routed 时跳过 `latest_wns` 更新，保留上一已知值 |
| `optimizer/nodes/rollback.py`、`optimizer/pure/state_space.py` | rollback 把 `high_fanout_nets=None`（与 `critical_paths=[]` 不一致），`_build_netlist_quality` 直接迭代 -> `TypeError: 'NoneType' object is not iterable`，iter3/4 全崩（Bug #2） | rollback 改 `high_fanout_nets=[]`；`_build_netlist_quality` 迭代加 `or []` 兜底 |
| `optimizer/pure/tool_router.py` | LLM 把 `pin_paths` 等数组参数序列化为逗号字符串，MCP schema 拒绝 `is not of type 'array'`（Bug #3） | 新增 `_coerce_array_arguments`，对 `pin_paths`/`critical_paths`/`critical_path_cells`/`cell_names` 字符串自动切分为 list（list 值不动） |
| `optimizer/pure/tool_summary.py` | `place_design` 非法 directive 错误（`not a recognized directive`）被当作普通 error 行，LLM 误判为 DRC 并 unplace 重试（Bug #4） | `place_design` 摘要优先检测 directive 未识别，置 `status=error` 并给明确提示（用合法 directive，勿 unplace 重试） |

**验证**：`optimizer/test_routed_guards.py` 8 项新单测（array coerce + NoneType 兜底）；全量 246 项通过。重放该设计时应断言 `best_checkpoint.dcp` 经 `check_design_status` 得 `is_routed=true` 且 WNS 落在真实 routed 区间（`-0.465` 量级），不再出现 `-0.003` 估算伪改善。

### 15.10 第七轮：chain-skip 误判与成本累计器失真修复（2026-07-10）

基于 `dcp_optimizer_run-20260710_190708` 三层日志（optimizer / vivado / prompt-LLM）交叉分析报告的 P0 项修复。两个缺陷均破坏决策基础：前者让有效策略的 Vivado P&R 链从未执行，后者让预算监控失真 2.77x。

| 文件 | 缺陷 | 修复 |
|------|------|------|
| `optimizer/pure/tool_chain_policy.py`、`optimizer/pure/constants.py` | `should_skip_chain_for_empty_result` 的 `has_empty_payload` 仅检查 `optimized_cells`/`critical_paths`，plan 风格输出（`lut_muxf_repack`/`muxf_tree_reorder`/`opt_design`/`combinational_rebalancing` 返回 `status:"ready"` + 非空 `steps` + `analysis_summary`，无 `optimized_cells`/`critical_paths`）被误判为 "no data produced" 而跳过 Vivado P&R 链。运行中 LUTMUXFRepack 识别出 19 个 LUT↔MUXF 对 + 5 个 LUT5 候选并产出 ready plan，但其 `opt_design AddRemap`（整个运行唯一未试过的逻辑重构）从未执行 | 新增 `has_ready_plan`：`status in ("ready","planned") 且 steps 非空` 视为有数据、不跳过链。`steps`（非 `analysis_summary`）是信号——skipped plan 也会附 `analysis_summary` 但从不附 `steps`；`status="skipped"` 仍由既有 `is_skipped` 检查先行捕获。两份副本（运行时用 tool_chain_policy、测试用 constants）同步修复 |
| `optimizer/pure/cost_tracking.py`（新增）、`optimizer/nodes/subgraphs/phase_{analyze,select_strategy,execute,evaluate}.py` | 成本累计仅在 EXECUTE 阶段（私有 `_track_cost`）调用，ANALYZE/SELECT_STRATEGY/EVALUATE 的 LLM 调用成本未计入 `state.cost.total_cost`。运行报告 $0.1262 恰为 EXECUTE 阶段之和，真实 $0.3488（2.77x 低估），污染 `check_exit` 预算守卫与 LLM 引用的剩余预算 | 抽取共享 `track_llm_call_cost(state, response)` 至 `optimizer/pure/cost_tracking.py`，四阶段 `_call_phase_llm` 在 `log_call` 后统一调用；phase_execute 删除私有 `_track_cost`、改用共享函数，消除双实现 |

**验证**：`optimizer/test_pure.py` 新增 8 项单测——4 项 chain-skip（plan-style ready 不跳过 / skipped 仍跳过 / ready 无 steps 仍判空 / 运行时与 constants 双副本行为一致）+ 4 项 cost tracking（单次累计 / 多次累计 / cache+reasoning token / 缺 usage 为 no-op）；`TestToolContracts` + `TestCostTracking` 14 项通过，全量 261 项通过。`test_graph.py` 因预先存在的 `_execute_exit_reason_after_timing_update` 导入断裂未计入，与本次修复无关。

### 15.11 第八轮：P2 调优修复（2026-07-11，run-20260710_190708 复盘 P2 项）

基于同一份三层交叉分析报告的 P2（调优）项修复。P0（chain-skip + 成本累计）见 §15.10，本轮处理 P2.6–P2.10 五项。

| 编号 | 文件 | 缺陷 | 修复 |
|------|------|------|------|
| P2.6 | `optimizer/nodes/prepare_context.py` | EVALUATE 决策指引对 `verdict=IMPROVED` 给 CONTINUE / SWITCH_STRATEGY 等权，LLM 8 次 EVALUATE 全选 SWITCH_STRATEGY，单轮即放弃仍产增益的策略 | 强化 `verdict=IMPROVED` 指引为「PREFER CONTINUE 1-2 轮再 SWITCH」，并给出切换判据（2+ 轮收益递减或瓶颈转移） |
| P2.7 | `strategy_library.py`、`optimizer/pure/context_snapshot.py`、`optimizer/nodes/subgraphs/phase_select_strategy.py` | `phys_opt` 类策略（PhysOpt / PhysOptAggressive / MUXFTreeReorder，链式 `vivado_phys_opt_design`/`physopt_and_route`）在 WNS<-0.5ns 时被 Vivado 明确告警无效（Physopt 32-745），却仍可选可执行；运行中 phys_opt 在 WNS=-0.542 执行两次零增益、~26s 浪费 | 新增 `PHYSOPT_CLASS_STRATEGIES` + `PHYSOPT_INEFFECTIVE_WNS_THRESHOLD=-0.5`；`context_snapshot` 在 WNS<阈值时将三类策略标 `[BLOCKED]`（catalog 展示），`phase_select_strategy` 新增 `_get_wns_ineffective_strategies` 并入阻断集（执行层拒绝），引导 LLM 改用 route-directive 探索 |
| P2.8 | `optimizer/pure/tool_router.py` | `vivado_place_design`（非 unplace 指令）对已全部 placed 的设计是 no-op（Vivado Place 30-281「all instances are placed」），LLM 直调 `place_design ExtraTimingOpt` 在已布线设计上浪费 10.2s | `call_tool` 在派发 `vivado_place_design`（directive≠unplace）前用 `_session_is_fully_placed`（直连 MCP `check_design_status`，绕过缓存取新鲜 is_placed）短路：is_placed=True 时返回 `{"status":"skipped"}` 而非调用 MCP。对 opt_design 等链安全--place_design 只放置未放置 cell，is_placed=True 时调用必为 no-op；失败时返回 False（保守不跳过） |
| P2.9 | `RapidWrightMCP/rapidwright_tools.py` | `analyze_critical_path_spread` 的 `input_file` 模式读 JSON 后直接 `critical_paths_data[0]` 索引；持久化快照是 DesignDataManager 信封 `{"_meta":..., "data": <list|json-string>}`，对信封 dict 做 `dict[0]` → `KeyError: 0`，input_file 模式不可用 | 读文件后解封信封：dict 取 `data`（若为 JSON 字符串再 `json.loads`）；归一化行加 `isinstance(list)` 守卫，非 list 载荷不再触发 dict-key/char 索引 |
| P2.10 | `optimizer/pure/execute_contracts.py` | `resolve_chain_step_arguments` 同时 append `selected_plan.fallback_reason` 与 `skill_result_data["pblock_fallback_reason"]`（二者在 frozen-plan 路径同文本），单行告警出现 `reason \| reason`（2-3 次） | `pblock_fallback_reason` 改为 `if fallback_reason and fallback_reason not in notes` 去重，重复文本仅记录一次 |

**验证**：`tests/test_p2_fixes.py` 新增 7 项单测--P2.7（常量 / catalog 阻断 / WNS 守卫三态）+ P2.8（is_placed 解析 / 失败保守返回）+ P2.10（同文本去重 / 异文本均保留），7 项通过。`optimizer/test_pure.py` + `tests/test_context_engineering_fixes.py` 251 项通过（2 项 `TestContradictionFixesP1` TTL 断言为预先存在，与本次修复无关；`test_graph.py` 仍因 `_execute_exit_reason_after_timing_update` 导入断裂未计入）。

