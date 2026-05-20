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

### 1. 代码级流程控制（强制 report_step_state）

**问题**：LLM 在大多数轮次中不会主动调用 `report_step_state`，导致系统丧失流程控制信号，无法驱动迭代切换。

**方案**：在工具循环中检测 `report_step_state` 缺失，递进式升级处理 -- 跳过工具执行并警告，多次缺失后自动合成默认状态继续流程。不依赖 prompt 提醒，在代码逻辑中强制执行。

**原则**：强制但不阻塞 -- 给 LLM 适应空间，但不因 LLM 的疏忽而死锁优化。

### 2. 弱引导 Dashboard（最大化 LLM 自主推理）

**问题**：原上下文快照包含 `FAILED`/`PLATEAUED` 标签、`do_not_repeat` 列表、具体策略建议，与 system prompt 重复约束 LLM。LLM 被告知"该做什么"而非"当前是什么"，推理空间被过度压缩。

**方案**：上下文快照重构为纯数据 Dashboard -- 只呈现原始测量值（WNS/TNS/trajectory/design_signals/critical_paths），不含判断标签或行动建议。明确声明 "This is a factual data dashboard... You decide the next action"。

**原则**：事实而非判断，数据而非建议，轨迹而非指令。system prompt 负责"规则"，Dashboard 负责"现状"，LLM 负责"决策"。

### 3. 简化交接提示词（去除重复引导）

**问题**：迭代交接提示词包含具体策略建议、失败列表、4步指令，与 system prompt 的策略选择指导重复，造成信息冗余和约束冲突。

**方案**：handoff 简化为纯事实结构 -- Planner 交接仅含 SITUATION/STATE/TRAJECTORY/STATUS，Worker 交接仅含 STATE/CRITICAL PATHS/STATUS。策略选择交还给 system prompt 统一指导。

### 4. 状态机驱动架构（V1 → V2）

**问题**：V1 采用消息对话驱动，流程控制隐式嵌入 LLM 对话循环，状态分散在类属性和局部变量中，难以观测和调试。

**方案**：V2 重构为显式状态机 -- 8 个节点 + 条件边构成有向图，集中式 `OptimizerState` 管理 6 个类型化子切片，`StateTracer` 记录每次状态转换的 JSON 轨迹。节点编排流程，纯函数封装逻辑（`optimizer/pure/` 10 个无状态模块，可独立单测）。

**原则**：显式优于隐式 -- 流程控制从 LLM 对话中剥离，交由代码图拓扑驱动。

### 5. 双层模型分工（Worker/Planner）

**问题**：单一模型既要执行具体工具操作又要进行全局策略规划，上下文窗口和角色混杂导致两个任务都做不好。

**方案**：分离为 Worker（执行层，250K tokens）和 Planner（规划层，1M tokens）两个模型层级。Worker 专注于单次迭代内的工具调用和状态报告，Planner 负责跨迭代的策略选择和上下文压缩。通过 `model_config.yaml` 配置不同的上下文窗口、压缩阈值和 fallback 模型。

**原则**：关注点分离 -- 执行和规划使用不同的上下文窗口和压缩策略。

### 6. 纯原生工具调用（去除文本回退）

**问题**：V1 保留了 XML/YAML 文本格式作为工具调用回退，增加了 parser 复杂度，且 LLM 倾向于生成文本而非严格工具调用，导致调用成功率不稳定。

**方案**：V2 仅支持 LLM 原生 function call，移除所有 XML/YAML 文本回退路径。工具 schema 通过标准 function calling 协议传递，LLM 必须生成结构化 tool_use 响应。

**原则**：单一调用路径 -- 不给 LLM"偷懒"生成文本的机会，强制结构化输出。

### 7. 策略库自动匹配

**问题**：LLM 缺乏 FPGA 优化领域知识，随机尝试策略效率低下。

**方案**：`strategy_library.py` 定义 9 个优化策略，每个策略附带触发条件（WNS 范围、fanout 阈值、congestion 等级等）和适用平台。系统根据设计特征自动匹配推荐，LLM 在推荐范围内选择而非盲目探索。

**原则**：领域知识编码为规则，LLM 负责推理和执行 -- 将"该做什么"从 prompt 提示升级为结构化策略库。

## 快速开始

```bash
# 环境设置（Java、Vivado、RapidWright）
make setup

# 运行 v2 优化器（默认，状态机驱动）
make run_optimizer DCP=input.dcp

# 运行 v1 优化器（消息对话驱动）
make run_optimizer_v1 DCP=input.dcp

# V2 测试模式（无 LLM，验证工具和 Skill 调用）
make run_test_v2 DCP=demo_corundum_25g_misses_timing.dcp
make run_skill_test_v2 DCP=demo_corundum_25g_misses_timing.dcp
```

**环境变量**:
- `OPENROUTER_API_KEY` -- OpenRouter API 密钥（必需）
- `VIVADO_EXEC` -- Vivado 可执行文件路径（默认 `vivado`）
- `JAVA_HOME` -- Java 安装路径（RapidWright 依赖）

## 项目结构

| 目录/文件 | 用途 |
|-----------|------|
| `dcp_optimizer.py` | 主 Agent 编排入口（v1 消息对话 + v2 状态机 `optimize_v2()` 入口） |
| `optimizer/` | v2 状态机驱动 Agent 框架（LangGraph 风格，8节点图） |
| `optimizer/pure/` | 从 DCPOptimizer 提取的无状态纯函数（10个模块，可独立单测） |
| `optimizer/nodes/` | 8个节点实现 + llm_tool_loop 子图 |
| `context_manager/` | 上下文/记忆管理、YAML 压缩 |
| `skills/` | Skill 框架（13个已注册 Skill：9策略 + 3分析 + 1测试） |
| `RapidWrightMCP/` | RapidWright MCP 服务器 |
| `VivadoMCP/` | Vivado MCP 服务器 |
| `strategy_library.py` | 策略库（9个策略 + 12个 Skill 指导） |
| `config_loader.py` / `model_config.yaml` | 模型层级与压缩阈值配置 |
| `validate_dcps.py` | DCP 等价性验证 |
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

## 文档

- [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md) -- 完整项目结构、数据流、v1->v2 迁移映射
- [skills/SKILL_SPECIFICATION.md](skills/SKILL_SPECIFICATION.md) -- Skill Descriptor v3 规范
- [docs/](docs/) -- 竞赛提交文档站点（benchmarks、FAQ、submission 指南）
