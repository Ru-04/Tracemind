"""Pydantic models mirroring docs/contracts/event-ingest.schema.json and event.schema.json."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tracemind.errors import Rejected

Severity = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class IngestEvent(BaseModel):
    """Raw, untrusted event. Semantic checks (timestamp, severity, ...) happen in normalize."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    timestamp: str = Field(min_length=1, max_length=64)
    severity: str = Field(min_length=1, max_length=16)
    message: str = Field(min_length=1, max_length=8192)
    error_type: str | None = Field(default=None, max_length=256)
    stack_trace: str | None = Field(default=None, max_length=32768)
    trace_id: str | None = Field(default=None, max_length=64)
    span_id: str | None = Field(default=None, max_length=32)
    release: str | None = Field(default=None, max_length=128)
    commit_sha: str | None = Field(default=None, max_length=64)
    attributes: dict[str, Any] = Field(default_factory=dict)


class NormalizedEvent(BaseModel):
    """Event as stored. Matches docs/contracts/event.schema.json."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    project_id: str
    service: str
    environment: str
    occurred_at: datetime
    received_at: datetime
    severity: Severity
    message: str
    error_type: str | None = None
    stack_trace: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    release: str | None = None
    commit_sha: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str
    fingerprint_version: int
    ingest_status: Literal["accepted"] = "accepted"
    redactions_applied: int = 0
    source: str | None = None


_KNOWN_FIELDS = frozenset(IngestEvent.model_fields)


def validate_ingest(raw: object) -> IngestEvent:
    """Validate one raw event. Raises Rejected with a stable code and never echoes input."""
    if not isinstance(raw, dict):
        raise Rejected("invalid_event")
    try:
        return IngestEvent.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors(include_input=False, include_url=False, include_context=False)[0]
        loc = err["loc"][0] if err["loc"] else None
        field = loc if isinstance(loc, str) and loc in _KNOWN_FIELDS else None
        kind = err["type"]
        if kind == "missing":
            raise Rejected("missing_field", field) from None
        if kind == "extra_forbidden":
            raise Rejected("unknown_field") from None
        if kind in ("string_too_long", "too_long"):
            raise Rejected(f"{field}_too_long" if field else "field_too_long", field) from None
        raise Rejected(f"invalid_{field}" if field else "invalid_field", field) from None
