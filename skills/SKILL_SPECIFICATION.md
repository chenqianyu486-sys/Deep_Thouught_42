# Skill 规范文档 — Skill Descriptor v3

本文档定义本项目中 Skill 的规范格式，基于 Skill Descriptor v3 生产级规范。

---

## 1. Skill 定义

Skill 是一个**版本化、可执行的工作单元**，具有强类型契约（输入/输出 Schema）、明确的副作用声明和标准化的错误处理。

每个 Skill 由一个 Python 类 + `@skill` 装饰器定义，注册时自动导出 JSON 描述符到 `skills/descriptors/*.json`。

---

## 2. 命名规则

```
{namespace}.{name}@{version}
```

| 部分 | 规则 | 示例 |
|------|------|------|
| `namespace` | 领域分组，与 `SkillCategory` 一致 | `analysis`, `placement`, `routing` |
| `name` | 短横线命名，动词_名词 | `net_detour`, `optimize_cell`, `smart_region` |
| `version` | 语义化版本 MAJOR.MINOR.PATCH | `1.0.0` |

---

## 3. `@skill` 装饰器完整参数

```python
@skill(
    # ── 标识 ──────────────────────────────────────
    name="skill_name",              # 必填。短名称，用作 registry 查找键
    namespace="analysis",           # 命名空间，默认 = category.value
    version="1.0.0",                # 语义版本
    display_name="Human Readable",  # 展示名，默认 = name 的 title 化

    # ── 描述 ──────────────────────────────────────
    description="...",              # 必填。须声明 READ-ONLY 或 MUTATING
    category=SkillCategory.ANALYSIS,  # 分类枚举

    # ── 契约 ──────────────────────────────────────
    idempotency="safe",             # "safe" | "idempotent" | "non-idempotent"
    side_effects=[],                # 副作用声明，空列表 = 纯读取
    timeout_ms=30000,               # 默认超时毫秒

    # ── 参数 ──────────────────────────────────────
    parameters=[
        ParameterSpec("param_name", type, "Description", default=None),
    ],
    required_context=["design"],    # 所需上下文字段
    output_schema={},               # JSON Schema（可选）

    # ── 错误 ──────────────────────────────────────
    error_codes=[
        "INVALID_PARAMETER",
        "TEMPORARILY_UNAVAILABLE",
        "SKILL_TIMEOUT",
    ],
)
class MySkill(Skill):
    def execute(self, context: SkillContext, param_name: str) -> SkillResult:
        try:
            result = do_something(context.design, param_name)
            return SkillResult(success=True, data=result)
        except Exception as e:
            return SkillResult(
                success=False, data=None, error=str(e),
                error_code=SkillErrorCode.INVALID_PARAMETER,
            )

    def validate_inputs(self, **kwargs) -> tuple[bool, str]:
        if "param_name" not in kwargs:
            return False, "param_name is required"
        return True, ""
```

---

## 4. 契约声明规则

### 4.1 幂等性 (`idempotency`)

| 值 | 语义 | 适用场景 |
|----|------|---------|
| `safe` | GET 语义，重复调用无害 | 分析类、搜索类（只读） |
| `idempotent` | PUT 语义，相同 key 回放返回相同结果 | 配置写入（24h 去重窗口） |
| `non-idempotent` | POST 语义，需精确一次执行 | 物理修改类（如 `optimize_cell`） |

### 4.2 副作用 (`side_effects`)

- `[]` = 纯读取，无状态变更
- `["cell_placement"]` = 修改了 cell 布局
- `["netlist_change"]` = 修改了网表
- 变异类 Skill **必须**声明非空 `side_effects`

### 4.3 description 规范

description 中必须包含以下信息之一：
- `READ-ONLY` — 纯读取
- `MUTATING. Side effects: XXX.` — 有副作用

---

## 5. 执行结果

### 5.1 SkillResult

```python
@dataclass
class SkillResult:
    success: bool            # 是否成功
    data: Any = None         # 成功时的数据（须 JSON 可序列化）
    error: str | None = None # 错误消息
    error_code: str = ""     # 规范错误码
```

### 5.2 SkillError（错误信封）

```python
@dataclass
class SkillError:
    code: str           # SkillErrorCode
    message: str        # 人类可读描述
    request_id: str     # 追踪 ID
    recoverable: bool   # Agent 能否重试
    retry_after_ms: int # 建议重试间隔
    user_message: str   # 给用户的提示
```

标准错误码：

| 码 | Recoverable | HTTP | Agent 行为 |
|----|-------------|------|-----------|
| `INVALID_PARAMETER` | ❌ | 400 | 提示用户修正，不重试 |
| `RESOURCE_NOT_FOUND` | ❌ | 404 | 告知用户，建议放宽条件 |
| `PERMISSION_DENIED` | ❌ | 403 | 给出可操作指引，不重试 |
| `QUOTA_EXCEEDED` | ✅ | 429 | 指数退避，通知用户 |
| `TEMPORARILY_UNAVAILABLE` | ✅ | 503 | 退避后重试，最多 3 次 |
| `SKILL_TIMEOUT` | ✅ | 504 | 幂等 Skill 可带 key 重试 |
| `CONCURRENT_MODIFICATION` | ✅ | 423 | 等待锁释放后重试 |

---

## 6. JSON 描述符

每次 `@skill` 装饰器注册后自动导出 JSON 文件到 `skills/descriptors/`。

```json
{
  "$schema": "https://spec.example.com/skill-descriptor-v3.json",
  "specVersion": "3.0",
  "id": "analysis.net_detour@1.0.0",
  "displayName": "Analyze Net Detour",
  "description": "Analyze detour ratios for cells on critical paths. READ-ONLY.",
  "idempotency": "safe",
  "sideEffects": [],
  "timeout": { "defaultMs": 30000, "maxMs": 60000 },
  "authentication": { "type": "none" },
  "parameters": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "pin_paths": {
        "type": "array",
        "description": "Pin path list from Vivado extract_critical_path_pins"
      }
    },
    "required": ["pin_paths"]
  },
  "returns": {
    "type": "object",
    "additionalProperties": false
  },
  "errors": [
    { "code": "INVALID_PARAMETER", "recoverable": false },
    { "code": "SKILL_TIMEOUT", "recoverable": true }
  ]
}
```

---

## 7. 调用链

```
Agent / MCP Tool
  → rapidwright_tools.py wrapper
    → SkillRegistry.get("skill_name")
    → SkillContext(design, call_id, idempotency_key)
    → skill.execute_with_telemetry(context, **kwargs)
        ├── 幂等性检查（idempotent/non-idempotent）
        ├── Heartbeat daemon（30s 间隔）
        ├── self.execute(context, **kwargs)
        ├── SkillTraceAttributes.emit()
        ├── SkillTelemetry.record_execution()
        └── return SkillResult
```

---

## 8. 验证

```bash
# 运行全部测试（28 项）
python skills/test_skill_framework.py

# 验证所有描述符
python skills/validate_descriptors.py

# 重新导出描述符
python -c "from skills import export_all; export_all()"
```

CI 验证检查项：
- ✅ 所有参数有 non-empty description
- ✅ JSON Schema `additionalProperties: false`
- ✅ 枚举数组至少 2 个成员
- ✅ 变异技能声明了 `sideEffects`
- ✅ 包含 `INVALID_PARAMETER`、`TEMPORARILY_UNAVAILABLE`、`SKILL_TIMEOUT`

---

## 9. 快速开始：创建新 Skill

1. 在 `skills/` 下新建 `your_skill.py`
2. 导入 `Skill`, `SkillResult`, `SkillCategory`, `ParameterSpec`, `skill`
3. 编写纯函数（业务逻辑）
4. 编写 Skill 类 + `@skill` 装饰器
5. 在 `skills/__init__.py` 中添加 `from skills import your_skill`
6. 运行测试和验证

```python
"""示例：最小 Skill"""

from skills.base import Skill, SkillResult, SkillCategory, ParameterSpec
from skills.context import SkillContext
from skills.skill_decorator import skill


def analyze_foo(design, param: str) -> dict:
    """纯函数：核心业务逻辑"""
    return {"result": f"analyzed {param}"}


@skill(
    name="analyze_foo",
    namespace="analysis",
    version="1.0.0",
    display_name="Analyze Foo",
    description="Analyze foo from the design. READ-ONLY.",
    category=SkillCategory.ANALYSIS,
    idempotency="safe",
    side_effects=[],
    timeout_ms=30000,
    parameters=[
        ParameterSpec("param", str, "Parameter description"),
    ],
    required_context=["design"],
)
class AnalyzeFooSkill(Skill):
    def execute(self, context: SkillContext, param: str) -> SkillResult:
        try:
            data = analyze_foo(context.design, param)
            return SkillResult(success=True, data=data)
        except Exception as e:
            return SkillResult(success=False, error=str(e))
```
