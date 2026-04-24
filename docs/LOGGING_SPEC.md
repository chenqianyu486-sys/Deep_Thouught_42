# 项目日志记录技术规范文档

## FPL26 FPGA后端优化智能体

---

## 1. 概述

### 1.1 目的

本规范旨在统一FPL26 FPGA后端优化智能体项目的日志行为，确保系统在开发、测试及生产环境中的**可观测性、可调试性及安全性**。

**核心驱动因素：**

| 因素 | 说明 |
|------|------|
| **故障排查** | FPGA编译（综合、布局布线）耗时长（数分钟至数小时），日志是诊断失败的唯一依据 |
| **性能分析** | 优化迭代需要追踪WNS/Fmax时序变化趋势，结构化日志便于时序数据可视化 |
| **审计合规** | MCP通信记录需满足内部安全审计要求 |
| **多智能体协同** | Planner/Worker双模型架构需要trace_id串联同一任务的跨模型日志 |

### 1.2 适用范围

本规范适用于以下所有Python模块和组件：

- `dcp_optimizer.py` — 主优化智能体入口
- `context_manager/` — 上下文管理模块（含lightyaml.py、manager.py、events.py等）
- `RapidWrightMCP/server.py` — RapidWright MCP服务器
- `VivadoMCP/vivado_mcp_server.py` — Vivado MCP服务器
- `validate_dcps.py` — DCP等价性验证器
- 所有自定义压缩策略（`strategies/`）

### 1.3 基本原则

```
原则1: 结构化优先 — 日志输出必须为JSON格式或标准化字符串，便于机器解析
原则2: 上下文丰富 — 每条日志必须包含trace_id、module、function等追溯字段
原则3: 安全脱敏 — 严禁记录API Keys、IP地址、完整Payload等敏感信息
原则4: 性能友好 — 避免在热路径高频输出DEBUG日志；循环内日志需有采样机制
```

---

## 2. 技术标准

### 2.1 日志库选型

**推荐方案：Python标准库 `logging` + 自定义JSON Formatter**

**理由：**

| 考量 | 说明 |
|------|------|
| 依赖控制 | 避免引入第三方依赖，保持zero-dependency风格（lightyaml.py已示范） |
| 生态兼容 | `logging` 模块与MCP SDK、RapidWright等外部库天然兼容 |
| 性能成熟 | `logging` 经过充分优化，支持多handler、轮转、层级控制 |
| 结构化扩展 | 通过自定义`JSONFormatter`可输出JSON，同时保留标准字符串Formatter备选 |

**备选方案：`structlog`**

适用于未来需要更复杂结构化日志的场景（如自动添加上下文、模板渲染）。当前阶段不强制要求。

### 2.2 日志级别定义

```
┌─────────────┬──────────────────────────────────────────────────────────────┐
│   LEVEL     │  定义与使用场景                                                │
├─────────────┼──────────────────────────────────────────────────────────────┤
│  DEBUG      │  详细调试信息                                                  │
│             │  - YAML解析中间状态（token值、缩进层级）                         │
│             │  - MCP原始请求/响应元数据（不含Body）                           │
│             │  - 压缩策略内部决策逻辑                                         │
│             │  示例: "Parsing scalar at line 42: type=hex, value=0x1F"     │
├─────────────┼──────────────────────────────────────────────────────────────┤
│  INFO       │  关键业务流程节点                                              │
│             │  - 优化迭代开始/结束                                            │
│             │  - Fmax/WNS计算完成                                            │
│             │  - MCP工具调用开始/完成                                         │
│             │  - 压缩完成及结果统计                                           │
│             │  示例: "[ITERATION] Iteration 5 started, current WNS=-0.312"  │
├─────────────┼──────────────────────────────────────────────────────────────┤
│  WARNING    │  非预期但不影响运行                                             │
│             │  - 配置文件缺失可选字段，使用默认值                              │
│             │  - 单次FPGA编译超时，已自动重试                                 │
│             │  - 压缩策略全部失败，切换备选方案                                │
│             │  示例: "Optional field 'clock_period' missing, using 10.0ns" │
├─────────────┼──────────────────────────────────────────────────────────────┤
│  ERROR      │  当前任务失败但服务可用                                         │
│             │  - 单次FPGA编译超时/崩溃                                        │
│             │  - MCP工具调用失败（网络/参数错误）                              │
│             │  - YAML解析失败（但有回退方案）                                  │
│             │  示例: "Vivado synthesis timeout after 300s, job_id=abc123"  │
├─────────────┼──────────────────────────────────────────────────────────────┤
│  CRITICAL   │  系统级致命错误                                                 │
│             │  - MCP连接永久丢失                                              │
│             │  - 内存溢出（FPGA比特流文件过大）                                │
│             │  - 磁盘空间耗尽                                                 │
│             │  示例: "MCP connection to Vivado lost after 3 retries"        │
└─────────────┴──────────────────────────────────────────────────────────────┘
```

### 2.3 格式化标准

#### 2.3.1 必须包含的标准字段

| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| `timestamp` | ISO8601字符串 | 日志产生时间 | `"2026-04-23T14:30:00.123Z"` |
| `level` | string | 日志级别 | `"INFO"`, `"DEBUG"` |
| `logger_name` | string | Logger标识 | `"context_manager.manager"` |
| `message` | string | 日志消息 | `"Compression completed"` |
| `trace_id` | string | 链路追踪ID | `"job-abc123-iter5"` |
| `module` | string | 模块名 | `"manager"` |
| `function` | string | 函数名 | `"compress_context"` |
| `line` | int | 代码行号 | `142` |

#### 2.3.2 推荐包含的可选字段

| 字段名 | 类型 | 说明 | 示例 |
|--------|------|------|------|
| `job_id` | string | 当前任务ID | `"fpga-opt-001"` |
| `iteration` | int | 当前迭代号 | `5` |
| `wns` | float | 当前WNS值 | `-0.312` |
| `fmax` | float | 当前Fmax值 | `245.6` |
| `duration_ms` | int | 操作耗时（毫秒） | `15230` |
| `error` | object | 错误详情（仅ERROR级别） | `{"type": "Timeout", "detail": "..."}` |

#### 2.3.3 JSON格式示例

```json
{
  "timestamp": "2026-04-23T14:30:00.123Z",
  "level": "INFO",
  "logger_name": "context_manager.manager",
  "message": "[COMPRESSION] Compression completed",
  "trace_id": "job-abc123-iter5",
  "module": "manager",
  "function": "compress_context",
  "line": 142,
  "job_id": "fpga-opt-001",
  "iteration": 5,
  "wns": -0.312,
  "duration_ms": 15230
}
```

### 2.4 上下文注入

#### 2.4.1 trace_id生成与传播

```python
# 在任务入口点生成trace_id
import uuid
from contextvars import ContextVar

trace_id_var: ContextVar[str] = ContextVar('trace_id', default='')

def start_job(job_id: str) -> str:
    """Start a new optimization job with trace_id."""
    trace_id = f"job-{job_id}-{uuid.uuid4().hex[:8]}"
    trace_id_var.set(trace_id)
    return trace_id
```

#### 2.4.2 自动上下文注入Filter

```python
import logging
from contextvars import ContextVar

trace_id_var: ContextVar[str] = ContextVar('trace_id', default='')

class ContextFilter(logging.Filter):
    """Automatically inject trace_id into log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get() or 'no-trace'
        record.job_id = getattr(record, 'job_id', None)
        record.iteration = getattr(record, 'iteration', None)
        return True

# 使用方式
handler = logging.StreamHandler()
handler.addFilter(ContextFilter())
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.info("Message with auto-injected trace_id")
```

---

## 3. 场景化规范

### 3.1 FPGA长耗时任务日志

#### 3.1.1 心跳日志要求

**背景：** FPGA综合/布局布线可能耗时5-30分钟，需定期输出心跳防止被误判为僵死进程。

```python
import time
import logging
from threading import Thread
from typing import Optional

logger = logging.getLogger(__name__)

class HeartbeatLogger:
    """
    定期输出心跳日志，用于长时间运行的FPGA任务。
    确保进程不被监控误判为僵死。
    """

    def __init__(
        self,
        interval_seconds: float = 60.0,
        message: str = "FPGA task still running",
        done_event=None  # threading.Event, 任务完成时设置
    ):
        self.interval = interval_seconds
        self.message = message
        self.done_event = done_event
        self._stop = False
        self._thread: Optional[Thread] = None

    def _heartbeat_loop(self):
        iteration = 0
        while not self._stop:
            if self.done_event and self.done_event.is_set():
                break
            elapsed = iteration * self.interval
            logger.info(
                "[HEARTBEAT] %s (elapsed: %ds)",
                self.message,
                int(elapsed),
                extra={
                    "heartbeat_elapsed_seconds": int(elapsed),
                    "heartbeat_count": iteration,
                }
            )
            iteration += 1
            time.sleep(self.interval)

    def start(self):
        self._stop = False
        self._thread = Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=5.0)

# 使用示例
def run_vivado_synthesis(dcp_path: str, timeout: int = 600) -> dict:
    """运行Vivado综合，带心跳日志。"""
    done_event = __import__('threading').Event()
    heartbeat = HeartbeatLogger(
        interval_seconds=30.0,
        message=f"Vivado synthesis in progress for {dcp_path}",
        done_event=done_event
    )
    heartbeat.start()

    try:
        result = _execute_vivado_synthesis(dcp_path, timeout=timeout)
        return result
    finally:
        done_event.set()
        heartbeat.stop()
```

#### 3.1.2 进度百分比日志

```python
import logging
import time

logger = logging.getLogger(__name__)

class ProgressTracker:
    """
    追踪并记录长时间运行任务的进度。
    适用于可分阶段的FPGA编译流程。
    """

    def __init__(self, total_steps: int, task_name: str):
        self.total_steps = total_steps
        self.task_name = task_name
        self.current_step = 0
        self.start_time = time.time()

    def update(self, step: int = None, message: str = ""):
        if step is not None:
            self.current_step = step
        else:
            self.current_step += 1

        elapsed = time.time() - self.start_time
        percent = (self.current_step / self.total_steps) * 100

        logger.info(
            "[PROGRESS] %s: %d/%d (%.1f%%) - %s (elapsed: %ds)",
            self.task_name,
            self.current_step,
            self.total_steps,
            percent,
            message,
            int(elapsed),
            extra={
                "progress_percent": round(percent, 1),
                "progress_current": self.current_step,
                "progress_total": self.total_steps,
                "progress_elapsed_seconds": int(elapsed),
            }
        )

    def complete(self, message: str = "Done"):
        elapsed = time.time() - self.start_time
        logger.info(
            "[PROGRESS] %s: Complete - %s (total: %ds)",
            self.task_name,
            message,
            int(elapsed),
            extra={
                "progress_percent": 100.0,
                "progress_elapsed_seconds": int(elapsed),
            }
        )
```

### 3.2 MCP通信日志

#### 3.2.1 请求/响应元数据记录

```python
import logging
import time
import hashlib
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_PAYLOAD_LOG_LENGTH = 1024  # 1KB
MAX_PAYLOAD_DISPLAY_LENGTH = 128

def _sanitize_payload(payload: Any, max_length: int = MAX_PAYLOAD_LOG_LENGTH) -> str:
    """
    对Payload进行安全处理：
    - 长度超过max_length时截断并显示哈希
    - 递归处理字典和列表
    """
    if payload is None:
        return None

    if isinstance(payload, str):
        if len(payload) > max_length:
            hash_suffix = hashlib.sha256(payload.encode()).hexdigest()[:8]
            return f"{payload[:MAX_PAYLOAD_DISPLAY_LENGTH]}...[SHA256:{hash_suffix}]"
        return payload

    if isinstance(payload, dict):
        return {k: _sanitize_payload(v, max_length) for k, v in payload.items()}

    if isinstance(payload, (list, tuple)):
        return [_sanitize_payload(item, max_length) for item in payload]

    return repr(payload)

def log_mcp_request(
    tool_name: str,
    arguments: dict,
    trace_id: str,
    job_id: Optional[str] = None
):
    """记录MCP工具调用请求。"""
    sanitized_args = _sanitize_payload(arguments)
    logger.info(
        "[MCP_REQUEST] Tool '%s' called",
        tool_name,
        extra={
            "mcp_tool_name": tool_name,
            "mcp_request_args": sanitized_args,
            "mcp_request_time": time.time(),
            "trace_id": trace_id,
            "job_id": job_id,
        }
    )

def log_mcp_response(
    tool_name: str,
    duration_ms: int,
    status: str,
    result: Any = None,
    error: Optional[dict] = None,
    trace_id: str = "",
    job_id: Optional[str] = None
):
    """记录MCP工具调用响应。"""
    sanitized_result = _sanitize_payload(result, max_length=2048) if result else None

    log_data = {
        "mcp_tool_name": tool_name,
        "mcp_response_duration_ms": duration_ms,
        "mcp_response_status": status,
        "mcp_response_result": sanitized_result,
        "trace_id": trace_id,
        "job_id": job_id,
    }

    if error:
        log_data["mcp_error"] = error
        logger.error(
            "[MCP_RESPONSE] Tool '%s' failed: %s (%dms)",
            tool_name,
            error.get("message", "Unknown"),
            duration_ms,
            extra=log_data
        )
    else:
        logger.info(
            "[MCP_RESPONSE] Tool '%s' succeeded (%dms)",
            tool_name,
            duration_ms,
            extra=log_data
        )
```

#### 3.2.2 敏感信息检测

```python
import re
import logging

logger = logging.getLogger(__name__)

SENSITIVE_FIELD_PATTERNS = [
    re.compile(r'.*key.*', re.IGNORECASE),
    re.compile(r'.*secret.*', re.IGNORECASE),
    re.compile(r'.*password.*', re.IGNORECASE),
    re.compile(r'.*token.*', re.IGNORECASE),
    re.compile(r'.*credential.*', re.IGNORECASE),
    re.compile(r'.*auth.*', re.IGNORECASE),
]

IP_PATTERN = re.compile(
    r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b|\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b'
)

def check_sensitive_in_args(arguments: dict) -> list:
    """检查参数中是否包含潜在敏感信息。"""
    sensitive_paths = []

    def recursive_check(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                current_path = f"{path}.{k}" if path else k
                for pattern in SENSITIVE_FIELD_PATTERNS:
                    if pattern.match(k):
                        sensitive_paths.append(current_path)
                recursive_check(v, current_path)
        elif isinstance(obj, (list, tuple)):
            for i, item in enumerate(obj):
                recursive_check(item, f"{path}[{i}]")

    recursive_check(arguments)
    return sensitive_paths

def mask_sensitive_args(arguments: dict) -> dict:
    """对参数中的敏感字段进行脱敏处理。"""
    sensitive = check_sensitive_in_args(arguments)
    masked = arguments.copy()

    for path in sensitive:
        parts = path.split(".")
        current = masked
        for part in parts[:-1]:
            if part not in current:
                break
            current = current[part]

        key = parts[-1].split("[")[0]
        if key in current:
            current[key] = "***REDACTED***"

    return masked
```

### 3.3 LightYAML解析日志

#### 3.3.1 解析上下文记录要求

**背景：** YAML解析失败时，需记录出错行的上下文片段而非仅"Parse Error"，便于快速定位问题。

```python
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)
LIGHTYAML_LOGGER = logging.getLogger("context_manager.lightyaml")

def log_yaml_parse_error(
    error_type: str,
    error_message: str,
    yaml_content: str,
    error_line: int,
    error_column: int,
    trace_id: Optional[str] = None
):
    """记录YAML解析错误，包含丰富的上下文信息。"""
    lines = yaml_content.split('\n')

    start_line = max(0, error_line - 4)
    end_line = min(len(lines), error_line + 2)

    context_lines: List[dict] = []
    for i in range(start_line, end_line):
        line_num = i + 1
        marker = ">>> " if line_num == error_line else "    "
        context_lines.append({
            "line_number": line_num,
            "content": lines[i] if i < len(lines) else "",
            "marker": marker,
        })

    context_str = "\n".join(
        f"  {marker}L{ln:04d}: {content}"
        for ln, marker, content in [
            (l["line_number"], l["marker"], l["content"]) for l in context_lines
        ]
    )

    logger.error(
        "[YAML_PARSE_ERROR] %s at line %d, column %d: %s\nContext:\n%s",
        error_type,
        error_line,
        error_column,
        error_message,
        context_str,
        extra={
            "yaml_error_type": error_type,
            "yaml_error_line": error_line,
            "yaml_error_column": error_column,
            "yaml_error_context": context_lines,
            "trace_id": trace_id,
        }
    )

def log_yaml_parse_success(
    yaml_content: str,
    parsed_count: int,
    duration_ms: float,
    trace_id: Optional[str] = None
):
    """记录YAML解析成功（DEBUG级别）。"""
    LIGHTYAML_LOGGER.debug(
        "[YAML_PARSE] Successfully parsed %d elements in %.2fms",
        parsed_count,
        duration_ms,
        extra={
            "yaml_parsed_count": parsed_count,
            "yaml_parse_duration_ms": round(duration_ms, 2),
            "yaml_content_length": len(yaml_content),
            "trace_id": trace_id,
        }
    )

def log_yaml_unsupported_feature(
    feature: str,
    line_number: int,
    line_content: str,
    trace_id: Optional[str] = None
):
    """记录不支持的YAML特性。"""
    LIGHTYAML_LOGGER.warning(
        "[YAML_UNSUPPORTED] Unsupported feature '%s' at line %d: %s",
        feature,
        line_number,
        line_content.strip()[:80],
        extra={
            "yaml_unsupported_feature": feature,
            "yaml_unsupported_line": line_number,
            "yaml_unsupported_content": line_content.strip()[:200],
            "trace_id": trace_id,
        }
    )
```

#### 3.3.2 序列化观测要求

**背景：** `LightYAML.dump()` 序列化操作需具备与`load()`同等级别的可观测性，包括执行耗时、输出大小、追踪ID传播和错误诊断。

**关键日志格式：**

| 前缀 | 级别 | 触发条件 | 关键字段 |
|------|------|---------|---------|
| `[YAML_DUMP]` | DEBUG | dump()成功完成 | `yaml_dump_duration_ms`, `yaml_input_type`, `yaml_output_length`, `yaml_node_count` |
| `[YAML_DUMP_ERROR]` | ERROR | dump()遇到不支持的数据类型 | `yaml_dump_error_type`, `yaml_input_type`, `trace_id` (含完整堆栈) |
| `[COMPRESS_DUMP]` | DEBUG | 压缩流程中的dump()完成 | `compress_dump_duration_ms`, `compress_dump_output_length` |
| `[COMPRESS_DUMP_ERROR]` | ERROR | 压缩流程中dump()失败 | 含yaml_data_keys和完整堆栈 |
| `[COMPRESS_ROUNDTRIP]` | DEBUG | 可选回环验证通过 | `compress_roundtrip_valid: true` |
| `[COMPRESS_ROUNDTRIP_FAIL]` | ERROR | 可选回环验证失败 | `compress_roundtrip_valid: false`（不阻断流程） |

### 3.4 敏感信息脱敏

#### 3.4.1 禁止记录的内容清单

```
┌─────────────────────────────────────────────────────────────────┐
│                    禁止记录的敏感信息                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. 认证凭据                                                      │
│     - API Keys / Secret Keys                                     │
│     - OAuth Tokens / Bearer Tokens                              │
│     - 用户名密码组合                                               │
│     - SSH密钥、私钥文件内容                                        │
│                                                                  │
│  2. 网络标识                                                      │
│     - IP地址（内网/生产环境）                                      │
│     - 主机名、内部域名                                             │
│     - 端口号（敏感服务如22/3306/6379）                             │
│                                                                  │
│  3. 业务敏感数据                                                  │
│     - FPGA比特流文件路径（生产环境）                                │
│     - 客户项目名称/ID                                              │
│     - 内部网络拓扑细节                                             │
│                                                                  │
│  4. MCP通信内容                                                   │
│     - RapidWright/Vivado工具调用的完整参数                         │
│     - 工具返回的完整JSON（需截断或哈希）                            │
│                                                                  │
│  5. 系统信息                                                      │
│     - 环境变量中的凭据（PATH/JAVA_HOME除外）                        │
│     - 本地文件路径（可能暴露用户名）                                │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

#### 3.4.2 脱敏处理器示例

```python
import logging
import re
import hashlib
from typing import Any

logger = logging.getLogger(__name__)

class SensitiveDataScrubber:
    """敏感数据脱敏处理器。在日志记录前自动扫描并脱敏敏感信息。"""

    API_KEY_PATTERNS = [
        re.compile(r'[a-zA-Z0-9]{32,}),
        re.compile(r'xox[baprs]-[0-9a-zA-Z]{10,}'),
        re.compile(r'sk-[a-zA-Z0-9]{48}'),
        re.compile(r'ghp_[a-zA-Z0-9]{36}'),
    ]

    IP_PATTERN = re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')

    @classmethod
    def scrub_dict(cls, data: dict, depth: int = 0) -> dict:
        if depth > 10:
            return {"_depth_exceeded": True}

        result = {}
        for key, value in data.items():
            key_lower = key.lower()
            if any(term in key_lower for term in ['key', 'secret', 'password', 'token', 'auth']):
                result[key] = "***REDACTED***"
            elif isinstance(value, dict):
                result[key] = cls.scrub_dict(value, depth + 1)
            elif isinstance(value, (list, tuple)):
                result[key] = cls.scrub_list(value, depth + 1)
            elif isinstance(value, str):
                result[key] = cls.scrub_string(value)
            else:
                result[key] = value
        return result

    @classmethod
    def scrub_list(cls, data: list, depth: int = 0) -> list:
        if depth > 10:
            return [{"_depth_exceeded": True}]
        return [
            cls.scrub_dict(item, depth + 1) if isinstance(item, dict)
            else cls.scrub_list(item, depth + 1) if isinstance(item, (list, tuple))
            else cls.scrub_string(item) if isinstance(item, str)
            else item
            for item in data
        ]

    @classmethod
    def scrub_string(cls, value: str) -> str:
        result = value
        for pattern in cls.API_KEY_PATTERNS:
            result = pattern.sub('***REDACTED_API_KEY***', result)

        def mask_ip(match):
            ip = match.group()
            parts = ip.split('.')
            if len(parts) == 4:
                return f"{parts[0]}.***.***.{parts[3]}"
            return "***.***.***.***"

        result = cls.IP_PATTERN.sub(mask_ip, result)
        return result

_scrubber = SensitiveDataScrubber()

def get_scrubber() -> SensitiveDataScrubber:
    return _scrubber
```

---

## 4. 最佳实践与反模式

### 4.1 推荐做法 (Do's)

#### 4.1.1 使用占位符而非字符串拼接

```python
# 正确：使用占位符
logger.info("Job %s started, iteration %d", job_id, iteration)
logger.debug("Processing signal %s with value 0x%x", signal_name, value)
logger.info("[TOOL_RESULT] New best WNS: %.4f (improved by %.4f)", new_wns, improvement)

# 错误：字符串拼接
logger.info(f"Job {job_id} started, iteration {iteration}")
logger.info("Job " + job_id + " started")
```

#### 4.1.2 异常捕获时记录堆栈

```python
# 正确：使用 exc_info=True 记录完整堆栈
try:
    result = mcp_session.call_tool("vivado_synthesize", args)
except TimeoutError as e:
    logger.error(
        "Vivado synthesis timed out after %ds for job %s",
        timeout_seconds,
        job_id,
        exc_info=True,
        extra={"timeout_seconds": timeout_seconds}
    )

# 正确：使用 logger.exception
try:
    parse_yaml(content)
except YAMLParseError as e:
    logger.exception(
        "[YAML_PARSE] Failed to parse YAML at line %d: %s",
        e.line_number,
        str(e)
    )

# 错误：吞没异常
try:
    do_something()
except Exception:
    pass  # 绝对禁止
```

#### 4.1.3 统一的Logger命名约定

```python
# 每个模块使用 __name__ 创建logger
# dcp_optimizer.py
logger = logging.getLogger(__name__)  # "dcp_optimizer"

# context_manager/manager.py
logger = logging.getLogger(__name__)  # "context_manager.manager"

# 使用自定义前缀区分模块内的逻辑分组
logger.info("[COMPRESSION] Starting compression: type=%s", compression_type)
logger.info("[ITERATION] Iteration %d started", iteration)
logger.info("[TOOL_RESULT] WNS updated: %.4f -> %.4f", old_wns, new_wns)
```

#### 4.1.4 日志级别动态配置

```python
import os
import logging

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

# context_manager模块默认DEBUG
logging.getLogger("context_manager").setLevel(logging.DEBUG)

# MCP模块默认INFO
logging.getLogger("RapidWrightMCP").setLevel(logging.INFO)
logging.getLogger("VivadoMCP").setLevel(logging.INFO)
```

### 4.2 禁止做法 (Don'ts)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              禁止做法清单                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  禁止在生产环境使用 print()                                                     │
│     - print()输出到stdout，无timestamp/level等元数据                            │
│     - 无法被日志系统收集、过滤、轮转                                             │
│     - 例外：CLI工具的用户直接输出（与日志系统分离）                               │
│                                                                               │
│  禁止在循环体内高频输出 DEBUG 级别日志                                          │
│     - FPGA编译可能产生数万次循环迭代                                             │
│     - 应使用采样机制：每N次迭代输出一次，或达到关键节点时输出                      │
│                                                                               │
│  禁止吞没异常而不记录日志                                                       │
│     - 异常被捕获后必须记录：至少包含 error类型、message、traceback                │
│     - 使用 logger.exception() 或 exc_info=True                                 │
│                                                                               │
│  禁止在日志中记录完整Payload（API响应、比特流等）                                │
│     - 完整记录会快速填满磁盘，且有敏感信息泄露风险                                 │
│     - 使用 _sanitize_payload() 截断或哈希                                       │
│                                                                               │
│  禁止使用字符串拼接构造日志消息                                                 │
│     - 正确：logger.info("Job %s failed", job_id)                                │
│     - 错误：logger.info(f"Job {job_id} failed")                                │
│                                                                               │
│  禁止记录内部路径/主机名等可能暴露部署结构的信息                                  │
│     - 如 /home/username/projects/fpga-designs                                    │
│     - 替换为相对路径或占位符                                                     │
│                                                                               │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. 配置与管理

### 5.1 日志轮转策略

#### 5.1.1 基于文件大小的轮转（推荐用于生产环境）

```python
import logging
from logging.handlers import RotatingFileHandler
import os

LOG_DIR = os.environ.get("LOG_DIR", "./logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 主日志文件：记录所有INFO及以上级别
main_handler = RotatingFileHandler(
    filename=os.path.join(LOG_DIR, "fpl26-optimization.log"),
    maxBytes=100 * 1024 * 1024,  # 100MB per file
    backupCount=10,
    encoding="utf-8",
)
main_handler.setLevel(logging.INFO)
main_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
))

# 调试日志文件：记录DEBUG及以上级别
debug_handler = RotatingFileHandler(
    filename=os.path.join(LOG_DIR, "fpl26-debug.log"),
    maxBytes=50 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
debug_handler.setLevel(logging.DEBUG)
debug_handler.addFilter(lambda record: record.levelno == logging.DEBUG)

# 错误日志文件：专门记录ERROR和CRITICAL
error_handler = RotatingFileHandler(
    filename=os.path.join(LOG_DIR, "fpl26-error.log"),
    maxBytes=50 * 1024 * 1024,
    backupCount=10,
    encoding="utf-8",
)
error_handler.setLevel(logging.ERROR)

# MCP通信日志（单独文件，便于安全审计）
mcp_handler = RotatingFileHandler(
    filename=os.path.join(LOG_DIR, "fpl26-mcp.log"),
    maxBytes=100 * 1024 * 1024,
    backupCount=15,
    encoding="utf-8",
)
mcp_handler.setLevel(logging.INFO)
mcp_handler.addFilter(lambda record: "MCP" in record.name or "[MCP_" in record.getMessage())

# 配置根logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(main_handler)
root_logger.addHandler(debug_handler)
root_logger.addHandler(error_handler)
root_logger.addHandler(mcp_handler)
```

#### 5.1.2 基于时间的轮转（推荐用于审计场景）

```python
from logging.handlers import TimedRotatingFileHandler
import logging

# 每天凌晨轮转，保留30天
daily_handler = TimedRotatingFileHandler(
    filename=os.path.join(LOG_DIR, "fpl26-audit.log"),
    when="midnight",
    interval=1,
    backupCount=30,
    encoding="utf-8",
    utc=True,
)
daily_handler.setLevel(logging.INFO)
daily_handler.setFormatter(logging.Formatter(
    '%(asctime)sZ - %(levelname)s - %(name)s - %(message)s'
))

audit_logger = logging.getLogger("audit")
audit_logger.addHandler(daily_handler)
audit_logger.setLevel(logging.INFO)
```

### 5.2 动态日志级别调整

#### 5.2.1 通过环境变量动态配置

```python
import logging
import os

def configure_logging():
    """根据环境变量配置日志系统。"""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    root_level = getattr(logging, log_level, logging.INFO)

    module_levels = {}
    for key, value in os.environ.items():
        if key.startswith("LOG_LEVEL_"):
            module_name = key[10:].replace("_", ".")
            module_levels[module_name] = getattr(logging, value.upper(), logging.INFO)

    logging.basicConfig(
        level=root_level,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=[logging.StreamHandler()]
    )

    for module_name, level in module_levels.items():
        logging.getLogger(module_name).setLevel(level)

# 使用方式：
# LOG_LEVEL=DEBUG 默认DEBUG级别
# LOG_LEVEL_CONTEXT_MANAGER=DEBUG context_manager模块DEBUG
# LOG_LEVEL_MCP=ERROR MCP模块只记录ERROR及以上
```

#### 5.2.2 通过MCP指令动态调整（生产环境推荐）

```python
import logging
from typing import Dict
from threading import RLock

class DynamicLogLevelManager:
    """支持运行时动态调整日志级别的管理器。通过MCP指令或API调用触发，无需重启服务。"""

    _instance = None
    _lock = RLock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._levels: Dict[str, int] = {}
                    cls._instance._original_levels: Dict[str, int] = {}
        return cls._instance

    def set_level(self, module_pattern: str, level: str) -> bool:
        numeric_level = getattr(logging, level.upper(), None)
        if numeric_level is None:
            return False

        with self._lock:
            if module_pattern not in self._original_levels:
                logger = logging.getLogger(module_pattern)
                self._original_levels[module_pattern] = logger.level

            self._levels[module_pattern] = numeric_level
            logging.getLogger(module_pattern).setLevel(numeric_level)

            logging.info(
                "[LOG_CONFIG] Log level for '%s' set to %s",
                module_pattern,
                level,
                extra={"config_module": module_pattern, "config_level": level}
            )
            return True

    def reset_level(self, module_pattern: str) -> bool:
        with self._lock:
            if module_pattern in self._original_levels:
                original = self._original_levels[module_pattern]
                logging.getLogger(module_pattern).setLevel(original)
                del self._original_levels[module_pattern]
                del self._levels[module_pattern]
                return True
            return False

    def get_current_levels(self) -> Dict[str, str]:
        result = {}
        for module, level in self._levels.items():
            result[module] = logging.getLevelName(level)
        return result
```

### 5.3 日志保留与清理

```python
import logging
import os
import time
from pathlib import Path

def cleanup_old_logs(log_dir: str, retention_days: int = 30):
    """清理过期的日志文件。"""
    cutoff_time = time.time() - (retention_days * 24 * 3600)
    log_path = Path(log_dir)

    removed_count = 0
    removed_size = 0

    for file_path in log_path.glob("*.log*"):
        if file_path.stat().st_mtime < cutoff_time:
            size = file_path.stat().st_size
            file_path.unlink()
            removed_count += 1
            removed_size += size
            logging.info(
                "[LOG_CLEANUP] Removed old log file: %s (size: %d bytes)",
                file_path.name,
                size
            )

    if removed_count > 0:
        logging.info(
            "[LOG_CLEANUP] Cleanup complete: removed %d files, freed %.2f MB",
            removed_count,
            removed_size / (1024 * 1024)
        )

if __name__ == "__main__":
    LOG_DIR = os.environ.get("LOG_DIR", "./logs")
    cleanup_old_logs(LOG_DIR, retention_days=30)
```

---

## 附录A：日志检查清单

在提交代码前，请确认以下检查项：

```
□ 1. 日志级别正确
  □ DEBUG - 仅开发/调试时记录
  □ INFO - 业务流程关键节点
  □ WARNING - 非预期但可恢复
  □ ERROR - 当前任务失败
  □ CRITICAL - 系统级致命错误

□ 2. 日志格式规范
  □ 包含 timestamp, level, logger_name, message
  □ 包含 trace_id 用于链路追踪
  □ 使用占位符而非字符串拼接

□ 3. 敏感信息保护
  □ 无API Keys/Tokens
  □ 无IP地址/主机名
  □ Payload已脱敏处理

□ 4. 异常记录完整
  □ 使用 exc_info=True 或 logger.exception()
  □ 包含错误上下文信息

□ 5. 性能考虑
  □ 循环内无高频DEBUG日志
  □ 大量数据无完整记录（已截断/采样）
```

---

## 附录B：关键文件检查列表

| 文件路径 | 当前日志状态 | 需要改进 |
|---------|-------------|---------|
| `dcp_optimizer.py` | 基础logging，部分print | 添加trace_id、JSON格式化、心跳日志 |
| `context_manager/manager.py` | 使用前缀标签 | 迁移到统一JSON格式 |
| `context_manager/events.py` | 仅exception记录 | 完善事件追踪 |
| `context_manager/lightyaml.py` | dump()/load()完整观测 | - |
| `context_manager/strategies/yaml_structured_compress.py` | 基础日志 | 添加压缩流程dump()计时和回环验证 |
| `RapidWrightMCP/server.py` | 基础INFO | 添加请求脱敏、trace_id传播 |
| `VivadoMCP/vivado_mcp_server.py` | 基础INFO | 添加请求脱敏、trace_id传播 |
