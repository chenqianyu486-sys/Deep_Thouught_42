"""Compression strategy implementations."""

from .base import CompressionStrategy
from .smart_compress import SmartCompressionStrategy
from .aggressive_compress import AggressiveCompressionStrategy

__all__ = [
    "CompressionStrategy",
    "SmartCompressionStrategy",
    "AggressiveCompressionStrategy",
]