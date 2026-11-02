"""The adapter contract (see tracemind/ingestion/adapters.py), enforced for EVERY adapter.

Adding a new adapter means adding it to ADAPTERS and a valid sample to VALID; it must then pass
the same contract tests unchanged."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import copy
import json
import random
import re
from datetime import UTC, datetime
from typing import Any

import jsonschema
import pytest
from tracemind.errors import Rejected
from tracemind.ingestion.adapters import (
    AdapterContext,
    AdapterFormatError,
    AdapterItem,
    AdapterRegistry,
    EventAdapter,
)
from tracemind.ingestion.models import validate_ingest
from tracemind.ingestion.native import NativeJsonAdapter
from tracemind.ingestion.normalize import prepare_event
from tracemind.ingestion.otlp import OtlpLogsAdapter

from tests.conftest import NOW, ROOT

CTX = AdapterContext(project_id="project-demo", max_events=5)
ADAPTERS: list[EventAdapter] = [NativeJsonAdapter(), OtlpLogsAdapter()]
NANOS = str(int(datetime(2026, 10, 28, 10, 15, 30, tzinfo=UTC).timestamp()) * 1_000_000_000)


def _sv(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def otlp_record(**over: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "timeUnixNano": NANOS,
        "severityNumber": 17,
        "severityText": "ERROR",
        "body": {"stringValue": "Database connection failed"},
        "attributes": [
            _sv("exception.type", "ConnectionError"),
            {"key": "retry_count", "value": {"intValue": "3"}},
        ],
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
        "spanId": "00f067aa0ba902b7",
    }
    rec.update(over)
    return rec


def otlp_body(
    *records: dict[str, Any], resource: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    res = resource or [
        _sv("service.name", "payments-api"),
        _sv("deployment.environment", "development"),
        _sv("service.version", "1.2.0"),
    ]
    return {
        "resourceLogs": [
            {"resource": {"attributes": res}, "scopeLogs": [{"logRecords": list(records)}]}
        ]
    }


NATIVE_EVENT = {
    "project_id": "project-demo",
    "service": "payments-api",
    "environment": "development",
    "timestamp": "2026-10-28T10:15:30Z",
    "severity": "ERROR",
    "message": "Database connection failed",
}
VALID: dict[str, Any] = {
    "tracemind-json": {"events": [NATIVE_EVENT, NATIVE_EVENT]},
    "otlp-logs": otlp_body(otlp_record(), otlp_record()),
}


def too_many(name: str) -> Any:
    n = CTX.max_events + 1
    if name == "tracemind-json":
        return {"events": [NATIVE_EVENT] * n}
    return otlp_body(*[otlp_record() for _ in range(n)])


def random_json(rng: random.Random, depth: int = 0) -> Any:
    kinds = ["int", "str", "none", "bool", "float"] + (["list", "dict"] if depth < 5 else [])
    kind = rng.choice(kinds)
    keys = [
        "events",
        "resourceLogs",
        "scopeLogs",
        "logRecords",
        "attributes",
        "key",
        "value",
        "body",
        "stringValue",
        "intValue",
        "timeUnixNano",
        "severityNumber",
        "resource",
    ]
    if kind == "int":
        return rng.choice([0, -1, 17, 10**30, 2**63])
    if kind == "str":
        return rng.choice(["", "x", "9" * 40, "-1", "ERROR", "\x00", "é" * 10])
    if kind == "none":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "float":
        return rng.choice([0.0, 1e308, -2.5])
    if kind == "list":
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(keys): random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))}


GARBAGE: list[Any] = [
    None,
    1,
    "x",
    True,
    [],
    {},
    [1, 2],
    {"events": None},
    {"events": {}},
    {"resourceLogs": None},
    {"resourceLogs": []},
    {"resourceLogs": [None, 1, "x", []]},
    {"resourceLogs": [{"scopeLogs": [{"logRecords": [None, 1, {}, {"timeUnixNano": "9" * 40}]}]}]},
    {"resourceLogs": [{"resource": {"attributes": "nope"}, "scopeLogs": "nope"}]},
    {
        "resourceLogs": [
            {"scopeLogs": [{"logRecords": [{"body": {"kvlistValue": {"values": [{"key": 1}]}}}]}]}
        ]
    },
]


def run(adapter: EventAdapter, body: Any, ctx: AdapterContext = CTX) -> list[AdapterItem] | str:
    """Returns the items, or the AdapterFormatError code. Anything else escapes and fails."""
    try:
        return adapter.parse(body, ctx)
    except AdapterFormatError as exc:
        return exc.code


@pytest.fixture(params=ADAPTERS, ids=lambda a: a.name)
def adapter(request) -> EventAdapter:
    return request.param  # type: ignore[no-any-return]


def test_names_are_unique_lowercase_identifiers() -> None:
    names = [a.name for a in ADAPTERS]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z][a-z0-9-]{1,31}", n) for n in names)
    assert set(names) == set(VALID)


def test_registry_rejects_duplicate_names_and_looks_up_by_name() -> None:
    registry = AdapterRegistry(list(ADAPTERS))
    assert registry.get("otlp-logs") is not None and registry.get("nope") is None
    with pytest.raises(ValueError):
        registry.register(NativeJsonAdapter())


def test_parse_is_deterministic_and_does_not_mutate_input(adapter) -> None:
    body = VALID[adapter.name]
    before = copy.deepcopy(body)
    first, second = run(adapter, body), run(adapter, body)
    assert body == before
    assert isinstance(first, list) and first == second


def test_valid_samples_flow_through_the_unchanged_pipeline_functions(
    adapter, project, settings, redactor
) -> None:
    schema = json.loads((ROOT / "docs/contracts/event-ingest.schema.json").read_text("utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    items = run(adapter, VALID[adapter.name])
    assert isinstance(items, list) and len(items) == 2
    for item in items:
        assert item.event is not None and item.rejection is None
        validator.validate(item.event)  # contract rule 5: the event-ingest shape
        prepared = prepare_event(
            validate_ingest(item.event), project, now=NOW, settings=settings, redactor=redactor
        )
        assert prepared.event.project_id == "project-demo"
        assert prepared.event.message == "Database connection failed"


@pytest.mark.parametrize("garbage", GARBAGE, ids=lambda g: repr(g)[:40])
def test_hostile_input_only_raises_adapter_format_error(adapter, garbage) -> None:
    out = run(adapter, garbage)
    assert isinstance(out, str) or all(isinstance(i, AdapterItem) for i in out)


def test_seeded_fuzz_never_escapes_the_contract(adapter) -> None:
    rng = random.Random(1234)
    for _ in range(400):
        body = random_json(rng)
        snapshot = copy.deepcopy(body)
        out = run(adapter, body)
        assert body == snapshot
        if not isinstance(out, str):
            assert len(out) <= CTX.max_events
            for item in out:
                assert (item.event is None) != (item.rejection is None)


def test_item_limit_is_enforced(adapter) -> None:
    assert run(adapter, too_many(adapter.name)) == "too_many_events"


def test_items_are_either_an_event_or_a_rejection_never_both(adapter) -> None:
    body = copy.deepcopy(VALID[adapter.name])
    out = run(adapter, body)
    assert isinstance(out, list)
    for item in out:
        assert (item.event is None) != (item.rejection is None)
        if item.rejection:
            assert isinstance(item.rejection, Rejected)


# ---- OTLP-specific behaviour -------------------------------------------------------------


def parse_one(record: dict[str, Any], **kw: Any) -> AdapterItem:
    out = run(OtlpLogsAdapter(), otlp_body(record, **kw))
    assert isinstance(out, list)
    return out[0]


def test_otlp_maps_fields_and_never_invents_values() -> None:
    event = parse_one(otlp_record(traceId="", spanId="")).event
    assert event is not None
    assert event["project_id"] == "project-demo"  # from the authenticated context
    assert event["service"] == "payments-api" and event["environment"] == "development"
    assert event["timestamp"] == "2026-10-28T10:15:30Z"
    assert event["error_type"] == "ConnectionError" and event["release"] == "1.2.0"
    assert event["attributes"] == {"retry_count": 3}
    assert "trace_id" not in event and "span_id" not in event
    assert "commit_sha" not in event and "stack_trace" not in event


def test_otlp_severity_number_is_used_only_when_text_is_absent() -> None:
    assert parse_one(otlp_record(severityText="")).event["severity"] == "ERROR"  # type: ignore[index]
    assert parse_one(otlp_record(severityText="", severityNumber=21)).event["severity"] == "FATAL"  # type: ignore[index]
    assert parse_one(otlp_record(severityText="", severityNumber=9)).event["severity"] == "INFO"  # type: ignore[index]


@pytest.mark.parametrize(
    ("record", "resource", "field"),
    [
        (otlp_record(timeUnixNano="0", observedTimeUnixNano="0"), None, "timestamp"),
        (otlp_record(severityText="", severityNumber=0), None, "severity"),
        (otlp_record(body={}), None, "message"),
        (otlp_record(), [_sv("deployment.environment", "dev")], "service"),
        (otlp_record(), [_sv("service.name", "payments-api")], "environment"),
    ],
)
def test_otlp_missing_required_values_are_rejected_not_defaulted(record, resource, field) -> None:
    item = parse_one(record, resource=resource)
    assert item.event is None and item.rejection is not None
    assert (item.rejection.reason, item.rejection.field) == ("missing_field", field)


def test_otlp_observed_time_is_a_fallback_only_when_event_time_is_missing() -> None:
    rec = otlp_record(timeUnixNano="0", observedTimeUnixNano=NANOS)
    assert parse_one(rec).event["timestamp"] == "2026-10-28T10:15:30Z"  # type: ignore[index]


def test_otlp_exception_attributes_become_fields_and_overflow_is_a_rejection() -> None:
    rec = otlp_record(
        attributes=[_sv("exception.type", "ValueError"), _sv("exception.stacktrace", "tb text")]
    )
    event = parse_one(rec).event
    assert event is not None
    assert event["stack_trace"] == "tb text" and event["attributes"] == {}
    overflow = parse_one(otlp_record(timeUnixNano="9" * 40))
    assert overflow.rejection is not None and overflow.rejection.reason == "invalid_timestamp"


def test_rejections_never_contain_input_values() -> None:
    marker = "NEVER-ECHO-ME"
    item = parse_one(otlp_record(body={"stringValue": ""}, attributes=[_sv("x", marker)]))
    assert item.rejection is not None
    assert marker not in repr(item.rejection.__dict__)
