# P0 修复计划：PBLOCK 区域尺寸 / 密度 / is_soft 三处一致化

## 目标

修复 `dcp_optimizer_run-20260705_130916` 暴露的 PBLOCK 空壳问题：当 `critical_path_cells` 被绑定到 pblock（局部 pblock，2026-07-04 改造）时，区域尺寸、密度指标、is_soft 判定仍按**全设计**计算，导致「63652-LUT 大区域 + 50 个绑定 cell + is_soft=True」的零约束空壳。

修复后：当提供 `critical_path_cells` 时，三处逻辑全部改为基于**绑定 cell 的资源**计算，使 pblock 成为真正起作用的紧约束。

## 根因回顾（三处不一致）

| 位置 | 当前（错误） | 修复后 |
|------|------|------|
| 区域尺寸 [pblock_strategy.py:254](../../skills/pblock_strategy.py#L254) | `required_lut = target_lut_count(全设计) × mult` | `required_lut = bound_luts × mult` |
| 密度 [pblock_strategy.py:421](../../skills/pblock_strategy.py#L421) | `required_lut / est_luts`（全设计/区域） | `bound_luts / est_luts`（绑定 cell/区域） |
| is_soft [pblock_strategy.py:427](../../skills/pblock_strategy.py#L427) | 假密度 98.6% > 0.8 → soft | 真密度 ~2% < 0.8 → hard |

## 修复设计

### 核心思路
新增 `critical_path_cells → 绑定 cell 资源` 的换算。当绑定 cell 可解析（≥50% 匹配）时，区域尺寸基于绑定 cell 资源 × multiplier；密度与 is_soft 基于绑定 cell 真实占用。不可解析时回退到原全设计行为（向后兼容）。

### 绑定 cell 资源换算
复用 [rapidwright_tools.py:2289](../../RapidWrightMCP/rapidwright_tools.py#L2289) 已有的 cell 类型分类模式，补上 MUXF：
- `LUT*` → luts
- `FD*`（FDPE/FDRE/FDSE/FDCE）→ ffs
- `MUXF*` → luts（MUXF 占用 SLICE F7/F8MUX site，按 LUT 等价计入 SLICE 站点需求）
- `DSP*` → dsps
- `RAMB*`/`BRAM*` → brams

对 50 个关键路径 cell（LUT6+FDRE+MUXF7 为主）：bound_luts ≈ 45，multiplier 2.0 → required_lut = 90 → required_slices = 23。滑动窗口找到 1 个 CLB 列（~600 SLICE）即满足 → 区域从 55 列缩到 ~1 列，且 is_soft=False（硬 pblock）。

## 改动清单

### 1. `skills/pblock_strategy.py`（核心）

**新增** `_estimate_bound_cell_resources(design, cell_names) -> dict | None`：
- 遍历 `design.getCell(name)`，按上述分类求和
- 返回 `{luts, ffs, dsps, brams, matched, total}`；`matched==0` 时返回 None
- 日志记录匹配率（与 `_compute_critical_path_center_from_cells` 风格一致）

**改 `generate_pblock_plan`**（在 adaptive multiplier 计算之后，line ~253 之后插入）：
```python
bound_resources = _estimate_bound_cell_resources(design, critical_path_cells) if critical_path_cells else None
if bound_resources and bound_resources["matched"] >= max(1, len(critical_path_cells) // 2):
    sizing_basis = "bound_cells"
    base_lut, base_ff = bound_resources["luts"], bound_resources["ffs"]
    base_dsp, base_bram = bound_resources["dsps"], bound_resources["brams"]
else:
    sizing_basis = "whole_design"  # 回退：无 cell 或匹配率低
    base_lut, base_ff = target_lut_count, target_ff_count
    base_dsp, base_bram = target_dsp_count, target_bram_count

required_lut = int(base_lut * adaptive_multiplier)
required_ff = int(base_ff * adaptive_multiplier)
required_dsp = int(base_dsp * resource_multiplier)
required_bram = int(base_bram * resource_multiplier)
```
> adaptive_multiplier 仍用 `target_lut_count`（全设计）做 small/medium/large 分类——「这是不是小设计」是全设计属性，与绑定 cell 数无关。

**改密度**（line 421）：
```python
if sizing_basis == "bound_cells":
    utilization_density = bound_resources["luts"] / est_luts  # 真实绑定密度
else:
    utilization_density = required_lut / est_luts
```

**is_soft**（line 427）：保持 `is_soft_recommended = utilization_density > 0.8`——现在反映真实密度，绑定 cell 少时自动 False（硬 pblock）。

**新增 result 字段**（return dict）：
```python
"sizing_basis": sizing_basis,          # "bound_cells" | "whole_design"
"bound_resources": bound_resources,     # None when whole_design
"bound_cell_count": bound_resources["matched"] if bound_resources else 0,
```

**修 `next_steps` 文案**（line 451）：`"vivado: place_design -unplace"` → `"vivado: unplace_cells(cells=critical_path_cells)  # 局部 unplace 绑定 cell"`，与实际链 ([constants.py:469](../../optimizer/pure/constants.py#L469)) 一致。

**改成功 message**（line 463-472）：`sizing_basis=="bound_cells"` 时追加 `(bound: N cells, sizing on bound cells)`。

**改 `critical_path_cells` 参数文档**（line 581、666）：
- 旧："Critical path cell names for region centering"
- 新："Critical path cells to BIND to the pblock (constraint targets). When provided, the region is sized around these cells (local pblock), not the whole design."

### 2. `skills/test_pblock_strategy.py`（新增测试）

新增 Section G：`_estimate_bound_cell_resources` 用 Mock 测试（无需 RapidWright Design）：
- `MockCell`（`getType()`）+ `MockDesign`（`getCell(name)`）
- 用例：LUT6+FDRE+MUXF7 → luts=2, ffs=1（MUXF 计入 luts）
- 用例：cell 名未命中 → matched=0 → None
- 用例：空列表 → None
- 用例：DSP/RAMB 分类

> `generate_pblock_plan` 主流程需完整 device/tile mock，过重；主流程留给集成测试，单测只覆盖新增纯函数。

### 3. 文档（CLAUDE.md 要求三件套）

- `architecture.md` §3.4：追加「区域尺寸绑定 cell 化」段落（bound-cell sizing / bound density / is_soft 基于绑定密度）
- `PROJECT_TREE_AND_DATA_FLOW.md` §3.9 PBLOCK 链说明：补一句区域尺寸基于绑定 cell
- `README.md`：仅在提及 PBLOCK 区域尺寸处补一句（若有）

### 4. 描述符自动重生成

`@skill` 装饰器在导入时自动调用 `write_descriptor()`（[descriptor.py:11](../../skills/descriptor.py#L11)）。改 `ParameterSpec` 文档后，运行测试即自动重生成 `skills/descriptors/optimization.pblock_strategy-at-1.0.0.json` 与 `optimization.execute_pblock_strategy-at-1.0.0.json`。无需手动步骤。

## 验证步骤

1. `python skills/test_pblock_strategy.py` → 现有 27 项 + 新增 ~5 项全过
2. `python skills/validate_descriptors.py`（若存在）→ 描述符合法
3. `make test-quick` → 纯函数单测全过
4. 描述符 JSON 已重生成（git diff 可见 ParameterSpec 描述更新）

## 不在本次范围（P1–P3，后续处理）

- P1：LLM 心智模型完整对齐（本次仅修 next_steps 文案 + 参数文档，已覆盖大部分）
- P2：MUXFTreeReorder 链补 P&R、`check_design_status` STATUS bug 复查
- P3：PBLOCK verdict 加「对照基线」剔除 P&R 噪声、小 delta 冷却阈值
- **不加区域最小宽度 floor**：保持修复纯粹（一致性）。1 列硬 pblock 是预期「局部 pblock」行为；若过紧导致 place_design 失败，EVALUATE 阶段会回滚并切策略，框架已有兜底
- **不改 `resource_multiplier` 默认值**：bound-cell 尺寸下，小绑定集的 required_slices 远小于 1 列容量，multiplier 对区域宽度影响可忽略