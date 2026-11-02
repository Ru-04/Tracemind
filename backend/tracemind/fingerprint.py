"""Deterministic error fingerprint, version 1. See docs/contracts/fingerprint.md."""

from __future__ import annotations

import hashlib
import re

FINGERPRINT_VERSION = 1

_WS = re.compile(r"\s+")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_FRAME = re.compile(r'File "(?P<path>[^"]+)", line \d+, in (?P<func>\S+)')
_NON_APP = re.compile(r"(site-packages|dist-packages|/lib/python\d|/python\d+/lib/|^<)", re.I)


def normalize_message(message: str) -> str:
    """Collapse whitespace and replace volatile values with placeholders."""
    text = _WS.sub(" ", message).strip()
    text = _UUID.sub("<uuid>", text)
    text = _HEX.sub("<hex>", text)
    text = _QUOTED.sub("<str>", text)
    return _NUM.sub("<num>", text)


def _path_tail(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:])


def top_application_frame(stack_trace: str | None) -> str | None:
    """Innermost Python-traceback frame outside site-packages and the standard library.

    Falls back to the innermost frame. Returns `<last two path segments>:<function>`, which is
    identical across machines and operating systems. Only Python tracebacks are parsed in V1.
    """
    if not stack_trace:
        return None
    frames = [(m["path"], m["func"]) for m in _FRAME.finditer(stack_trace)]
    if not frames:
        return None
    for path, func in reversed(frames):
        if not _NON_APP.search(path.replace("\\", "/")):
            return f"{_path_tail(path)}:{func}"
    path, func = frames[-1]
    return f"{_path_tail(path)}:{func}"


def signature(message: str, stack_trace: str | None) -> str:
    frame = top_application_frame(stack_trace)
    if frame is not None:
        return f"frame:{frame}"
    return f"msg:{normalize_message(message)}"


def compute_fingerprint(
    *,
    project_id: str,
    service: str,
    environment: str,
    error_type: str | None,
    message: str,
    stack_trace: str | None,
) -> str:
    """`message` and `stack_trace` must already be redacted."""
    parts = [
        f"v{FINGERPRINT_VERSION}",
        project_id,
        service,
        environment,
        error_type or "-",
        signature(message, stack_trace),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
