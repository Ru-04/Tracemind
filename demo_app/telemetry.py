"""Structured JSON log events from the demo app, one line per failure on stdout.

The field names follow docs/contracts/event-ingest.schema.json, so a log shipper can forward the
line to TraceMind unchanged. Nothing is invented: `commit_sha` and `release` are emitted only when
the operator provides them, and `span_id` is never fabricated.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sys
import traceback
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOGGER_NAME = "demo_app.telemetry"

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")


class _StdoutHandler(logging.Handler):
    """Writes to whatever sys.stdout is at emit time, so captured output stays testable."""

    def emit(self, record: logging.LogRecord) -> None:
        sys.stdout.write(record.getMessage() + "\n")
        sys.stdout.flush()


def configure() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(h, _StdoutHandler) for h in logger.handlers):
        logger.addHandler(_StdoutHandler())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def new_trace_id() -> str:
    return secrets.token_hex(16)


def trace_id_from_traceparent(header: str | None) -> str | None:
    """Return the trace id of a valid W3C `traceparent` header, else None."""
    if not header:
        return None
    match = _TRACEPARENT.match(header.strip().lower())
    if not match or set(match.group(1)) == {"0"}:
        return None
    return match.group(1)


def _display_path(filename: str) -> str:
    path = Path(filename)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return "/".join(path.parts[-3:])


def format_stack(exc: BaseException) -> str:
    """Python-traceback text with repo-relative paths, so it is the same on every machine."""
    tb = traceback.TracebackException.from_exception(exc)
    lines = ["Traceback (most recent call last):"]
    for frame in tb.stack:
        lines.append(
            f'  File "{_display_path(frame.filename)}", line {frame.lineno}, in {frame.name}'
        )
        if frame.line:
            lines.append(f"    {frame.line}")
    lines.append(f"{type(exc).__name__}: {exc}")
    return "\n".join(lines)


def build_event(
    exc: BaseException,
    *,
    trace_id: str,
    env: Mapping[str, str],
    method: str,
    path: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    event: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "project_id": env.get("DEMO_PROJECT_ID", "project-demo"),
        "service": env.get("DEMO_SERVICE", "demo-payments"),
        "environment": env.get("DEMO_ENVIRONMENT", "development"),
        "timestamp": moment.isoformat().replace("+00:00", "Z"),
        "severity": "ERROR",
        "message": str(exc) or type(exc).__name__,
        "error_type": type(exc).__name__,
        "stack_trace": format_stack(exc),
        "trace_id": trace_id,
        "attributes": {"http.method": method, "http.path": path},
    }
    if env.get("DEMO_RELEASE"):
        event["release"] = env["DEMO_RELEASE"]
    sha = env.get("DEMO_COMMIT_SHA", "")
    if _SHA.match(sha):  # an invalid value is dropped, never repaired or guessed
        event["commit_sha"] = sha.lower()
    return event


def emit(event: dict[str, Any]) -> None:
    logging.getLogger(LOGGER_NAME).info(
        json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    )
