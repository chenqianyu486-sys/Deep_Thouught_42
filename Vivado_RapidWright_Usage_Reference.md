# Vivado 2025.1 与 RapidWright 使用参考

> **版本**: 2026-06-29
> **适用对象**: Claude Code（离线参考）
> **范围**: Vivado 2025.1 命令、参数、报告；RapidWright API、模块、操作流程
> **性质**: 事实陈述，不含代码示例

---

## 目录

- [第一部分 Vivado 2025.1](#第一部分-vivado-20251)
  - [1. 安装与许可证](#1-安装与许可证)
  - [2. 2025.1 版本特性](#2-20251-版本特性)
  - [3. 启动模式与批处理](#3-启动模式与批处理)
  - [4. 关键 TCL 命令完整参考](#4-关键-tcl-命令完整参考)
  - [5. 报告解析](#5-报告解析)
  - [6. 设计检查点（DCP）操作](#6-设计检查点dcp操作)
  - [7. 预设实现策略（Implementation Strategy）完整列表](#7-预设实现策略implementation-strategy完整列表)
- [第二部分 RapidWright](#第二部分-rapidwright)
  - [8. 安装与版本对应](#8-安装与版本对应)
  - [9. 核心类与方法](#9-核心类与方法)
  - [10. DCP 文件读写](#10-dcp-文件读写)
  - [11. 关键模块详解](#11-关键模块详解)
  - [12. 教程与示例场景](#12-教程与示例场景)
- [第三部分 Vivado 与 RapidWright 协作](#第三部分-vivado-与-rapidwright-协作)
- [第四部分 常见数据与默认值](#第四部分-常见数据与默认值)
- [第五部分 参考资料](#第五部分-参考资料)

---

# 第一部分 Vivado 2025.1

## 1. 安装与许可证

### 1.1 安装

- **发布日期**: 2025 年 5 月底
- **下载**: AMD 官方下载中心
- **安装方式**:
  - 交互式安装器（默认）
  - 静默安装（`xsetup -b ConfigGen` 生成配置后批处理安装）
  - 容器化（AMD 提供 Docker 镜像）
- **操作系统**: Windows 10/11、RHEL/CentOS、Ubuntu LTS
- **磁盘空间**: 完整安装约 100-200 GB；选择性器件安装可显著减少

### 1.2 许可证

- **许可类型**: Vivado ML Standard、HL Design Edition、HL System Edition
- **免费版本**: Vivado ML Webpack（设备受限，xcvu3p 不在 Webpack 范围）
- **学术许可**: AMD University Program 提供捐赠
- **云端**: AWS Marketplace 提供预装 Vivado ML 2025.1 Developer AMI
- **许可证服务器**: FLEXnet，需要 license.lic 文件

### 1.3 启动命令

| 模式 | 命令 | 用途 |
|---|---|---|
| GUI | `vivado` | 交互式图形界面 |
| TCL 交互 | `vivado -mode tcl` | 交互式 TCL 控制台 |
| TCL 批处理 | `vivado -mode tcl -source script.tcl` | 批处理执行脚本 |
| GUI 工程 | `vivado project_name.xpr` | 打开工程文件 |
| 无工程 | `vivado -mode tcl` | 无工程模式（推荐批处理） |
| Journal | `vivado -journal <path>` | 指定日志位置 |

### 1.4 Vivado 2025.1 CLI 关键参数

| 参数 | 作用 |
|---|---|
| `-mode batch` | 批处理模式（不启动 GUI） |
| `-mode tcl` | TCL 交互模式 |
| `-mode gui` | GUI 模式（默认） |
| `-source <file>` | 启动时执行 TCL 脚本 |
| `-notrace` | 关闭消息回显 |
| `-log <file>` | 指定 log 文件 |
| `-journal <file>` | 指定 journal 文件 |
| `-tempDir <dir>` | 指定临时目录 |
| `-lic_waittime <sec>` | 等待许可证秒数 |

---

## 2. 2025.1 版本特性

### 2.1 新增器件支持

- **Spartan UltraScale+**: XCSU10P、XCSU25P、XCSU35P
- **Versal AI Edge 第二代**: XC2VE3558、XC2VE3504、XC2VE3858、XC2VE3804
- **Versal Prime 第二代**: XC2VM3558、XC2VM3858

### 2.2 核心新功能

- **选择性器件安装**: 安装时选择特定器件，减小下载/磁盘占用
- **Versal QoR 增强**:
  - Calibration Skew Correction（校准偏斜校正）
  - 多阶段 NoC 支持（按时间片划分 QoS 和带宽）
  - 显著提升 Versal SSIT 器件的 FMAX
- **Versal PS 侧灵活启动**: 先启动 PS，再动态加载 PL
- **全新 AXI 交换机 IP**: 基于 RTL 的完全可自定义 IP
- **IP 集成器增强**: "时钟和复位"和"中断和 AXI-4 Lite"专用视图
- **全新 Pblock 规划器**: 一站式 Pblock 创建工具
- **report_dfx_summary GUI 支持**: DFX 调试增强

### 2.3 Intelligent Design Runs（ML-driven）

- **触发命令**: `synth_design` / `impl_1` 运行时设置 `STRATEGY` 为 `Flow_RuntimeOptimized`、`Performance_Explore` 等并启用智能模式
- **核心机制**:
  - 利用 `report_qor_suggestions` 报告改进建议
  - 基于 ML 预测选择最佳策略组合
  - 增量编译（incremental compile）
- **迭代上限**: 最多运行 6 次布局布线迭代
- **输出**: 每次迭代产出 DCP，可选择最佳

### 2.4 编程接口现状

- **无官方 Python API**（Vivado 仅有 TCL 接口）
- **TCL 是唯一原生脚本语言**
- **Jupyter Notebook**: 2025.1 增强对 Vivado IP Integrator 的 Python 支持（通过 tcl_invoke）
- **PYNQ**: 与 Vivado 无关，是 Zynq SoC 上 Python 嵌入式开发框架

---

## 3. 启动模式与批处理

### 3.1 无工程模式（Project-less Mode）

无工程模式是批处理最常用的方式：

- **起点**: 直接 `open_checkpoint` 或 `read_verilog`
- **优势**: 不需要 .xpr 工程文件，灵活度高
- **典型流程**:
  1. `read_verilog` / `read_vhdl` 读取 RTL
  2. `read_xdc` 读取约束
  3. `synth_design`
  4. `place_design`
  5. `route_design`
  6. `write_checkpoint` 写出 DCP
  7. `report_timing_summary` 报告时序

### 3.2 工程模式（Project Mode）

- **核心文件**: `<name>.xpr`（Vivado Project File）
- **优势**: GUI 可视化、IP 集成、版本管理
- **批处理**: `open_project <name>.xpr` 后调用 implementation run
- **限制**: 资源管理复杂，迁移性差

### 3.3 增量编译（Incremental Compile）

- **用途**: 加速实现迭代，保留已有优化结果
- **关键命令**: `set_property incremental_checkpoint <path> [get_runs impl_1]`
- **增量模式**:
  - 默认模式：保留 placement 和 routing
  - 可调参数：WNS 阈值（-incremental_poor_qor_threshold）
- **优点**:
  - 二次实现时间显著缩短（典型 30-50%）
  - 保留上一次实现的优化状态
- **限制**: 设计大幅修改时无效

---

## 4. 关键 TCL 命令完整参考

### 4.1 综合命令

#### `synth_design`

**功能**: 综合 RTL 代码

**完整语法**:
```
synth_design [-top <arg>] [-part <arg>] [-include_dirs <args>]
  [-generic <args>] [-verilog_define <args>] [-flatten_hierarchy <arg>]
  [-rtl_skip_constraints] [-rtl_skip_ip] [-mode <arg>] [-directive <arg>]
  [-retiming] [-resource_sharing <arg>] [-shreg_min_size <arg>]
  [-max_dsp <arg>] [-max_bram <arg>] [-max_uram <arg>] [-max_buram <arg>]
  [-no_lc] [-no_srlextract] [-keep_equivalent_registers] [-fsm_extraction <arg>]
  [-hierarchical_block_threshold <arg>] [-hierarchical_block_replicate <arg>]
  [-hierarchical_block <args>] [-fanout_limit <arg>] [-bufg <arg>]
  [-mvcstyle <arg>] [-debug_log] [-sweep] [-quiet] [-verbose]
```

**常用 directive（综合策略）**:

| Directive | 用途 |
|---|---|
| `Default` | 默认平衡策略 |
| `RuntimeOptimized` | 最短综合时间 |
| `AreaOptimized_high` | 高面积优化 |
| `AreaOptimized_medium` | 中等面积优化 |
| `AreaMapLargeShiftRegToBRAM` | 大移位寄存器映射到 BRAM |
| `AlternateRoutability` | 可布线性优化 |
| `AreaMultThresholdDSP` | 面积优化，DSP 阈值 |
| `FewerCarryChains` | 减少进位链 |
| `FlowOptimized_high` | 高流程优化 |
| `FlowAreaOptimized_high` | 高面积+流程优化 |
| `FlowAlternateRoutability` | 可布线性+流程优化 |
| `FlowPerfOptimized_high` | 高性能+流程优化 |
| `FlowPerfThresholdCarry` | 性能+进位链优化 |
| `PerformanceOptimized` | 默认性能优化 |
| `PerformanceRetiming` | 性能优化+retiming |
| `PerformanceExtraTimingOpt` | 高级时序优化 |
| `AggressiveExplore` | 激进探索 |

#### `opt_design`

**功能**: 逻辑优化（综合后/实现后均可运行）

**完整语法**:
```
opt_design [-retarget] [-propconst] [-sweep] [-bram_power_opt]
  [-remap] [-aggressive_remap] [-resynth_area] [-resynth_seq_area]
  [-resynth_remap] [-directive <arg>] [-muxf_remap]
  [-hier_fanout_limit <arg>] [-bufg_opt] [-mbufg_opt]
  [-shift_register_opt] [-dsp_register_opt] [-srl_remap_modes <args>]
  [-control_set_merge] [-control_set_opt] [-merge_equivalent_drivers]
  [-carry_remap] [-debug_log] [-property_opt_only] [-quiet] [-verbose]
```

**常用 directive**:

| Directive | 用途 |
|---|---|
| `Default` | 默认优化（retarget、propconst、sweep、bram_power_opt 等） |
| `Explore` | 探索性优化，运行额外算法以改善 QoR |
| `ExploreArea` | Explore + resynth_area，减少 LUT 数量 |
| `ExploreWithRemap` | Explore + aggressive_remap，压缩逻辑级数 |
| `ExploreSequentialArea` | Explore + resynth_seq_area，减少寄存器和组合逻辑 |
| `RuntimeOptimized` | 无 bram_power_opt，最快运行时间 |
| `RQS` | 由 QoR 建议文件驱动的策略选择 |
| `AddRemap` | LUT 重新映射 |

### 4.2 实现命令

#### `place_design`

**功能**: 布局（Place）

**完整语法**:
```
place_design [-directive <arg>] [-no_timing_driven] [-timing_summary]
  [-unplace] [-post_place_opt] [-no_psip] [-clock_vtree_type <arg>]
  [-no_bufg_opt] [-ultrathreads] [-quiet] [-verbose]
```

**关键参数**:
- `-directive`: 布局策略（见下方策略表）
- `-post_place_opt`: 布局后优化关键路径
- `-unplace`: 仅取消布局，不进行新布局
- `-no_timing_driven`: 禁用时序驱动（拥塞场景可选）
- `-no_psip`: 禁用布局器中物理综合（PSIP）
- `-no_bufg_opt`: 禁用全局缓冲插入

**常用 directive 详解**:

| Directive | 适用场景 | 关键算法 |
|---|---|---|
| `Default` | 通用 | 默认布局 |
| `Explore` | 时序紧张 | 增强细节布局和后布局优化 |
| `EarlyBlockPlacement` | 模块化设计 | RAM/DSP 早期时序驱动布局 |
| `WLDrivenBlockPlacement` | 拥塞 | RAM/DSP 线长驱动布局 |
| `ExtraNetDelay_high` | 高扇出长距离 | 最高悲观度网络延迟估计 |
| `ExtraNetDelay_low` | 低扇出短距离 | 最低悲观度网络延迟估计 |
| `AltSpreadLogic_high` | 严重拥塞 | 最高程度逻辑展开 |
| `AltSpreadLogic_medium` | 中等拥塞 | 中等程度逻辑展开 |
| `AltSpreadLogic_low` | 轻拥塞 | 最低程度逻辑展开 |
| `ExtraPostPlacementOpt` | 精细优化 | 增强布局后优化 |
| `ExtraTimingOpt` | 严重时序违例 | 备选时序驱动算法 |
| `SSI_SpreadLogic_high` | SSI 器件 | 跨 SLR 最高程度逻辑分散 |
| `SSI_SpreadLogic_low` | SSI 器件 | 跨 SLR 最低程度逻辑分散 |
| `SSI_SpreadSLLs` | SSI 器件 | 跨 SLR 分区，为高连接区域分配额外空间 |
| `SSI_BalanceSLLs` | SSI 器件 | 跨 SLR 均衡 SLL |
| `SSI_BalanceSLRs` | SSI 器件 | 均衡各 SLR 单元数 |
| `SSI_HighUtilSLRs` | SSI 器件 | 各 SLR 内紧凑布局 |
| `RuntimeOptimized` | 时间敏感 | 最少迭代，更快运行时间 |
| `Quick` | 快速迭代 | 最快运行时间，非时序驱动 |
| `RQS` | 策略建议 | 由 QoR 建议文件驱动的策略选择 |
| `Auto_1` | ML 驱动 | 机器学习最佳预测指令 |
| `Auto_2` | ML 驱动 | 机器学习次佳预测指令 |
| `Auto_3` | ML 驱动 | 机器学习第三佳预测指令 |

#### `route_design`

**功能**: 布线（Route）

**完整语法**:
```
route_design [-unroute <arg>] [-release_memory] [-nets <args>]
  [-physical_nets] [-pins <args>] [-directive <arg>] [-tns_cleanup]
  [-no_timing_driven] [-preserve] [-delay] [-auto_delay]
  [-max_delay <arg>] [-min_delay <arg>] [-timing_summary] [-finalize]
  [-ultrathreads] [-eco] [-no_psir] [-quiet] [-verbose]
```

**关键参数**:
- `-directive`: 布线策略（见下方策略表）
- `-no_timing_driven`: 禁用时序驱动
- `-preserve`: 保留已有布线
- `-unroute`: 取消布线
- `-nets`: 仅对指定网络布线
- `-tns_cleanup`: TNS 清理
- `-finalize`: 完成部分布线的连接
- `-eco`: 增量 ECO 模式布线

**常用 directive**:

| Directive | 适用场景 |
|---|---|
| `Default` | 默认布线 |
| `Explore` | 初始布线后探索不同关键路径布线 |
| `AggressiveExplore` | 极度拥塞，更激进的关键路径探索（运行时间显著增加） |
| `NoTimingRelaxation` | 不放宽时序约束，运行更长时间以达标 |
| `MoreGlobalIterations` | 全程使用详细时序分析，运行更多全局迭代 |
| `HigherDelayCost` | 延迟优先于迭代次数，用运行时间换取性能 |
| `AdvancedSkewModeling` | 高偏斜时钟网络，使用更精确的偏斜模型 |
| `AlternateCLBRouting` | UltraScale 器件布通困难，使用备选布线算法 |
| `RuntimeOptimized` | 最少迭代，更快运行时间 |
| `Quick` | 最快运行时间，非时序驱动 |

#### `phys_opt_design`

**功能**: 物理优化（布局后/布线后）

**完整语法**:
```
phys_opt_design [-fanout_opt] [-placement_opt] [-memory_rewire_opt]
  [-routing_opt] [-slr_crossing_opt] [-restruct_opt]
  [-insert_negative_edge_ffs] [-interconnect_retime] [-lut_opt]
  [-casc_opt] [-critical_cell_opt] [-dsp_register_opt]
  [-bram_register_opt] [-uram_register_opt] [-bram_enable_opt]
  [-shift_register_opt] [-hold_fix] [-aggressive_hold_fix] [-retime]
  [-force_replication_on_nets <args>] [-directive <arg>]
  [-critical_pin_opt] [-clock_opt] [-path_groups <args>]
  [-tns_cleanup] [-sll_reg_hold_fix] [-quiet] [-verbose]
```

**选项详解**:

| 选项 | 功能 | 适用场景 |
|---|---|---|
| `-fanout_opt` | 高扇出网络优化 | 扇出 > 1000 信号 |
| `-placement_opt` | 重新布局优化 | 时序违例 |
| `-routing_opt` | 重新布线优化 | 拥塞 |
| `-slr_crossing_opt` | SLR 交叉优化 | 跨 SLR 路径 |
| `-memory_rewire_opt` | BRAM/URAM 关键信号引脚重新连接 | Versal 器件 BRAM/URAM 时序 |
| `-insert_negative_edge_ffs` | 插入负沿触发器 | 时序优化 |
| `-critical_cell_opt` | 关键路径单元优化 | 关键路径 |
| `-dsp_register_opt` | DSP 寄存器优化 | DSP 时序 |
| `-bram_register_opt` | BRAM 寄存器优化 | BRAM 时序 |
| `-uram_register_opt` | URAM 寄存器优化 | URAM 时序 |
| `-bram_enable_opt` | BRAM 使能优化 | BRAM 控制 |
| `-shift_register_opt` | 移位寄存器优化 | SRL |
| `-hold_fix` | Hold 时间修复 | 短路径违例 |
| `-aggressive_hold_fix` | 激进 Hold 修复 | 严重 hold 违例 |
| `-retime` | 寄存器重定时 | 跨组合逻辑 |
| `-force_replication_on_nets` | 强制复制指定网络 | 高扇出 |
| `-critical_pin_opt` | 关键引脚优化 | 关键路径 |
| `-clock_opt` | 时钟优化 | 时钟偏斜 |
| `-path_groups` | 限定优化路径组 | 局部优化 |
| `-tns_cleanup` | TNS 清理 | 多违例路径 |
| `-sll_reg_hold_fix` | SLL 寄存器 hold 修复 | SLL hold 违例 |

**常用 directive**:

| Directive | 用途 |
|---|---|
| `Default` | 默认物理优化 |
| `Explore` | 多算法多轮探索，包括高扇出复制 |
| `ExploreWithHoldFix` | 多算法探索 + hold 违例修复 |
| `ExploreWithAggressiveHoldFix` | 多算法探索 + 激进 hold 违例修复 |
| `AggressiveExplore` | 更激进的探索算法 |
| `AlternateReplication` | 备选关键单元复制策略 |
| `AggressiveFanoutOpt` | 更激进的高扇出优化 |
| `AlternateFlowWithRetiming` | 激进复制 + DSP/BRAM 优化 + retiming [BLOCKED: retiming] |
| `AddRetime` | 默认流程 + retiming [BLOCKED: retiming] |
| `RuntimeOptimized` | 减少优化，最短运行时间（fanout_opt + critical_cell_opt + placement_opt + bram_enable_opt） |
| `RQS` | 由 QoR 建议文件驱动的策略选择 |

### 4.3 时序报告命令

#### `report_timing_summary`

**功能**: 时序综合报告

**完整语法**:
```
report_timing_summary [-delay_type <arg>] [-min_slack <arg>] 
  [-max_slack <arg>] [-path_type <arg>] [-report_unconstrained] 
  [-check_timing_verbose] [-sort_by <arg>] [-of_objects <args>] 
  [-max_paths <arg>] [-nworst <arg>] [-unique_pins] 
  [-include_headers] [-file <arg>] [-append] [-return_string] 
  [-warn_on_violation] [-slack_lesser_than <arg>] 
  [-slack_greater_than <arg>] [-quiet] [-verbose]
```

**关键参数**:
- `-delay_type min|max`: max=setup 检查，min=hold 检查
- `-max_paths <N>`: 报告的路径数
- `-nworst <N>`: 每个时钟组的最差路径数
- `-slack_lesser_than <value>`: 仅报告 slack 小于该值
- `-slack_greater_than <value>`: 仅报告 slack 大于该值
- `-path_type summary|short|full|end`: 报告详细程度
- `-return_string`: 返回字符串而非写入文件
- `-file <name>`: 输出到文件
- `-append`: 追加到文件

**报告内容**:
- WNS (Worst Negative Slack): 最差负 slack
- TNS (Total Negative Slack): 总负 slack
- WHS (Worst Hold Slack): 最差 hold slack
- THS (Total Hold Slack): 总 hold slack
- TPWS (Total Pulse Width Slack): 脉冲宽度 slack
- 每个时钟组的详细情况

#### `report_timing`

**功能**: 详细时序路径报告

**完整语法**:
```
report_timing [-delay_type <arg>] [-min_slack <arg>] [-max_slack <arg>] 
  [-path_type <arg>] [-report_unconstrained] [-check_timing_verbose] 
  [-sort_by <arg>] [-of_objects <args>] [-max_paths <arg>] 
  [-nworst <arg>] [-unique_pins] [-include_headers] [-file <arg>] 
  [-append] [-return_string] [-warn_on_violation] 
  [-slack_lesser_than <arg>] [-slack_greater_than <arg>] 
  [-cells <args>] [-from <args>] [-to <args>] [-through <args>] 
  [-rise] [-fall] [-quiet] [-verbose]
```

**关键参数**:
- `-from <args>`: 起点（cell pin、port 等）
- `-to <args>`: 终点
- `-through <args>`: 经过的 cell/pin
- `-cells <args>`: 限定到指定层级单元
- `-delay_type min|max`: 路径类型
- `-max_paths <N>`: 路径数

**报告内容**: 单条路径的完整延迟分解（logic delay / net delay / clock skew）

#### `report_design_analysis`

**功能**: 设计分析（时序、拥塞、复杂度等）

**完整语法**:
```
report_design_analysis [-congestion] [-timing] 
  [-complexity] [-logic_level_distribution] [-logic_level_depth <arg>] 
  [-logic_level_distribution_max <arg>] [-file <arg>] 
  [-append] [-return_string] [-quiet] [-verbose]
```

**分析维度**:

| 维度 | 输出 | 用途 |
|---|---|---|
| `-congestion` | 拥塞等级 1-5（按 Tile 区域） | 评估布线难度 |
| `-timing` | 关键路径逻辑深度、扇出 | 评估时序难度 |
| `-complexity` | 设计复杂度评分 | 综合判断 |
| `-logic_level_distribution` | 逻辑级数分布 | 评估深度 |

**拥塞等级含义**:
- Level 1-2: 轻拥塞
- Level 3: 中等拥塞
- Level 4-5: 严重拥塞

**拥塞等级查询**:
```
get_property STATS.CONGESTION_LEVEL [get_runs impl_1]
```

### 4.4 报告命令汇总

| 命令 | 功能 | 输出 |
|---|---|---|
| `report_timing_summary` | 时序综合 | WNS/TNS/WHS/THS |
| `report_timing` | 详细路径 | 单条路径延迟 |
| `report_design_analysis` | 设计分析 | 拥塞/复杂度/逻辑级 |
| `report_drc` | 设计规则检查 | DRC 违例 |
| `report_utilization` | 资源利用 | LUT/FF/DSP/BRAM 使用 |
| `report_route_status` | 布线状态 | 布线错误/未布网络 |
| `report_power` | 功耗分析 | 动态/静态功耗 |
| `report_clock_networks` | 时钟网络 | 时钟结构 |
| `report_clock_interaction` | 时钟交互 | 跨时钟路径 |
| `report_methodology` | 方法学检查 | Vivado 建议 |
| `report_qor_suggestions` | QoR 建议 | 改进策略 |
| `report_high_fanout_nets` | 高扇出网络 | 扇出 > 阈值 |
| `report_pipeline_analysis` | 流水线分析 | 性能瓶颈 |

### 4.5 约束命令

#### 时钟约束

| 命令 | 用途 | 常用参数 |
|---|---|---|
| `create_clock` | 创建时钟 | `-period <ns> -name <name> [get_ports <port>]` |
| `create_generated_clock` | 创建生成时钟 | `-source <pin> -divide_by <N>` |
| `set_clock_uncertainty` | 时钟不确定性 | `<value> [get_clocks <clk>]` |
| `set_clock_latency` | 时钟延迟 | `-source/-early/-late` |
| `set_input_delay` | 输入延迟 | `-clock <clk> -max/-min <value>` |
| `set_output_delay` | 输出延迟 | `-clock <clk> -max/-min <value>` |
| `set_clock_groups` | 时钟组 | `-asynchronous/-logically_exclusive` |
| `set_false_path` | 虚假路径 | `-from/-to/-through` |
| `set_max_delay` | 最大延迟 | `-from/-to` |
| `set_min_delay` | 最小延迟 | `-from/-to` |
| `set_multicycle_path` | 多周期路径 | `-setup/-hold -from/-to` |
| `set_case_analysis` | 常量传播 | `<value> [get_pins <pin>]` |

#### 物理约束

| 命令 | 用途 |
|---|---|
| `create_pblock` | 创建 Pblock |
| `add_cells_to_pblock` | 添加 cells 到 Pblock |
| `add_aps_to_pblock` | 添加引脚到 Pblock |
| `resize_pblock` | 调整 Pblock 大小 |
| `set_property LOC` | 锁定 cell 位置 |
| `set_property BEL` | 锁定 cell 内部 BEL |
| `set_property FIXED_ROUTE` | 固定路由 |

### 4.6 常用对象查询命令

| 命令 | 用途 |
|---|---|
| `get_cells` | 获取 cell 对象 |
| `get_nets` | 获取网络对象 |
| `get_pins` | 获取引脚对象 |
| `get_ports` | 获取端口对象 |
| `get_clocks` | 获取时钟对象 |
| `get_timing_paths` | 获取时序路径 |
| `get_pblocks` | 获取 Pblock |
| `all_clocks` | 所有时钟 |
| `all_inputs` | 所有输入 |
| `all_outputs` | 所有输出 |
| `all_registers` | 所有寄存器 |

**过滤语法**:
```
get_cells -hierarchical -filter {NAME =~ "*cpu*"}
get_nets -hierarchical -filter {FLAT_PIN_COUNT > 1000}
```

---

## 5. 报告解析

### 5.1 报告格式

- **RPT**: 人类可读纯文本（默认）
- **TXT**: 等同 RPT
- **JSON**: 部分命令支持（`report_timing_summary` 等）
- **CSV**: 部分命令支持
- **XML**: 旧版格式

### 5.2 关键值提取

**使用 `return_string`**:
```
set timing_str [report_timing_summary -return_string]
# 解析字符串
```

**使用正则提取 WNS**:
- WNS 在报告中格式为 "WNS(ns): -0.123" 或 "Worst Negative Slack (WNS): -0.123 ns"

**使用 `get_property`**:
```
get_property STATS.WNS [get_runs impl_1]
```

**使用 `get_timing_paths`**:
```
set paths [get_timing_paths -max_paths 10 -slack_lesser_than 0]
foreach path $paths { ... }
```

### 5.3 JSON 输出命令

支持 `-json` 的命令（2025.1）:
- `report_timing_summary -return_string`（部分）
- `report_utilization`（部分）
- `report_design_analysis`

### 5.4 报告关键数据格式

**时序报告示例**:
```
WNS(ns)  TNS(ns)  WHS(ns)  THS(ns)  TPWS(ns)
 -0.123   -2.456    0.050    0.000      0.000
```

**资源利用示例**:
```
+----------------------------+------+-------+-----------+-------+
|          Site Type         | Used | Fixed | Available | Util% |
+----------------------------+------+-------+-----------+-------+
| Slice LUTs                 | 1234 |     0 |    394080 |  0.31 |
| Slice Registers            | 2345 |     0 |    788160 |  0.30 |
| DSPs                       |   12 |     0 |      2280 |  0.53 |
| Block RAM Tile             |   10 |     0 |       720 |  1.39 |
+----------------------------+------+-------+-----------+-------+
```

---

## 6. 设计检查点（DCP）操作

### 6.1 DCP 文件用途

- **Checkpoint**: 设计快照
- **包含内容**: 完整网表、约束、布局、布线信息
- **文件大小**: 数 MB 到数 GB（取决于设计规模）
- **跨设计复用**: 增量编译的输入

### 6.2 关键命令

| 命令 | 用途 |
|---|---|
| `open_checkpoint` | 打开 DCP |
| `write_checkpoint` | 写出 DCP |
| `read_checkpoint` | 在无工程模式读取 DCP |
| `save_constraints_as` | 保存约束 |
| `import_constraints` | 导入约束 |

### 6.3 `open_checkpoint` 完整语法

```
open_checkpoint [-part <arg>] [-strict] [-quiet] [-verbose] <files>
```

**关键参数**:
- `-part <arg>`: 切换目标器件
- `-strict`: 严格模式（不允许 DCP 与当前工程器件不匹配）

### 6.4 `write_checkpoint` 完整语法

```
write_checkpoint [-force] [-quiet] [-verbose] <file>
```

**典型用法**:
```
write_checkpoint -force post_route.dcp
```

### 6.5 FPL26 比赛 DCP 流程

1. **输入 DCP**: 已完整 place-and-route 的设计快照
2. **中间 DCP**: 修改过程中保存的中间状态
3. **输出 DCP**: Fmax 提升后的新设计

---

## 7. 预设实现策略（Implementation Strategy）完整列表

### 7.1 预设策略

| 策略名称 | 内部配置（place / phys_opt / route） | 用途 |
|---|---|---|
| `Default` | Default / Default / Default | 通用 |
| `Flow_Quick` | Quick / - / Quick | 最快编译 |
| `Flow_RuntimeOptimized` | Quick / - / Quick | 快速迭代 |
| `Flow_PerfOptimized_high` | Explore / Explore / Explore | 高性能 |
| `Flow_PerfOptimized_medium` | Default / Default / Explore | 中等性能 |
| `Flow_AreaOptimized_high` | AltSpreadLogic_high / AlternateReplication / Explore | 高面积优化 |
| `Flow_AreaOptimized_medium` | AltSpreadLogic_high / AlternateReplication / Default | 中等面积优化 |
| `Flow_AlternateRoutability` | AltSpreadLogic_high / - / Explore | 可布线性 |
| `Flow_MapPhysOpt` | Default / Default / Default | 启用物理优化 |
| `Flow_PostRoutePhysOpt` | Default / Default / Default + post-route phys_opt | 布线后物理优化 |
| `Flow_RemapPhysOpt` | - / AddRemap / - | 物理优化+重映射 |
| `Flow_CongOptimized` | SpreadLogic_high / - / Explore | 拥塞优化 |

### 7.2 Performance 系列

| 策略 | 内部配置 |
|---|---|
| `Performance_Explore` | Explore / Explore / Explore |
| `Performance_ExtraTimingOpt` | ExtraTimingOpt / Default / Default |
| `Performance_AggressiveExplore` | AggressiveExplore / AggressiveExplore / AggressiveExplore |
| `Performance_Retiming` | Default / AddRetime / Default |
| `Performance_RetimingExplore` | Explore / AddRetime / Explore |
| `Performance_NetDelay_high` | WLBlockPlacement / Default / HigherDelayCost |
| `Performance_NetDelay_medium` | WLBlockPlacement / Default / Default |
| `Performance_NetDelay_low` | WLBlockPlacement / Default / LowerDelayCost |
| `Performance_WLBlockPlacement` | WLBlockPlacement / Default / Default |
| `Performance_RefinePlacement` | ExtraPostPlacementOpt / Default / NoTimingRelaxation |
| `Performance_SpreadLogic` | SpreadLogic_high / Default / Default |
| `Performance_BalancePlace` | BalancePlace / Default / Default |
| `Performance_BalanceSLR` | BalanceSLR / Default / Default |
| `Performance_HighUtilSLR` | HighUtilSLR / Default / Default |
| `Performance_ExplorePostRoute` | Explore / Default / Explore |
| `Performance_NetDelayOptimization` | WLBlockPlacement / Default / HigherDelayCost |
| `Performance_PlaceDesOpt` | Default / Default / Default |
| `Performance_RemapPhysOpt` | Default / AddRemap / Default |
| `Performance_PhysOpt` | Default / Default / Default + post-route phys_opt |
| `Performance_Aggressive` | AggressiveExplore / AggressiveExplore / AggressiveExplore |
| `Performance_CriticalPathsOpt` | Explore / Explore / Explore |

### 7.3 Area 系列

| 策略 | 内部配置 |
|---|---|
| `Area_Explore` | AltSpreadLogic_high / AlternateReplication / - |
| `Area_ExploreWithRemap` | AltSpreadLogic_high / AlternateReplication + AddRemap / - |
| `Area_ExploreSequentialArea` | AltSpreadLogic_high / Default / - |
| `Area_ExploreLUTRemap` | AltSpreadLogic_high / AddRemap / - |
| `Area_ReduceLUTs` | AltSpreadLogic_high / Default / Default |
| `Area_ReduceLUTs_with_Retiming` | AltSpreadLogic_high / AddRetime / Default |
| `Area_ShiftRegToBRAM` | AltSpreadLogic_high / Default / Default |
| `Area_MapLargeShiftRegToBRAM` | Default / Default / Default + 映射选项 |
| `Area_ShiftRegToURAM` | AltSpreadLogic_high / Default / Default |

### 7.4 Flow 系列

| 策略 | 内部配置 |
|---|---|
| `Flow_Quick` | Quick / - / Quick |
| `Flow_RuntimeOptimized` | Quick / - / Quick |
| `Flow_PerfOptimized_high` | Explore / Explore / Explore |
| `Flow_PerfOptimized_medium` | Default / Default / Explore |
| `Flow_AreaOptimized_high` | AltSpreadLogic_high / AlternateReplication / Explore |
| `Flow_AreaOptimized_medium` | AltSpreadLogic_high / AlternateReplication / Default |
| `Flow_AlternateRoutability` | AltSpreadLogic_high / - / Explore |
| `Flow_MapPhysOpt` | Default / Default / Default |
| `Flow_PostRoutePhysOpt` | Default / Default / Default + post-route phys_opt |
| `Flow_RemapPhysOpt` | - / AddRemap / - |
| `Flow_CongOptimized` | SpreadLogic_high / - / Explore |

### 7.5 Congestion 系列

| 策略 | 内部配置 |
|---|---|
| `Congestion_Default` | Default / Default / Default |
| `Congestion_SpreadLogic_high` | SpreadLogic_high / Default / Default |
| `Congestion_SpreadLogic_medium` | SpreadLogic_medium / Default / Default |
| `Congestion_SpreadLogic_low` | SpreadLogic_low / Default / Default |
| `Congestion_AltSpreadLogic_high` | AltSpreadLogic_high / Default / Default |
| `Congestion_AltSpreadLogic_medium` | AltSpreadLogic_medium / Default / Default |
| `Congestion_AltSpreadLogic_low` | AltSpreadLogic_low / Default / Default |
| `Congestion_Explore` | Explore / Default / Explore |
| `Congestion_NetDelay_high` | WLBlockPlacement / Default / HigherDelayCost |
| `Congestion_NetDelay_medium` | WLBlockPlacement / Default / Default |
| `Congestion_NetDelay_low` | WLBlockPlacement / Default / LowerDelayCost |
| `Congestion_RouteFlow` | Default / Default / Explore |
| `Congestion_BalanceRoute` | Default / Default / BalanceRoute |
| `Congestion_Place_BalanceSLR` | BalanceSLR / Default / Default |
| `Congestion_Place_BalanceSLR_BalanceRoute` | BalanceSLR / Default / BalanceRoute |
| `Congestion_Place_BalanceSLR_ExploreRoute` | BalanceSLR / Default / Explore |

### 7.6 Power 系列

| 策略 | 内部配置 |
|---|---|
| `Power_Default` | Default / Default / Default + power |
| `Power_Explore` | Explore / Explore / Explore + power |
| `Power_Optimized` | Default / Default / Default + power opt |
| `Power_Low` | Default / Default / Default + power low |

### 7.7 SSI 系列

| 策略 | 内部配置 |
|---|---|
| `SSI_Default` | Default / Default / Default |
| `SSI_Explore` | Explore / Explore / Explore |
| `SSI_Quick` | Quick / - / Quick |
| `SSI_BalancePlace` | BalancePlace / Default / Default |
| `SSI_BalanceSLR` | BalanceSLR / Default / Default |
| `SSI_HighUtilSLR` | HighUtilSLR / Default / Default |
| `SSI_SpreadLogic_high` | SpreadLogic_high / Default / Default |
| `SSI_SpreadLogic_low` | SpreadLogic_low / Default / Default |
| `SSI_AltSpreadLogic_high` | AltSpreadLogic_high / Default / Default |
| `SSI_AltSpreadLogic_low` | AltSpreadLogic_low / Default / Default |
| `SSI_PerfOptimized` | Explore / Default / Explore |
| `SSI_AreaOptimized` | AltSpreadLogic_high / Default / Default |

### 7.8 Block 设计与模块化

| 策略 | 用途 |
|---|---|
| `Block_Default` | 块设计默认 |
| `Block_Explore` | 块设计探索 |
| `Block_High` | 块设计高性能 |
| `Block_Low` | 块设计低资源 |
| `Block_Quick` | 块设计快速 |

### 7.9 Implementation Strategy 应用方式

**工程模式**:
```
set_property strategy <StrategyName> [get_runs impl_1]
launch_runs impl_1 -to_step write_bitstream
```

**批处理模式**:
```
set_param synth.elaboration.rodinMoreOptions "..."
# 或在启动时
synth_design -directive <DirectiveName>
place_design -directive <DirectiveName>
route_design -directive <DirectiveName>
phys_opt_design -directive <DirectiveName>
```

---

# 第二部分 RapidWright

## 8. 安装与版本对应

### 8.1 项目概况

- **主仓库**: https://github.com/Xilinx/RapidWright
- **官网**: https://www.rapidwright.io/
- **文档**: https://www.rapidwright.io/docs/index.html
- **许可证**: Apache 2.0
- **维护方**: AMD Research and Advanced Development（前 Xilinx Research Labs）
- **核心作者**: Chris Lavin, Eddie Hung 等
- **核心论文**: FCCM 2018: "RapidWright: Enabling Custom Crafted Implementations for FPGAs"

### 8.2 版本对应表

| RapidWright 版本 | 发布日期 | 对应 Vivado |
|---|---|---|
| v2025.2.2-beta | 2025-06-05 | Vivado 2025.2 |
| v2025.2.1-beta | 2025-02-19 | Vivado 2025.2 |
| v2025.2.0-beta | 2024-12-02 | Vivado 2025.2 |
| v2025.1.3-beta | 2024-10-03 | Vivado 2025.1 |
| v2025.1.1-beta | 2024-08-13 | Vivado 2025.1 |
| v2025.1.0-beta | 2024-06-25 | Vivado 2025.1 |
| v2024.2.3-beta | 2024-05-29 | Vivado 2024.2 |
| v2024.2.2-beta | 2024-03-26 | Vivado 2024.2 |
| v2024.2.1-beta | 2024-01-15 | Vivado 2024.2 |
| v2024.2.0-beta | 2023-12-05 | Vivado 2024.2 |

**FPL26 比赛固定 commit**: `f63afef`（比赛仓库通过 git submodule 引入）

### 8.3 安装方式

#### 方式 1: Python pip 安装（推荐）

**前提**:
- Python 3.x
- Java 1.8 或更高
- pip

**安装命令**:
```
pip install rapidwright
```

**说明**:
- 通过 JPype 实现 Python 3 到 Java 的桥接
- 自动下载 standalone JAR
- 支持 tab 补全 Java 类方法
- 可运行 RapidWright GUI 应用

**验证安装**:
```
python -c "import rapidwright; print(rapidwright.__version__)"
```

#### 方式 2: 独立 JAR 包

**下载**:
- GitHub Releases 页面下载预编译 JAR
- 或从 rapidwright.io 下载

**运行**:
```
java -jar rapidwright-<version>.jar
```

#### 方式 3: Gradle 源码编译

**前提**:
- Java 8-21
- Gradle

**步骤**:
1. 克隆仓库: `git clone https://github.com/Xilinx/RapidWright.git`
2. 切换到目标版本: `git checkout v2025.1.3-beta`
3. 构建: `gradle build`
4. 生成的 JAR 在 `build/libs/`

#### 方式 4: Binder 在线试用

- **链接**: https://mybinder.org/v2/gh/clavin-xlnx/RapidWright-binder/python3-no-docker
- 用途: 无需本地安装的 Jupyter Notebook 环境

### 8.4 Java 版本要求

- **最低**: Java 8
- **推荐**: Java 11（比赛使用）或更高
- **构建**: Gradle 7.x+

### 8.5 依赖

- Or-tools 9.14.6206
- protobuf 4.31.1
- Jacl 1.4.1（XDC TCL 解析）
- ELK（NetlistBrowser 原理图）
- JUnit 5（测试）

---

## 9. 核心类与方法

### 9.1 类层次总览

```
com.xilinx.rapidwright
├── device                           # 器件模型
│   ├── Device                       # FPGA 器件
│   ├── Tile                         # 物理瓦片
│   ├── Site                         # 物理位置
│   ├── PIP                          # 可编程互连点
│   ├── BEL                          # 基本逻辑单元
│   ├── Wire                         # 物理连线
│   ├── Node                         # RRG 节点
│   ├── PartNameTools                # 器件名工具
│   └── Series                       # 器件系列
├── design                           # 物理设计
│   ├── Design                       # 顶层设计
│   ├── Cell                         # 实例 cell
│   ├── Net                          # 网络
│   ├── Pin                          # 引脚
│   ├── Port                         # 端口
│   ├── SiteInst                     # 站点实例
│   ├── SitePinInst                  # 站点引脚实例
│   └── UnplaceCellException         # 异常
├── place                            # 布局
│   ├── Placer                       # 布局器基类
│   ├── ...
│   └── Router
├── route                            # 路由
│   ├── RWRoute                      # 时序驱动路由器
│   └── ...
├── ecopt                            # ECO
│   ├── ECOTools                     # ECO 工具
│   └── ...
├── bitstream                        # 比特流
│   ├── Bitstream                    # 比特流读取
│   └── ...
├── debug                            # 调试
│   ├── ...
│   └──
├── gui                              # GUI
│   ├── NetlistBrowser               # 网表浏览器
│   └── ...
├── interchange                      # FPGA Interchange
│   ├── ...
│   └──
└── util                             # 工具
    ├── JobScheduler                 # 任务调度
    ├── FileTools                    # 文件工具
    └── ...
```

### 9.2 核心类详解

#### `Device` 类

**功能**: FPGA 器件模型

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getDevice(String partName)` | 获取指定器件 |
| `getDevice(Device.AWS_F1)` | 获取 AWS F1 实例器件 |
| `getName()` | 获取器件名 |
| `getTiles()` | 获取所有 Tile |
| `getTile(int row, int col)` | 获取指定坐标 Tile |
| `getSites()` | 获取所有 Site |
| `getAllPIPs()` | 获取所有 PIP |
| `getWires()` | 获取所有 Wire |
| `getBELs()` | 获取所有 BEL |
| `getRootTimingModel()` | 获取根时序模型 |

**支持的器件系列**:
- 7-Series (Artix-7, Kintex-7, Virtex-7, Zynq-7000)
- UltraScale
- UltraScale+ (含 Zynq UltraScale+ MPSoC, RFSoC)
- Versal（除 VP1902 外的所有器件，含 V80）

#### `Design` 类

**功能**: 物理设计顶层对象

**关键方法**:

| 方法 | 功能 |
|---|---|
| `readCheckpoint(String path)` | 读取 DCP |
| `writeCheckpoint(String path)` | 写出 DCP |
| `readCheckpoint(String path, CodePerfTracker)` | 读取 DCP（带性能跟踪） |
| `getPartName()` | 获取目标器件名 |
| `setPartName(String partName)` | 设置目标器件名 |
| `getName()` | 获取设计名 |
| `getNets()` | 获取所有网络 |
| `getCells()` | 获取所有 cell |
| `getPlace()` | 布局状态 |
| `getRoute()` | 布线状态 |
| `getTopEDIF()` | 获取顶层 EDIF |
| `getO() ` | 设计状态 |

**检查点操作参数**:
- `Design.readCheckpoint(path)`: 默认行为
- 内部调用: `CodePerfTracker` 跟踪

**DCP 读取性能**: 加载 DCP 比 Vivado 快 3-5 倍

#### `Net` 类

**功能**: 网络对象

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` | 获取网络名 |
| `getSource()` | 获取源引脚 |
| `getSinks()` | 获取所有 sink |
| `getPins()` | 获取所有引脚 |
| `getFanout()` | 获取扇出数 |
| `getType()` | 获取类型（GND/VCC/SIGNAL） |
| `isPIPPlaced()` | 是否已布 PIP |
| `getPIPs()` | 获取所有 PIP |
| `getRouteTree()` | 获取路由树 |
| `getSourceSiteInst()` | 获取源 site 实例 |
| `setSource(PortInst)` | 设置源 |
| `addSink(PortInst)` | 添加 sink |
| `disconnect()` | 断开网络 |
| `getCriticalPath()` | 获取关键路径 |

**Net 类型常量**:
- `NetType.GND`
- `NetType.VCC`
- `NetType.SIGNAL`

#### `Cell` 类

**功能**: Cell 实例

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` | 获取 cell 名 |
| `getType()` | 获取 cell 类型 |
| `getBEL()` | 获取 BEL 位置 |
| `setBEL(BEL bel)` | 设置 BEL 位置 |
| `getSiteInst()` | 获取 site 实例 |
| `getPins()` | 获取所有引脚 |
| `getPin(String name)` | 获取指定引脚 |
| `isLocked()` | 是否锁定 |
| `setLocked(boolean)` | 设置锁定 |
| `getTile()` | 获取所在 Tile |
| `unplace()` | 取消布局 |

**Cell 类型**:
- LUT（LUT1-LUT6）
- FF（寄存器）
- DSP（DSP48E2 等）
- BRAM（Block RAM）
- URAM
- CARRY（进位链）
- MUX（多路选择器）
- IO/IBUF/OBUF/IOBUF

#### `Site` 类

**功能**: 物理位置

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` | 获取 site 名 |
| `getType()` | 获取类型（SLICE/DSP/BRAM 等） |
| `getBELs()` | 获取所有 BEL |
| `getBEL(String name)` | 获取指定 BEL |
| `getTile()` | 获取所在 Tile |
| `getRows()` / `getColumns()` | 获取坐标 |
| `isAvailable()` | 是否可用 |
| `getSitePinInsts()` | 获取所有 site pin |

**常见 Site 类型**:
- SLICE（逻辑）
- DSP48E2
- RAMB36 / RAMB18（BRAM）
- URAM288
- IO/IBUF/OBUF
- BUFG / BUFR / BUFH（时钟缓冲）
- MMCM / PLL
- PCIE

#### `Tile` 类

**功能**: 物理瓦片

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` | 获取 Tile 名 |
| `getType()` | 获取 Tile 类型 |
| `getSites()` | 获取所有 Site |
| `getWires()` | 获取所有 Wire |
| `getPIPs()` | 获取所有 PIP |
| `getRow()` / `getCol()` | 获取坐标 |
| `getSliceSites()` | 获取所有 SLICE |

**Tile 类型**:
- CLB（可配置逻辑块）
- DSP
- BRAM
- INT（互连）
- CLK（时钟）
- IO
- CFG（配置）

#### `PIP` 类

**功能**: 可编程互连点

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getStartWire()` | 获取起始 Wire |
| `getEndWire()` | 获取结束 Wire |
| `isPIPDownhill()` | 是否下坡 PIP |
| `isBidirectional()` | 是否双向 |
| `getTile()` | 获取所在 Tile |

**PIP 类型**:
- PIP 单向（仅 start→end）
- PIP 双向（可双向使用）

#### `BEL` 类

**功能**: 基本逻辑单元

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` | 获取 BEL 名 |
| `getType()` | 获取类型 |
| `getSite()` | 获取所在 Site |
| `getPins()` | 获取所有引脚 |
| `getPin(String name)` | 获取指定引脚 |

**常见 BEL 类型**:
- LUT（6输入 LUT）
- FF（寄存器）
- CARRY（进位）
- MUX（多路选择器）
- RAMB
- DSP

#### `SiteInst` 类

**功能**: Site 实例（已布局的 Site）

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getSite()` | 获取 Site |
| `getCells()` | 获取所有 Cell |
| `getSitePinInsts()` | 获取所有 SitePinInst |
| `place(Cell, BEL)` | 布局 Cell |
| `unplaceCell(Cell)` | 取消布局 |
| `isUsed()` | 是否被使用 |

#### `PortInst` / `SitePinInst` 类

**功能**: 端口实例 / Site 引脚实例

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` | 获取名字 |
| `getCell()` | 获取关联 Cell |
| `getNet()` | 获取关联 Net |
| `isOutward()` | 方向（输入/输出） |

### 9.3 关键枚举与常量

| 枚举 | 用途 |
|---|---|
| `Device.AWS_F1` | AWS F1 实例 (xcvu9p) |
| `Device.AWS_F2` | AWS F2 实例 (xcvu47p) |
| `NetType` | 网络类型 (GND/VCC/SIGNAL) |
| `SiteTypeEnum` | Site 类型 |
| `BELType` | BEL 类型 |
| `TileTypeEnum` | Tile 类型 |
| `PIPDirection` | PIP 方向 |

### 9.4 Java API 入口

**典型 Java 入口代码结构**:
```java
import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.design.Cell;
import com.xilinx.rapidwright.design.SiteInst;
import com.xilinx.rapidwright.device.Site;
import com.xilinx.rapidwright.device.BEL;

public class MyOptimizer {
    public static void main(String[] args) {
        Device device = Device.getDevice(Device.AWS_F1);
        Design design = Design.readCheckpoint("input.dcp");
        // 修改 design
        design.writeCheckpoint("output.dcp");
    }
}
```

### 9.5 Python API 入口

**典型 Python 入口代码结构**:
```python
import rapidwright
from com.xilinx.rapidwright.device import Device
from com.xilinx.rapidwright.design import Design
from com.xilinx.rapidwright.design import Net, Cell
from com.xilinx.rapidwright.device import Site, BEL

# 获取器件
device = Device.getDevice(Device.AWS_F1)
print(device.getName())  # 'xcvu9p'

# 读取 DCP
design = Design.readCheckpoint("input.dcp")

# 获取所有网络
for net in design.getNets():
    print(f"Net: {net.getName()}, Fanout: {net.getFanout()}")

# 写出 DCP
design.writeCheckpoint("output.dcp")
```

---

## 10. DCP 文件读写

### 10.1 DCP 文件结构

DCP 包含以下内容:
- **EDIF 网表**: 设计逻辑
- **约束 (XDC)**: 时序/物理约束
- **布局信息**: cell 到 Site 的映射
- **布线信息**: PIP 链
- **时序数据**: pre-route 估计
- **功耗数据**: pre-route 估计

### 10.2 读 DCP

**Java**:
```java
Design design = Design.readCheckpoint("/path/to/input.dcp");
```

**Python**:
```python
design = Design.readCheckpoint("/path/to/input.dcp")
```

**性能**: 加载 100k cell DCP 约 5-10 秒（RapidWright），Vivado 启动 30-60 秒

### 10.3 写 DCP

**Java**:
```java
design.writeCheckpoint("/path/to/output.dcp");
```

**Python**:
```python
design.writeCheckpoint("/path/to/output.dcp")
```

### 10.4 DCP 文件内容验证

**可读取的内容**:
- 网表（设计逻辑）
- 约束（XDC）
- 已布局 cells
- 已布线 nets
- 时序估计

**不能读取的内容**:
- 加密的 DCP（需 Vivado 解密）
- bitstream 相关配置

### 10.5 FPGA Interchange Format

**用途**: 开放标准，连接不同工具

**支持工具**:
- Vivado（2024.1+）
- RapidWright
- DREAMPlaceFPGA
- VTR（实验性）

**RapidWright 互转**:
- DCP → FPGA Interchange
- FPGA Interchange → DCP

---

## 11. 关键模块详解

### 11.1 RWRoute

**功能**: 时序驱动路由器

**位置**: `com.xilinx.rapidwright.route`

**模式**:
- Timing-driven routing（时序驱动，默认）
- Wirelength-driven routing（线长驱动）
- Partial routing（部分路由）

**关键参数**:

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | 必填 | 输入 DCP |
| `--output` | 必填 | 输出 DCP |
| `--routeMode` | TIMING | 路由模式 |
| `--maxIterations` | 100 | 最大迭代次数 |
| `--timingWeight` | 1.0 | 时序权重 |
| `--wirelengthWeight` | 0.5 | 线长权重 |
| `--congestionWeight` | 0.5 | 拥塞权重 |
| `--numThreads` | 1 | 线程数 |

**性能**（3,855 nets 设计）:
- 总路由时间: 8.21 秒
- 迭代次数: 7
- 数据路径延迟: 2,331 ps（与 Vivado 一致）
- Slack 差异: ~20 ps

**支持架构**:
- 7-Series
- UltraScale
- UltraScale+
- Versal（部分）

**与 Vivado route_design 对比**:
- 速度: RWRoute 快 10-100 倍
- QoR: 略低于 Vivado（~5-10%）
- 适用: 快速迭代、原型验证

### 11.2 ECOTools

**功能**: 工程变更指令（ECO）

**关键方法**:

| 方法 | 功能 |
|---|---|
| `disconnectNet(Net)` | 断开网络 |
| `connectNet(Net, PortInst, PortInst)` | 连接网络 |
| `removeCell(Cell)` | 移除 cell |
| `addCell(...)` | 添加 cell |
| `replaceCell(...)` | 替换 cell |
| `mergeNets(...)` | 合并网络 |
| `splitNet(...)` | 拆分网络 |

**典型场景**:
- 时序违例后定点修改
- 修复保持时间违例
- 优化关键路径

### 11.3 DesignTools

**功能**: 通用设计工具

**关键方法**:

| 方法 | 功能 |
|---|---|
| `createMissingSitePinInsts(Design)` | 创建缺失的 site pin 实例 |
| `optimizeFanout(Design, Net, int k)` | 高扇出网络优化（按 k 分割） |
| `optimizeLUTInputCone(Design, LUT)` | 优化 LUT 输入锥 |
| `analyzeFabricForPblock(Design, long lutCount, ...)` | 分析 pblock 候选区域 |
| `convertFabricRegionToPblock(...)` | 转换区域为 pblock |
| `reportTimingSummary(Design)` | 时序报告 |
| `parallelRouterInit(Design, int threads)` | 并行路由初始化 |

**扇出优化示例参数**:
- `k=2-3`: fanout 200-500
- `k=3-5`: fanout 500-1500
- `k=5-8`: fanout > 1500

### 11.4 VivadoTools

**功能**: 调用 Vivado 工具

**关键方法**:

| 方法 | 功能 |
|---|---|
| `reportPlaceStatus(...)` | 调用 Vivado `report_place_status` |
| `reportRouteStatus(...)` | 调用 Vivado `report_route_status` |
| `getVivadoDcpLoadError(...)` | 获取 Vivado 加载错误 |
| `checkDcpAgainstVivado(...)` | 与 Vivado 交叉验证 DCP |
| `runVivadoWithArguments(...)` | 执行 Vivado 命令 |

### 11.5 Bitstream

**功能**: 比特流操作

**能力**:
- 读取现有 bitstream
- 修改配置数据
- 不能生成完整 bitstream（需 Vivado）

### 11.6 NetlistBrowser

**功能**: 网表浏览器 GUI

**能力**:
- 可视化 cell、net 关系
- 查看 cell 内部结构
- 原理图导出（基于 ELK）
- 2025.2.0 起的 Unit Instance Schematic Viewer

### 11.7 任务调度 (JobScheduler)

**功能**: 并行化任务

**关键方法**:

| 方法 | 功能 |
|---|---|
| `scheduleJob(Runnable)` | 调度任务 |
| `waitForAllJobs()` | 等待所有任务 |
| `setThreadCount(int)` | 设置线程数 |

### 11.8 报告 (ReportTimingData)

**功能**: 时序报告生成

**支持**:
- 解析 Vivado 时序报告
- 解析 Vivado utilization 报告
- 生成 RapidWright 内部时序报告

---

## 12. 教程与示例场景

### 12.1 官方教程列表

来源: https://www.rapidwright.io/docs/Tutorials.html

| 教程 | 描述 |
|---|---|
| RWRoute Timing-driven Routing | 时序驱动路由 |
| RWRoute Wirelength-driven Routing | 线长驱动路由 |
| RWRoute Partial Routing | 部分路由 |
| Report Timing Example | 时序报告示例 |
| Reusing Timing-Closed Logic As A Shell | 复用时序收敛逻辑 |
| Use DREAMPlaceFPGA via FPGA Interchange | DREAMPlaceFPGA 集成 |
| Polynomial Generator | 秒级生成已放置已路由电路 |
| ECO Insert Route Debug | ECO 插入调试核 |
| SLR Crosser DCP Creator | 跨 SLR DCP 创建 |
| IPI with Pre-Implemented Blocks | IP Integrator 预实现模块 |
| PipelineGenerator | 流水线生成器 |
| PipelineGenerator with Routing | 带路由的流水线生成器 |
| Pre-implemented Modules Part I/II | 预实现模块教程 |
| SLR Bridge | SLR 桥接创建 |
| FPGA 2019 Deep Dive | FPGA 2019 深度教程 |
| FCCM 2019 Workshop | FCCM 2019 工作坊 |
| FPL 2019 Tutorial | FPL 2019 教程 |
| ICCAD 2023 Hands-on Tutorial | ICCAD 2023 实践教程 |

### 12.2 典型使用场景

#### 场景 1: 高扇出网络优化

- **问题**: 扇出 > 1000 的网络延迟大
- **RapidWright 方案**: `DesignTools.optimizeFanout(design, net, k)`
  - 复制源寄存器，按 k 分割负载
- **效果**: 显著减少高扇出网络延迟

#### 场景 2: Pblock 区域约束重放置

- **问题**: 关键路径 cells 跨越大区域
- **RapidWright 方案**:
  - `DesignTools.analyzeCriticalPathSpread(design, ...)`
  - `DesignTools.analyzeFabricForPblock(design, ...)`
  - `DesignTools.convertFabricRegionToPblock(...)`
- **效果**: 关键路径 cell 物理邻近

#### 场景 3: 时序报告解析

- **RapidWright 方案**:
  - `ReportTimingData` 解析 Vivado 时序报告
  - 提取 WNS/TNS/WHS/THS
  - 分类失败原因

#### 场景 4: FPGA Interchange Format 集成

- **用途**: 接入其他开源工具
- **RapidWright 方案**: DCP → Interchange → 其他工具

#### 场景 5: 预实现模块复用

- **RapidWright 方案**:
  - `ReuseTimingClosedLogicAsShell` 教程
  - 把已优化模块作为新设计的 shell

#### 场景 6: SLR 桥接创建

- **RapidWright 方案**:
  - `SLRBridge` 教程
  - 在跨 SLR 路径中插入桥接

---

# 第三部分 Vivado 与 RapidWright 协作

### 13.1 协作模式

**模式 1: 批处理 Vivado + 后期 RapidWright 分析**
- Vivado 完成 place/route
- RapidWright 读取 DCP 分析
- RapidWright 写出 DCP 给 Vivado 验证

**模式 2: RapidWright 预处理 + Vivado 完整流程**
- RapidWright 快速迭代 DCP
- Vivado 完整 place/route 验证

**模式 3: Vivado + RapidWright 混合 ECO**
- Vivado 主流程
- RapidWright 做定点 ECO
- Vivado 重新 route

**模式 4: 比赛 FPL26 模式**
- 读取已 route DCP
- RapidWright 分析高扇出/关键路径
- Vivado phys_opt_design 优化
- 写出新 DCP
- Vivado 验证

### 13.2 数据交换格式

**DCP**: 主交换格式
- Vivado 写出 → RapidWright 读取
- RapidWright 写出 → Vivado 读取

**FPGA Interchange**: 开放格式
- Vivado 2024.1+ 支持读写
- RapidWright 支持读写
- 第三方工具支持

### 13.3 时序模型差异

**Vivado 时序模型**:
- 完整时序引擎
- 包含时钟偏斜、不确定性
- 包含 wire delay 精确估算
- 包含 SSIT/SLR 互连

**RapidWright 时序模型**:
- 轻量级近似
- 数据路径延迟与 Vivado 一致
- 时钟偏斜/不确定性差异 ~20 ps
- 2019 起引入（参考 FPT 2019 Timing Model Paper）

**应用建议**:
- 用 RapidWright 快速迭代
- 用 Vivado 最终验证

### 13.4 FPL26 比赛的双 MCP 服务器架构

**RapidWrightMCP**:
- 提供工具: `initialize_rapidwright`, `read_checkpoint`, `write_checkpoint`, `get_design_info`, `optimize_fanout`, `optimize_lut_input_cone`, `analyze_fabric_for_pblock`, `convert_fabric_region_to_pblock`, `get_supported_devices`, `get_device_info`, `search_cells`, `get_tile_info`, `search_sites`

**VivadoMCP**:
- 提供工具: `open_checkpoint`, `write_checkpoint`, `report_timing_summary`, `get_critical_high_fanout_nets`, `analyze_critical_path_spread`, `report_utilization_for_pblock`, `create_and_apply_pblock`, `report_route_status`, `write_edif`, `phys_opt_design`, `route_design`, `place_design`, `run_tcl`, `restart_vivado`

**LLM 交互**:
- LLM 通过 OpenRouter 调用
- 通过 MCP 协议与服务器通信
- 服务器执行工具，返回结果

---

# 第四部分 常见数据与默认值

### 14.1 报告关键数据

| 数据 | 默认值 | 说明 |
|---|---|---|
| WNS | 0 | Worst Negative Slack（setup） |
| TNS | 0 | Total Negative Slack（setup） |
| WHS | >0 | Worst Hold Slack |
| THS | >0 | Total Hold Slack |
| TPWS | >0 | Total Pulse Width Slack |
| 拥塞等级 | 1-5 | 1=轻，5=严重 |
| 时序路径数（默认） | 100 | `report_timing` 默认 |
| 物理优化 directive | Default | 可选 Explore 等 |

### 14.2 RapidWright 默认参数

| 参数 | 默认 | 说明 |
|---|---|---|
| 路由迭代次数 | 100 | 可调整 |
| 时序权重 | 1.0 | RWRoute |
| 线长权重 | 0.5 | RWRoute |
| 拥塞权重 | 0.5 | RWRoute |
| Java 内存 | -Xmx2g | 默认 2GB |
| 并行线程 | 1 | 可调整 |

### 14.3 FPL26 比赛 DCP 关键数据

| 项 | 值 |
|---|---|
| 目标器件 | xcvu3p-ffvc1517-2-e |
| LUTs 总数 | 394,080 |
| FFs 总数 | 788,160 |
| DSPs 总数 | 2,280 |
| BRAMs 总数 | 720 |
| 时钟约束 | `clk_fpl26contest` |
| Fmax 公式 | 1000 / (period - WNS) |
| 时间预算 | 1 小时/benchmark |
| LLM 预算 | $1/benchmark |

### 14.4 13 个 Benchmark 详细数据

| Benchmark | LUTs | FFs | DSPs | BRAMs | 初始 Fmax (MHz) |
|---|---|---|---|---|---|
| amd_mini-isp | 3k | 4k | 40 | 12 | 307 |
| boom_soc | 227k | 98k | 61 | 161 | 48.2 |
| corescore_500_mod | 100k | 120k | 0 | 250 | 344.2 |
| finn_radioml | 74k | 46k | 0 | 252 | 284.9 |
| ispd16_example2 | 289k | 234k | 200 | 384 | 107.6 |
| logicnets_jscl | 31k | 2k | 0 | 0 | 403.6 |
| rosetta_3d-rendering | 14k | 5k | 3 | 0 | 270.9 |
| rosetta_digit-recognition | 23k | 23k | 0 | 16 | 367.0 |
| rosetta_optical-flow | 34k | 37k | 4 | 26 | 324.9 |
| rosetta_spam-filter | 5k | 13k | 2 | 2 | 437.4 |
| vexriscv_re-place | 2k | 1k | 4 | 6 | 310.2 |
| vexriscv_re-place_v2 | 2k | 2k | 4 | 4 | 397.5 |
| vtr_mcml | 43k | 15k | 10 | 51 | 262.2 |

---

# 第五部分 参考资料

### 比赛官方

- [FPL'26 Contest Website](https://xilinx.github.io/fpl26_optimization_contest/)
- [FPL'26 Contest Details](https://xilinx.github.io/fpl26_optimization_contest/details.html)
- [FPL'26 Scoring Criteria](https://xilinx.github.io/fpl26_optimization_contest/score.html)
- [FPL'26 Benchmark Details](https://xilinx.github.io/fpl26_optimization_contest/benchmarks.html)
- [FPL'26 Runtime Environment](https://xilinx.github.io/fpl26_optimization_contest/runtime.html)
- [FPL'26 Benchmarks v1.1.0 Release](https://github.com/Xilinx/fpl26_optimization_contest/releases/tag/v1.1.0)
- [FPL'26 Contest GitHub](https://github.com/Xilinx/fpl26_optimization_contest)

### Vivado 2025.1 官方文档

- [Vivado What's New - AMD](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado/vivado-whats-new.html)
- [UG973 Vivado Release Notes](https://docs.amd.com/r/en-US/ug973-vivado-release-notes-install-license)
- [UG835 Vivado TCL Command Reference](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands)
- [UG835 place_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/place_design)
- [UG835 route_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/route_design)
- [UG835 phys_opt_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/phys_opt_design)
- [UG835 opt_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/opt_design)
- [UG835 synth_design](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/synth_design)
- [UG835 report_timing_summary](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/report_timing_summary)
- [UG835 report_timing](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/report_timing)
- [UG835 report_design_analysis](https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/report_design_analysis)
- [UG904 Implementation](https://docs.amd.com/r/en-US/ug904-vivado-implementation)
- [UG904 Implementation Strategy Descriptions](https://docs.amd.com/r/en-US/ug904-vivado-implementation/Implementation-Strategy-Descriptions)
- [UG904 phys_opt_design](https://docs.amd.com/r/en-US/ug904-vivado-implementation/phys_opt_design)
- [UG894 Using Tcl Scripting](https://docs.amd.com/r/en-US/ug894-vivado-tcl-scripting)
- [UG892 Documentation Navigator](https://docs.amd.com/r/en-US/ug892-vivado-design-flows-overview)
- [UG949 智能设计运行 (简体中文)](https://docs.amd.com/r/2025.1-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87/ug949-vivado-design-methodology/%E4%BD%BF%E7%94%A8%E6%99%BA%E8%83%BD%E8%AE%BE%E8%AE%A1%E8%BF%90%E8%A1%8C)
- [2025.1 Release - AMD Wiki](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/3281321985/2025.1+Release)
- [AWS Marketplace Vivado ML 2025.1 AMI](https://aws.amazon.com/marketplace/pp/prodview-evssv7ysyt6h4)
- [XilinxTclStore GitHub](https://github.com/Xilinx/XilinxTclStore)

### RapidWright

- [RapidWright GitHub](https://github.com/Xilinx/RapidWright)
- [RapidWright 官网](https://www.rapidwright.io/)
- [RapidWright 文档](https://www.rapidwright.io/docs/index.html)
- [RapidWright Publications](https://www.rapidwright.io/docs/Papers.html)
- [RapidWright FAQ](https://www.rapidwright.io/docs/FAQ.html)
- [RapidWright Tutorials](https://www.rapidwright.io/docs/Tutorials.html)
- [RapidWright Javadoc](http://www.rapidwright.io/javadoc/)
- [RapidWright Python PIP 安装](https://www.rapidwright.io/docs/Install_RapidWright_as_a_Python_PIP_Package.html)
- [RWRoute Timing-driven Routing 教程](https://www.rapidwright.io/docs/RWRoute_timing_driven_routing.html)
- [RWRoute Wirelength-driven Routing 教程](https://www.rapidwright.io/docs/RWRoute_wirelength_driven_routing.html)
- [RWRoute Partial Routing 教程](https://www.rapidwright.io/docs/RWRoute_partial_routing.html)
- [Report Timing Example 教程](https://www.rapidwright.io/docs/Report_Timing_Example.html)
- [DREAMPlaceFPGA 集成教程](https://www.rapidwright.io/docs/DREAMPlaceFPGA.html)
- [Design Checkpoints 文档](https://www.rapidwright.io/docs/Design_Checkpoints.html)
- [FCCM 2018 RapidWright 论文 PDF](https://www.rapidwright.io/docs/_downloads/c2ac737a132c3fb753fc780a629cf468/FCCM18-RapidWright.pdf)
- [ICCAD 2023 RapidWright Invited Paper](https://ieeexplore.ieee.org/document/10323739)
- [FPT 2019 Timing Model Paper](https://ieeexplore.ieee.org/document/8977880)
- [FPGA 2019 RapidWright Paper](https://dl.acm.org/doi/10.1145/3289602.3293928)
- [FPL 2024 DynaRapid Paper (Best Paper)](https://www.rapidwright.io/docs/Papers.html)
- [RapidStream 2.0 - ACM TRETS](https://dl.acm.org/doi/10.1145/3593025)
- [RapidWright DeepWiki](https://deepwiki.com/Xilinx/RapidWright)

### 中文资源

- [Vivado 2025.1 新功能 - 知乎](https://zhuanlan.zhihu.com/p/1953107304959415543)
- [下载 AMD Vivado 2025.1 - FPGA技术网](https://fpga.eetrend.com/content/2025/100592169.html)
- [Vivado Implementation Strategy 选择指南 - CSDN](https://blog.csdn.net/Js_cold/article/details/156447214)
- [使用Vivado进行物理优化 phys_opt_design - CSDN](https://blog.csdn.net/u011565038/article/details/137557429)
- [FPGA Retiming 优化技术 - FPGA技术网](https://fpga.eetrend.com/blog/2023/100567854.html)

---

> **文档共 5 大部分**：Vivado 2025.1（命令、报告、策略）+ RapidWright（API、模块、教程）+ 协作模式 + 常见数据。所有命令、参数、API 方法以表格形式呈现，便于快速检索。
