# FPL26 优化竞赛 -- FPGA 时序收敛 Agent

基于 LLM Agent 的 FPGA EDA 全流程优化系统，参赛 FPL 2026 优化竞赛。

通过 LLM（DeepSeek V4 Flash）编排 Vivado 和 RapidWright 工具链，自动执行 P&R 优化策略（PBLOCK、PhysOpt、Fanout 优化等），迭代逼近时序收敛目标（WNS >= 0）。

## 架构概述

系统提供两套并行的 Agent 架构：

| 维度 | V1 (消息对话驱动) | V2 (状态机驱动) |
|------|-------------------|-----------------|
| 入口 | `DCPOptimizer.optimize()` (dcp_optimizer.py) | `optimize_v2()` + `optimizer/` 包 |
| 状态管理 | 分散在类属性和局部变量 | 集中式 `OptimizerState` (6个类型化子切片) |
| 流程控制 | 隐式，嵌入 LLM 对话循环 | 显式图拓扑：8节点 + 条件边 |
| 可观测性 | ad-hoc 日志 | `StateTracer` JSON 状态转换追踪 |
| 纯函数 | 混合在 ~8000 行类中 | 提取到 `optimizer/pure/` (10个独立模块，可单测) |
| 默认状态 | 旧版（需显式 `--v2` 或 `make run_optimizer_v1`） | **默认**（`make run_optimizer` 自动加 `--v2`） |

**V2 状态机拓扑**:
```
init_analysis -> [条件: WNS >= 0?]
  |-- YES -> save_output -> end
  +-- NO  -> iteration_start -> select_model -> prepare_context
            -> llm_tool_loop(子图) -> iteration_end -> check_exit
            -> [条件: done?]
              |-- YES -> save_output -> end
              +-- NO  -> iteration_start (循环)
```

详见 [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md) 的第 1.1 和 1.2 节。

## 设计意图

| # | 原则 | 一句话 |
|---|------|--------|
| 1 | 强制但不阻塞 | 代码级检测 `report_step_state` 缺失，自动合成 CONTINUE，不因 LLM 疏忽死锁 |
| 2 | 事实而非判断 | Dashboard 只含原始测量值，注入为末条 user message（最大注意力权重） |
| 3 | 去除冗余 | Dashboard 是唯一实时数据源；Handoff 仅传递迭代间记忆（trajectory + failed_strategies + exit_reason），不重复 Dashboard 已有的 WNS/critical paths |
| 4 | 显式优于隐式 | 8 节点状态机 + 条件边，`StateTracer` 记录 JSON 轨迹 |
| 5 | 关注点分离 | Worker（250K，执行）/ Planner（1M，规划），不同压缩策略 |
| 6 | 单一调用路径 | V2 仅原生 function call，移除 XML/YAML 文本回退 |
| 7 | 单一事实来源 | 运行时数据写入 `OptimizerState`，消除 `MemoryManager` shadow 副本 |
| 8 | 领域知识编码 | 9 个策略 + 触发条件，系统呈现可用选项，LLM 自主选择 |
| 9 | 数据可信 | Dashboard 字段新鲜度追踪（`DASHBOARD_REFRESH_MAP`），过时数据自动标注 |
| 10 | 信息保留 | 压缩标记保留关键指标（WNS/TNS/FE/delta/status），LLM 可决策无需重调工具 |

## flow_control Tool 设计

LLM 每轮响应必须调用 `report_step_state(step_id, result_status, flow_control, strategy_phase, strategy_name)` 工具，通过 `flow_control` 字段控制迭代行为，通过 `strategy_phase` / `strategy_name` 追踪 4 阶段策略生命周期。系统在工具执行前检查该信号，决定是继续执行还是退出循环。

**信号语义**:

| 信号 | 含义 | 系统行为 | is_done | 记录失败 |
|------|------|---------|---------|---------|
| `CONTINUE` | 当前策略仍在推进 | 继续工具循环 | - | - |
| `NEXT_ITERATION` | 本轮成功改善，边际收益趋零 | 结束迭代，进入下一轮（新上下文 + 模型重评估） | False | 否 |
| `SWITCH_STRATEGY` | 当前策略无效/失败 | 结束迭代 + 注入强制分析引导 | False | **是** |
| `DONE` | WNS >= 0，时序收敛 | 退出优化 | **True** | 否 |
| `EXHAUSTED` | 所有策略用尽 | 退出优化 | **True** | 否 |
| `RETRY` / `ROLLBACK` | LLM 级别指导 | 不触发系统动作，继续循环 | - | - |

**设计要点**:
- `NEXT_ITERATION` 与 `SWITCH_STRATEGY` 的关键区别：前者表示"策略成功但该换轮了"，后者表示"策略失败了"。这避免了 LLM 在一个迭代内穷举所有策略。
- `DONE` 的安全网：若 LLM 在 WNS < 0 时误用 `DONE`，系统不退出优化，而是以 `done_reason="flow_control_done_next_iteration"` 进入下一轮。
- 缺失处理：若 LLM 未调用 `report_step_state`，系统自动合成 `CONTINUE` 并注入提示，不因格式疏忽死锁。
- Dashboard 每轮注入为末条 user message，包含 per-path slack/logic_delay/net_delay/levels 等时序细节，辅助 LLM 判断何时切换信号。

详见 [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md) 的 5.3 节。

## 快速开始

```bash
# 环境设置（Java、Vivado、RapidWright、aiohttp）
make setup

# 运行 v2 优化器（默认，状态机驱动）
make run_optimizer DCP=input.dcp

# 运行 v1 优化器（消息对话驱动）
make run_optimizer_v1 DCP=input.dcp

# V2 测试模式（无 LLM，验证工具和 Skill 调用）
make run_test_v2 DCP=demo_corundum_25g_misses_timing.dcp
make run_skill_test_v2 DCP=demo_corundum_25g_misses_timing.dcp

# 启用 Web Dashboard 实时监控（浏览器打开 http://localhost:8080）
make run_optimizer_dashboard DCP=input.dcp
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090  # 自定义端口
```

**环境变量**:
- `OPENROUTER_API_KEY` -- OpenRouter API 密钥（必需）
- `VIVADO_EXEC` -- Vivado 可执行文件路径（默认 `vivado`）
- `JAVA_HOME` -- Java 安装路径（RapidWright 依赖）

## Web Dashboard 实时监控

启动优化器时附加 `--dashboard` 即可开启 Web 监控界面，基于 aiohttp + WebSocket 实时推送状态快照。

```bash
# 启动（默认端口 8080）
make run_optimizer_dashboard DCP=input.dcp

# 自定义端口
make run_optimizer_dashboard DCP=input.dcp DASHBOARD_PORT=9090
```

浏览器打开 `http://localhost:8080`（或指定端口），页面包含以下面板：

| 面板 | 监控内容 |
|------|---------|
| **Timing** | WNS / TNS / Failing Endpoints 实时值，WNS 历史折线图（含零线标注），Critical Paths 列表 |
| **Iteration** | 当前迭代数、无改善计数、工具轮次、错误数、策略序列、本轮工具使用情况 |
| **Strategy Lifecycle** | 4阶段指示器（ANALYZE→SELECT→EXECUTE→EVALUATE）、当前策略名称、当前阶段、评估结果（IMPROVED/REGRESSION/UNCHANGED） |
| **Model** | 当前模型名称、Worker / Planner 模型、LLM 调用次数、连续成功 / 失败计数 |
| **Cost** | 总费用（带进度条）、Token 统计（prompt / completion / reasoning） |
| **Control** | 运行状态（Running / DONE）、已耗时、超时阈值、压缩次数、输入 / 输出 DCP 路径 |
| **Transition History** | 节点切换历史表格（时间、节点、迭代、WNS、模型、费用、耗时） |
| **LLM Log** | 最新的 LLM 用户 prompt 和 assistant 响应 |

**特性**:
- 断线自动重连（3 秒间隔），心跳检测保持连接活性
- 状态值变化时闪烁高亮，WNS 图表自动滚动
- 通过 `DashboardStateTracer` 在每次状态机节点退出时推送序列化快照

## 项目结构

| 目录/文件 | 用途 |
|-----------|------|
| `dcp_optimizer.py` | 主 Agent 编排入口（v1 消息对话 + v2 状态机 `optimize_v2()` 入口） |
| `optimizer/` | v2 状态机驱动 Agent 框架（LangGraph 风格，8节点图） |
| `optimizer/pure/` | 从 DCPOptimizer 提取的无状态纯函数（10个模块，可独立单测） |
| `optimizer/nodes/` | 8个节点实现 + llm_tool_loop 子图 |
| `context_manager/` | 上下文/记忆管理、YAML 压缩（增强标记格式、14 个受保护分析工具） |
| `skills/` | Skill 框架（13个已注册 Skill：9策略 + 3分析 + 1测试） |
| `RapidWrightMCP/` | RapidWright MCP 服务器 |
| `VivadoMCP/` | Vivado MCP 服务器 |
| `strategy_library.py` | 策略库（9个策略 + 12个 Skill 指导） |
| `config_loader.py` / `model_config.yaml` | 模型层级与压缩阈值配置 |
| `validate_dcps.py` | DCP 等价性验证 |
| `dashboard/` | Web Dashboard 实时状态监控（aiohttp + WebSocket，`--dashboard` 启用） |
| `docs/` | 竞赛提交文档站点 |

## 策略库

`strategy_library.py` 定义了 9 个优化策略，系统根据设计特征自动匹配推荐：

| 策略 | 触发条件 | 平台 |
|------|---------|------|
| PBLOCK | distributed 场景（avg_distance > 70） | Vivado + RapidWright |
| PhysOpt | 1-2 paths with spread, WNS > -2.0 | Vivado |
| Fanout | fanout > 100, 无 spread | RapidWright + Vivado |
| PinSwap | WNS 卡在 ~-0.3ns, LUT 输入引脚延迟差异 | RapidWright + Vivado |
| LUTCascade | >3 级 LUT 串联 | RapidWright + Vivado |
| CellReplication | fanout > 10 或 delay > 0.3ns | RapidWright + Vivado |
| CongestionSpreading | congestion=HIGH, PBLOCK/PhysOpt 无效 | RapidWright + Vivado |
| RegisterRetiming | 深组合逻辑链（>2 LUTs） | RapidWright + Vivado |
| NetSwap | SLICE 内布线拥塞 | RapidWright + Vivado |

详见 [skills/SKILL_SPECIFICATION.md](skills/SKILL_SPECIFICATION.md)。

## 模型配置

通过 `model_config.yaml` 配置两个模型层级（当前两者使用相同基础模型，通过上下文窗口和压缩参数区分）：

| 参数 | Worker | Planner |
|------|--------|---------|
| 模型 | deepseek/deepseek-v4-flash | deepseek/deepseek-v4-flash |
| max_tokens | 250K | 1M |
| soft_threshold | 175K | 200K |
| hard_limit | 200K | 300K |
| preserve_turns | 40 (正常) / 25 (硬限制) | 60 (正常) / 40 (硬限制) |
| min_importance | 0.15 (正常) / 0.35 (硬限制) | 0.10 (正常) / 0.25 (硬限制) |
| reasoning | 开启（max_output_tokens=16384） | 开启（max_output_tokens=16384） |
| cost_hard_limit | $1.00 | $1.00 |
| fallback | stepfun/step-3.5-flash, xiaomi/mimo-v2-flash | xiaomi/mimo-v2.5 |

## 测试

| 命令 | 说明 | LLM | 用途 |
|------|------|-----|------|
| `make run_test DCP=x.dcp` | v1 测试模式 | 否 | 硬编码优化流程验证 |
| `make run_skill_test DCP=x.dcp` | v1 Skill 测试 | 否 | 仅 Skill 调用验证 |
| `make run_test_v2 DCP=x.dcp` | v2 测试模式 | 否 | MCP 工具 + Skill + place/route |
| `make run_skill_test_v2 DCP=x.dcp` | v2 Skill 测试 | 否 | 仅 Skill 调用验证（快速） |
| `make run_optimizer DCP=x.dcp` | v2 完整优化 | 是 | 状态机驱动 LLM 优化 |
| `make run_optimizer_v1 DCP=x.dcp` | v1 完整优化 | 是 | 消息对话驱动 LLM 优化 |

## 扩展性维护清单

新增工具或策略时，需同步更新以下位置以保持 Dashboard 可信度、压缩保护机制和状态机行为：

| 新增内容 | 需更新的文件/常量 | 说明 |
|---------|------------------|------|
| 分析型工具（`rapidwright_analyze_*`） | `yaml_structured_compress.py` → `PROTECTED_ANALYSIS_TOOLS` | 加入集合防止结果被压缩为标记（鬼打墙） |
| 刷新 Dashboard 字段的工具 | `optimizer/pure/constants.py` → `DASHBOARD_REFRESH_MAP` | 工具名→字段名映射，Dashboard 自动标注新鲜度 |
| 新策略类型 | `yaml_structured_compress.py` → `_is_failed_strategy_tool_result()` | 添加策略名→工具名模式匹配，支持失败策略压缩 |
| 新策略类型 | `optimizer/pure/iteration_logic.py` → `infer_strategy_from_tools()` | 添加工具名→策略名推断映射 |
| 压缩标记需保留的新 YAML 字段 | `yaml_structured_compress.py` → `_build_compressed_marker()` | 在 YAML 解析循环中添加字段提取 |
| 执行后需强制 WNS 评估的工具 | `optimizer/nodes/subgraphs/llm_tool_loop.py` → `POST_EVAL_TOOLS` | 工具执行后自动调 `report_timing_summary` 并注入 `[EVAL]` 通知 |
| 执行后需自动串联 Vivado 工具的 Skill | `optimizer/pure/constants.py` → `SKILL_CHAIN_ACTIONS` | Skill 名→工具链列表，含 `args_from_skill` 参数传递机制。新增链式 Skill 时添加映射 |
| Dashboard 中不应标注新鲜度的静态字段 | `optimizer/pure/context_snapshot.py` → `_STATIC_RESOURCE_KEYS` | 设计资源（LUT/FF 等）在优化中不变，跳过 `(initial, not refreshed)` 标注 |
| 工具返回空结果的新模式 | `optimizer/nodes/iteration_end.py` → `_EMPTY_RESULT_PATTERNS` | 空结果匹配时用 `reason=tool_error`（可重试）而非 `strategy_ineffective`（永久排除） |

## 文档

- [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md) -- 完整项目结构、数据流、v1->v2 迁移映射
- [skills/SKILL_SPECIFICATION.md](skills/SKILL_SPECIFICATION.md) -- Skill Descriptor v3 规范
- [docs/](docs/) -- 竞赛提交文档站点（benchmarks、FAQ、submission 指南）
