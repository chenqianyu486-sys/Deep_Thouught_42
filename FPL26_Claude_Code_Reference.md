# FPL26 FPGA 后端时序优化 Agent · 客观参考资料

> **版本**: 2026-06-29
> **适用对象**: Claude Code（离线参考）
> **性质**: 纯事实陈述

---

## 一、比赛基础事实

### 1.1 比赛定位

FPL'26 比赛全称是 **"Agentic FPGA Backend Optimization Competition @ FPL'26"**，由 AMD/Xilinx 主办，依托 FPL 2026 会议颁奖。这是 FPL 会议史上的首届此类比赛。

比赛任务的本质是 **Fmax 优化**，不是从零做 place-and-route。给定一个已完整 place-and-route 的 Vivado DCP 文件，输出一个在保持逻辑等价、仍完全 place-and-route 的前提下 Fmax 尽可能提升的新 DCP。

### 1.2 评分公式

`Score = α − (0.1 × α) × β − (0.1 × α) × γ`

- α：Fmax 提升量（MHz）
- β：OpenRouter LLM 调用花费（美元）
- γ：挂钟运行时间（秒 / 3600）
- Score < 0 记 0；验证失败记 0；按 benchmark 排名取算术平均

时间上限 1 小时/benchmark，花费上限 $1/benchmark。

### 1.3 验证标准

1. `report_route_status` 完全布线、0 路由错误
2. `report_timing_summary` hold 和 pulse width 全部满足
3. 输出与输入网表逻辑等价（结构检查 + xsim 仿真，默认 200 测试向量）

### 1.4 允许与禁止

允许：Vivado 2025.1（必须）、RapidWright（git submodule，commit f63afef）、OpenRouter LLM API。禁止自带 API key 突破 $1/benchmark 上限，禁止使用 OpenRouter 以外的远程 LLM。

---

## 二、目标器件与 Benchmark

### 2.1 器件

Xilinx Virtex UltraScale+ `xcvu3p-ffvc1517-2-e`，资源 394k LUTs / 788k FFs / 2280 DSPs / 720 BRAMs。每个 benchmark 包含名为 `clk_fpl26contest` 的创建时钟约束，Fmax = 1000 / (period − WNS)。

### 2.2 评测环境

AWS m7a.2xlarge 实例：8 vCPU（第 4 代 AMD EPYC）、32 GB RAM、Ubuntu 22.02、预装 Vivado ML 2025.1 Developer AMI。最终评测使用一组赛后才公开的隐藏 benchmark。

---

## 三、Vivado 2025.1 工具链

### 3.1 2025.1 版本特性

2025 年 5 月底发布。新增 Spartan UltraScale+（XCSU10P/XCSU25P/XCSU35P）、Versal AI Edge 第二代（XC2VE3558/3504/3858/3804）、Versal Prime 第二代（XC2VM3558/3858）器件支持。核心新功能包括：选择性器件安装、Versal QoR 增强（Calibration Skew Correction、多阶段 NoC）、Versal PS 侧灵活启动、全新 AXI 交换机 IP、全新 Pblock 规划器。

**Intelligent Design Runs**：利用 `report_qor_suggestions`、基于 ML 的策略预测和增量编译，最多运行 6 次布局布线迭代。

### 3.2 关键 TCL 命令

**place_design 策略**：Default、Explore、ExtraTimingOpt、WLBlockPlacement、ExtraPostPlacementOpt、AddRetime、AltSpreadLogic_high、SpreadLogic_high/medium/low。

**route_design 策略**：Default、Explore、HigherDelayCost、LowerDelayCost、NoTimingRelaxation。

**phys_opt_design 选项**：`fanout_opt`、`placement_opt`、`routing_opt`、`slr_crossing_opt`、`rewire`、`insert_negative_edge_ffs`、`critical_cell_opt`、`dsp_register_opt`、`bram_register_opt`、`uram_register_opt`、`bram_enable_opt`、`shift_register_opt`、`hold_fix`、`aggressive_hold_fix`、`retime`、`force_replication_on_nets`、`critical_pin_opt`、`clock_opt`、`path_groups`、`tns_cleanup`、`sll_reg_hold_fix`。指令包括 Explore、AddRetime、AlternateReplication、Default、AggressiveExplore。

**opt_design 指令**：Default、Explore、AddRemap、AreaMultThresholdDSP、SSI、PowerOpt、NoBramPowerOpt。

**约束 API**：`set_clock_uncertainty`、`set_input_delay`、`set_output_delay`、`create_clock`、`create_generated_clock`、`set_clock_groups -asynchronous`、`set_false_path`、`set_max_delay`、`set_min_delay`、`set_multicycle_path`。

### 3.3 report_design_analysis

支持 `-timing`、`-congestion`、`-logic_level_distribution`、`-complexity` 分析，可通过 `get_property STATS.CONGESTION_LEVEL` 获取拥塞等级。

### 3.4 编程接口

Vivado 无官方 Python API，核心脚本接口是 TCL。可通过 `vivado -mode tcl -source script.tcl` 批处理模式运行。报告格式支持 RPT、TXT、JSON（部分命令支持 `-json` 或 `-return_string`）。

### 3.5 Vivado 与 RapidWright 关系

Vivado 2025.1 不集成 RapidWright。两者通过 DCP 文件互补，RapidWright 加载 DCP 比 Vivado 快 3-5 倍。

---

## 四、RapidWright 工具链

### 4.1 项目概况

主仓库 https://github.com/Xilinx/RapidWright，官网 https://www.rapidwright.io/，许可证 Apache 2.0，由 AMD Research and Advanced Development（前 Xilinx Research Labs）维护。核心论文 FCCM 2018: "RapidWright: Enabling Custom Crafted Implementations for FPGAs"。

### 4.2 版本对应

FPL26 比赛要求 Vivado 2025.1，对应 RapidWright v2025.1.x-beta 系列，推荐 v2025.1.3-beta（2024-10-03 发布）。比赛仓库固定 RapidWright commit 为 f63afef。

当前最新 release 为 v2025.2.2-beta（2025-06-05）。版本号与 Vivado 同步。

### 4.3 支持架构

7-Series、UltraScale、UltraScale+（含轻量级时序模型）、Versal（2025.2.0 起支持除 VP1902 外的所有 Versal 器件，含 V80）。

### 4.4 API

双语言支持：Java（原生，需 Java 11+，兼容 Java 8，Gradle 构建）和 Python（通过 JPype 桥接，`pip install rapidwright`）。

核心类：`Device`（FPGA 器件模型）、`Design`（物理网表）、`Net`（网络）、`Site`（物理位置）、`Tile`（物理瓦片）、`PIP`（可编程互连点）、`BEL`（基本逻辑单元）。

DCP 文件支持：`Design.readCheckpoint()` 读取、`Design.writeCheckpoint()` 写出。

其他模块：RWRoute（时序驱动路由器，PathFinder 风格，3,855 nets 设计约 8 秒）、DesignTools（并行化预处理）、ECOTools（工程变更指令）、VivadoTools（调用 Vivado 报告）、Bitstream Manipulation（无法生成完整 bitstream）。

### 4.5 核心论文

| 年份 | 会议/期刊 | 标题 | 备注 |
|---|---|---|---|
| 2018 | FCCM | RapidWright: Enabling Custom Crafted Implementations for FPGAs | 原始论文 |
| 2019 | FPGA | Build Your Own Domain-specific Solutions with RapidWright | |
| 2019 | FPT | An Open-source Lightweight Timing Model for RapidWright | |
| 2021 | FPT | RWRoute: An Open-source Timing-driven Router for Commercial FPGAs | RWRoute 核心论文 |
| 2022 | FPGA | RapidStream: Parallel Physical Implementation of FPGA HLS Designs | Best Paper |
| 2023 | ICCAD | Invited Paper: RapidWright: Unleashing the Full Power of FPGA Technology with Domain-Specific Tooling | |
| 2023 | TRETS | RapidStream 2.0: Automated Parallel Implementation of Latency-Insensitive FPGA Designs | |
| 2024 | FPL | DynaRapid: Fast-Tracking from C to Routed Circuits | Best Paper |

### 4.6 FPL26 比赛中的 RapidWright 用法

比赛提供两个 MCP 服务器：RapidWrightMCP（Python，调用 RapidWright Java API）和 VivadoMCP（Python，调用 Vivado TCL 命令）。LLM 通过 OpenRouter 调用（默认模型曾为 `x-ai/grok-4.1-fast`，后改为 `gemini 3.1 fast lite`，亦支持 `anthropic/claude-sonnet-4`）。

比赛示例展示三种优化策略：高扇出网络分割（按 fanout 阈值选择 k=2-8）、Pblock 区域约束重放置（当关键路径 cells 分布 >70 tiles 时用 pblock 收紧）、`phys_opt_design(directive="Explore")` 物理优化。LogicNets DCP 从 403.55 MHz 提升到 521.10 MHz（+29.1%）。

比赛允许修改 RapidWright Java 源码（submodule）。

### 4.7 关键性能数据

以 3,855 个可路由网络的设计为例：RWRoute 总路由时间 8.21 秒（7 次迭代），数据路径延迟 2,331 ps（与 Vivado 验证完全一致），Slack 差异约 20 ps（来自时钟偏斜和不确定性）。

---

## 五、Agent 架构与 LLM-EDA 现有项目

### 5.1 自主性分级（"The Dawn of Agentic EDA" 综述 arXiv 2025-12）

| 级别 | 定义 | 人类角色 | 代表系统 |
|---|---|---|---|
| L0 | 手动设计 | 操作者 | 传统 CAD |
| L1 | AI 助手 | 提问者 | ChatGPT |
| L2 | AI Copilot | 审核者 | Synopsys.ai Copilot、VerilogCoder |
| L3 | 任务自主 | 监督者 | AutoChip |
| L4 | 流程自主 | 架构师 | ChatEDA |
| L5 | 自进化 | 观察者 | 未来 |

### 5.2 LLM-EDA 核心项目

| 项目 | 来源 | 年份 | 核心内容 |
|---|---|---|---|
| VPR-LLM | U of Toronto (Vaughn Betz 组) | FPL 2025 | LLM 驱动 VPR (FPGA Place & Route) 命令脚本生成与错误修复 |
| ChipNeMo | NVIDIA | ICML 2024 / arXiv 2023 | 领域适配 LLM，支持工程 assistant chatbot、EDA 工具 agent、RTL 生成 |
| ChatEDA | CUHK (Qiang Xu) | MLCAD 2023 / TCAD 2024 | LLM 自主 agent，任务分解、脚本生成、工具执行，实现 RTL→GDSII |
| OpenROAD Agent | OpenROAD-Assistant | IEEE 2025 | LLM 集成 OpenROAD，实时脚本生成 + 错误自我修正 |
| LAAFD | Los Alamos National Lab | arXiv 2026 | LLM Agent 自动化 FPGA HLS 设计，GPT-5 达到手工调优 99.9% 性能 |
| PDAgent-Bench | arXiv 2026 | 首个 VLSI 物理设计 LLM agent 基准，353 个任务覆盖 5 个维度 |
| AutoEDA | arXiv 2025 | 基于微服务的 LLM Agent EDA 流程自动化框架 |
| FPGA-Agent | GitHub: fuxingxin/FPGA-Agent | 2026 | 多 Agent FPGA 调试助手，含 Timing Analyzer Agent |
| ChipGPT | 中科院 | arXiv 2023 | 四阶段零代码逻辑设计框架 |
| Chip-Chat | 多机构 | arXiv/IEEE 2023 | 探索 LLM 对话式硬件设计 |

### 5.3 FPGA-Agent 多 Agent 架构

Planner Agent、RTL Parser Agent、Simulation Analyzer Agent、Timing Analyzer Agent、Constraint Checker Agent、Repair Agent、Closed-loop Executor Agent。

### 5.4 LAAFD 三阶段架构

Phase 1 - Translation Agent（C++ → HLS C++ 翻译）；Phase 2 - Validation（Compile Fixer Agent + Runtime Fixer Agent）；Phase 3 - Optimization（Judge Agent LLM-as-a-judge + Optimizer Agent）。

### 5.5 Prompt 优化工具

DSPy（https://github.com/stanfordnlp/dspy）和 GEPA（https://github.com/gepa-ai/gepa），比赛文档推荐使用。

---

## 六、FPGA 时序优化核心算法

### 6.1 Placement 算法

| 工具/论文 | 来源 | 年份 | 核心 |
|---|---|---|---|
| VTR/VPR SA | VTR 项目 | 持续 | 模拟退火，主流开源 placer |
| Better Together | FPL 2024 | 2024 | Best Paper，解析法 + SA 混合策略 |
| DREAMPlaceFPGA | Rachel Selinar (UT Dallas) | 2020-2024 | GPU 加速，深度学习工具包 |
| DREAMPlace | Yibo Lin (PKU) | DAC 2020 | 通用 VLSI 布局，~16x GPU 加速 |
| DeepPlace | Thinklab-SJTU | NeurIPS 2021 | DRL agent 顺序放置 macros |
| PRNet | Thinklab-SJTU | NeurIPS 2022 | Policy-gradient 布局 + 生成式 routing |
| GraphPlace | ForceDrift | 2023-2024 | 异构图神经网络 macro placement |
| FPGA DRL Placement | arXiv | 2024 | 首个 FPGA DRL 布局 agent |
| Quantum Annealing | arXiv | 2023 | QUBO 量子退火 |
| RippleFPGA | CUHK-EDA | ICCAD 2016 / TCAD 2018 | 可布线性驱动的同时打包和布局 |
| Analytical Timing-Driven Placer | Springer | 2024 | 解析时序驱动布局 |

### 6.2 Routing 算法

| 工具/论文 | 来源 | 年份 | 核心 |
|---|---|---|---|
| PathFinder | IEEE TCAD | 1995 | FPGA 布线基石，迭代协商拥塞 |
| Improving PathFinder | IEEE | 2023 | 关键路径延迟改进 |
| RWRoute | RapidWright | FPT 2021 | 时序驱动开源路由器 |
| OpenPARF | PKU-IDEA | arXiv 2023 | GPU 加速多 die FPGA 布线 |
| Potter | CUHK | FPL 2024 | 并行可重叠路由器 |
| GRoute | University of Guelph | FPL 2024 | FPGA'24 路由冠军 |
| AceRoute | 北京大学 | 2024 | FPGA'24 路由季军 |

### 6.3 时序优化

| 方向 | 关键论文 | 说明 |
|---|---|---|
| Buffer Insertion | Van Ginneken 1990; O(n log n) DAC 2003; TCAD 2005 | O(n log n) 最优 buffer 插入 |
| Retiming | Leiserson-Saxe 1991 | 通过移动寄存器位置平衡组合逻辑延迟 |
| Post-placement C-slow Retiming | UC Berkeley 2003 | Xilinx Virtex FPGA |
| Practical Timing Closure | arXiv 2025 | 深入分析 FPGA/ASIC 时序收敛挑战，Xilinx 案例研究 |
| Slack transfer | 经典 | 关键路径 slack 转移 |
| Wire/Driver sizing | 经典 | 调整连线/驱动单元尺寸 |

### 6.4 ML for EDA

| 工具/论文 | 来源 | 年份 | 核心 |
|---|---|---|---|
| PreRoutGNN | Thinklab-SJTU | AAAI 2024 | 两阶段 GNN，pre-route timing 预测 |
| Pre-route Timing GNN+CNN | VLSI Journal | 2024 | GNN+CNN 混合框架 |
| EDA-AI | Thinklab-SJTU | 持续 | 包含 DeepPlace/PRNet/PreRoutGNN/HubRouter/FlexPlanner/DSBRouter |
| Graph Signal Processing for Placement | arXiv | 2025 | GCN 加速和增强布局过程 |

---

## 七、FPGA'24 路由大赛（直接前身）获奖方案

### 7.1 比赛概况

AMD/Xilinx 在 ISFPGA 2024 举办的 "Runtime-First FPGA Interchange Routing Contest"，目标器件 xcvu3p，强调运行时间。官网：https://xilinx.github.io/fpga24_routing_contest/。

### 7.2 前 5 名

| 名次 | 队名 | 单位 | 队员 | 指导老师 | 论文 |
|---|---|---|---|---|---|
| 1st | GRoute | University of Guelph | Dani Maarouf, Timothy Martin, Charlotte Barnes | Shawki Areibi, Gary Grewal | A High-Performance Routing Engine for Large-Scale FPGAs (FPL 2024) |
| 2nd | CUFR | 香港中文大学 | Xinshi Zang, Wenhao Lin, Shiju Lin, Qin Luo | Evangeline F.Y. Young | Potter: A Parallel Overlap-Tolerant Router for UltraScale FPGAs |
| 3rd | AceRoute | 北京大学 / DeePoly | Ziyun Zhang, Xinming Wei, Sunan Zou, Jiaxi Zhang, Ping Fan | Guojie Luo | AceRoute: Adaptive Compute-Efficient FPGA Routing with Pluggable Intra-Connection Bidirectional Exploration |
| 4th | Team Cuckoo | 北京大学 | Jiarui Wang, Xun Jiang, Chunyuan Zhao | Yibo Lin | 已开源 |
| 5th | Hao³ | 中国科学技术大学 | Wenbin Teng, Qianyu Cheng, Zhendong Zheng, Binze Jiang, Yixuan Zhu, Zihan Wang | Chao Wang, Teng Wang | 已开源 |

### 7.3 开源仓库

- CUFR: https://github.com/xszang/parallel-routing（已合并上游）
- Team Cuckoo: https://github.com/PKU-IDEA/OpenPARF/tree/master/fpga24contest（已合并上游）
- Hao³: https://github.com/Reconfigurable-Computing/RapidWright（已合并上游）

---

## 八、关键 GitHub 仓库

### 8.1 FPL26 比赛核心

| 仓库 | URL | 说明 |
|---|---|---|
| Xilinx/fpl26_optimization_contest | https://github.com/Xilinx/fpl26_optimization_contest | FPL26 比赛官方仓库 |
| Xilinx/RapidWright | https://github.com/Xilinx/RapidWright | RapidWright 主仓库 |
| Xilinx/fpga24_routing_contest | https://github.com/Xilinx/fpga24_routing_contest | FPGA'24 路由比赛 |
| xszang/parallel-routing | https://github.com/xszang/parallel-routing | FPGA'24 第 2 名 CUFR |
| PKU-IDEA/OpenPARF | https://github.com/PKU-IDEA/OpenPARF | FPGA'24 第 4 名 Team Cuckoo |
| Reconfigurable-Computing/RapidWright | https://github.com/Reconfigurable-Computing/RapidWright | FPGA'24 第 5 名 Hao³ fork |

### 8.2 FPGA P&R 开源工具

| 仓库 | URL | 说明 |
|---|---|---|
| verilog-to-routing/vtr-verilog-to-routing | https://github.com/verilog-to-routing/vtr-verilog-to-routing | VTR，MIT |
| berkeley-abc/abc | https://github.com/berkeley-abc/abc | ABC 逻辑综合 |
| rachelselinar/DREAMPlaceFPGA | https://github.com/rachelselinar/DREAMPlaceFPGA | GPU 加速 FPGA 放置 |
| limbo018/DREAMPlace | https://github.com/limbo018/DREAMPlace | DREAMPlace 通用版 |
| cuhk-eda/ripple-fpga | https://github.com/cuhk-eda/ripple-fpga | RippleFPGA |
| Thinklab-SJTU/EDA-AI | https://github.com/Thinklab-SJTU/EDA-AI | DeepPlace/PRNet/PreRoutGNN |
| ForceDrift/GraphPlace | https://github.com/ForceDrift/GraphPlace | GNN macro placement |

### 8.3 Vivado 自动化与 LLM Agent

| 仓库 | URL | 说明 |
|---|---|---|
| Xilinx/XilinxTclStore | https://github.com/Xilinx/XilinxTclStore | 官方 TCL 脚本仓库 |
| mapleleavessssssss-wq/vivado-mcp | https://github.com/mapleleavessssssss-wq/vivado-mcp | Vivado MCP 服务器（30 个工具） |
| fuxingxin/FPGA-Agent | https://github.com/fuxingxin/FPGA-Agent | 多 Agent FPGA 调试助手 |
| wuhy68/ChatEDA | https://github.com/wuhy68/ChatEDA | LLM EDA Agent |
| Thinklab-SJTU/Awesome-LLM4EDA | https://github.com/Thinklab-SJTU/Awesome-LLM4EDA | LLM4EDA 资源列表 |
| Thinklab-SJTU/awesome-ai4eda | https://github.com/Thinklab-SJTU/awesome-ai4eda | AI4EDA 资源列表 |
| stanfordnlp/dspy | https://github.com/stanfordnlp/dspy | Prompt 优化 |
| gepa-ai/gepa | https://github.com/gepa-ai/gepa | Prompt 优化 |
| modelcontextprotocol | https://github.com/modelcontextprotocol | MCP 协议官方 |

---

## 九、关键学术论文

### 9.1 FPGA P&R 核心

| 论文 | 会议/期刊 | 年份 |
|---|---|---|
| RapidWright: Enabling Custom Crafted Implementations for FPGAs | FCCM | 2018 |
| RWRoute: An Open-source Timing-driven Router for Commercial FPGAs | FPT | 2021 |
| RapidStream: Parallel Physical Implementation of FPGA HLS Designs | FPGA (Best Paper) | 2022 |
| DynaRapid: Fast-Tracking from C to Routed Circuits | FPL (Best Paper) | 2024 |
| Better Together: Combining Analytical and Annealing Methods for FPGA Placement | FPL (Best Paper) | 2024 |
| PathFinder: A Negotiation-Based Performance-Driven Router for FPGAs | IEEE TCAD | 1995 |
| RippleFPGA | TCAD | 2018 |
| DREAMPlace | DAC | 2020 |
| DeepPlace | NeurIPS | 2021 |
| PRNet | NeurIPS | 2022 |
| PreRoutGNN | AAAI | 2024 |
| Practical Timing Closure in FPGA/ASIC Designs | arXiv | 2025 |
| A Fast Algorithm for Optimal Buffer Insertion | IEEE TCAD | 2005 |
| An O(nlogn) Time Algorithm for Optimal Buffer Insertion | DAC | 2003 |
| FPGA DRL Placement | arXiv | 2024 |
| An Analytical Timing-Driven Placer for Heterogeneous FPGAs | Springer | 2024 |
| On Improving the Critical Path Delay of PathFinder at Smaller History Factors | IEEE | 2023 |
| A High-Performance Routing Engine for Large-Scale FPGAs (GRoute) | FPL | 2024 |
| Potter: A Parallel Overlap-Tolerant Router for UltraScale FPGAs | GLSVLSI | 2024 |
| AceRoute: Adaptive Compute-Efficient FPGA Routing | 论文 | 2024 |
| An Open-Source Fast Parallel Routing Approach for Commercial FPGAs | GLSVLSI | 2024 |
| OpenPARF | arXiv | 2023 |

### 9.2 LLM-EDA 核心

| 论文 | 会议/期刊 | 年份 |
|---|---|---|
| VPR-LLM: LLM-Powered Command Scripting for FPGA P&R | FPL | 2025 |
| ChipNeMo: Domain-Adapted LLMs for Chip Design | ICML | 2024 |
| ChatEDA: A LLM Powered Autonomous Agent for EDA | MLCAD | 2023 |
| OpenROAD Agent: An Intelligent Self-Correcting Script Generator | IEEE | 2025 |
| LAAFD: LLM-based Agents for Accelerated FPGA Design | arXiv | 2026 |
| PDAgent-Bench: Characterizing LLM Agents for Physical Design | arXiv | 2026 |
| The Dawn of Agentic EDA: A Survey of Autonomous Digital Chip Design | arXiv | 2025 |
| A Survey of Research in LLMs for EDA | TODAES | 2025 |
| LLM4EDA: Emerging Progress in LLMs for EDA | arXiv | 2023 |
| LLMs for Electronic Design Automation: A Comprehensive Overview | arXiv | 2025 |
| AutoEDA: Microservice-based LLM Agents for EDA | arXiv | 2025 |
| ChipGPT: How far are we from natural language hardware design | arXiv | 2023 |
| Chip-Chat: Challenges and Opportunities in Conversational Hardware Design | arXiv | 2023 |

---

## 十、参考资料

### 比赛官方

- [FPL'26 Contest Website](https://xilinx.github.io/fpl26_optimization_contest/)
- [FPL'26 Contest Details](https://xilinx.github.io/fpl26_optimization_contest/details.html)
- [FPL'26 Scoring Criteria](https://xilinx.github.io/fpl26_optimization_contest/score.html)
- [FPL'26 Benchmark Details](https://xilinx.github.io/fpl26_optimization_contest/benchmarks.html)
- [FPL'26 Runtime Environment](https://xilinx.github.io/fpl26_optimization_contest/runtime.html)
- [FPL'26 Benchmarks v1.1.0 Release](https://github.com/Xilinx/fpl26_optimization_contest/releases/tag/v1.1.0)
- [FPL'26 Contest GitHub](https://github.com/Xilinx/fpl26_optimization_contest)

### 前身比赛

- [FPGA'24 Routing Contest](https://xilinx.github.io/fpga24_routing_contest/)
- [FPGA'24 Contest Results](https://xilinx.github.io/fpga24_routing_contest/results.html)
- [FPGA'24 官方结果幻灯片](https://xilinx.github.io/fpga24_routing_contest/fpga24-contest-slides.pdf)

### Vivado 2025.1 文档

- [Vivado What's New - AMD](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado/vivado-whats-new.html)
- [UG835 place_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/place_design)
- [UG835 phys_opt_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/phys_opt_design)
- [UG904 phys_opt_design](https://docs.amd.com/r/en-US/ug904-vivado-implementation/phys_opt_design)
- [UG835 opt_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/opt_design)
- [UG835 report_timing_summary](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/report_timing_summary)
- [UG835 report_timing](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/report_timing)
- [UG904 Implementation Strategy Descriptions](https://docs.amd.com/r/en-US/ug904-vivado-implementation/Implementation-Strategy-Descriptions)
- [UG894 Using Tcl Scripting](https://docs.amd.com/r/en-US/ug894-vivado-tcl-scripting)
- [2025.1 Release - AMD Wiki](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/3281321985/2025.1+Release)
- [AWS Marketplace Vivado ML 2025.1 AMI](https://aws.amazon.com/marketplace/pp/prodview-evssv7ysyt6h4)

### RapidWright

- [RapidWright GitHub](https://github.com/Xilinx/RapidWright)
- [RapidWright 官网](https://www.rapidwright.io/)
- [RapidWright 文档](https://www.rapidwright.io/docs/index.html)
- [RapidWright Publications](https://www.rapidwright.io/docs/Papers.html)
- [RapidWright Python PIP 安装](https://www.rapidwright.io/docs/Install_RapidWright_as_a_Python_PIP_Package.html)
- [RWRoute Timing-driven Routing 教程](https://www.rapidwright.io/docs/RWRoute_timing_driven_routing.html)
- [FCCM 2018 RapidWright 论文 PDF](https://www.rapidwright.io/docs/_downloads/c2ac737a132c3fb753fc780a629cf468/FCCM18-RapidWright.pdf)
- [ICCAD 2023 RapidWright Invited Paper](https://ieeexplore.ieee.org/document/10323739)
- [FPT 2019 Timing Model Paper](https://ieeexplore.ieee.org/document/8977880)
- [FPGA 2019 RapidWright Paper](https://dl.acm.org/doi/10.1145/3289602.3293928)
- [RapidStream 2.0 - ACM TRETS](https://dl.acm.org/doi/10.1145/3593025)

### FPGA'24 获奖论文

- [GRoute 论文 (FPL 2024)](https://doi.ieeecomputersociety.org/10.1109/FPL64840.2024.00017)
- [Potter 论文](https://diri-lin.top/attaches/potter.pdf)
- [CUFR GLSVLSI 2024 论文](https://github.com/xszang/parallel-routing/blob/main/doc/glsvlsi24-camera-ready.pdf)
- [AceRoute 论文](https://xmwei.com/assets/pdf/wei2024aceroute.pdf)

### LLM-EDA 论文

- [VPR-LLM (FPL 2025)](https://www.eecg.utoronto.ca/~vaughn/papers/fpl2025_VPR_LLM.pdf)
- [ChatEDA Paper](https://arxiv.org/abs/2308.10204)
- [ChipNeMo Paper](https://arxiv.org/abs/2311.00176)
- [ChipGPT Paper](https://arxiv.org/abs/2305.14019)
- [Chip-Chat Paper](https://arxiv.org/abs/2305.13243)
- [OpenROAD Agent](https://ieeexplore.ieee.org/document/11106006)
- [LAAFD Paper](https://arxiv.org/html/2602.06085v1)
- [PDAgent-Bench Paper](https://arxiv.org/html/2606.17253v1)
- [The Dawn of Agentic EDA](https://arxiv.org/html/2512.23189v1)
- [Survey of Research in LLMs for EDA](https://arxiv.org/abs/2501.09655)
- [LLM4EDA Survey](https://arxiv.org/abs/2401.12224)
- [LLMs for EDA Comprehensive Overview](https://arxiv.org/pdf/2508.20030)
- [AutoEDA Paper](https://arxiv.org/abs/2508.01012)

### FPGA P&R 论文

- [PathFinder Original Paper](https://ieeexplore.ieee.org/document/1377269)
- [Improving PathFinder (IEEE 2023)](https://ieeexplore.ieee.org/document/10376068)
- [Van Ginneken Buffer Insertion Fast Algorithm (TCAD 2005)](https://ieeexplore.ieee.org/document/1432879)
- [O(nlogn) Buffer Insertion (DAC 2003)](https://cecs.uci.edu/~papers/compendium94-03/papers/2003/dac03/pdffiles/34_2.pdf)
- [Practical Timing Closure (arXiv 2025)](https://arxiv.org/abs/2510.26985)
- [PreRoutGNN (AAAI 2024)](https://arxiv.org/abs/2403.00012)
- [Better Together (FPL 2024)](https://www.computer.org/csdl/proceedings-article/fpl/2024/300700a043/20TW4ra90Bi)
- [FPGA-Placement via Quantum Annealing (arXiv 2023)](https://arxiv.org/abs/2312.15467)
- [FPGA DRL Placement (arXiv 2024)](https://arxiv.org/html/2404.13061v1)
- [Graph Signal Processing for Chip Placement (arXiv 2025)](https://arxiv.org/html/2502.17632v1)
- [OpenPARF Paper](https://arxiv.org/pdf/2306.16665)
- [DREAMPlace (DAC 2020)](https://ieeexplore.ieee.org/document/9122053)

### 中文视角

- [Vivado 2025.1 新功能 - 知乎](https://zhuanlan.zhihu.com/p/1953107304959415543)
- [下载 AMD Vivado 2025.1 - FPGA技术网](https://fpga.eetrend.com/content/2025/100592169.html)
- [Vivado Implementation Strategy 选择指南 - CSDN](https://blog.csdn.net/Js_cold/article/details/156447214)
- [使用Vivado进行物理优化 phys_opt_design - CSDN](https://blog.csdn.net/u011565038/article/details/137557429)
- [FPGA Retiming 优化技术 - FPGA技术网](https://fpga.eetrend.com/blog/2023/100567854.html)
- [AI4EDA 完整研究报告 - juejin](https://juejin.cn/post/7655378241421164571)

---

> **文档共 10 章**，涵盖比赛规则、目标器件、Vivado 2025.1 工具链、RapidWright 工具链、Agent 架构、FPGA P&R 算法、FPGA'24 获奖方案、关键仓库与论文。所有引用均为可点击 Markdown 链接。
