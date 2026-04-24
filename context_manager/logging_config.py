"""
Centralized logging configuration for FPL26 optimization contest.

This module provides:
- JSON formatter for structured logging
- Context filter for automatic trace_id injection
- Sanitized payload handling for MCP communication
- Centralized log level management

Usage:
    from context_manager.logging_config import setup_logging, get_trace_id, set_trace_id

    setup_logging()  # Call once at application startup
    set_trace_id("job-123-iter1")
    logger = logging.getLogger(__name__)
    logger.info("Message with auto-injected trace_id")
"""

import logging
import logging.handlers
import os
import sys
import time
import uuid
import hashlib
import re
import json
from datetime import datetime, timezone
from typing import Any, Optional
from contextvars import ContextVar
from threading import RLock

# Global trace_id context variable
trace_id_var: ContextVar[str] = ContextVar('trace_id', default='')
job_id_var: ContextVar[Optional[str]] = ContextVar('job_id', default=None)
iteration_var: ContextVar[Optional[int]] = ContextVar('iteration', default=None)

# ============================================================================
# JSON Formatter
# ============================================================================

class JSONFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.
    Outputs JSON with consistent field ordering for log aggregation systems.
    """

    def __init__(self, include_extra: bool = True):
        super().__init__()
        self.include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()

        log_entry = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger_name": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add trace_id if available
        trace_id = getattr(record, 'trace_id', None) or trace_id_var.get('')
        if trace_id:
            log_entry["trace_id"] = trace_id

        # Add job_id if available
        job_id = getattr(record, 'job_id', None) or job_id_var.get()
        if job_id:
            log_entry["job_id"] = job_id

        # Add iteration if available
        iteration = getattr(record, 'iteration', None) or iteration_var.get()
        if iteration is not None:
            log_entry["iteration"] = iteration

        # Add any extra fields
        if self.include_extra:
            extra_fields = {
                k: v for k, v in vars(record).items()
                if k not in (
                    'name', 'msg', 'args', 'created', 'filename', 'funcName',
                    'levelname', 'levelno', 'lineno', 'module', 'msecs',
                    'message', 'pathname', 'process', 'processName',
                    'relativeCreated', 'thread', 'threadName', 'trace_id',
                    'job_id', 'iteration', 'stack_info', 'exc_info', 'exc_text',
                    'message', 'asctime'
                )
            }
            if extra_fields:
                log_entry["extra"] = extra_fields

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


class StandardFormatter(logging.Formatter):
    """
    Standard human-readable formatter with structured prefix.
    Used when JSON output is not required.
    """

    def __init__(self, fmt: str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"):
        super().__init__(fmt)

    def format(self, record: logging.LogRecord) -> str:
        # Add trace_id to message if available
        trace_id = getattr(record, 'trace_id', None) or trace_id_var.get('')
        if trace_id:
            record.message = f"[trace_id={trace_id}] {record.getMessage()}"
        return super().format(record)


# ============================================================================
# Context Filter
# ============================================================================

class ContextFilter(logging.Filter):
    """
    Filter that automatically injects trace_id, job_id, and iteration
    from context variables into log records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get('') or ''
        job_id = job_id_var.get()
        if job_id is not None:
            record.job_id = job_id
        iteration = iteration_var.get()
        if iteration is not None:
            record.iteration = iteration
        return True


# ============================================================================
# Payload Sanitization
# ============================================================================

# Maximum payload length before truncation
MAX_PAYLOAD_LOG_LENGTH = 1024
MAX_PAYLOAD_DISPLAY_LENGTH = 128

# Sensitive field patterns
SENSITIVE_FIELD_PATTERNS = [
    re.compile(r'.*key.*', re.IGNORECASE),
    re.compile(r'.*secret.*', re.IGNORECASE),
    re.compile(r'.*password.*', re.IGNORECASE),
    re.compile(r'.*token.*', re.IGNORECASE),
    re.compile(r'.*credential.*', re.IGNORECASE),
    re.compile(r'.*auth.*', re.IGNORECASE),
]

# API key patterns
API_KEY_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9]{48}'),  # OpenAI
    re.compile(r'ghp_[a-zA-Z0-9]{36}'),  # GitHub
    re.compile(r'xox[baprs]-[0-9a-zA-Z]{10,}'),  # Slack
    re.compile(r'[a-zA-Z0-9]{32,}_[a-zA-Z0-9]{32,}'),  # Generic double-key
]

# IP address pattern
IP_PATTERN = re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')


def sanitize_payload(payload: Any, max_length: int = MAX_PAYLOAD_LOG_LENGTH) -> Any:
    """
    Recursively sanitize a payload for logging:
    - Truncates long strings with hash suffix
    - Masks sensitive field names
    - Handles nested dicts and lists
    """
    if payload is None:
        return None

    if isinstance(payload, str):
        if len(payload) > max_length:
            hash_suffix = hashlib.sha256(payload.encode()).hexdigest()[:8]
            return f"{payload[:MAX_PAYLOAD_DISPLAY_LENGTH]}...[SHA256:{hash_suffix}]"
        return mask_sensitive_string(payload)

    if isinstance(payload, dict):
        return {k: sanitize_payload(v, max_length) for k, v in payload.items()}

    if isinstance(payload, (list, tuple)):
        return [sanitize_payload(item, max_length) for item in payload]

    return payload


def mask_sensitive_string(value: str) -> str:
    """Mask sensitive patterns in a string (API keys, IPs)."""
    result = value

    # Mask API keys
    for pattern in API_KEY_PATTERNS:
        result = pattern.sub('***REDACTED_API_KEY***', result)

    # Mask IP addresses (show first octet only)
    def mask_ip(match):
        ip = match.group()
        parts = ip.split('.')
        if len(parts) == 4:
            return f"{parts[0]}.***.***.{parts[3]}"
        return "***.***.***.***"

    result = IP_PATTERN.sub(mask_ip, result)

    return result


def check_sensitive_in_args(arguments: dict) -> list:
    """
    Check if arguments contain sensitive fields.
    Returns list of paths to sensitive fields.
    """
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
    """Mask sensitive fields in arguments dict."""
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


# ============================================================================
# Log Level Management
# ============================================================================

class DynamicLogLevelManager:
    """
    Singleton for managing log levels at runtime.
    Supports dynamic adjustment without restart.
    """

    _instance = None
    _lock = RLock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._levels: dict = {}
                    cls._instance._original_levels: dict = {}
        return cls._instance

    def set_level(self, module_pattern: str, level: str) -> bool:
        """Set log level for a module pattern."""
        numeric_level = getattr(logging, level.upper(), None)
        if numeric_level is None:
            return False

        with self._lock:
            if module_pattern not in self._original_levels:
                logger = logging.getLogger(module_pattern)
                self._original_levels[module_pattern] = logger.level

            logging.getLogger(module_pattern).setLevel(numeric_level)
            self._levels[module_pattern] = numeric_level
            return True

    def reset_level(self, module_pattern: str) -> bool:
        """Reset module to original log level."""
        with self._lock:
            if module_pattern in self._original_levels:
                logging.getLogger(module_pattern).setLevel(self._original_levels[module_pattern])
                del self._original_levels[module_pattern]
                del self._levels[module_pattern]
                return True
            return False

    def get_current_levels(self) -> dict:
        """Get all currently configured log levels."""
        return {m: logging.getLevelName(l) for m, l in self._levels.items()}


# ============================================================================
# Trace ID Management
# ============================================================================

def generate_trace_id(job_id: str = "", iteration: int = 0) -> str:
    """Generate a new trace_id."""
    parts = ["job"]
    if job_id:
        parts.append(job_id)
    if iteration > 0:
        parts.append(f"iter{iteration}")
    parts.append(uuid.uuid4().hex[:8])
    return "-".join(parts)


def set_trace_id(trace_id: str) -> None:
    """Set the current trace_id context variable."""
    trace_id_var.set(trace_id)


def get_trace_id() -> str:
    """Get the current trace_id context variable."""
    return trace_id_var.get('')


def set_job_context(job_id: str, iteration: int = 0) -> str:
    """Set job context and generate trace_id."""
    job_id_var.set(job_id)
    iteration_var.set(iteration)
    trace_id = generate_trace_id(job_id, iteration)
    trace_id_var.set(trace_id)
    return trace_id


def clear_trace_context() -> None:
    """Clear all trace context variables."""
    trace_id_var.set('')
    job_id_var.set(None)
    iteration_var.set(None)


# ============================================================================
# Logger Setup
# ============================================================================

def setup_logging(
    level: str = None,
    use_json: bool = None,
    log_dir: str = None,
    configure_root: bool = True
) -> None:
    """
    Setup logging configuration for the application.

    Args:
        level: Default log level (default: from LOG_LEVEL env or INFO)
        use_json: Use JSON formatter (default: True in production, False in dev)
        log_dir: Directory for file handlers (optional)
        configure_root: Whether to configure the root logger
    """
    # Determine defaults from environment
    if level is None:
        level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    if use_json is None:
        use_json = os.environ.get('LOG_JSON', 'true').lower() != 'false'
    if log_dir is None:
        log_dir = os.environ.get('LOG_DIR', '')

    numeric_level = getattr(logging, level, logging.INFO)

    # Determine formatter
    if use_json:
        formatter = JSONFormatter(include_extra=True)
    else:
        formatter = StandardFormatter()

    # Configure root logger
    if configure_root:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)  # Capture all, let handlers filter

        # Remove existing handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Console handler
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(ContextFilter())
        root_logger.addHandler(console_handler)

        # File handlers if log_dir specified
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

            # Main log file
            main_handler = logging.handlers.RotatingFileHandler(
                filename=os.path.join(log_dir, "fpl26-optimization.log"),
                maxBytes=100 * 1024 * 1024,  # 100MB
                backupCount=10,
                encoding="utf-8"
            )
            main_handler.setLevel(logging.INFO)
            main_handler.setFormatter(formatter)
            main_handler.addFilter(ContextFilter())
            root_logger.addHandler(main_handler)

            # Error log file
            error_handler = logging.handlers.RotatingFileHandler(
                filename=os.path.join(log_dir, "fpl26-error.log"),
                maxBytes=50 * 1024 * 1024,
                backupCount=10,
                encoding="utf-8"
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(formatter)
            error_handler.addFilter(ContextFilter())
            root_logger.addHandler(error_handler)

    # Set specific module levels from environment
    for key, value in os.environ.items():
        if key.startswith('LOG_LEVEL_'):
            module_name = key[10:].replace('_', '.')
            module_level = getattr(logging, value.upper(), None)
            if module_level:
                logging.getLogger(module_name).setLevel(module_level)


# ============================================================================
# Heartbeat Logger for Long-Running Tasks
# ============================================================================

class HeartbeatLogger:
    """
    Periodic heartbeat logging for long-running FPGA tasks.
    Prevents tasks from being mistaken for dead processes.
    """

    def __init__(
        self,
        interval_seconds: float = 60.0,
        message: str = "Task still running",
        done_event=None  # threading.Event
    ):
        self.interval = interval_seconds
        self.message = message
        self.done_event = done_event
        self._stop = False
        self._thread = None
        self._logger = logging.getLogger(__name__)

    def _heartbeat_loop(self):
        iteration = 0
        while not self._stop:
            if self.done_event and self.done_event.is_set():
                break
            elapsed = iteration * self.interval
            self._logger.info(
                "[HEARTBEAT] %s (elapsed: %ds, count: %d)",
                self.message,
                int(elapsed),
                iteration,
                extra={
                    "heartbeat_elapsed_seconds": int(elapsed),
                    "heartbeat_count": iteration,
                }
            )
            iteration += 1
            # Use stepped sleep to allow faster stop response
            time.sleep(self.interval)

    def start(self):
        """Start the heartbeat thread."""
        from threading import Thread
        self._stop = False
        self._thread = Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the heartbeat thread."""
        self._stop = True
        if self._thread:
            self._thread.join(timeout=5.0)


# ============================================================================
# Progress Tracker for Multi-Step Tasks
# ============================================================================

class ProgressTracker:
    """
    Track and log progress of multi-step tasks like FPGA build flow.
    """

    def __init__(self, total_steps: int, task_name: str):
        self.total_steps = total_steps
        self.task_name = task_name
        self.current_step = 0
        self.start_time = time.time()
        self._logger = logging.getLogger(__name__)

    def update(self, step: int = None, message: str = ""):
        """Update progress to specified step."""
        if step is not None:
            self.current_step = step
        else:
            self.current_step += 1

        elapsed = time.time() - self.start_time
        percent = (self.current_step / self.total_steps) * 100

        self._logger.info(
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
        """Mark task as complete."""
        elapsed = time.time() - self.start_time
        self._logger.info(
            "[PROGRESS] %s: Complete - %s (total: %ds)",
            self.task_name,
            message,
            int(elapsed),
            extra={
                "progress_percent": 100.0,
                "progress_elapsed_seconds": int(elapsed),
            }
        )
