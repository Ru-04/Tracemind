"""Secret and PII redaction. Runs before fingerprinting, persistence and logging.

Pattern redaction cannot catch unknown secret shapes; that residual risk is documented in
docs/threat-model.md. Redaction is idempotent: markers are never matched again.
"""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "cookie",
    "private_key",
    "private-key",
    "credential",
    "session",
)

_KEY_NAMES = (
    r"[a-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|"
    r"private[_-]?key|credential|session[_-]?id|cookie)[a-z0-9_.-]*"
)

# (kind, pattern, replacement). Order matters: specific shapes before generic key=value.
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
        ),
        "[REDACTED:private_key]",
    ),
    (
        "url_credentials",
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@"),
        r"\1[REDACTED:url_credentials]@",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
        "[REDACTED:jwt]",
    ),
    (
        "token",
        re.compile(r"\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}", re.I),
        r"\1\2[REDACTED:token]",
    ),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws_key]"),
    (
        "api_key",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gsk_[A-Za-z0-9]{20,}|"
            r"xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,})"
        ),
        "[REDACTED:api_key]",
    ),
    (
        "secret",
        re.compile(
            rf"\b({_KEY_NAMES})([\"']?\s*[:=]\s*)(?!\[REDACTED)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)",
            re.I,
        ),
        r"\1\2[REDACTED:secret]",
    ),
]

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")


class Redactor:
    def __init__(self, *, redact_emails: bool = True) -> None:
        self._redact_emails = redact_emails

    def text(self, value: str) -> tuple[str, int]:
        count = 0
        for _kind, pattern, repl in _PATTERNS:
            value, n = pattern.subn(repl, value)
            count += n
        if self._redact_emails:
            value, n = _EMAIL.subn("[REDACTED:email]", value)
            count += n
        return value, count

    def value(self, obj: Any) -> tuple[Any, int]:
        """Redact a JSON-like value recursively. Sensitive keys have their whole value replaced."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            total = 0
            for key, val in obj.items():
                k, nk = self.text(str(key))
                total += nk
                if _is_sensitive_key(str(key)) and not _is_marker(val):
                    out[k] = "[REDACTED:key]"
                    total += 1
                else:
                    out[k], n = self.value(val)
                    total += n
            return out, total
        if isinstance(obj, list):
            items: list[Any] = []
            total = 0
            for item in obj:
                new, n = self.value(item)
                items.append(new)
                total += n
            return items, total
        return obj, 0


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _is_marker(val: Any) -> bool:
    return isinstance(val, str) and val.startswith("[REDACTED")
