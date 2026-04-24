"""
LightYAML - YAML parser/generator backed by pyyaml.

Maintains backward-compatible API with the original zero-dependency implementation.
Internally uses the standard pyyaml library for all parsing and serialization.

Supported: strings, integers, floats, booleans, null, Mapping, Sequence, nested structures
"""

import logging
import time
from typing import Any, Optional, Tuple
from collections import OrderedDict

import yaml

# Module-level logger
_logger = None


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = logging.getLogger("context_manager.lightyaml")
    return _logger


def _get_trace_id() -> str:
    try:
        from .logging_config import get_trace_id
        return get_trace_id()
    except ImportError:
        return ""


# --- Custom exception hierarchy (backward-compatible) ---

class LightYAMLError(ValueError):
    pass


class YAMLParseError(LightYAMLError):
    pass


class YAMLEncodeError(LightYAMLError):
    pass


class YAMLUnsupportedError(LightYAMLError):
    """Kept for backward compatibility; pyyaml supports all standard YAML features."""
    pass


# --- Custom Dumper/Loader for OrderedDict preservation ---

def _ordered_dict_dumper(dumper, data):
    return dumper.represent_mapping('tag:yaml.org,2002:map', data.items())


class _OrderedDictLoader(yaml.SafeLoader):
    """Custom SafeLoader that preserves mapping order using OrderedDict."""

    def __init__(self, stream):
        super().__init__(stream)

    def construct_mapping(self, node, deep=False):
        mapping = OrderedDict()
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            value = self.construct_object(value_node, deep=deep)
            mapping[key] = value
        return mapping


# Register OrderedDict representer
yaml.add_representer(OrderedDict, _ordered_dict_dumper)


class LightYAML:
    """YAML parser/generator backed by pyyaml.

    Provides the same API as the original zero-dependency implementation,
    delegating to pyyaml for all parsing and serialization.
    """

    @classmethod
    def dump(cls, data: Any, indent: int = 2, trace_id: str = None) -> str:
        """Serialize Python object to YAML string.

        Args:
            data: Python object to serialize
            indent: Number of spaces for indentation
            trace_id: Optional trace ID for logging correlation
        """
        logger = _get_logger()
        start_time = time.perf_counter()
        trace_id = trace_id or _get_trace_id()

        try:
            if data is None:
                result = 'null\n'
            elif isinstance(data, bool):
                result = 'true\n' if data else 'false\n'
            elif isinstance(data, (int, float)):
                result = f'{data}\n'
            elif isinstance(data, str):
                result = cls._dump_string(data) + '\n'
            else:
                result = yaml.dump(
                    data,
                    default_flow_style=False,
                    indent=indent,
                    sort_keys=False,
                    allow_unicode=True,
                    Dumper=yaml.Dumper,
                )
        except yaml.YAMLError as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.exception(
                "[YAML_DUMP_ERROR] Failed to serialize %s: %s",
                type(data).__name__, str(e),
                extra={
                    "yaml_dump_error_type": "YAMLEncodeError",
                    "yaml_input_type": type(data).__name__,
                    "yaml_dump_duration_ms": round(duration_ms, 2),
                    "trace_id": trace_id,
                }
            )
            raise YAMLEncodeError(str(e)) from e

        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(
            "[YAML_DUMP] Serialized %s data into %d chars in %.2fms",
            type(data).__name__, len(result), duration_ms,
            extra={
                "yaml_dump_duration_ms": round(duration_ms, 2),
                "yaml_input_type": type(data).__name__,
                "yaml_output_length": len(result),
                "yaml_node_count": cls._estimate_node_count(data),
                "trace_id": trace_id,
            }
        )
        return result

    @classmethod
    def _estimate_node_count(cls, data: Any) -> int:
        """Count approximate number of nodes in a data structure."""
        count = 1
        if isinstance(data, dict):
            for v in data.values():
                count += cls._estimate_node_count(v)
        elif isinstance(data, (list, tuple)):
            for item in data:
                count += cls._estimate_node_count(item)
        return count

    @classmethod
    def _dump_string(cls, value: str) -> str:
        """Serialize a string scalar for top-level output.

        pyyaml adds document end marker (...) to top-level scalars,
        so we handle strings directly to avoid that artifact.
        """
        needs_quoting = (
            not value
            or value != value.strip()
            or any(c in value for c in ':{}[]#&*!|>\'"\n\r')
            or value.lower() in ('true', 'false', 'yes', 'no', 'on', 'off', 'null', 'none', '~')
        )
        if needs_quoting:
            escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
            return f'"{escaped}"'
        return value

    @classmethod
    def load(cls, yaml_str: str, trace_id: str = None) -> Any:
        """Deserialize YAML string to Python object.

        Args:
            yaml_str: YAML string to parse
            trace_id: Optional trace ID for logging
        """
        if not yaml_str or not yaml_str.strip():
            return None

        # Strip BOM, normalize line endings
        yaml_str = yaml_str.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n')
        logger = _get_logger()
        start_time = time.perf_counter()
        trace_id = trace_id or _get_trace_id()

        try:
            result = yaml.load(yaml_str, Loader=_OrderedDictLoader)
        except yaml.YAMLError as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.exception(
                "[YAML_PARSE_ERROR] Failed to parse YAML: %s",
                str(e),
                extra={
                    "yaml_parse_duration_ms": round(duration_ms, 2),
                    "yaml_content_length": len(yaml_str),
                    "trace_id": trace_id,
                }
            )
            raise YAMLParseError(str(e)) from e

        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(
            "[YAML_PARSE] Successfully parsed YAML in %.2fms",
            duration_ms,
            extra={
                "yaml_parse_duration_ms": round(duration_ms, 2),
                "yaml_content_length": len(yaml_str),
                "trace_id": trace_id,
            }
        )
        return result

    @classmethod
    def validate(cls, yaml_str: str) -> Tuple[bool, Optional[str]]:
        """Validate if a YAML string is valid."""
        try:
            cls.load(yaml_str)
            return True, None
        except LightYAMLError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Unknown error: {e}"

    @classmethod
    def roundtrip(cls, data: Any, indent: int = 2) -> Tuple[str, Any]:
        """Serialize and then deserialize (roundtrip test)."""
        yaml_str = cls.dump(data, indent)
        parsed = cls.load(yaml_str)
        return yaml_str, parsed


if __name__ == '__main__':
    # Simple test
    data = {'name': 'test', 'items': [1, 2, 3]}
    yaml = LightYAML.dump(data)
    print("Dump result:")
    print(yaml)
    parsed = LightYAML.load(yaml)
    print("Parsed:", parsed)
    print("Roundtrip OK:", parsed == data or str(parsed) == str(data))
