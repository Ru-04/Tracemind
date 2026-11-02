from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "tracemind"


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Only the message and an explicit `ctx` dict are emitted."""

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            doc.update(ctx)
        return json.dumps(doc, default=str)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not any(getattr(h, "_tracemind", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler._tracemind = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def log(logger: logging.Logger, level: int, msg: str, **ctx: Any) -> None:
    """`ctx` must contain identifiers, counts and reason codes only — never payload content."""
    logger.log(level, msg, extra={"ctx": ctx})
