# 脱敏与输出上限：逐字复制自 agent-service/aivirteach_agent/security.py。
from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)((?:password|passwd|token|api[_-]?key|secret)\s*[=:]\s*)"
        r"([^\s,;\"']+)"
    ),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
)


def redact_text(value: str) -> tuple[str, int]:
    redactions = 0
    result = value
    for pattern in _SECRET_PATTERNS:
        result, count = pattern.subn(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", result)
        redactions += count
    return result, redactions


def sanitize_value(value: Any, *, max_chars: int) -> tuple[Any, bool, int]:
    """Redact strings recursively, then cap the serialized diagnostic payload."""
    redactions = 0

    def walk(item: Any) -> Any:
        nonlocal redactions
        if isinstance(item, str):
            clean, count = redact_text(item)
            redactions += count
            return clean
        if isinstance(item, dict):
            return {str(key): walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item

    cleaned = walk(value)
    import json

    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return cleaned, False, redactions
    return encoded[:max_chars] + "\n...[truncated]", True, redactions
