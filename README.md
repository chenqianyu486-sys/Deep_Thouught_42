# FPL26 优化竞赛 — FPGA 时序收敛 Agent

基于 LLM Agent 的 FPGA EDA 全流程优化系统，参赛 FPL 2026 优化竞赛。

## 概述

通过 LLM（DeepSeek V4 Pro / Flash）编排 Vivado 和 RapidWright 工具链，自动执行 P&R 优化策略（PBLOCK、PhysOpt、Fanout 优化等），迭代逼近时序收敛目标（WNS ≥ 0）。

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

## 项目结构

| 目录/文件 | 用途 |
|-----------|------|
| `dcp_optimizer.py` | 主 Agent 编排入口（v1 消息对话 + v2 状态机） |
| `optimizer/` | v2 状态机驱动 Agent 框架（LangGraph 风格） |
| `context_manager/` | 上下文/记忆管理、YAML 压缩 |
| `skills/` | Skill 框架（分析/优化策略） |
| `RapidWrightMCP/` | RapidWright MCP 服务器 |
| `VivadoMCP/` | Vivado MCP 服务器 |
| `strategy_library.py` | 策略库与 Skill 推荐数据 |
| `config_loader.py` / `model_config.yaml` | 模型层级与压缩阈值配置 |
| `validate_dcps.py` | DCP 等价性验证 |
| `docs/` | 竞赛提交文档站点 |

详见 [PROJECT_TREE_AND_DATA_FLOW.md](PROJECT_TREE_AND_DATA_FLOW.md)。
