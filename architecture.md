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
- **无持久化**: 快照不进入 MessageStore，完全绕过压缩系统，每次 API 调用从当前状态重建
- **无策略引导**（2026-06）：`design_delay_profile` 不再附带 `strategy_hint`，`_append_architecture_hints()` 重命名为 `_append_architecture_insights()`，仅输出纯数据描述
- **设计状态标注（DesignState 枚举）**: 从 `report_timing_summary` 的 `Design State` 字段解析，设置 `state.timing.design_state` 为 `DesignState.UNPLACED`（未布局） / `PLACED`（仅布局） / `ROUTED`（已布线）。Dashboard M1 根据状态显示不同粒度的警告：UNPLACED→"WNS based on wireload estimates"，PLACED→"WNS based on estimated routing delays"。非 ROUTED 状态时 Level 1 RW 预检查自动跳过。
- **`do_not_repeat` 推导**: 从 `state.iteration.tools_used` 聚合被调用 > 3 次且 WNS delta < 0.01ns 的工具，最多 5 条
- **`strategy_catalog` 排除机制**: `strategy_ineffective`（TTL 阻断）和冷却策略不在 catalog 中移除，而是标为 `[BLOCKED]` 占位符（含剩余轮数/原因）。`strategy_not_applicable`、`tool_error`、`no_improvement` 完全移出 catalog（可立即重试）。排除逻辑在 `inject_merged_dashboard()` 中拆分 hard-exclude vs blocked 两组。
- **`field_freshness` 逐字段新鲜度追踪**: `refreshed_fields: set[str]` 升级为 `field_freshness: dict[str, str]`，为每个Dashboard字段独立追踪 `"fresh"`/`"stale"` 状态。`init_analysis` 完成后全部初始化为 `fresh`；工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段为 `fresh`；设计修改工具（`DESIGN_MODIFICATION_TOOLS` 共23个，2026-06-27 补充5个缺失工具）执行后全部降级为 `stale`（EXECUTE 和 EVALUATE 两阶段均处理）。Dashboard 中每个值后显示 `[fresh]`/`[stale]` 标记，供LLM决策是否信任。
- **TTL 机制**: `strategy_ineffective` 策略在 `STRATEGY_RETRY_TTL=3` 轮迭代后自动解封（`blocked_until_iter` 字段）。`strategy_not_applicable` 及其他 reason 无 TTL 阻断（`blocked_until_iter=current`），可在下一轮立即重试。`record_strategy_failure` 去重时刷新 `blocked_until_iter`（2026-06-27 修复：之前去重后不刷新，导致反复失败策略永久不再被阻断）。

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
| 策略目录分层暴露 | `inject_merged_dashboard()` | `strategy_ineffective` + 冷却策略标为 `[BLOCKED]` 占位符；`tool_error`/`no_improvement` 完全移出；`get_strategy_catalog(blocked_strategies=...)` 渲染 |
| 空结果模式匹配 | `iteration_end.py` | `optimized_count: 0` → `tool_error`（可重试）非 `strategy_ineffective`（永久排除） |
| Improvement 阈值 | `STRATEGY_IMPROVEMENT_EPSILON_NS=0.050` | 低于 50ps 视为无改善 |

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
        {"tool": "vivado_place_design", "args": {"directive": "unplace"}},
        {"tool": "vivado_create_and_apply_pblock",
         "args_from_skill": {
             "pblock_name": "pblock_name",
             "pblock_ranges": "pblock_ranges",
             "is_soft": "is_soft_recommended",
         }},
        {"tool": "vivado_place_design", "args": {}},
        {"tool": "vivado_route_design", "args": {}},
    ],
}
```

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

**`_EMPTY_RESULT_PATTERNS` 空结果模式匹配**：当 LLM 调用 `SWITCH_STRATEGY` 且工具输出包含 `"0 candidates"` / `"no candidates"` / `"optimized_count": 0` 等模式时，归类为 `tool_error`（冷却后可重试）而非 `strategy_ineffective`（永久排除）。

**分级过滤逻辑**：
- `reason="strategy_ineffective"` → 永久排除
- `reason ∈ {tool_error, execution_failure}` → 冷却 2 个迭代后可重试
- `reason = "data_quality_error"` → 冷却 3 个迭代（同 `strategy_ineffective`）

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

### 4.5 report_step_state 格式提醒（双重提醒）

**提醒 1 — User Message（一次性，精简版）**: FORMAT_GUARD（约 4000 字符，含 EXECUTE 工具映射 + DESIGN CONSISTENCY 要求 + CELL NAME CONTRACT），由 `format_guard_injected` 标志控制，仅注入一次。

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

`MAX_STRATEGY_CYCLES=5`。EVALUATE 的 `SWITCH_STRATEGY` 信号触发循环回 SELECT_STRATEGY（跳过 ANALYZE）。失败策略通过 TTL 机制（3 轮迭代后自动解封）而非永久阻止。

---

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

`STRATEGY_MAP` 新增 `"LogicOptimization": StrategyEntry("opt_design_strategy", "rapidwright_execute_opt_design_strategy")`。FORMAT_GUARD 策略列表同步添加。`_STRATEGY_MAPPING_LINES` 自动生成。

### 6.6 Dashboard 数据新鲜度与工具 rate limit

**字段级新鲜度追踪**: `field_freshness: dict[str, str]`（2026-06-24 新增，替代 `refreshed_fields: set[str]`）。每个 Dashboard 数据字段独立标注 `[fresh]`/`[stale]`。初始化全部 `fresh`，工具调用通过 `DASHBOARD_REFRESH_MAP` 刷新对应字段，设计修改工具（`DESIGN_MODIFICATION_TOOLS`）执行后全部降级为 `stale`。EXECUTE 和 EVALUATE 两阶段对称处理（2026-06-27 修复：EVALUATE 之前缺失 stale 标记逻辑，造成 false-fresh）。

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
2. 保存 pre-unplace checkpoint 到 `/tmp/pre_unplace_{iter}_{round}.dcp`
3. 后续 `vivado_place_design`（非 unplace）清除标志
4. 阶段退出时若标志仍为 True → 从 pre-unplace checkpoint 恢复 + 刷新 WNS/TNS/FE

### Layer 4: Vivado 执行工具错误检测
- VivadoMCP 检测 `^ERROR: [` 文本模式，返回 JSON `{"error": "..."}`
- `phase_execute.py` 检查 JSON `error` 键 + 文本 `ERROR: [` 模式
- 任一检测到错误: 中止链 + 从 pre-chain checkpoint 恢复

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
    "EarlyBlockPlacement", "LateBlockPlacement", "NetDelay_high", "NetDelay_medium",
    "NetDelay_low", "SSI_SpreadLogic_high", "SSI_SpreadLogic_low",
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
    "Congestion_Explore", "Congestion_NetDelay_high", "Congestion_NetDelay_medium",
    "Congestion_NetDelay_low", "SSI_Explore", "SSI_Quick",
    "Area_Default", "Area_Explore", "AlternateRoutability",
}
```

**call_tool 入口守卫**: 检查 directive 参数和 retime/interconnect_retime 布尔选项。place_design/route_design 指令需在 SAFE_DIRECTIVES 白名单中方可执行。

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

### 上下文注入层次（分层上下文管理，2026-06 增强）

显式四层注入架构，每层有明确的生命周期与注意力权重策略：

| 层 | 生命周期 | 内容 | 注入位置 |
|------|---------|------|---------|
| L1 STATIC | 不变（system message） | SYSTEM_PROMPT.TXT + FORMAT_GUARD | 消息列表最前 |
| L2 PINNED | 每轮重建，绕过压缩 | **CellNameRegistry 快照**（canonical cell 名 + 模块索引） | system 之后，独立 user 消息 |
| L3 DYNAMIC | 每轮重建，phase-aware | Dashboard 7-module StateSpace | 最后一条 user 消息 |
| L4 EPHEMERAL | 受压缩管理（preserve_role_turns=6） | 最近对话轮次 + 压缩后 YAML 历史 | 消息列表主体 |

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
| `optimizer/pure/tool_router.py` | `call_tool` 增加 `entity_registry` 参数 + 边界校验 |
| `optimizer/pure/context_snapshot.py` | **新增** `inject_pinned_cell_registry()` |
| `optimizer/nodes/prepare_context.py` | FORMAT_GUARD 增加 "CELL NAME CONTRACT" 段，指引 LLM 使用 [CELL REGISTRY]、禁止 device site / bare type 名 |
| `optimizer/nodes/subgraphs/phase_*.py` | 4 阶段 `_call_phase_llm` 调用 Pinned 注入 + 传 registry |
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
