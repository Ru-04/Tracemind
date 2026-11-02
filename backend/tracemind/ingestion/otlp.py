"""OpenTelemetry OTLP/JSON logs adapter (`?format=otlp-logs`).

A real, public telemetry format converted into TraceMind events. It needs no OpenTelemetry SDK
and no third-party platform; it only reads the documented OTLP JSON log structure
(resourceLogs → scopeLogs → logRecords). Missing required values are rejected, never invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from tracemind.errors import Rejected
from tracemind.ingestion.adapters import AdapterContext, AdapterFormatError, AdapterItem

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# OTLP severityNumber ranges → names understood by the normalizer.
_SEVERITY_RANGES = (
    (4, "TRACE"),
    (8, "DEBUG"),
    (12, "INFO"),
    (16, "WARN"),
    (20, "ERROR"),
    (24, "FATAL"),
)
_MAX_VALUE_DEPTH = 4


def _dict(x: Any) -> dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _list(x: Any) -> list[Any]:
    return x if isinstance(x, list) else []


def _any_value(v: Any, depth: int = 0) -> Any:
    v = _dict(v)
    if "stringValue" in v and isinstance(v["stringValue"], str):
        return v["stringValue"]
    if "boolValue" in v and isinstance(v["boolValue"], bool):
        return v["boolValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v and isinstance(v["doubleValue"], int | float):
        return v["doubleValue"]
    if depth < _MAX_VALUE_DEPTH and "arrayValue" in v:
        return [_any_value(i, depth + 1) for i in _list(_dict(v["arrayValue"]).get("values"))]
    if depth < _MAX_VALUE_DEPTH and "kvlistValue" in v:
        return _attrs(_dict(v["kvlistValue"]).get("values"), depth + 1)
    return None


def _attrs(items: Any, depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in _list(items):
        kv = _dict(kv)
        key = kv.get("key")
        if isinstance(key, str):
            out[key] = _any_value(kv.get("value"), depth)
    return out


def _timestamp(record: dict[str, Any]) -> str | None:
    for field in ("timeUnixNano", "observedTimeUnixNano"):
        raw: Any = record.get(field)
        try:
            nanos = int(raw)  # OTLP/JSON encodes 64-bit integers as strings
        except (TypeError, ValueError):
            continue
        if nanos <= 0:
            continue
        try:
            seconds, rest = divmod(nanos, 1_000_000_000)
            moment = _EPOCH + timedelta(seconds=seconds, microseconds=rest // 1000)
        except OverflowError:
            return "invalid"
        return moment.isoformat().replace("+00:00", "Z")
    return None


def _severity(record: dict[str, Any]) -> str | None:
    text = record.get("severityText")
    if isinstance(text, str) and text.strip():
        return text
    number = record.get("severityNumber")
    if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= 24:
        return next(name for limit, name in _SEVERITY_RANGES if number <= limit)
    return None


class OtlpLogsAdapter:
    name: ClassVar[str] = "otlp-logs"

    def parse(self, body: Any, ctx: AdapterContext) -> list[AdapterItem]:
        resource_logs = _dict(body).get("resourceLogs")
        if not isinstance(resource_logs, list):
            raise AdapterFormatError("invalid_envelope")
        total = sum(
            len(_list(_dict(scope).get("logRecords")))
            for rl in resource_logs
            for scope in _list(_dict(rl).get("scopeLogs"))
        )
        if total == 0:
            raise AdapterFormatError("invalid_envelope")
        if total > ctx.max_events:
            raise AdapterFormatError("too_many_events")

        items: list[AdapterItem] = []
        for rl in resource_logs:
            res = _attrs(_dict(_dict(rl).get("resource")).get("attributes"))
            for scope in _list(_dict(rl).get("scopeLogs")):
                for rec in _list(_dict(scope).get("logRecords")):
                    items.append(self._record(rec, res, ctx))
        return items

    @staticmethod
    def _record(rec: Any, res: dict[str, Any], ctx: AdapterContext) -> AdapterItem:
        if not isinstance(rec, dict):
            return AdapterItem(rejection=Rejected("invalid_event"))
        attrs = _attrs(rec.get("attributes"))
        service = res.get("service.name")
        environment = res.get("deployment.environment") or res.get("deployment.environment.name")
        timestamp = _timestamp(rec)
        severity = _severity(rec)
        message = _any_value(rec.get("body"))
        if not isinstance(message, str):
            message = attrs.get("exception.message")
        for value, name in (
            (service, "service"),
            (environment, "environment"),
            (timestamp, "timestamp"),
            (severity, "severity"),
            (message, "message"),
        ):
            if not isinstance(value, str) or not value:
                return AdapterItem(rejection=Rejected("missing_field", name))
        if timestamp == "invalid":
            return AdapterItem(rejection=Rejected("invalid_timestamp", "timestamp"))

        event: dict[str, Any] = {
            "project_id": ctx.project_id,
            "service": service,
            "environment": environment,
            "timestamp": timestamp,
            "severity": severity,
            "message": message,
        }
        for key, source in (
            ("error_type", attrs.pop("exception.type", None)),
            ("stack_trace", attrs.pop("exception.stacktrace", None)),
            ("trace_id", rec.get("traceId") or None),
            ("span_id", rec.get("spanId") or None),
            ("release", res.get("service.version")),
        ):
            if source is not None:
                event[key] = source
        attrs.pop("exception.message", None)
        event["attributes"] = {k: v for k, v in attrs.items() if v is not None}
        return AdapterItem(event=event)
