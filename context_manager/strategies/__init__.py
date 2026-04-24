"""Compression strategy implementations."""

from ..interfaces import CompressionStrategy
from .yaml_structured_compress import YAMLStructuredCompressor

__all__ = [
    "CompressionStrategy",
    "YAMLStructuredCompressor",
]