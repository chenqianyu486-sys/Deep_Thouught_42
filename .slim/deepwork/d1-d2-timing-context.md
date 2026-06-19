# D1+D2: 关键路径逐点延迟分解 + 时钟域上下文

## 目标
解决上下文工程核心断层：LLM 能看到 *WHAT* fails 但看不到 *WHY* a specific cell/net dominates delay。
- D1: 结构化保留逐点延迟分解（每 cell 增量 logic delay、每 net 增量 route delay、cumulative arrival）
- D2: 提取时钟域上下文（clock skew、uncertainty、source/dest clock、path group、cross-clock 标记）

## 已确认研究上下文（来自 explorer 报告 + 代码验证）

### 根因（决定性证据）
`VivadoMCP/vivado_mcp_server.py:626-761` `extract_critical_path_cells`:
- 运行 `report_timing -return_string`，原始输出**含完整 Data Path Delay 逐点分解**
- 解析器第 725/728 行 `logic_delay += incr_delay` / `net_delay += incr_delay` 把逐点增量**求和丢弃**
- 第 693-699 行 `dash_count <= 2: continue` **跳过 clock launch 段**（含 skew/uncertainty）
- 第 696 行 `break` 跳过 destination clock / required time 段
- 只保留 5 个聚合字段 `{cells, slack, logic_delay, net_delay, levels}`

### 真实 report_timing 格式（已验证样本）
来源: `RapidWright/tcl/rwroute/dump_all_dsp_delay.tcl:469-514`
- Header 段: Slack / Source / Destination / Path Group / Path Type / Requirement / Data Path Delay / Logic Levels / Clock Path Skew / DCD / SCD / CPR / Clock Uncertainty
- ---1--- clock launch 段（当前被跳过）
- ---2--- data path 段（当前唯一被解析）: Location + CellType (Prop_xxx) 行 + delay 行 + net 行
- ---3--- clock capture 段 + pessimism/uncertainty + setup + required/arrival/slack

### 关键数据结构
- `optimizer/state.py:45-53` CriticalPathEntry: cells/path_length/iteration/slack/logic_delay/net_delay/levels
- `optimizer/state.py:493-502` DashboardTimingPath: endpoint_name/source_clock/dest_clock/slack/logic_delay_pct/route_delay_pct/logic_levels/path_group
- `optimizer/pure/critical_path.py:23-31` 常量: MAX_CRITICAL_PATHS=10, DISPLAY_LIMIT_*, DISPLAY_CELLS_PER_PATH=6
- `optimizer/pure/state_space.py:40` MAX_VIOLATING_PATHS=20
- `optimizer/pure/state_space.py:806-845` _convert_critical_path: **靠字符串匹配 cell 名猜时钟域**（bug 源，第 821-831 行硬编码 clk_fpl26contest）

### 注入链路
1. `extract_critical_path_cells` (VivadoMCP) → JSON
2. `parse_critical_path_cells` (critical_path.py:84) → list[dict]
3. `update_critical_paths` (critical_path.py:139) → state.timing.critical_paths (CriticalPathEntry)
4. `_build_timing_clusters` (state_space.py:162) → DashboardTimingClusters
5. `_convert_critical_path` (state_space.py:806) → DashboardTimingPath
6. `format_state_space_for_llm` (state_space.py:472) → YAML 文本（M2 第 576-591 行）
7. `inject_merged_dashboard` → 最后一条 user 消息

### 消费者（向后兼容约束）
- skills: lut_cascade_flattening (list[list[str]]), critical_path_cell_replication (list[dict]), pin_swapping (list[dict]), register_retiming (list[dict])
- RapidWrightMCP/rapidwright_tools.py: list[list[str]] / list[dict]
- compute_violation_summary: 用 entry.cells/slack/logic_delay/levels
- 约束: `cells` 字段必须保留不变，新字段可选

### 预算配置（model_config.yaml）
- worker: max 250K, soft 175K, hard 200K, token_budget 80K
- planner: max 1M, soft 200K, hard 300K, token_budget 80K
- 用户已批准放宽 token 预算

## 实施计划（8 步）

### Step 0: model_config.yaml 预算提升
- worker token_budget 80K→100K, soft 175K→180K
- planner token_budget 80K→120K, soft 200K→250K
- 压缩截断: worker 时序报告 4000→8000, planner 12000→16000, 工具结果保留 5→8
- verify: config_loader.py 加载无报错

### Step 1: state.py 数据结构
- 新增 PathNode dataclass (kind/name/cell_type/location/incr_delay/cumul_delay/fanout/net_status)
- 新增 ClockDomainInfo dataclass (source_clock/dest_clock/path_group/path_type/requirement/clock_skew/clock_uncertainty/source_clock_delay/dest_clock_delay/is_cross_clock)
- 扩展 CriticalPathEntry: +nodes/startpoint/endpoint_pin/arrival_time/required_time/clock/top_delay_nodes
- 扩展 DashboardTimingPath: +startpoint/clock_skew/clock_uncertainty/is_cross_clock/delay_hotspots
- verify: import 无报错

### Step 2: vivado_mcp_server.py 解析器重写
- 重写 extract_critical_path_cells (626-761)
- 解析 header: Source/Dest/Path Group/Path Type/Requirement/Skew/DCD/SCD/Uncertainty/required/arrival/slack
- 解析 data path: Location+CellType(Prop_) 行 + delay 行 + net 行 → PathNode 列表
- 计算 top_delay_nodes (top-3 by incr_delay)
- 保留 cells 字段向后兼容
- verify: 用 dump_all_dsp_delay.tcl 样本文本单元测试

### Step 3: critical_path.py 透传
- parse_critical_path_cells: 透传 nodes/startpoint/endpoint_pin/arrival/required/top_delay_nodes/clock
- update_critical_paths: 构造 PathNode/ClockDomainInfo
- format_critical_paths_snapshot/handoff: 追加 startpoint/skew/uncertainty/cross-clock 摘要
- verify: 既有测试通过

### Step 4: state_space.py _convert_critical_path 重写
- 删除字符串猜测时钟域（第 821-831 行）
- 用 entry.clock 真实字段
- 填充 delay_hotspots (top-5)
- verify: test_state_space.py 通过

### Step 5: 常量调整
- MAX_CRITICAL_PATHS 10→15
- DISPLAY_CELLS_PER_PATH 6→10
- 新增 MAX_DELAY_HOTSPOTS=5
- DISPLAY_LIMIT_SNAPSHOT 8→10
- verify: 单元测试通过

### Step 6: state_space.py 相位裁剪重写
- 全相位诊断可见（EXECUTE/EVALUATE 不再只走 M2b）
- ANALYZE top-3 路径展开全量 nodes
- M2 格式化追加 startpoint/clock_skew/clock_uncertainty/delay_hotspots
- verify: test_state_space.py 断言 EXECUTE 输出含 delay_hotspots

### Step 7: 端到端 + 文档
- make run_init_analysis DCP=demo_corundum_25g_misses_timing.dcp
- 更新 architecture.md §3.5 + PROJECT_TREE_AND_DATA_FLOW.md §3.2

## Oracle 审阅结果（CONDITIONAL GO）

### 已解决的 Blocker
- **B1 压缩配置歧义**: 已澄清。WorkerCompressor(worker_compress.py:60-63) 和 PlannerCompressor(planner_compress.py:59-62) 都 override `_get_adaptive_max_chars`，基类 yaml_structured_compress.py:924 的 8000 不生效。实际生效: worker timing=4000, planner timing=12000。history_retrieval_limit 在 interfaces.py:75 默认 5，model_config.yaml worker=8/planner=10 已覆盖。Step 0 只改 worker_compress.py:63 + planner_compress.py:62 + model_config.yaml token_budget/soft_threshold。

### 已纳入的 Major 修复
- **M1 endpoint cell 解析**: Step 2 必须处理三种行模式: (a) cell+Prop_ 行后跟 delay 行 (b) endpoint cell 行 pin 在同行无 delay (incr=0) (c) net 行。pending_cell 配对 + endpoint 同行发射。
- **M2 replication skill 预存 bug**: critical_path_cell_replication_strategy.py:182-197 期望 cells:[{name,delay,type,fanout}]，当前返回 cells:list[str]（预存 bug，test_mode 喂合成数据掩盖）。新增 Step 3b: 从 nodes 派生 cells_rich 字段供该 skill 消费。这是 D1 的直接收益。
- **M3 cells+nodes 冗余**: cells 从 nodes 派生 `[n.name for n in nodes if n.kind=="cell"]`，单一事实源，不重复存储。
- **M4 JSON 截断**: tool_summary.py:20 filter_tool_result 对非 timing 工具走通用 head/tail 截断会破坏 JSON。修复: (a) filter_tool_result 增加 extract_critical_path_cells 到 timing 关键字分支 (b) summarize_tool_result 增加 vivado_extract_critical_path_cells 专门分支提取 top_delay_nodes/clock_skew 进 key_details。

### 已纳入的 Minor 修复
- **m1 clock 空值 fallback**: entry.clock=None 时 dashboard 显示 ? （非崩溃），硬编码 clk_fpl26contest 被移除（行为变更，可接受）
- **m2 hold 超范围**: 仅 -delay_type max，hold 不在本次范围
- **m3 Vivado 版本**: 正则用 ^-{3,} 宽松匹配（既有代码已是此模式）
- **m4 pins 工具不改**: extract_critical_path_pins 仅需 data path pins，不改是正确的
- **m5 EXECUTE 热点精简**: EXECUTE/EVALUATE 用单行摘要 `delay_hotspots: cell_X=0.357ns(16%), cell_Y=0.079ns(14%)`，ANALYZE 用完整列表
- **m6 并行机会**: Step 0(config) + Step 5(constants) 独立于 Step 1-4，可并行
- **m7 Step 2 拆分**: 2a header 解析 / 2b data path nodes / 2c top_delay_nodes

## 修订后实施顺序
0. model_config.yaml + worker_compress.py + planner_compress.py (config, 独立)
1. state.py 数据结构
2a. vivado_mcp_server.py header 解析（clock 字段）
2b. vivado_mcp_server.py data path nodes 解析（PathNode + endpoint 处理）
2c. vivado_mcp_server.py top_delay_nodes 计算 + cells 从 nodes 派生
3a. critical_path.py 透传新字段
3b. critical_path.py 新增 cells_rich 派生（供 replication skill）
4. state_space.py _convert_critical_path 重写 + M2 格式化
5. 常量调整（独立，可与 2-4 并行）
6. state_space.py 相位裁剪（EXECUTE 单行摘要，ANALYZE 全量）
7. tool_summary.py 修复 JSON 截断 + 增加 critical_path_cells 摘要分支
8. 端到端验证 + 文档同步

## 状态
- [x] Step 0 — model_config.yaml + worker/planner_compress.py 预算提升
- [x] Step 1 — state.py PathNode/ClockDomainInfo + 扩展 CriticalPathEntry/DashboardTimingPath
- [x] Step 2a/2b/2c — vivado_mcp_server.py 解析器重写（header+nodes+top_delay+cells派生）
- [x] Step 3a/3b — critical_path.py 透传 + cells_rich 派生
- [x] Step 4 — state_space.py _convert_critical_path 重写 + M2 格式化
- [x] Step 5 — 常量调整（MAX_CRITICAL_PATHS=15, DISPLAY_CELLS=10, MAX_DELAY_HOTSPOTS=5）
- [x] Step 6 — state_space.py 相位裁剪（ANALYZE 全量, EXECUTE 单行摘要）
- [x] Step 7 — tool_summary.py JSON 截断修复 + critical_path_cells 摘要分支
- [x] Step 8 — 端到端验证 + 文档同步

## 验证结果
- 61 个既有测试全部通过（零回归）
- 端到端集成测试通过：Vivado report_timing → parser → state → dashboard → LLM YAML
- D1 字段验证: startpoint, nodes (per-cell/net incr_delay), top_delay_nodes, delay_hotspots
- D2 字段验证: source_clock, dest_clock, clock_skew, clock_uncertainty, is_cross_clock
- 向后兼容: cells/slack/logic_delay/net_delay/levels 保留，legacy list[str] 格式仍可解析
- 预存 bug 修复: critical_path_cell_replication skill 现在可通过 derive_cells_rich 获得期望的 rich 格式
