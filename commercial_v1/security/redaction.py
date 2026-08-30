"""递归敏感字段脱敏和异常文本清洗。"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "<redacted>"
SENSITIVE_KEY_FRAGMENTS = (
    "access_token", "refresh_token", "token", "app_secret", "secret", "password",
    "authorization", "cookie", "signature", "device_credential", "device_session",
    "activation_code", "license_code",
)


def _sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): REDACTED if _sensitive_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:access_token|refresh_token|app_secret|device_credential|activation_code|password)\s*[:=]\s*)[^\s,;&]+"),
]


def sanitize_text(text: object) -> str:
    result = str(text)
    for pattern in _PATTERNS:
        result = pattern.sub(lambda m: m.group(1) + REDACTED, result)
    return result
