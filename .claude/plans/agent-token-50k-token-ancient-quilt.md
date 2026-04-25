# Token 50K 问题分析报告

## 背景

用户发现 agent 向模型发送的 prompt_tokens 只有 ~50K，远低于 flash 模型支持的 500K 上下文窗口。需要分析是 token 计算器错误还是上下文管理器设计问题。

## 关键文件与代码路径

| 组件 | 文件 | 行号 |
|------|------|------|
| 字符级 Token 估算 | `dcp_optimizer.py` | 118, 749-755 |
| 压缩触发决策 | `dcp_optimizer.py` | 902-968 |
| tiktoken 精确估算 | `context_manager/estimator.py` | 20-52 |
| YAML 压缩器 | `context_manager/strategies/yaml_structured_compress.py` | 133-380 |
| 压缩阈值配置 | `model_config.yaml` | 全部 |
| MemoryManager | `context_manager/manager.py` | 30-326 |
| API 调用 | `dcp_optimizer.py` | 1835-1842 |

## 根因分析

### 问题一：压缩触发使用字符级估算，与 tiktoken 严重偏离【核心问题】

`dcp_optimizer.py:749` 的 `_estimate_tokens()` 使用 `len(text) // 4`（字符级估算）来判断是否需要压缩。而 `model_config.yaml` 配置的阈值（flash: soft=400K, hard=450K）也是基于这个不精确的估算器推导的。

**数据流：**
1. `_compress_context()`（第 917 行）调用 `_estimate_tokens()` 得到字符级估算值
2. 与 `model_config.yaml` 中的阈值（400K/450K）比较
3. 若超过阈值，调用 YAML 压缩器
4. YAML 压缩器内部使用 `ContextEstimator.estimate_from_messages()`（tiktoken）进行预算管理

**问题：** 对于 FPGA 优化场景产生的数据密集型内容（时序报告中的数字、文件路径、JSON、YAML），字符与 token 的比例可以高达 1:8~1:15，即 `len(text) // 4` 会**严重高估** token 数量。例如：

- 时序报告行 `"WNS(ns) TNS(ns) Failing Endpoints  -0.099 -12.5 42"`：48 字符 → 12 字符-token，但 tiktoken 仅约 8 个 token，高估 50%
- 数字串 `"1234567890"`：10 字符 → 2.5 字符-token，但 tiktoken 仅 1 个 token，高估 150%
- YAML 结构化内容由于大量空格、缩进、标点，字符/token 比例更高

**后果：**
- 字符级估算认为上下文已满（>400K），触发压缩
- 但实际 tiktoken 计数可能只有 100K-200K
- YAML 压缩器按 tiktoken 预算（350K）工作，但由于实际内容不足，压缩后输出远小于预期
- **多次压缩后，YAML 摘要的累积效果无法达到预期的大型上下文**

### 问题二：压缩阈值与内容生成速率不匹配

`model_config.yaml` 的阈值是针对模型的**最大**上下文窗口（flash 500K, pro 1M）设定的：
- flash: soft=400K(80%), hard=450K(90%), budget=350K
- pro: soft=800K(80%), hard=900K(90%), budget=700K

但每次迭代生成的内容量有限（工具结果被 `_filter_tool_result` 截断到 30K 字符，且进一步被智能过滤缩减）。经过 50 次迭代后：
- 系统提示：~5000 字符
- 每次迭代：~3000-5000 字符（经截断和过滤后）
- 50 次迭代总计：150K-250K 字符 ≈ 37.5K-62.5K 字符-token

**这本身就在 50K 附近！** 也就是说，即使没有任何压缩，50 次迭代积累的内容经过字符级估算也只有 ~50K。

### 问题三：两套 Token 计数系统不一致

项目中存在两套独立的 token 计数系统：

| 系统 | 位置 | 精度 | 用途 |
|------|------|------|------|
| 字符级 `_estimate_tokens_char_based` | `dcp_optimizer.py:118` | 不精确 | 压缩触发决策、模型选择 |
| tiktoken `ContextEstimator` | `context_manager/estimator.py:20-52` | 精确 | YAML 压缩器内部预算管理 |

- `_compress_context()` 用字符级做触发判断
- YAML 压缩器用 tiktoken 做预算管理
- 两者结果可能相差 2-5 倍
- 这导致系统行为不可预测：有时压缩过早（字符级高估），有时压缩过晚（字符级低估）

### 问题四：死配置造成维护混淆

`WorkingMemoryConfig`（`working_memory.py:11-16`）和 `MemoryManagerConfig`（`manager.py:22-27`）有以下默认值：

```python
# WorkingMemoryConfig
max_tokens: int = 80_000
hard_limit_tokens: int = 150_000

# MemoryManagerConfig  
soft_threshold: int = 80_000
hard_limit: int = 150_000
```

这些值**从未被实际使用**——压缩逻辑完全依赖 `model_config.yaml` 的阈值。但这些值恰好接近用户观察到的 50K 现象，容易误导排查方向。

### 问题五：压缩累积机制导致历史信息重复

每次压缩后，旧的 YAML 摘要作为 `role:system, protected=true` 保留在 working memory 中。多次压缩后：

```
[SysPrompt, YAML_Summary_1, YAML_Summary_2, ..., YAML_Summary_N]
```

但每次新的压缩只处理**新增**的对话消息（旧 YAML 摘要作为 system message 被保护），导致：
1. 历史上下文以冗余的 YAML 摘要形式存在
2. 每次压缩的"有效增量"很小
3. 总上下文增长缓慢

## 我的判断

**核心问题是上下文管理器的设计问题（问题二 + 问题三），而非 token 计算器本身的错误。** `ContextEstimator`（tiktoken）的实现是正确的，但系统并未在关键决策路径上使用它。

具体来说：
- `ContextEstimator`（`context_manager/estimator.py`）使用 `tiktoken` 精确编码，**实现正确**
- 但 `_compress_context()` 的触发决策完全基于字符级估算，**这是设计缺陷**
- 配置的阈值（400K/450K）未经实际校准，可能不适合当前内容类型

## 推荐修复方案

### 方案 A：将压缩触发切换到 tiktoken 估算（推荐）

将 `_compress_context()` 中的 `_estimate_tokens()`（字符级）替换为 `ContextEstimator.estimate_from_messages()`（tiktoken 精确计数）。

**改动点：**
1. `dcp_optimizer.py:749` - `_estimate_tokens()` 改用 `ContextEstimator`
2. `dcp_optimizer.py:917` - `_compress_context()` 中获取 token 计数
3. 同时调整 `model_config.yaml` 的阈值（需要对照 tiktoken 实际输出校准）

**优点：** 修复根本问题，使压缩触发与预算管理使用同一套计数系统
**成本：** tiktoken 比字符级慢，但在压缩触发频率下可接受

### 方案 B：降低压缩阈值匹配实际内容量（备选）

如果保留字符级估算，则需要大幅降低 `model_config.yaml` 中的阈值：
- 根据实际字符/token 比例校准
- 或改为相对于当前上下文的动态阈值

**优点：** 改动小
**缺点：** 阈值需要持续调优，且无法解决两套计数系统不一致的根本问题

### 方案 C：增大每次迭代的上下文保留量（辅助）

增加 `TOOL_RESULT_TRUNCATE`（当前 30K）和 `RECENT_TURNS_TO_KEEP`（当前 20），并减少 `_filter_tool_result` 的智能截断。

**优点：** 直接增加每轮迭代的内容量
**缺点：** 治标不治本，且会增加 API 调用成本

## 验证方法

1. **运行对比测试：** 在 `_compress_context()` 中同时记录字符级和 tiktoken 计数，观察两者偏差：
   ```python
   char_tokens = self._estimate_tokens()
   tik_tokens = self._context_estimator.estimate_from_messages(current_msgs)
   logger.info(f"Token comparison: char={char_tokens}, tiktoken={tik_tokens}, ratio={char_tokens/tik_tokens:.2f}")
   ```

2. **检查压缩频率：** 在 `compression_details` 中查看压缩触发时的 `tokens_before` 和 `tokens_after`，对比 API 返回的 `prompt_tokens`

3. **验证单次迭代内容量：** 统计每次迭代新增的字符数和 tiktoken 数，确认内容生成速率

4. **观察 API 返回：** 在 API 调用日志（第 1870-1898 行）中检查 `prompt_tokens` 随迭代的变化趋势
