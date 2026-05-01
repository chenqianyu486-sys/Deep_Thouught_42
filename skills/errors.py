# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache 2.0

"""
Error contract for Skill execution.

Defines canonical error codes, metadata, and the standard error envelope
per the Skill Descriptor v3 specification.
"""

from dataclasses import dataclass
from typing import Optional


class SkillErrorCode:
    """Canonical error codes for Skill execution.

    Every Skill MUST return one of these codes on failure.
    """
    INVALID_PARAMETER = "INVALID_PARAMETER"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    SKILL_TIMEOUT = "SKILL_TIMEOUT"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"


ERROR_METADATA: dict[str, dict] = {
    "INVALID_PARAMETER": {
        "recoverable": False, "http_status": 400,
        "description": "Client provided invalid arguments. Do NOT retry.",
    },
    "RESOURCE_NOT_FOUND": {
        "recoverable": False, "http_status": 404,
        "description": "Requested resource does not exist. Report to user.",
    },
    "PERMISSION_DENIED": {
        "recoverable": False, "http_status": 403,
        "description": "Caller lacks necessary permissions. Do NOT retry.",
    },
    "QUOTA_EXCEEDED": {
        "recoverable": True, "http_status": 429,
        "description": "Rate limit hit. Apply exponential backoff.",
    },
    "TEMPORARILY_UNAVAILABLE": {
        "recoverable": True, "http_status": 503,
        "description": "Backend temporarily unavailable. Retry with backoff.",
    },
    "SKILL_TIMEOUT": {
        "recoverable": True, "http_status": 504,
        "description": "Execution exceeded timeout. Idempotent skills may retry.",
    },
    "CONCURRENT_MODIFICATION": {
        "recoverable": True, "http_status": 423,
        "description": "Resource locked by another invocation. Retry after lock released.",
    },
}


@dataclass
class SkillError:
    """Standardized error envelope per Skill Descriptor specification.

    Every execution error MUST be representable as a SkillError for
    consistent Agent-side handling.
    """
    code: str = ""
    message: str = ""
    request_id: str = ""
    recoverable: bool = False
    retry_after_ms: int = 0
    user_message: str = ""

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.request_id:
            d["requestId"] = self.request_id
        if self.recoverable:
            d["recoverable"] = True
        if self.retry_after_ms:
            d["retryAfterMs"] = self.retry_after_ms
        if self.user_message:
            d["userMessage"] = self.user_message
        return d

    @classmethod
    def from_code(cls, code: str, message: str = "", request_id: str = "",
                  **overrides) -> "SkillError":
        """Create a SkillError from a standard code with metadata defaults."""
        meta = ERROR_METADATA.get(code, {})
        return cls(
            code=code,
            message=message or meta.get("description", ""),
            request_id=request_id,
            recoverable=overrides.get("recoverable", meta.get("recoverable", False)),
            retry_after_ms=overrides.get("retry_after_ms", 0),
            user_message=overrides.get("user_message", ""),
        )
