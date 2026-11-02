"""Normalization of one validated event into the stored representation. Pure, no I/O."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from tracemind.config import Settings
from tracemind.errors import Rejected
from tracemind.fingerprint import FINGERPRINT_VERSION, compute_fingerprint, normalize_message
from tracemind.ingestion.models import IngestEvent, NormalizedEvent, Severity
from tracemind.ingestion.redact import Redactor
from tracemind.projects import Project

_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")  # \Z: `$` would accept a trailing newline
_HEX = re.compile(r"^[0-9a-f]+$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

SEVERITY_ALIASES: dict[str, Severity] = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "NOTICE": "INFO",
    "WARN": "WARNING",
    "WARNING": "WARNING",
    "ERR": "ERROR",
    "ERROR": "ERROR",
    "CRIT": "CRITICAL",
    "CRITICAL": "CRITICAL",
    "FATAL": "CRITICAL",
}
_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


@dataclass(frozen=True)
class PreparedEvent:
    event: NormalizedEvent
    content_hash: str
    incident_eligible: bool
    summary: str


def parse_timestamp(value: str, *, now: datetime, future_skew: timedelta) -> datetime:
    if not _RFC3339.match(value):
        raise Rejected("invalid_timestamp", "timestamp")
    try:
        parsed = datetime.fromisoformat(value.upper())
        if parsed.tzinfo is None:  # cannot happen after the regex; defensive
            raise ValueError
        utc = parsed.astimezone(UTC)
    except (ValueError, OverflowError):  # e.g. 0001-01-01T00:00:00+05:00 overflows in UTC
        raise Rejected("invalid_timestamp", "timestamp") from None
    if utc > now + future_skew:
        raise Rejected("timestamp_in_future", "timestamp")
    return utc


def normalize_severity(value: str) -> Severity:
    try:
        return SEVERITY_ALIASES[value.strip().upper()]
    except KeyError:
        raise Rejected("invalid_severity", "severity") from None


def _name(value: str, field: str) -> str:
    lowered = value.strip().lower()
    if not _NAME.match(lowered):
        raise Rejected(f"invalid_{field}", field)
    return lowered


def _hex_id(value: str | None, length: int, field: str) -> str | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if len(lowered) != length or not _HEX.match(lowered) or set(lowered) == {"0"}:
        raise Rejected(f"invalid_{field}", field)
    return lowered


def _check_attributes(attrs: dict[str, Any], settings: Settings) -> None:
    if len(attrs) > settings.max_attributes:
        raise Rejected("attributes_too_large", "attributes")

    def depth(obj: Any, level: int) -> None:
        if level > settings.max_attribute_depth:
            raise Rejected("attributes_too_deep", "attributes")
        if isinstance(obj, dict):
            for v in obj.values():
                depth(v, level + 1)
        elif isinstance(obj, list):
            for v in obj:
                depth(v, level + 1)

    depth(attrs, 1)
    try:
        size = len(json.dumps(attrs, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise Rejected("invalid_attributes", "attributes") from None
    if size > settings.max_attributes_bytes:
        raise Rejected("attributes_too_large", "attributes")


def _bad_text(obj: Any) -> bool:
    """True if any string (or dict key) holds NUL or a character UTF-8 cannot encode.
    PostgreSQL cannot store either, so such events are rejected up front."""
    if isinstance(obj, str):
        if "\x00" in obj:
            return True
        try:
            obj.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(obj, dict):
        return any(_bad_text(k) or _bad_text(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_bad_text(v) for v in obj)
    return False


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def prepare_event(
    raw: IngestEvent,
    project: Project,
    *,
    now: datetime,
    settings: Settings,
    redactor: Redactor,
    source: str | None = None,
) -> PreparedEvent:
    """Normalize, redact, fingerprint. Raises Rejected; never defaults an invalid value."""
    if raw.project_id != project.project_id:
        raise Rejected("project_mismatch", "project_id")
    occurred_at = parse_timestamp(
        raw.timestamp, now=now, future_skew=timedelta(seconds=settings.future_skew_seconds)
    )
    severity = normalize_severity(raw.severity)
    service = _name(raw.service, "service")
    environment = _name(raw.environment, "environment")
    if service not in project.allowed_services:
        raise Rejected("unknown_service", "service")

    error_type = raw.error_type.strip() if raw.error_type else None
    if error_type and _CONTROL.search(error_type):
        raise Rejected("invalid_error_type", "error_type")
    error_type = error_type or None

    trace_id = _hex_id(raw.trace_id, 32, "trace_id")
    span_id = _hex_id(raw.span_id, 16, "span_id")
    commit_sha = raw.commit_sha.strip().lower() if raw.commit_sha else None
    if commit_sha is not None and not _SHA.match(commit_sha):
        raise Rejected("invalid_commit_sha", "commit_sha")
    if raw.event_id is not None and not _EVENT_ID.match(raw.event_id):
        raise Rejected("invalid_event_id", "event_id")
    if _bad_text([raw.message, raw.error_type, raw.stack_trace, raw.release, raw.attributes]):
        raise Rejected("invalid_characters")
    _check_attributes(raw.attributes, settings)

    redactions = 0

    def red(value: str | None) -> str | None:
        nonlocal redactions
        if value is None:
            return None
        out, n = redactor.text(value)
        redactions += n
        return out

    message = red(raw.message)
    stack_trace = red(raw.stack_trace)
    error_type = red(error_type)
    release = red(raw.release)
    attributes, n_attrs = redactor.value(raw.attributes)
    redactions += n_attrs
    assert message is not None

    fingerprint = compute_fingerprint(
        project_id=project.project_id,
        service=service,
        environment=environment,
        error_type=error_type,
        message=message,
        stack_trace=stack_trace,
    )

    event_id = (
        raw.event_id
        or "derived-"
        + hashlib.sha256(
            "|".join(
                [
                    project.project_id,
                    service,
                    environment,
                    occurred_at.isoformat(),
                    severity,
                    message,
                    error_type or "-",
                    trace_id or "-",
                    span_id or "-",
                ]
            ).encode("utf-8")
        ).hexdigest()
    )

    content_hash = hashlib.sha256(
        _canonical(
            [
                project.project_id,
                service,
                environment,
                occurred_at.isoformat(),
                severity,
                message,
                error_type,
                stack_trace,
                trace_id,
                span_id,
                release,
                commit_sha,
                attributes,
            ]
        ).encode("utf-8")
    ).hexdigest()

    event = NormalizedEvent(
        event_id=event_id,
        project_id=project.project_id,
        service=service,
        environment=environment,
        occurred_at=occurred_at,
        received_at=now,
        severity=severity,
        message=message,
        error_type=error_type,
        stack_trace=stack_trace,
        trace_id=trace_id,
        span_id=span_id,
        release=release,
        commit_sha=commit_sha,
        attributes=attributes,
        fingerprint=fingerprint,
        fingerprint_version=FINGERPRINT_VERSION,
        redactions_applied=redactions,
        source=source,
    )
    eligible = _RANK[severity] >= _RANK[settings.min_incident_severity]
    summary = f"{error_type or 'Error'}: {normalize_message(message)[:200]}"
    return PreparedEvent(event, content_hash, eligible, summary)
