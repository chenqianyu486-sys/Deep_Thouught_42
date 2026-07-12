# Vivado 2025.1 与 RapidWright 使用参考

> **版本**: 2026-07-12
> **适用对象**: Claude Code（离线参考）
> **范围**: Vivado 2025.1 命令、参数、报告；RapidWright API、模块、操作流程
> **性质**: 事实陈述，不含代码示例
> **校验方法**: Vivado 命令语法/指令取自本地 `vivado <cmd> -help`（Vivado v2025.1, SW Build 6140274）；实现策略及其内部 step 指令映射通过 `create_project` + `set_property strategy` 在 xcvu3p 与 xcvu9p 两个器件上实测验证。RapidWright API 取自本地 `javap` 与 `RELEASE_NOTES.TXT`（bundled 版本 2025.2.1-beta）。

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

取自 `vivado -help`（启动器参数，非 TCL 命令参数）:

| 参数 | 作用 |
|---|---|
| `-mode <gui\|tcl\|batch>` | 启动模式（默认 gui） |
| `-source <file>` | 启动时 source 指定 TCL 文件 |
| `-script <file>` | 执行指定脚本文件后退出 |
| `-init` | source `vivado.tcl` 文件 |
| `-log <file>` | 指定 log 文件（默认 vivado.log） |
| `-journal <file>` | 指定 journal 文件（默认 vivado.jou） |
| `-nolog` / `-nojournal` | 不生成 log / journal |
| `-applog` / `-appjournal` | 以追加模式打开 log / journal |
| `-tempDir <dir>` | 指定临时目录 |
| `-tclargs <arg>` | 向 TCL 传入 argc/argv 参数 |
| `-robot <arg>` | Robot JAR 文件名 |
| `-version` | 输出版本信息后退出 |
| `-verbose` | 暂停消息上限 |
| `[<project>]` | 直接加载 .xpr 工程或 .dcp 检查点 |

> 注：旧文档列出的 `-notrace`、`-lic_waittime` 在 `vivado` 启动器中**均不存在**。

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

**完整语法**（取自 `synth_design -help`）:
```
synth_design [-name <arg>] [-part <arg>] [-constrset <arg>] [-top <arg>]
  [-include_dirs <args>] [-generic <args>] [-define <args>]
  [-verilog_define <args>] [-vhdl_define <args>]
  [-flatten_hierarchy <arg>] [-gated_clock_conversion <arg>]
  [-directive <arg>] [-rtl] [-lint] [-file <arg>] [-bufg <arg>]
  [-no_lc] [-lut_cascade] [-shreg_min_size <arg>] [-mode <arg>]
  [-fsm_extraction <arg>] [-rtl_skip_mlo] [-rtl_skip_ip]
  [-rtl_skip_constraints] [-srl_style <arg>]
  [-keep_equivalent_registers] [-resource_sharing <arg>]
  [-cascade_dsp <arg>] [-control_set_opt_threshold <arg>]
  [-incremental_mode <arg>] [-max_bram <arg>] [-max_uram <arg>]
  [-max_dsp <arg>] [-max_bram_cascade_height <arg>]
  [-max_uram_cascade_height <arg>] [-global_retiming <arg>]
  [-no_srlextract] [-assert] [-no_timing_driven] [-sfcu]
  [-debug_log] [-quiet] [-verbose]
```

> 注：旧文档列出的 `-retiming`、`-max_buram`、`-hierarchical_block*`、`-fanout_limit`、`-mvcstyle`、`-sweep` 在 2025.1 的 `synth_design` 中均**不存在**（`-sweep` 属于 `opt_design`；retiming 用 `-global_retiming`；层级块相关选项不属于 `synth_design`）。

**directive（综合策略，大小写敏感，取自 -help）**:

| Directive | 用途 |
|---|---|
| `default` | 默认综合流程（注意全小写） |
| `RuntimeOptimized` | 减少 RTL/时序优化以缩短综合时间 |
| `AreaOptimized_high` | 通用面积优化（含 AreaMapLargeShiftRegToBRAM、AreaThresholdUseDSP） |
| `AreaOptimized_medium` | 通用面积优化（三元加法器、进位链阈值、面积优化复用器） |
| `AlternateRoutability` | 改善可布线性，减少 MUXF/CARRY 使用 |
| `AreaMapLargeShiftRegToBRAM` | 检测大移位寄存器并用 BRAM 实现 |
| `AreaMultThresholdDSP` | 降低 DSP 推断阈值 |
| `FewerCarryChains` | 提高用 LUT 替代进位链的操作数阈值 |
| `PerformanceOptimized` | 通用时序优化（含逻辑级数缩减，代价为面积） |
| `LogicCompaction` | 配置乘法器 LUT/进位链以便布局器紧凑打包 |
| `PowerOptimized_high` | 高功耗优化 |
| `PowerOptimized_medium` | 中等功耗优化 |

> 注：旧文档列出的 `FlowOptimized_high`、`FlowAreaOptimized_high`、`FlowAlternateRoutability`、`FlowPerfOptimized_high`、`FlowPerfThresholdCarry`、`PerformanceRetiming`、`PerformanceExtraTimingOpt`、`AggressiveExplore` 在 2025.1 的 `synth_design` 中均**不存在**；retiming 由 `-global_retiming` 选项控制，而非 directive。

#### `opt_design`

**功能**: 逻辑优化（综合后/实现后均可运行）

**完整语法**（取自 `opt_design -help`）:
```
opt_design [-retarget] [-propconst] [-sweep] [-bram_power_opt] [-remap]
  [-aggressive_remap] [-resynth_remap] [-resynth_area] [-resynth_seq_area]
  [-directive <arg>] [-muxf_remap] [-hier_fanout_limit <arg>]
  [-bufg_opt] [-mbufg_opt] [-shift_register_opt] [-dsp_register_opt]
  [-srl_remap_modes <arg>] [-control_set_merge] [-control_set_opt]
  [-merge_equivalent_drivers] [-carry_remap] [-debug_log]
  [-property_opt_only] [-quiet] [-verbose]
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
| `AddRemap` | LUT 重新映射（注：`-help` 未列出，但 2025.1 运行时合法，RC=0） |

### 4.2 实现命令

#### `place_design`

**功能**: 布局（Place）

**完整语法**（取自 `place_design -help`）:
```
place_design [-directive <arg>] [-subdirective <args>] [-no_timing_driven]
  [-eco] [-timing_summary] [-unplace] [-post_place_opt] [-no_psip]
  [-psip_options <args>] [-sll_align_opt] [-clock_vtree_type <arg>]
  [-no_bufg_opt] [-ultrathreads] [-no_noc_opt]
  [-net_delay_weight <arg>] [-quiet] [-verbose]
```

**关键参数**:
- `-directive`: 布局策略（见下方策略表）
- `-subdirective`: 按布局阶段（Floorplan/GPlace/DPlace）施加子策略，格式 `<phase>.<sub>.<low|med|high>`，可多选（Tcl 列表）
- `-post_place_opt`: 布局后优化关键路径
- `-unplace`: 仅取消布局，不进行新布局
- `-no_timing_driven`: 禁用时序驱动（拥塞场景可选）
- `-no_psip`: 禁用布局器中物理综合（PSIP）
- `-psip_options`: 显式开启 PSIP 阶段某些优化（critical_cell_opt/fanout_opt/retime 等）
- `-no_bufg_opt`: 禁用全局缓冲插入
- `-no_noc_opt`: 禁用 NoC 相关布局优化（Versal）
- `-net_delay_weight`: 网络延迟权重
- `-eco`: ECO 模式，保留已有布局仅放置新增单元

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

> 注：`place_design -help` 的 "Supported values include" 仅列出 5 个（`Default`/`Explore`/`AggressiveExplore`/`RuntimeOptimized`/`Quick`），但该列表**非穷举**。上表中其余 directive（`AltSpreadLogic_*`、`ExtraNetDelay_*`、`SSI_*`、`WLDrivenBlockPlacement`、`ExtraPostPlacementOpt`、`ExtraTimingOpt`、`RQS`、`Auto_1/2/3`）经 `set_property STEPS.PLACE_DESIGN.ARGS.DIRECTIVE` 实测均为合法值（被预设实现策略使用）。注意 `WLBlockPlacement`（无 "Driven"）不存在，正确名为 `WLDrivenBlockPlacement`。

#### `route_design`

**功能**: 布线（Route）

**完整语法**（取自 `route_design -help`）:
```
route_design [-unroute] [-release_memory] [-nets <args>] [-physical_nets]
  [-pins <arg>] [-directive <arg>] [-tns_cleanup]
  [-no_timing_driven] [-preserve] [-delay] [-auto_delay]
  -max_delay <arg> -min_delay <arg> [-timing_summary] [-finalize]
  [-ultrathreads] [-eco] [-no_psir] [-quiet] [-verbose]
```

> 注：`-unroute` 不带参数（旧文档误写为 `-unroute <arg>`）；`-pins` 取单值 `<arg>`（旧文档误写为 `<args>`）。`-max_delay`/`-min_delay` 与 `-delay`/`-auto_delay` 配合使用。

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

**完整语法**（取自 `phys_opt_design -help`）:
```
phys_opt_design [-fanout_opt] [-placement_opt] [-routing_opt]
  [-slr_crossing_opt] [-insert_negative_edge_ffs] [-restruct_opt]
  [-interconnect_retime] [-lut_opt] [-casc_opt] [-cell_group_opt]
  [-equ_drivers_opt] [-critical_cell_opt] [-dsp_register_opt]
  [-bram_register_opt] [-uram_register_opt] [-bram_enable_opt]
  [-shift_register_opt] [-hold_fix] [-aggressive_hold_fix] [-retime]
  [-force_replication_on_nets <args>] [-directive <arg>]
  [-critical_pin_opt] [-clock_opt] [-path_groups <args>]
  [-tns_cleanup] [-sll_reg_hold_fix] [-memory_rewire_opt]
  [-quiet] [-verbose]
```

**选项详解**:

| 选项 | 功能 | 适用场景 |
|---|---|---|
| `-fanout_opt` | 高扇出网络优化 | 扇出 > 1000 信号 |
| `-placement_opt` | 重新布局优化 | 时序违例 |
| `-routing_opt` | 重新布线优化 | 拥塞 |
| `-slr_crossing_opt` | SLR 交叉优化 | 跨 SLR 路径 |
| `-memory_rewire_opt` | BRAM/URAM 关键信号引脚重新连接 | Versal 器件 BRAM/URAM 时序 |
| `-restruct_opt` | 关键路径逻辑重构 | 关键路径 |
| `-interconnect_retime` | 互连重定时 | 跨互连 retiming |
| `-lut_opt` | LUT 优化 | 关键 LUT |
| `-casc_opt` | 级联优化 | DSP/进位级联 |
| `-cell_group_opt` | 单元组优化 | 关键单元组 |
| `-equ_drivers_opt` | 等价驱动合并 | 等价驱动网络 |
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

**完整语法**（取自 `report_timing_summary -help`）:
```
report_timing_summary [-check_timing_verbose] [-delay_type <arg>]
  [-no_detailed_paths] [-setup] [-hold] [-max_paths <arg>]
  [-nworst <arg>] [-unique_pins] [-path_type <arg>] [-no_reused_label]
  [-input_pins] [-no_pr_attribute] [-no_pblock] [-routable_nets]
  [-slack_lesser_than <arg>] [-report_unconstrained]
  [-significant_digits <arg>] [-no_header] [-file <arg>] [-append]
  [-name <arg>] [-return_string] [-warn_on_violation] [-datasheet]
  [-cells <args>] [-rpx <arg>] [-quiet] [-verbose]
```

> 注：旧文档列出的 `-min_slack`、`-max_slack`、`-sort_by`、`-of_objects`、`-include_headers`、`-slack_greater_than` 在 `report_timing_summary` 中**均不存在**（`-sort_by`/`-of_objects`/`-slack_greater_than` 属于 `report_timing`；`-include_headers` 应为 `-no_header`）。

**关键参数**:
- `-delay_type max|min|min_max`: max=setup 检查，min=hold 检查，min_max=两者
- `-setup` / `-hold`: 仅报告 setup / hold 检查
- `-max_paths <N>`: 报告的路径数
- `-nworst <N>`: 每个时钟组的最差路径数
- `-slack_lesser_than <value>`: 仅报告 slack 小于该值
- `-path_type summary|short|full|end`: 报告详细程度
- `-no_detailed_paths`: 不输出详细路径，仅汇总
- `-return_string`: 返回字符串而非写入文件
- `-file <name>`: 输出到文件
- `-append`: 追加到文件
- `-rpx <name>`: 输出可重打开的报告对象（Report Object）

**报告内容**:
- WNS (Worst Negative Slack): 最差负 slack
- TNS (Total Negative Slack): 总负 slack
- WHS (Worst Hold Slack): 最差 hold slack
- THS (Total Hold Slack): 总 hold slack
- TPWS (Total Pulse Width Slack): 脉冲宽度 slack
- 每个时钟组的详细情况

#### `report_timing`

**功能**: 详细时序路径报告

**完整语法**（取自 `report_timing -help`）:
```
report_timing [-from <args>] [-rise_from <args>] [-fall_from <args>]
  [-to <args>] [-rise_to <args>] [-fall_to <args>]
  [-through <args>] [-rise_through <args>] [-fall_through <args>]
  [-delay_type <arg>] [-setup] [-hold] [-max_paths <arg>]
  [-nworst <arg>] [-unique_pins] [-path_type <arg>] [-input_pins]
  [-no_header] [-no_reused_label] [-slack_lesser_than <arg>]
  [-slack_greater_than <arg>] [-group <args>] [-sort_by <arg>]
  [-no_report_unconstrained] [-user_ignored] [-of_objects <args>]
  [-significant_digits <arg>] [-column_style <arg>] [-file <arg>]
  [-append] [-name <arg>] [-no_pr_attribute] [-no_pblock]
  [-routable_nets] [-return_string] [-warn_on_violation]
  [-cells <args>] [-rpx <arg>] [-quiet] [-verbose]
```

> 注：旧文档列出的 `-min_slack`/`-max_slack`/`-check_timing_verbose`/`-include_headers`/`-rise`/`-fall` 在 `report_timing` 中**均不存在**。方向过滤用 `-rise_from`/`-fall_from`/`-rise_to`/`-fall_to`/`-rise_through`/`-fall_through`（非 `-rise`/`-fall`）；`-include_headers` 应为 `-no_header`。

**关键参数**:
- `-from <args>`: 起点（cell pin、port 等）
- `-to <args>`: 终点
- `-through <args>`: 经过的 cell/pin
- `-rise_from`/`-fall_from`/`-rise_to`/`-fall_to`/`-rise_through`/`-fall_through`: 按跳变方向过滤
- `-cells <args>`: 限定到指定层级单元
- `-delay_type max|min|min_max`: 路径类型
- `-setup` / `-hold`: 仅报告 setup / hold
- `-group <args>`: 限定路径组
- `-sort_by <arg>`: 排序方式
- `-of_objects <args>`: 针对指定路径对象报告
- `-max_paths <N>`: 路径数

**报告内容**: 单条路径的完整延迟分解（logic delay / net delay / clock skew）

#### `report_design_analysis`

**功能**: 设计分析（时序、拥塞、复杂度等）

**完整语法**（取自 `report_design_analysis -help`）:
```
report_design_analysis [-file <arg>] [-csv <arg>] [-append] [-return_string]
  [-complexity] [-cells <args>] [-bounding_boxes <args>]
  [-hierarchical_depth <arg>] [-rent_greater_than <arg>]
  [-instances_greater_than <arg>] [-instances_lesser_than <arg>]
  [-av_fanout_greater_than <arg>] [-congestion] [-min_congestion_level <arg>]
  [-timing] [-setup] [-hold] [-show_all] [-full_logical_pin]
  [-routed_vs_estimated] [-logic_level_distribution]
  [-logic_level_dist_paths <arg>] [-min_level <arg>] [-max_level <arg>]
  [-return_timing_paths] [-of_timing_paths <args>] [-max_paths <arg>]
  [-extend] [-routes] [-end_point_clocks <args>] [-logic_levels <arg>]
  [-qor_summary] [-json <arg>] [-name <arg>] [-no_pr_attribute]
  [-quiet] [-verbose]
```

> 注：旧文档列出的 `-logic_level_depth`、`-logic_level_distribution_max` **不存在**；逻辑级数过滤用 `-logic_levels`/`-min_level`/`-max_level`，分布路径用 `-logic_level_dist_paths`。`-json <arg>` 可直接输出 JSON。

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
| `resize_pblock` | 调整 Pblock 大小（-add/-remove/-from/-to） |
| `delete_pblock` | 删除 Pblock |
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

### 5.3 结构化/机器可读输出

各报告命令的结构化输出参数（取自 -help，2025.1）:
- `report_design_analysis -json <file>`：直接输出 JSON（本表唯一带 `-json` 的命令）
- `report_design_analysis -csv <file>`：输出 CSV
- `report_timing_summary -rpx <file>` / `report_timing -rpx <file>` / `report_methodology -rpx` / `report_drc -rpx` / `report_power -rpx`：输出可重打开的 Report Object（.rpx，XML 格式）
- `report_utilization -spreadsheet_file <file>`：输出电子表格
- 其余报告命令多以 `-return_string` 返回纯文本字符串供 TCL 解析

> 注：旧文档称 `report_timing_summary`/`report_utilization` 支持 `-json`，实测二者**无 `-json` 参数**。

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
open_checkpoint [-part <arg>] [-ignore_timing] [-quiet] [-verbose] <file>
```

**关键参数**:
- `-part <arg>`: 切换目标器件
- `-ignore_timing`: 不加载时序数据（仅读网表/布局/布线），加速加载
- `<file>`: DCP 文件路径

> 注：旧文档列出的 `-strict` 在 `open_checkpoint` 中**不存在**。

### 6.4 `write_checkpoint` 完整语法

```
write_checkpoint [-force] [-cell <arg>] [-logic_function_stripped] [-encrypt]
  [-key <arg>] [-quiet] [-verbose] [<file>]
```

**关键参数**:
- `-force`: 覆盖已存在文件
- `-cell <arg>`: 仅写出指定 cell 的 DCP（模块复用/out-of-context）
- `-logic_function_stripped`: 剥离逻辑功能信息（保密）
- `-encrypt`: 写出加密 DCP
- `-key <arg>`: 加密密钥
- `[<file>]`: 输出路径（不指定时写到工程默认位置）

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

### 7.1 校验说明与默认策略

> **重要更正**：旧文档本节列出的 ~70 个策略名（`Flow_PerfOptimized_*`、`Flow_AreaOptimized_*`、`Flow_MapPhysOpt`、`Flow_PostRoutePhysOpt`、`Flow_RemapPhysOpt`、`Flow_CongOptimized`、`Performance_AggressiveExplore`、`Performance_RetimingExplore`、`Performance_NetDelay_medium`、`Performance_SpreadLogic`、`Performance_BalancePlace`、`Performance_BalanceSLR`、`Performance_HighUtilSLR`、`Performance_ExplorePostRoute`、`Performance_NetDelayOptimization`、`Performance_PlaceDesOpt`、`Performance_RemapPhysOpt`、`Performance_PhysOpt`、`Performance_Aggressive`、`Performance_CriticalPathsOpt`、`Area_ExploreSequentialArea`、`Area_ExploreLUTRemap`、`Area_ReduceLUTs*`、`Area_ShiftRegTo*`、`Area_MapLargeShiftRegToBRAM`、`Congestion_Default`、`Congestion_AltSpreadLogic_*`、`Congestion_Explore`、`Congestion_NetDelay_*`、`Congestion_RouteFlow`、`Congestion_BalanceRoute`、`Congestion_Place_BalanceSLR*`、全部 `Power_*`、全部 `SSI_*`、全部 `Block_*`）经 `set_property strategy` 在 **xcvu3p 与 xcvu9p 两个器件上实测**，均报 `Strategy 'X' is not supported by the flow 'Vivado Implementation 2025'`，即**在 Vivado 2025.1 中不存在**。`get_impl_strategies` 在 batch TCL 中未注册，无法穷举；完整列表以 UG904 2025.1 或 GUI 为准。下表为实测有效的策略及其权威内部 step 指令映射（通过设置策略后读取 `STEPS.*.ARGS.DIRECTIVE` 获得）。

- **默认策略**：`Vivado Implementation Defaults`（opt=Default / place=Default / phys_opt=Default / route=Default，phys_opt 启用）。注意默认策略名不是 `Default`。

### 7.2 实测有效策略及内部配置（opt / place / phys_opt / route）

| 策略名称 | opt_design | place_design | phys_opt_design | route_design | 备注 |
|---|---|---|---|---|---|
| `Flow_Quick` | RuntimeOptimized | Quick | -(禁用) | Quick | 最快编译 |
| `Flow_RuntimeOptimized` | RuntimeOptimized | RuntimeOptimized | -(禁用) | RuntimeOptimized | 快速迭代 |
| `Performance_Explore` | Explore | Explore | Explore | Explore | 高性能通用 |
| `Performance_ExtraTimingOpt` | Default | ExtraTimingOpt | Explore | NoTimingRelaxation | 额外时序优化 |
| `Performance_Retiming` | Default | ExtraPostPlacementOpt | AlternateFlowWithRetiming | Explore | 含 retiming |
| `Performance_NetDelay_high` | Default | ExtraNetDelay_high | AggressiveExplore | NoTimingRelaxation | 高悲观度网络延迟 |
| `Performance_NetDelay_low` | Explore | ExtraNetDelay_low | AggressiveExplore | NoTimingRelaxation | 低悲观度网络延迟 |
| `Performance_WLBlockPlacement` | Explore | WLDrivenBlockPlacement | Explore | Explore | RAM/DSP 线长驱动 |
| `Performance_RefinePlacement` | Default | ExtraPostPlacementOpt | Explore | Explore | 精细布局优化 |
| `Performance_BalanceSLLs` | Default | SSI_BalanceSLLs | Explore | Explore | 跨 SLR 均衡 SLL |
| `Performance_BalanceSLRs` | Default | SSI_BalanceSLRs | Explore | Explore | 跨 SLR 均衡单元数 |
| `Performance_HighUtilSLRs` | Default | SSI_HighUtilSLRs | Explore | Explore | 各 SLR 紧凑布局 |
| `Area_Explore` | ExploreArea | Default | -(禁用) | Default | 面积探索（仅 opt 改变） |
| `Area_ExploreWithRemap` | ExploreWithRemap | Default | -(禁用) | Default | 面积探索 + remap |
| `Congestion_SpreadLogic_high` | Default | AltSpreadLogic_high | AggressiveExplore | AlternateCLBRouting | 严重拥塞 |
| `Congestion_SpreadLogic_medium` | Default | AltSpreadLogic_medium | Explore | AlternateCLBRouting | 中等拥塞 |
| `Congestion_SpreadLogic_low` | Default | AltSpreadLogic_low | Explore | AlternateCLBRouting | 轻拥塞 |

> 说明：表中 "phys_opt -(禁用)" 表示该策略 `STEPS.PHYS_OPT_DESIGN.IS_ENABLED=0`，即不运行物理优化步骤。所有已测策略的 `STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED=0`（布线后物理优化默认关闭）。`Area_*` 系列仅改变 opt_design directive，place/route/phys_opt 均为 Default 且禁用 phys_opt。

### 7.3 已删除/不存在的策略类别（2025.1）

以下类别在 Vivado 2025.1 的 "Vivado Implementation 2025" flow 中**已不存在**（实测在 xcvu3p 与 xcvu9p 上均不支持）：

- **Flow_\***：除 `Flow_Quick`、`Flow_RuntimeOptimized` 外，其余 `Flow_PerfOptimized_*`/`Flow_AreaOptimized_*`/`Flow_MapPhysOpt`/`Flow_PostRoutePhysOpt`/`Flow_RemapPhysOpt`/`Flow_CongOptimized`/`Flow_AlternateRoutability` 均无效。
- **Power_\***：`Power_Default`/`Power_Explore`/`Power_Optimized`/`Power_Low` 全部无效（功耗优化改由 `power_opt_design` 命令或工程选项控制）。
- **SSI_\***：`SSI_Default`/`SSI_Explore`/`SSI_Quick`/`SSI_BalancePlace`/`SSI_BalanceSLR`/`SSI_HighUtilSLR`/`SSI_SpreadLogic_*`/`SSI_AltSpreadLogic_*`/`SSI_PerfOptimized`/`SSI_AreaOptimized` 全部无效。SSI 相关优化已并入 `Performance_BalanceSLLs`/`Performance_BalanceSLRs`/`Performance_HighUtilSLRs` 三个策略（其 place directive 使用 `SSI_BalanceSLLs`/`SSI_BalanceSLRs`/`SSI_HighUtilSLRs`）。
- **Block_\***：`Block_Default`/`Block_Explore`/`Block_High`/`Block_Low`/`Block_Quick` 全部无效。
- **Congestion_\***：仅 `Congestion_SpreadLogic_high/medium/low` 有效，其余 `Congestion_Default`/`Congestion_AltSpreadLogic_*`/`Congestion_Explore`/`Congestion_NetDelay_*`/`Congestion_RouteFlow`/`Congestion_BalanceRoute`/`Congestion_Place_BalanceSLR*` 无效。
- **Area_\***：仅 `Area_Explore`/`Area_ExploreWithRemap` 有效，其余无效。
- **Performance_\***：仅上表 9 个有效，其余 `Performance_AggressiveExplore`/`Performance_RetimingExplore`/`Performance_NetDelay_medium`/`Performance_SpreadLogic`/`Performance_BalancePlace`/`Performance_BalanceSLR`/`Performance_HighUtilSLR`/`Performance_ExplorePostRoute`/`Performance_NetDelayOptimization`/`Performance_PlaceDesOpt`/`Performance_RemapPhysOpt`/`Performance_PhysOpt`/`Performance_Aggressive`/`Performance_CriticalPathsOpt` 无效。

### 7.4 Implementation Strategy 应用方式

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

发布日期取自 bundled `RELEASE_NOTES.TXT`（旧文档日期系统性偏早约 1 年，已全部纠正）:

| RapidWright 版本 | 发布日期 | 对应 Vivado |
|---|---|---|
| v2025.2.1-beta | 2026-02-18 | Vivado 2025.2 |
| v2025.2.0-beta | 2025-12-01 | Vivado 2025.2 |
| v2025.1.3-beta | 2025-10-02 | Vivado 2025.1 |
| v2025.1.2-beta | 2025-10-02（Maven jar 损坏，已废弃，用 2025.1.3） | Vivado 2025.1 |
| v2025.1.1-beta | 2025-08-13 | Vivado 2025.1 |
| v2025.1.0-beta | 2025-06-25 | Vivado 2025.1 |
| v2024.2.3-beta | 2025-05-29 | Vivado 2024.2 |
| v2024.2.2-beta | 2025-03-25 | Vivado 2024.2 |
| v2024.2.1-beta | 2025-01-15 | Vivado 2024.2 |
| v2024.2.0-beta | 2024-12-04 | Vivado 2024.2 |

> **本仓库 bundled 版本**：`2025.2.1-beta`（由 `RapidWright/jars/rapidwright-api-lib-2025.2.1.jar` 与 `RELEASE_NOTES.TXT` 顶端条目确认），对应 Vivado 2025.2。注：本仓库 DCP 为 Vivado 2025.1 生成，RapidWright 2025.2.x 可向下兼容读取 2025.1 DCP。

> **关于 "固定 commit f63afef"**：旧文档与本仓库 `FPL26_Claude_Code_Reference.md` 均称比赛固定 RapidWright commit `f63afef`（对应 v2025.1.3-beta）。但本仓库中 `RapidWright/` 在 git tree 中以普通目录（mode `040000`，非 gitlink `160000`） vendored，`.git/modules/RapidWright` 不存在，故该 commit 无法在本地核对；且实际 bundled 版本为 2025.2.1-beta，与 v2025.1.3-beta 不符。以实际 bundled 版本为准。

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

（取自 `RapidWright/jars/` 实际 jar 版本）
- Or-tools 9.14.6206（`ortools-java-9.14.6206.jar`）
- protobuf 4.31.1（`protobuf-java-4.31.1.jar`）
- Jacl 1.4.1（`jacl-1.4.1.jar`，XDC TCL 解析）
- ELK 0.10.0（`org.eclipse.elk.*-0.10.0.jar`，NetlistBrowser 原理图）
- JUnit 5.7.1（`junit-jupiter-*-5.7.1.jar`，测试）
- 另含 guava 33.6.0-jre、jgrapht-core 1.3.0、kryo 5.2.1、commons-io 2.20.0 等

---

## 9. 核心类与方法

### 9.1 类层次总览

```
com.xilinx.rapidwright
├── device                           # 器件模型
│   ├── Device                       # FPGA 器件（含 AWS_F1/PYNQ_Z1/KCU105 常量）
│   ├── Tile                         # 物理瓦片
│   ├── Site                         # 物理位置
│   ├── PIP                          # 可编程互连点
│   ├── BEL                          # 基本逻辑单元
│   ├── Wire / Node                  # 物理连线 / 节点
│   ├── Part / Series / FamilyType   # 器件名 / 系列 / 架构
│   ├── ClockRegion / SLR            # 时钟区域 / SSI SLR
│   └── SiteTypeEnum / TileTypeEnum / BELTypeEnum  # 类型枚举
├── design                           # 物理设计
│   ├── Design                       # 顶层设计
│   ├── Cell / Net / SiteInst        # cell / 网络 / 站点实例
│   ├── SitePinInst / Port / PinType # 站点引脚实例 / 端口 / 引脚类型
│   ├── NetType                      # 网络类型枚举（WIRE/GND/VCC/UNKNOWN）
│   ├── DesignTools                  # 设计工具（createMissingSitePinInsts 等）
│   ├── Module / ModuleInst          # 预实现模块
│   └── Unisim / UnisimManager       # 原语映射
├── eco                              # ECO（旧文档误写为 ecopt）
│   └── ECOTools                     # ECO 工具
├── rwroute                          # 路由（旧文档误写为 route）
│   ├── RWRoute                      # 时序/线长驱动路由器
│   └── GlobalSignalRouting
├── router                           # 路由底层
│   └── RouteNode                    # 路由节点
├── bitstream                        # 比特流
├── gui                              # GUI（NetlistBrowser）
├── interchange                      # FPGA Interchange
└── util                             # 工具
    ├── VivadoTools                  # 调用 Vivado（reportRouteStatus/runTcl 等）
    ├── JobScheduler                 # 任务调度
    └── FileTools                    # 文件工具
```

> 注：包名经 `javap`/jar 核对。RapidWright 无独立 `place` 包（布局由 Vivado 或外部 placer 完成）；`ECOTools` 在 `eco` 包（非 `ecopt`）；`RWRoute` 在 `rwroute` 包（非 `route`）；`VivadoTools`/`JobScheduler`/`FileTools` 在 `util` 包。

### 9.2 核心类详解

#### `Device` 类

**功能**: FPGA 器件模型

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getDevice(String partName)` | 静态，按器件名获取 Device |
| `getDevice(Part part)` | 静态，按 Part 对象获取 Device |
| `getName()` | 器件名 |
| `getTiles()` | 所有 Tile（`Tile[][]`） |
| `getTile(int row, int col)` / `getTile(String)` / `getTile(int)` | 按坐标/名/索引取 Tile |
| `getRows()` / `getColumns()` | 行列数 |
| `getSite(String)` | 按名取 Site |
| `getAllSitesOfType(SiteTypeEnum)` | 按类型取所有 Site |
| `getAllCompatibleSites(SiteTypeEnum)` | 按类型取所有兼容 Site |
| `getBELs(SiteTypeEnum)` / `getBEL(SiteTypeEnum, String)` | 按类型取 BEL |
| `getSLRs()` / `getNumOfSLRs()` / `getMasterSLR()` | SSI SLR 信息 |
| `getClockRegions()` / `getClockRegion(int, int)` | 时钟区域 |
| `getNode(String)` / `getWire(String)` | 取 Node / Wire |
| `getSeries()` / `getFamilyType()` / `getArchitecture()` | 系列/架构 |
| `getActivePackage()` / `getPackages()` | 封装 |
| `AWS_F1` / `PYNQ_Z1` / `KCU105` | 预置器件名常量（String） |
| `RAPIDWRIGHT_VERSION` | RapidWright 版本字符串 |

> 注：旧文档列出的 `getSites()`、`getAllPIPs()`、`getWires()`、`getBELs()`（无参）、`getRootTimingModel()` 在 `Device` 中**均不存在**；Site 按 `getSite(String)`/`getAllSitesOfType` 查询，PIP 按 Tile 取（`tile.getPIPs()`），Wire 按 `getWire(String)`/`getWireCount()`，BEL 按 `getBELs(SiteTypeEnum)`。`AWS_F2` 不存在。

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
| `readCheckpoint(String path)` | 静态，读取 DCP 返回 Design |
| `readCheckpoint(String, CodePerfTracker)` | 读取 DCP（带性能跟踪） |
| `writeCheckpoint(String path)` | 写出 DCP |
| `getPartName()` / `setPartName(String)` | 目标器件名 |
| `getName()` / `setName(String)` | 设计名 |
| `getNets()` / `getNet(String)` | 所有网络 / 按名取网络 |
| `getCells()` | 所有 cell |
| `getSiteInsts()` | 所有 SiteInst |
| `getTopEDIF()` / `getNetlist()` | 顶层 EDIF 网表 |
| `unrouteDesign()` / `unplaceDesign()` | 清除布线 / 清除布局 |
| `updateDesignWithCheckpointPlaceAndRoute(String)` | 从 DCP 同步布局布线 |
| `createAndPlaceCell(...)` | 创建并放置 cell |

**检查点操作参数**:
- `Design.readCheckpoint(path)`: 默认行为
- 内部调用: `CodePerfTracker` 跟踪

**DCP 读取性能**: 加载 DCP 比 Vivado 快 3-5 倍

> 注：旧文档列出的 `getPlace()`、`getRoute()`、`getO()` 在 `Design` 中**均不存在**。布局/布线状态分别由 `cell.isPlaced()` 与 `net.hasPIPs()` 判定，或用 `unplaceDesign()`/`unrouteDesign()` 清除。

#### `Net` 类

**功能**: 网络对象

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` / `setName(String)` | 网络名 |
| `getType()` | 网络类型（`NetType`） |
| `getSource()` | 源 SitePinInst |
| `getSinkPins()` | 所有 sink SitePinInst（`List<SitePinInst>`） |
| `getPins()` | 所有引脚（`List<SitePinInst>`） |
| `getFanOut()` | 扇出数（int） |
| `hasPIPs()` | 是否已布 PIP |
| `getPIPs()` / `getCopyOfPIPs()` | 所有 PIP |
| `setSource(SitePinInst)` | 设置源（参数是 SitePinInst，非 PortInst） |
| `addPin(SitePinInst)` / `removePin(SitePinInst)` | 增删引脚 |
| `unroute()` | 取消本网布线 |
| `getSiteInsts()` | 关联的 SiteInst 集合 |
| `getSourceTile()` | 源所在 Tile |
| `lockRouting()` / `unlockRouting()` | 锁定/解锁布线 |
| `isStaticNet()` / `isVCCNet()` / `isGNDNet()` / `isClockNet()` | 网络性质判断 |

**Net 类型常量**（`NetType` 枚举）:
- `NetType.WIRE`（普通信号网）
- `NetType.GND`
- `NetType.VCC`
- `NetType.UNKNOWN`

> 注：旧文档列出的 `getSinks()`、`getFanout()`、`isPIPPlaced()`、`getRouteTree()`、`getSourceSiteInst()`、`addSink()`、`disconnect()`、`getCriticalPath()` 在 `Net` 中**均不存在**；正确名见上表。`NetType.SIGNAL` 不存在，普通信号网为 `NetType.WIRE`。

#### `Cell` 类

**功能**: Cell 实例

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` / `updateName(String)` | cell 名 |
| `getType()` / `setType(String)` | cell 类型字符串 |
| `getEDIFCellInst()` / `getEDIFHierCellInst()` | 关联 EDIF 单元实例 |
| `getBEL()` | 所在 BEL（BEL 在构造时设定，无 `setBEL(BEL)`） |
| `getBELName()` / `getSiteName()` | BEL 名 / site 名 |
| `getSiteInst()` / `setSiteInst(SiteInst)` | site 实例 |
| `getSite()` / `getTile()` | 所在 Site / Tile |
| `getBELPin(EDIFPortInst)` / `getBELPin(EDIFHierPortInst)` | 按 EDIF 端口取 BELPin |
| `getCorrespondingSitePinName(String)` / `getSitePinFromLogicalPin(String, List)` | 取 site pin 名 |
| `getPinMappingsL2P()` / `getPinMappingsP2L()` | 逻辑<->物理引脚映射 |
| `getPhysicalPinMapping(String)` / `getPhysicalPinMappings()` | 物理引脚映射 |
| `isPlaced()` | 是否已布局 |
| `unplace()` | 取消布局 |
| `isLocked()` / `setLocked(boolean)` | 锁定状态 |
| `isBELFixed()` / `setBELFixed(boolean)` / `isSiteFixed()` | BEL/Site 固定 |
| `fixPin(String)` / `unFixPin(String)` | 固定/解除固定引脚映射 |
| `copyCell(String, EDIFHierCellInst)` | 复制 cell |
| `connectStaticSourceToPins(NetType, String...)` | 连接静态源到引脚 |
| `addProperty(String, ...)` / `getProperty(String)` | EDIF 属性操作 |

> 注：旧文档列出的 `getPins()`、`setBEL(BEL)`、`getPin(String)` 在 `Cell` 中**均不存在**（`getType()` 与 `unplace()` 实际存在，上一篇纠正有误已修正）。引脚经 `getBELPin`/`getCorrespondingSitePinName`/`getPinMappings*` 获取；BEL 在构造时设定，不可运行时修改。

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
| `getName()` | site 名 |
| `getSiteTypeEnum()` | site 类型（`SiteTypeEnum`，如 SLICE/DSP48E2） |
| `getAlternateSiteTypeEnums()` | 可切换的备选类型 |
| `getBEL(String)` / `getBELs()` | 取 BEL |
| `getTile()` / `getDevice()` | 所在 Tile / 所属 Device |
| `getInstanceX()` / `getInstanceY()` | 实例坐标（`getRows`/`getColumns` 在 Device 上） |
| `getRpmX()` / `getRpmY()` | RPM 坐标 |
| `getIntTile()` | 关联的互连 Tile |
| `getSitePinCount()` / `getPinName(int)` / `getPinIndex(String)` | site pin 信息 |
| `getBELPins(int)` / `getBELPin(String)` | site wire 上的 BELPin |
| `getSitePIPs()` / `getSitePIPCount()` | site 内 PIP |
| `getClockRegion()` | 所属时钟区域 |
| `isInputPin(int)` / `isOutputPin(int)` | pin 方向判断 |

> 注：旧文档列出的 `getType()`（应为 `getSiteTypeEnum`）、`getRows()`/`getColumns()`（在 `Device` 上，Site 用 `getInstanceX/Y`）、`isAvailable()`、`getSitePinInsts()`（在 `SiteInst` 上）在 `Site` 中**均不存在**。

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
| `getName()` / `getRootName()` | Tile 名 / 根名 |
| `getTileTypeEnum()` | Tile 类型（`TileTypeEnum`） |
| `getSites()` / `getSiteIndex(Site)` | 所含 Site |
| `getPIPs()` / `getPIPs(int)` / `getBackwardPIPs(int)` | 所含 PIP |
| `getRow()` / `getColumn()` | 坐标（注意无 `getCol`） |
| `getTileXCoordinate()` / `getTileYCoordinate()` | 物理坐标 |
| `getWireCount()` / `getWireName(int)` / `getWireIndex(String)` / `getWireNames()` | Wire 信息 |
| `getWireConnections(int)` | Wire 连接关系 |
| `getTileNeighbor(int, int)` / `getTileXYNeighbor(int, int)` | 相邻 Tile |
| `getManhattanDistance(Tile)` | 曼哈顿距离 |
| `getClockRegion()` / `getSLR()` | 所属时钟区域 / SLR |
| `getDevice()` | 所属 Device |

> 注：旧文档列出的 `getType()`（应为 `getTileTypeEnum`）、`getWires()`（应为 `getWireCount`/`getWireNames`）、`getCol()`（应为 `getColumn`）、`getSliceSites()`（应为 `getSites`）在 `Tile` 中**均不存在**。

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
| `getStartWire()` / `getEndWire()` | 起/止 Wire |
| `getStartWireName()` / `getEndWireName()` | 起/止 Wire 名 |
| `getStartWireIndex()` / `getEndWireIndex()` | 起/止 Wire 索引 |
| `getStartNode()` / `getEndNode()` | 起/止 Node |
| `getTile()` / `setTile(Tile)` | 所在 Tile |
| `getPIPType()` | PIP 类型（`PIPType` 枚举，含方向信息） |
| `isBidirectional()` | 是否双向 |
| `isReversed()` / `isPIPFixed()` / `isRouteThru()` | 是否反向/固定/旁路 |
| `getAllPossibleEndWires()` | 所有可能的终止 Wire |

> 注：旧文档列出的 `isPIPDownhill()` 在 `PIP` 中**不存在**；方向/类型信息由 `getPIPType()`（返回 `PIPType` 枚举）获取。

**PIP 类型**:
- PIP 单向（仅 start→end）
- PIP 双向（可双向使用）

#### `BEL` 类

**功能**: 基本逻辑单元

**关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` | BEL 名 |
| `getBELType()` | BEL 类型字符串 |
| `getBELClass()` | BEL 类别（`BELClass`） |
| `getSiteTypeEnum()` | 所属 Site 类型（BEL 不持有 Site 引用） |
| `getPins()` | 所有 BELPin（`BELPin[]`） |
| `getPin(String)` / `getPin(int)` | 取 BELPin |
| `isLUT()` / `isFF()` / `isCarry()` | 是否 LUT/FF/进位 |
| `isStaticSource()` / `isGndSource()` / `isVccSource()` | 是否静态源 |
| `canInvert()` / `getInvertingPin()` / `getNonInvertingPin()` | 反相相关 |

> 注：旧文档列出的 `getType()`（应为 `getBELType`）、`getSite()`（BEL 无 Site 引用，仅有 `getSiteTypeEnum`）在 `BEL` 中**均不存在**。

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
| `getSite()` / `getSiteName()` | 对应 Site / site 名 |
| `getSiteTypeEnum()` / `getPrimarySiteTypeEnum()` | site 类型 |
| `getCellMap()` / `getCell(String)` / `getCell(BEL)` | cell 集合 / 取 cell |
| `addCell(Cell)` / `createCell(...)` | 添加 / 创建 cell |
| `getSitePinInsts()` / `getSitePinInst(String)` | 所有 SitePinInst / 按名取 |
| `addPin(SitePinInst)` / `removePin(SitePinInst)` | 增删 site pin |
| `getBELs()` / `getBEL(String)` / `getBELPin(...)` | BEL 信息 |
| `place(Site)` | 将本 SiteInst 布局到指定 Site |
| `unPlace()` | 取消本 SiteInst 布局 |
| `isPlaced()` | 是否已布局 |
| `isAnchor()` / `isSiteLocked()` / `setSiteLocked(boolean)` | 锚点/锁定 |
| `getTile()` / `getName()` | Tile / 名 |
| `getConnectedNets()` | 相连网络 |

> 注：旧文档列出的 `getCells()`（应为 `getCellMap`/`getCell`）、`place(Cell, BEL)`（SiteInst 只有 `place(Site)`，放置 cell 到 BEL 用 `cell.setSiteInst(si)` + `cell.setBEL(bel)`）、`unplaceCell(Cell)`（应为 `unPlace`）、`isUsed()`（应为 `isPlaced`）在 `SiteInst` 中**均不存在**。

#### `SitePinInst` / `PortInst` 类

**功能**: Site 引脚实例（`SitePinInst`，物理层）/ 端口实例（`PortInst`，逻辑层）

**`SitePinInst` 关键方法**:

| 方法 | 功能 |
|---|---|
| `getName()` / `getSitePinName()` | 引脚名 / site pin 名 |
| `getSiteInst()` / `setSiteInst(SiteInst)` | 所属 SiteInst |
| `getNet()` / `setNet(Net)` | 关联 Net |
| `getPort()` / `setPort(Port)` | 关联逻辑 Port |
| `isOutPin()` | 是否输出引脚（方向判断） |
| `getPinType()` / `setPinType(PinType)` | 引脚类型 |
| `isRouted()` / `setRouted(boolean)` | 是否已布线 |
| `getSite()` / `getTile()` | 所属 Site / Tile |
| `getBELPin()` | 关联 BELPin |
| `getConnectedNode()` / `getConnectedWireIndex()` | 连接 Node / Wire |
| `isLUTInputPin()` | 是否 LUT 输入引脚 |

> 注：旧文档列出的 `getCell()`（SitePinInst 无此方法，用 `getSiteInst`）、`isOutward()`（应为 `isOutPin`）在 `SitePinInst` 中**均不存在**。`PortInst` 为逻辑端口实例（关联 EDIFPort/Net），与物理层 `SitePinInst` 不同。

### 9.3 关键枚举与常量

| 枚举/常量 | 用途 |
|---|---|
| `Device.AWS_F1` | AWS F1 器件名常量（String，xcvu9p） |
| `Device.PYNQ_Z1` / `Device.KCU103` / `Device.KCU105` | 其它预置器件名常量 |
| `NetType` | 网络类型枚举（`WIRE`/`GND`/`VCC`/`UNKNOWN`） |
| `SiteTypeEnum` | Site 类型枚举（SLICE/DSP48E2/RAMB36…） |
| `TileTypeEnum` | Tile 类型枚举（INT/CLB/CLE_*/BRAM/…） |
| `BELClass` | BEL 类别枚举（BEL 上 `getBELClass()`） |
| `PIPType` | PIP 类型枚举（PIP 上 `getPIPType()`，含方向） |
| `PinType` | SitePinInst 引脚类型枚举 |
| `FamilyType` / `Series` | 器件架构/系列枚举 |

> 注：旧文档列出的 `Device.AWS_F2`（不存在，Device 仅有 `AWS_F1`/`PYNQ_Z1`/`KCU105` 等常量）、`BELType`（应为 `BELClass`，`getBELType()` 返回 String 而非枚举）、`PIPDirection`（应为 `PIPType`）均**有误**；`NetType` 取值为 `WIRE`/`GND`/`VCC`/`UNKNOWN`，无 `SIGNAL`。

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
    print(f"Net: {net.getName()}, Fanout: {net.getFanOut()}")

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

**功能**: 时序/线长驱动路由器

**位置**: `com.xilinx.rapidwright.rwroute.RWRoute`（旧文档误写为 `com.xilinx.rapidwright.route`）

**CLI 调用**（取自 `java -cp ... com.xilinx.rapidwright.rwroute.RWRoute --help`）:
```
USAGE: <input.dcp|input.phys> <output.dcp>
```
即仅接受两个位置参数（输入 DCP/phys、输出 DCP），**无 `--input`/`--output`/`--routeMode`/`--maxIterations`/`--timingWeight` 等 CLI 标志**。

**模式**（通过编程 API 配置，非 CLI）:
- Timing-driven routing（时序驱动，默认）
- Wirelength-driven routing（线长驱动）
- Partial routing（部分路由，由 `PartialRouter` 提供）

**关键配置**（在 Java 代码中通过 `RWRouteConfig`/`Connection` 等 API 设置，非命令行参数）:

| 配置项 | 默认 | 说明 |
|---|---|---|
| routeMode | TIMING | 路由模式（TIMING/WIRELENGTH） |
| maxIterations | 100 | 最大迭代次数 |
| timingWeight | 1.0 | 时序权重 |
| wirelengthWeight | 0.5 | 线长权重 |
| congestionWeight | 0.5 | 拥塞权重 |
| numThreads | 1 | 线程数 |

> 注：旧文档把上述配置项写成 CLI `--flag`，实测 `RWRoute --help` 仅输出 `USAGE: <input.dcp|input.phys> <output.dcp>`。这些权重/迭代参数是内部配置，需在代码中设置。

**性能**（3,855 nets 设计，来自 RWRoute 论文/教程）:
- 总路由时间: 8.21 秒
- 迭代次数: 7
- 数据路径延迟: 2,331 ps（与 Vivado 一致）
- Slack 差异: ~20 ps

**支持架构**:
- 7-Series
- UltraScale
- UltraScale+
- Versal（部分，2025.2.0 起初步支持 SLR crossing）

**与 Vivado route_design 对比**:
- 速度: RWRoute 快 10-100 倍
- QoR: 略低于 Vivado（~5-10%）
- 适用: 快速迭代、原型验证

### 11.2 ECOTools

**功能**: 工程变更指令（ECO）

**关键方法**（包 `com.xilinx.rapidwright.eco`，均静态）:

| 方法 | 功能 |
|---|---|
| `disconnectNet(Design, EDIFHierPortInst...)` | 断开网络（按层级端口实例） |
| `disconnectNetPath(Design, List<String>)` | 断开指定路径上的网络 |
| `connectNet(Design, Cell, String, Net)` | 将 cell 引脚连到指定 Net |
| `connectNet(Design, List<String>)` | 按层级名连接网络 |
| `removeCell(Design, List<EDIFHierCellInst>, Map)` | 移除 cell |
| `removeCellPath(Design, List<String>, Map)` | 移除指定路径上的 cell |
| `createCell(Design, EDIFCell, List<String>)` | 创建 cell（非 `addCell`） |
| `createAndPlaceInlineCellOnInputPin(...)` | 在输入引脚创建并放置内联 cell |
| `createNet(Design, List<String>)` | 创建网络 |
| `refactorCell(Design, EDIFHierCellInst, EDIFHierCellInst)` | 重构/替换 cell（非 `replaceCell`） |

**典型场景**:
- 时序违例后定点修改
- 修复保持时间违例
- 优化关键路径

> 注：旧文档列出的 `disconnectNet(Net)`/`connectNet(Net,PortInst,PortInst)`/`removeCell(Cell)` 签名不符（实际以 `Design` + `EDIFHierPortInst`/`EDIFHierCellInst` 为参数），`addCell`（应为 `createCell`）、`replaceCell`（应为 `refactorCell`）、`mergeNets`、`splitNet` 在 `ECOTools` 中**均不存在**。

### 11.3 DesignTools

**功能**: 通用设计工具

**关键方法**（包 `com.xilinx.rapidwright.design`，均静态）:

| 方法 | 功能 |
|---|---|
| `createMissingSitePinInsts(Design)` / `(Design, Net)` | 创建缺失的 site pin 实例 |
| `calculateUtilization(Design, PBlock)` | 计算 PBlock 资源利用 |
| `placeCell(Cell, Design)` | 放置单个 cell |
| `fullyUnplaceCell(Cell, Map)` | 完全取消放置 cell |
| `optimizeLUT1Inverters(Design)` | LUT1 反相器优化 |
| `unroutePins(Net, Collection<SitePinInst>)` | 取消指定 pin 布线 |
| `unrouteSourcePin(SitePinInst)` / `unrouteSourcePins(List)` | 取消源 pin 布线 |
| `routeAlternativeOutputSitePin(Net, SitePinInst)` | 布线备选输出 site pin |
| `findRoutingPath(RouteNode, RouteNode)` | 查找布线路径 |
| `stampPlacement(Design, Module, Map)` | 模块布局盖戳 |
| `getRoutedSitePin(Cell, Net, String)` | 取已布线 site pin |

> 注：旧文档列出的 `optimizeFanout(Design,Net,int)`、`optimizeLUTInputCone(Design,LUT)`、`analyzeFabricForPblock`、`convertFabricRegionToPblock`、`reportTimingSummary(Design)`、`parallelRouterInit(Design,int)` 在 `DesignTools` 中**均不存在**。扇出优化与 LUT 输入锥优化是独立工具类（`FanOutOptimization`/`LUTInputConeOpt`，见 RELEASE_NOTES），不在 `DesignTools`；`reportTimingSummary` 是 Vivado TCL 命令，非 RapidWright 方法。

**扇出优化经验参数**（独立工具，按 k 分割负载）:
- `k=2-3`: fanout 200-500
- `k=3-5`: fanout 500-1500
- `k=5-8`: fanout > 1500

### 11.4 VivadoTools

**功能**: 调用 Vivado 工具

**关键方法**（包 `com.xilinx.rapidwright.util`，均静态）:

| 方法 | 功能 |
|---|---|
| `reportRouteStatus(Design)` / `(Path)` | 调用 Vivado `report_route_status`，返回 `ReportRouteStatusResult` |
| `placeDesign(Path, Path, boolean)` | 调用 Vivado 布局 |
| `routeDesignAndGetStatus(Design, Path)` | 布线并返回状态 |
| `placeAndRouteDesignAndGetStatus(Design, Path)` | 布局+布线并返回状态 |
| `writeBitstream(Design, Path)` / `(Path, Path, boolean)` | 生成比特流 |
| `getWorstSetupSlack(Path, Path, boolean)` | 取最差 setup slack |
| `runTcl(Path, String/Path, boolean)` | 执行 Vivado TCL 脚本 |
| `roundTripDCPThruVivado(Design/Path, ...)` | DCP 经 Vivado 往返校验 |
| `searchVivadoLog(List<String>, String)` | 搜索 Vivado 日志 |

**预置命令常量**: `REPORT_ROUTE_STATUS`、`PLACE_DESIGN`、`ROUTE_DESIGN`、`WRITE_CHECKPOINT`、`WRITE_EDIF`

> 注：旧文档列出的 `reportPlaceStatus`（无此方法，布局状态用 `placeAndRouteDesignAndGetStatus`）、`getVivadoDcpLoadError`（不存在）、`checkDcpAgainstVivado`（应为 `roundTripDCPThruVivado`）、`runVivadoWithArguments`（应为 `runTcl`）在 `VivadoTools` 中**均不存在**。

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

### 11.7 并行化与任务调度

RapidWright 无 `JobScheduler` 类（在 `build/libs/rapidwright.jar` 与 `jars/rapidwright-api-lib-2025.2.1.jar` 中均**不存在**，旧文档该小节为虚构内容）。RWRoute 内部使用 Java 标准并发机制（`ExecutorService` 等）；部分工具方法通过并行流（`Stream.parallel()`）加速。

### 11.8 报告解析

**功能**: 解析 Vivado 报告文件

**实际类**: `com.xilinx.rapidwright.examples.ReportTimingExample`（旧文档名为 `ReportTimingData`，该类**不存在**）

**支持**:
- 解析 Vivado 时序报告（`report_timing` / `report_timing_summary`）
- 提取 WNS/TNS/WHS/THS 等关键值
- 示例代码演示如何从日志中解析延迟数据

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
