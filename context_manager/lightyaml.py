"""
LightYAML - Lightweight pure-Python standard library YAML parser/generator.

Design Goals:
- Zero external dependencies: Uses only Python built-in modules
- Lightweight subset: Supports basic types and nested structures
- FPGA-friendly: Handles special characters in signal names ([, ], -, _)

Feature Boundaries:
- Supported: strings, integers, floats, booleans, null, Mapping, Sequence, nested structures, comments
- NOT Supported: anchors (&), aliases (*), multi-line block strings (|/ >), type tags (!!)
"""

import logging
import time
from typing import Union, Dict, List, Any, Optional, Tuple
from collections import OrderedDict
import re

# Module-level logger - lazy initialization to maintain zero-dependency feel
_logger = None

def _get_logger() -> logging.Logger:
    """Get or create the module logger (lazy initialization)."""
    global _logger
    if _logger is None:
        _logger = logging.getLogger("context_manager.lightyaml")
    return _logger


def _get_trace_id() -> str:
    """Get trace_id from context, with zero-dependency fallback."""
    try:
        from .logging_config import get_trace_id  # noqa: F811
        return get_trace_id()
    except ImportError:
        return ""


class LightYAMLError(ValueError):
    pass


class YAMLParseError(LightYAMLError):
    pass


class YAMLEncodeError(LightYAMLError):
    pass


class YAMLUnsupportedError(LightYAMLError):
    pass


class LightYAML:
    """Lightweight YAML parser/generator."""

    BOOLEAN_TRUE = {'true', 'True', 'TRUE', 'yes', 'Yes', 'YES', 'on', 'On', 'ON'}
    BOOLEAN_FALSE = {'false', 'False', 'FALSE', 'no', 'No', 'NO', 'off', 'Off', 'OFF'}
    NULL_VALUES = {'null', 'Null', 'NULL', '~', 'None', 'none'}

    # Pre-compiled regex patterns for performance
    _NUMERIC_RE = re.compile(r'^[+-]?(\d+\.?\d*|\d*\.?\d+)([eE][+-]?\d+)?$')
    _HEX_RE = re.compile(r'^0[xX][0-9a-fA-F]+$')
    _INT_RE = re.compile(r'^[+-]?[0-9]+$')
    # Escape translation table for _unquote
    _ESCAPE_TRANS = str.maketrans({'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'"})

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
            elif isinstance(data, int):
                result = f'{data}\n'
            elif isinstance(data, float):
                result = f'{data}\n'
            elif isinstance(data, str):
                result = cls._dump_string(data) + '\n'
            elif isinstance(data, (list, tuple)):
                if cls._is_simple_sequence(data):
                    result = cls._dump_flow_sequence(data) + '\n'
                else:
                    result = cls._dump_sequence(data, 0, indent) + '\n'
            elif isinstance(data, dict):
                result = cls._dump_mapping(data, 0, indent) + '\n'
            elif isinstance(data, OrderedDict):
                result = cls._dump_mapping(data, 0, indent) + '\n'
            else:
                raise YAMLEncodeError(f"Unsupported data type: {type(data).__name__}")
        except YAMLEncodeError:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.exception(
                "[YAML_DUMP_ERROR] Failed to serialize %s: %s",
                type(data).__name__,
                f"Unsupported data type: {type(data).__name__}",
                extra={
                    "yaml_dump_error_type": "YAMLEncodeError",
                    "yaml_input_type": type(data).__name__,
                    "yaml_dump_duration_ms": round(duration_ms, 2),
                    "trace_id": trace_id,
                }
            )
            raise

        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(
            "[YAML_DUMP] Serialized %s data into %d chars in %.2fms",
            type(data).__name__,
            len(result),
            duration_ms,
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
        """Count approximate number of nodes in a data structure.

        Used for observability metrics to estimate serialization complexity.
        """
        count = 1
        if isinstance(data, dict):
            for v in data.values():
                count += cls._estimate_node_count(v)
        elif isinstance(data, (list, tuple)):
            for item in data:
                count += cls._estimate_node_count(item)
        return count

    @classmethod
    def _is_simple_sequence(cls, seq: List) -> bool:
        """Check if a list is suitable for flow output (all elements are simple scalars)."""
        for item in seq:
            if isinstance(item, (list, tuple, dict, OrderedDict)):
                return False
            if isinstance(item, str) and (
                item.strip() != item or
                any(c in item for c in ':{}[]#&\'"|>\n\r') or
                item.lstrip().startswith('-') or
                item.lstrip().startswith('#')
            ):
                return False
        return True

    @classmethod
    def _dump_flow_sequence(cls, seq: List) -> str:
        """Serialize a simple list to flow format [1, 2, 3]."""
        parts = []
        for item in seq:
            if item is None:
                parts.append('null')
            elif isinstance(item, bool):
                parts.append('true' if item else 'false')
            elif isinstance(item, int):
                parts.append(str(item))
            elif isinstance(item, float):
                parts.append(str(item))
            elif isinstance(item, str):
                parts.append(cls._dump_string(item))
        return '[' + ', '.join(parts) + ']'

    @classmethod
    def _dump_string(cls, value: str) -> str:
        """Serialize a string."""
        needs_quotes = False
        if not value:
            needs_quotes = True
        elif value.strip() != value:
            needs_quotes = True
        elif value.startswith(('-', '#')):
            needs_quotes = True
        elif any(c in value for c in ':{}[]#&*!|>\'"'):
            needs_quotes = True
        elif value.lower() in ('true', 'false', 'yes', 'no', 'on', 'off', 'null', 'none', '~'):
            needs_quotes = True
        elif cls._NUMERIC_RE.match(value):
            needs_quotes = True
        elif '\n' in value or '\r' in value:
            needs_quotes = True

        if needs_quotes:
            escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
            return f'"{escaped}"'
        return value

    @classmethod
    def _dump_sequence(cls, seq: List, level: int, indent: int) -> str:
        """Serialize a list."""
        lines = []
        prefix = ' ' * (level * indent)
        for item in seq:
            if item is None:
                lines.append(f'{prefix}- null')
            elif isinstance(item, bool):
                lines.append(f'{prefix}- {"true" if item else "false"}')
            elif isinstance(item, int):
                lines.append(f'{prefix}- {item}')
            elif isinstance(item, float):
                lines.append(f'{prefix}- {item}')
            elif isinstance(item, str):
                dumped = cls._dump_string(item)
                # In block format, strings are concatenated directly after "- ".
                # If the string itself starts with "-", it conflicts with the list item marker.
                # Detect this case and force quoting.
                if dumped and not dumped.startswith('"') and not dumped.startswith("'"):
                    stripped = dumped.lstrip()
                    if stripped.startswith('-'):
                        dumped = f'"{dumped}"'
                lines.append(f'{prefix}- {dumped}')
            elif isinstance(item, (list, tuple)):
                nested = cls._dump_sequence(item, level + 1, indent)
                lines.append(f'{prefix}-')
                lines.append((' ' * ((level + 1) * indent)) + nested)
            elif isinstance(item, dict):
                # Use flow syntax for dict list items with multiple keys
                if len(item) == 1:
                    k, v = next(iter(item.items()))
                    lines.append(f'{prefix}- {cls._dump_string(str(k))}: {cls._dump_value(v, level + 1, indent)}')
                else:
                    # Multiple keys: use flow syntax {k1: v1, k2: v2}
                    inner = ', '.join(f'{cls._dump_string(str(k))}: {cls._dump_value(v, level + 1, indent)}' for k, v in item.items())
                    lines.append(f'{prefix}- {{{inner}}}')
            elif isinstance(item, OrderedDict):
                if len(item) == 1:
                    k, v = next(iter(item.items()))
                    lines.append(f'{prefix}- {cls._dump_string(str(k))}: {cls._dump_value(v, level + 1, indent)}')
                else:
                    inner = ', '.join(f'{cls._dump_string(str(k))}: {cls._dump_value(v, level + 1, indent)}' for k, v in item.items())
                    lines.append(f'{prefix}- {{{inner}}}')
            else:
                lines.append(f'{prefix}- {cls._dump_string(str(item))}')
        return '\n'.join(lines)

    @classmethod
    def _dump_mapping(cls, mapping: Dict, level: int, indent: int) -> str:
        """Serialize a mapping (key-value pairs)."""
        lines = []
        prefix = ' ' * (level * indent)
        for key, value in mapping.items():
            key_str = cls._dump_string(str(key))
            if value is None:
                lines.append(f'{prefix}{key_str}: null')
            elif isinstance(value, bool):
                lines.append(f'{prefix}{key_str}: {"true" if value else "false"}')
            elif isinstance(value, int):
                lines.append(f'{prefix}{key_str}: {value}')
            elif isinstance(value, float):
                lines.append(f'{prefix}{key_str}: {value}')
            elif isinstance(value, str):
                lines.append(f'{prefix}{key_str}: {cls._dump_string(value)}')
            elif isinstance(value, (list, tuple)):
                lines.append(f'{prefix}{key_str}:')
                lines.append(cls._dump_sequence(value, level + 1, indent))
            elif isinstance(value, dict):
                lines.append(f'{prefix}{key_str}:')
                lines.append(cls._dump_mapping(value, level + 1, indent))
            elif isinstance(value, OrderedDict):
                lines.append(f'{prefix}{key_str}:')
                lines.append(cls._dump_mapping(value, level + 1, indent))
            else:
                lines.append(f'{prefix}{key_str}: {cls._dump_string(str(value))}')
        return '\n'.join(lines)

    @classmethod
    def _dump_value(cls, value: Any, level: int, indent: int) -> str:
        """Serialize a single value."""
        if value is None:
            return 'null'
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(value)
        if isinstance(value, str):
            return cls._dump_string(value)
        if isinstance(value, (list, tuple)):
            return cls._dump_sequence(value, level, indent)
        if isinstance(value, dict):
            return cls._dump_mapping(value, level, indent)
        if isinstance(value, OrderedDict):
            return cls._dump_mapping(value, level, indent)
        return cls._dump_string(str(value))

    @classmethod
    def load(cls, yaml_str: str, trace_id: str = None) -> Any:
        """Deserialize YAML string to Python object.

        Args:
            yaml_str: YAML string to parse
            trace_id: Optional trace ID for logging
        """
        if not yaml_str or not yaml_str.strip():
            return None

        yaml_str = yaml_str.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n')
        logger = _get_logger()
        start_time = time.perf_counter()

        try:
            # Validate flow structure bracket matching
            open_brackets = 0
            open_braces = 0
            in_quote = False
            quote_char = None
            error_line = 1
            error_column = 0
            for char_idx, char in enumerate(yaml_str):
                if char == '\n':
                    error_line += 1
                    error_column = 0
                else:
                    error_column += 1
                if char in '"\'' and not in_quote:
                    in_quote = True
                    quote_char = char
                elif char == quote_char and in_quote:
                    in_quote = False
                    quote_char = None
                elif not in_quote:
                    if char == '[':
                        open_brackets += 1
                    elif char == ']':
                        open_brackets -= 1
                        if open_brackets < 0:
                            cls._log_yaml_parse_error(
                                logger, "YAMLParseError",
                                "Unexpected closing square bracket ']'",
                                yaml_str, error_line, error_column, trace_id
                            )
                            raise YAMLParseError("Unexpected closing square bracket ']'")
                    elif char == '{':
                        open_braces += 1
                    elif char == '}':
                        open_braces -= 1
                        if open_braces < 0:
                            cls._log_yaml_parse_error(
                                logger, "YAMLParseError",
                                "Unexpected closing curly brace '}'",
                                yaml_str, error_line, error_column, trace_id
                            )
                            raise YAMLParseError("Unexpected closing curly brace '}'")

            if open_brackets > 0:
                cls._log_yaml_parse_error(
                    logger, "YAMLParseError",
                    f"Unclosed flow structure '[' (missing {open_brackets} closing bracket(s))",
                    yaml_str, error_line, error_column, trace_id
                )
                raise YAMLParseError(f"Unclosed flow structure '['")
            if open_braces > 0:
                cls._log_yaml_parse_error(
                    logger, "YAMLParseError",
                    f"Unclosed flow structure '{{' (missing {open_braces} closing brace(s))",
                    yaml_str, error_line, error_column, trace_id
                )
                raise YAMLParseError(f"Unclosed flow structure '{{'")

            # Tab check
            if '\t' in yaml_str:
                tab_pos = yaml_str.index('\t')
                tab_line = yaml_str[:tab_pos].count('\n') + 1
                cls._log_yaml_parse_error(
                    logger, "YAMLParseError",
                    "Tab characters are not allowed in YAML",
                    yaml_str, tab_line, 1, trace_id
                )
                raise YAMLParseError("Tab characters are not allowed in YAML")

            # Check for unsupported features
            unsupported_features = []
            if '&' in yaml_str:
                unsupported_features.append("anchor (&)")
            if '*' in yaml_str:
                unsupported_features.append("alias (*)")
            if '!!' in yaml_str:
                unsupported_features.append("type tag (!!)")
            if '|' in yaml_str:
                unsupported_features.append("multi-line literal block (|)")
            if '>' in yaml_str:
                unsupported_features.append("multi-line folded block (>)")

            if unsupported_features:
                for feature in unsupported_features:
                    logger.warning(
                        "[YAML_UNSUPPORTED] Unsupported feature '%s' detected",
                        feature,
                        extra={"yaml_unsupported_feature": feature, "trace_id": trace_id}
                    )
                raise YAMLUnsupportedError(f"Unsupported YAML feature(s): {', '.join(unsupported_features)}")

            # Preprocess: remove inline comments
            lines = yaml_str.split('\n')
            processed_lines = []
            for line in lines:
                processed = cls._remove_inline_comment(line)
                processed_lines.append(processed)

            # Parse
            result, _ = cls._parse_block(processed_lines, 0, -1)

            # Log success at DEBUG level
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

        except (YAMLParseError, YAMLUnsupportedError):
            raise
        except Exception as e:
            # Log unexpected errors with context
            cls._log_yaml_parse_error(
                logger, type(e).__name__,
                str(e),
                yaml_str, 1, 0, trace_id
            )
            raise

    @classmethod
    def _log_yaml_parse_error(
        cls,
        logger: logging.Logger,
        error_type: str,
        error_message: str,
        yaml_content: str,
        error_line: int,
        error_column: int,
        trace_id: str = None
    ):
        """Log YAML parse error with context around the error location."""
        lines = yaml_content.split('\n')

        # Get context: 3 lines before, error line, 2 lines after
        start_line = max(0, error_line - 4)
        end_line = min(len(lines), error_line + 2)

        context_lines = []
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

        logger.exception(
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

    @classmethod
    def _remove_inline_comment(cls, line: str) -> str:
        """Remove inline comments (outside of quotes)."""
        in_quote = False
        quote_char = None
        for i, c in enumerate(line):
            if c in '"\'' and not in_quote:
                in_quote = True
                quote_char = c
            elif c == quote_char and in_quote:
                in_quote = False
                quote_char = None
            elif c == '#' and not in_quote:
                return line[:i].rstrip()
        return line

    @classmethod
    def _find_colon_outside_quotes(cls, s: str) -> int:
        """Find colon position outside of quotes and brackets"""
        in_quote = False
        quote_char = None
        bracket_depth = 0
        for i, c in enumerate(s):
            if c in '"\'' and not in_quote:
                in_quote = True
                quote_char = c
            elif c == quote_char and in_quote:
                in_quote = False
                quote_char = None
            elif c == '[' and not in_quote:
                bracket_depth += 1
            elif c == ']' and not in_quote:
                bracket_depth -= 1
            elif c == ':' and not in_quote and bracket_depth == 0:
                return i
        return -1

    @classmethod
    def _parse_block(cls, lines: List[str], start: int, parent_indent: int) -> Tuple[Any, int]:
        """Parse a block, returns (parsed_result, lines_consumed)."""
        result = None
        i = start
        current_mapping = None
        current_list = None

        while i < len(lines):
            line = lines[i]

            # Empty line
            if not line.strip():
                i += 1
                continue

            # Comment line
            if line.strip().startswith('#'):
                i += 1
                continue

            # Tab check
            if '\t' in line:
                raise YAMLParseError("Tab characters are not allowed in YAML")

            # Calculate indent
            indent = len(line) - len(line.lstrip())

            # Indent decreased, exit (but don't check at top level)
            if parent_indent >= 0 and indent <= parent_indent:
                break

            stripped = line.strip()

            # Flow structure (entire line)
            if (stripped.startswith('[') and stripped.endswith(']')) or \
               (stripped.startswith('{') and stripped.endswith('}')):
                return (cls._parse_flow_value(stripped), i - start + 1)

            # Quoted string (may contain colons internally)
            if (stripped.startswith('"') and stripped.endswith('"')) or \
               (stripped.startswith("'") and stripped.endswith("'")):
                return (cls._parse_scalar(stripped), i - start + 1)

            # List item
            if stripped.startswith('- '):
                content = stripped[2:].strip()

                # Determine if we're processing a list
                if current_list is None:
                    current_list = []
                    result = current_list

                if not content:
                    # Empty list item, check for nested content
                    i += 1
                    if i < len(lines):
                        next_line = lines[i]
                        next_stripped = next_line.strip()
                        if next_stripped and not next_stripped.startswith('#'):
                            next_indent = len(next_line) - len(next_line.lstrip())
                            if next_indent > indent:
                                # Nested content
                                nested, consumed = cls._parse_block(lines, i, next_indent - 1)
                                current_list.append(nested)
                                i += consumed
                            elif ':' not in next_stripped:
                                current_list.append(cls._parse_scalar(next_stripped))
                                i += 1
                            else:
                                current_list.append(None)
                        else:
                            current_list.append(None)
                    else:
                        current_list.append(None)
                elif content.startswith('[') and content.endswith(']') and content.count('[') == 1:
                    current_list.append(cls._parse_flow_sequence(content[1:-1]))
                    i += 1
                elif content.startswith('{') and content.endswith('}') and content.count('{') == 1:
                    current_list.append(cls._parse_flow_mapping(content[1:-1]))
                    i += 1
                elif ':' in content:
                    # Dict as list item
                    colon_pos = cls._find_colon_outside_quotes(content)
                    if colon_pos < 0:
                        current_list.append(cls._parse_scalar(content))
                        i += 1
                        continue
                    key = cls._unquote(content[:colon_pos].strip())
                    rest = content[colon_pos + 1:].strip()
                    if rest:
                        current_list.append({key: cls._parse_scalar(rest)})
                    else:
                        # Check for nested content
                        next_idx = i + 1
                        if next_idx < len(lines):
                            next_line = lines[next_idx]
                            next_stripped = next_line.strip()
                            if next_stripped and not next_stripped.startswith('#'):
                                next_indent = len(next_line) - len(next_line.lstrip())
                                if next_indent > indent:
                                    nested, consumed = cls._parse_block(lines, next_idx, indent)
                                    current_list.append({key: nested})
                                    i += consumed + 1
                                    continue
                        current_list.append({key: None})
                    i += 1
                else:
                    current_list.append(cls._parse_scalar(content))
                    i += 1

            # Key-value pair
            elif ':' in stripped:
                colon_pos = cls._find_colon_outside_quotes(stripped)
                if colon_pos < 0:
                    i += 1
                    continue
                key = cls._unquote(stripped[:colon_pos].strip())
                rest = stripped[colon_pos + 1:].strip()

                # Determine if we're processing a mapping
                if current_mapping is None:
                    current_mapping = OrderedDict()
                    result = current_mapping

                if rest:
                    # Inline value
                    current_mapping[key] = cls._parse_scalar(rest)
                    i += 1
                else:
                    # Check for nested content
                    i += 1
                    if i < len(lines):
                        next_line = lines[i]
                        next_stripped = next_line.strip()

                        if next_stripped and not next_stripped.startswith('#'):
                            next_indent = len(next_line) - len(next_line.lstrip())

                            if next_indent > indent:
                                # Nested content
                                if next_stripped.startswith('- '):
                                    nested, consumed = cls._parse_block(lines, i, next_indent - 1)
                                    current_mapping[key] = nested
                                    i += consumed
                                elif ':' in next_stripped:
                                    nested, consumed = cls._parse_block(lines, i, next_indent - 1)
                                    current_mapping[key] = nested
                                    i += consumed
                                elif next_indent > indent:
                                    # Nested scalar
                                    current_mapping[key] = cls._parse_scalar(next_stripped)
                                    i += 1
                                else:
                                    current_mapping[key] = None
                            else:
                                current_mapping[key] = None
                        else:
                            current_mapping[key] = None
                    else:
                        current_mapping[key] = None

            else:
                # Scalar line (no colon)
                if current_mapping is None:
                    return (cls._parse_scalar(stripped), i - start + 1)
                i += 1

        # Return result and lines consumed
        return (result if result is not None else None, i - start)

    @classmethod
    def _parse_scalar(cls, s: str) -> Any:
        """Parse a scalar value."""
        if not s:
            return None

        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            return cls._unquote(s)

        if s in cls.NULL_VALUES:
            return None

        if s in cls.BOOLEAN_TRUE:
            return True
        if s in cls.BOOLEAN_FALSE:
            return False

        if cls._HEX_RE.match(s):
            return int(s, 16)

        if cls._INT_RE.match(s):
            return int(s)

        if cls._NUMERIC_RE.match(s):
            return float(s)

        return s

    @classmethod
    def _unquote(cls, s: str) -> str:
        """Remove quotes and handle escape sequences."""
        if len(s) < 2:
            return s
        if not ((s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'"))):
            return s
        content = s[1:-1]
        if '\\' not in content:
            return content
        # Handle escape sequences efficiently
        escape_map = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'"}
        result = []
        i = 0
        n = len(content)
        while i < n:
            if content[i] == '\\' and i + 1 < n:
                next_c = content[i + 1]
                result.append(escape_map.get(next_c, next_c))
                i += 2
            else:
                result.append(content[i])
                i += 1
        return ''.join(result)

    @classmethod
    def _parse_flow_value(cls, s: str) -> Any:
        """Parse a flow value."""
        s = s.strip()
        if not s:
            return None
        if s.startswith('[') and s.endswith(']'):
            return cls._parse_flow_sequence(s[1:-1])
        if s.startswith('{') and s.endswith('}'):
            return cls._parse_flow_mapping(s[1:-1])
        return s

    @classmethod
    def _parse_flow_sequence(cls, content: str) -> List:
        """Parse a flow sequence."""
        content = content.strip()
        if not content:
            return []

        result = []
        i = 0
        n = len(content)

        while i < n:
            while i < n and content[i] in ' \t':
                i += 1
            if i >= n:
                break

            if content[i] == '[':
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if content[j] == '[':
                        depth += 1
                    elif content[j] == ']':
                        depth -= 1
                    j += 1
                result.append(cls._parse_flow_sequence(content[i+1:j-1]))
                i = j
            elif content[i] == '{':
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if content[j] == '{':
                        depth += 1
                    elif content[j] == '}':
                        depth -= 1
                    j += 1
                result.append(cls._parse_flow_mapping(content[i+1:j-1]))
                i = j
            elif content[i] in '"\'':
                quote = content[i]
                j = i + 1
                while j < n and content[j] != quote:
                    if content[j] == '\\' and j + 1 < n:
                        j += 2
                    else:
                        j += 1
                result.append(cls._unquote(content[i:j+1]))
                i = j + 1
            elif content[i] == ',':
                i += 1
            else:
                j = i
                while j < n and content[j] not in ', \t':
                    j += 1
                value_str = content[i:j].strip()
                if value_str:
                    result.append(cls._parse_scalar(value_str))
                i = j

        return result

    @classmethod
    def _parse_flow_mapping(cls, content: str) -> Dict:
        """Parse a flow mapping."""
        content = content.strip()
        if not content:
            return {}

        result = OrderedDict()
        i = 0
        n = len(content)

        while i < n:
            while i < n and content[i] in ' \t':
                i += 1
            if i >= n:
                break

            if content[i] == '{':
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if content[j] == '{':
                        depth += 1
                    elif content[j] == '}':
                        depth -= 1
                    j += 1
                result[f'_nested_{len(result)}'] = cls._parse_flow_mapping(content[i+1:j-1])
                i = j
            elif content[i] == '[':
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if content[j] == '[':
                        depth += 1
                    elif content[j] == ']':
                        depth -= 1
                    j += 1
                result[f'_nested_{len(result)}'] = cls._parse_flow_sequence(content[i+1:j-1])
                i = j
            elif content[i] in '"\'':
                quote = content[i]
                j = i + 1
                while j < n and content[j] != quote:
                    if content[j] == '\\' and j + 1 < n:
                        j += 2
                    else:
                        j += 1
                key = cls._unquote(content[i:j+1])
                i = j + 1

                while i < n and content[i] in ' \t':
                    i += 1

                if i < n and content[i] == ':':
                    i += 1
                    while i < n and content[i] in ' \t':
                        i += 1

                    if i < n and content[i] in '"\'':
                        quote = content[i]
                        j = i + 1
                        while j < n and content[j] != quote:
                            if content[j] == '\\' and j + 1 < n:
                                j += 2
                            else:
                                j += 1
                        result[key] = cls._unquote(content[i:j+1])
                        i = j + 1
                    elif i < n and content[i] == '{':
                        depth = 1
                        j = i + 1
                        while j < n and depth > 0:
                            if content[j] == '{':
                                depth += 1
                            elif content[j] == '}':
                                depth -= 1
                            j += 1
                        result[key] = cls._parse_flow_mapping(content[i+1:j-1])
                        i = j
                    elif i < n and content[i] == '[':
                        depth = 1
                        j = i + 1
                        while j < n and depth > 0:
                            if content[j] == '[':
                                depth += 1
                            elif content[j] == ']':
                                depth -= 1
                            j += 1
                        result[key] = cls._parse_flow_sequence(content[i+1:j-1])
                        i = j
                    else:
                        j = i
                        while j < n and content[j] not in ',}':
                            j += 1
                        value_str = content[i:j].strip()
                        result[key] = cls._parse_scalar(value_str) if value_str else None
                        i = j
                else:
                    result[key] = None

            elif content[i] == ':':
                i += 1
                while i < n and content[i] in ' \t':
                    i += 1
                if i < n and content[i] not in ',}':
                    j = i
                    while j < n and content[j] not in ',}':
                        j += 1
                    value_str = content[i:j].strip()
                    result[''] = cls._parse_scalar(value_str) if value_str else None
                    i = j
                else:
                    result[''] = None
            elif content[i] == ',':
                i += 1
            else:
                j = i
                while j < n and content[j] not in ':, \t':
                    j += 1
                key = content[i:j].strip()
                i = j

                while i < n and content[i] in ' \t':
                    i += 1

                if i < n and content[i] == ':':
                    i += 1
                    while i < n and content[i] in ' \t':
                        i += 1

                    if i < n and content[i] in '"\'':
                        quote = content[i]
                        j = i + 1
                        while j < n and content[j] != quote:
                            if content[j] == '\\' and j + 1 < n:
                                j += 2
                            else:
                                j += 1
                        result[key] = cls._unquote(content[i:j+1])
                        i = j + 1
                    elif i < n and content[i] == '{':
                        depth = 1
                        j = i + 1
                        while j < n and depth > 0:
                            if content[j] == '{':
                                depth += 1
                            elif content[j] == '}':
                                depth -= 1
                            j += 1
                        result[key] = cls._parse_flow_mapping(content[i+1:j-1])
                        i = j
                    elif i < n and content[i] == '[':
                        depth = 1
                        j = i + 1
                        while j < n and depth > 0:
                            if content[j] == '[':
                                depth += 1
                            elif content[j] == ']':
                                depth -= 1
                            j += 1
                        result[key] = cls._parse_flow_sequence(content[i+1:j-1])
                        i = j
                    else:
                        j = i
                        while j < n and content[j] not in ',}':
                            j += 1
                        value_str = content[i:j].strip()
                        result[key] = cls._parse_scalar(value_str) if value_str else None
                        i = j
                else:
                    result[key] = None

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
