"""Compression strategy implementations."""

from .base import CompressionStrategy
from .smart_compress import SmartCompressionStrategy
from .aggressive_compress import AggressiveCompressionStrategy
from .xml_structured_compress import XMLStructuredCompressor

__all__ = [
    "CompressionStrategy",
    "SmartCompressionStrategy",
    "AggressiveCompressionStrategy",
    "XMLStructuredCompressor",
]