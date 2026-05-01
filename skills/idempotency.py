# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Idempotency support for Skills.

Provides in-memory idempotency key storage (24-hour window) and
concurrent mutation guard for non-idempotent skills.

For production use, replace the in-memory store with Redis or a database.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional


@dataclass
class IdempotencyRecord:
    """A stored idempotency record."""
    key: str
    result_json: str  # JSON-serialized result data
    created_at: datetime

    def is_expired(self, window_hours: int = 24) -> bool:
        return datetime.now() - self.created_at > timedelta(hours=window_hours)


class IdempotencyStore:
    """In-memory idempotency key store.

    Deduplication window: 24 hours (configurable per lookup).
    Thread-safe for basic concurrent access via classmethod locking.

    For production, subclass or replace with a Redis-backed store.
    """
    _store: dict[str, IdempotencyRecord] = {}
    _inflight: dict[str, str] = {}  # resource_id -> idempotency_key

    @classmethod
    def reset(cls):
        """Clear all stored records and inflight locks. For testing."""
        cls._store.clear()
        cls._inflight.clear()

    @classmethod
    def is_duplicate(cls, key: str, window_hours: int = 24) -> bool:
        """Check if a key has been seen within the deduplication window."""
        record = cls._store.get(key)
        if record is None:
            return False
        if record.is_expired(window_hours):
            cls._store.pop(key, None)
            return False
        return True

    @classmethod
    def get_result(cls, key: str) -> Optional[Any]:
        """Retrieve a cached result by idempotency key."""
        record = cls._store.get(key)
        if record is None:
            return None
        try:
            return json.loads(record.result_json)
        except (json.JSONDecodeError, TypeError):
            return record.result_json

    @classmethod
    def store(cls, key: str, result: Any) -> None:
        """Store a result under an idempotency key."""
        try:
            result_json = json.dumps(result, default=str)
        except (TypeError, ValueError):
            result_json = str(result)
        cls._store[key] = IdempotencyRecord(
            key=key,
            result_json=result_json,
            created_at=datetime.now(),
        )

    # ── Concurrent mutation guard ──────────────────────────────

    @classmethod
    def has_inflight(cls, resource_id: str) -> bool:
        """Check if a resource has an inflight mutation."""
        return resource_id in cls._inflight

    @classmethod
    def get_inflight_key(cls, resource_id: str) -> Optional[str]:
        """Get the idempotency key of the inflight mutation for a resource."""
        return cls._inflight.get(resource_id)

    @classmethod
    def set_inflight(cls, resource_id: str, idempotency_key: str) -> bool:
        """Set an inflight lock for a resource.

        Returns True if lock was acquired, False if already locked.
        """
        if resource_id in cls._inflight:
            return False
        cls._inflight[resource_id] = idempotency_key
        return True

    @classmethod
    def clear_inflight(cls, resource_id: str) -> None:
        """Release the inflight lock for a resource."""
        cls._inflight.pop(resource_id, None)
