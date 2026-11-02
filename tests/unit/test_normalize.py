# mypy: disable-error-code="no-untyped-def"
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tracemind.config import Settings
from tracemind.errors import Rejected
from tracemind.ingestion.models import validate_ingest
from tracemind.ingestion.normalize import normalize_severity, prepare_event
from tracemind.ingestion.redact import Redactor
from tracemind.projects import Project

from tests.conftest import NOW


def prep(raw: dict[str, Any], project: Project, settings: Settings, redactor: Redactor):
    return prepare_event(
        validate_ingest(raw), project, now=NOW, settings=settings, redactor=redactor
    )


def test_valid_event_is_normalized(raw_event, project, settings, redactor) -> None:
    raw_event.update(
        service=" Payments-API ",
        environment="Development",
        severity="error",
        timestamp="2026-10-28T12:15:30+02:00",
        trace_id="4BF92F3577B34DA6A3CE929D0E0E4736",
    )
    ev = prep(raw_event, project, settings, redactor).event
    assert ev.service == "payments-api"
    assert ev.environment == "development"
    assert ev.severity == "ERROR"
    assert ev.occurred_at == datetime(2026, 10, 28, 10, 15, 30, tzinfo=UTC)
    assert ev.occurred_at.utcoffset() == timedelta(0)
    assert ev.received_at == NOW
    assert ev.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ev.span_id is None


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("warn", "WARNING"),
        ("FATAL", "CRITICAL"),
        ("Trace", "DEBUG"),
        ("err", "ERROR"),
        ("notice", "INFO"),
    ],
)
def test_severity_aliases(alias, expected, raw_event, project, settings, redactor) -> None:
    raw_event["severity"] = alias
    assert prep(raw_event, project, settings, redactor).event.severity == expected


def test_fractional_nanoseconds_accepted(raw_event, project, settings, redactor) -> None:
    raw_event["timestamp"] = "2026-10-28T10:15:30.123456789Z"
    assert prep(raw_event, project, settings, redactor).event.occurred_at.year == 2026


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"timestamp": "2026-10-28T10:15:30"}, "invalid_timestamp"),  # no offset
        ({"timestamp": "yesterday"}, "invalid_timestamp"),
        ({"timestamp": "2026-13-45T10:15:30Z"}, "invalid_timestamp"),
        ({"timestamp": "2026-10-28T12:30:00Z"}, "timestamp_in_future"),
        ({"timestamp": "0001-01-01T00:00:00+05:00"}, "invalid_timestamp"),  # overflows in UTC
        ({"timestamp": "9999-12-31T23:59:59-05:00"}, "invalid_timestamp"),
        ({"message": "bad \x00 byte"}, "invalid_characters"),
        ({"message": "lone surrogate \ud800"}, "invalid_message"),  # pydantic's str check
        ({"attributes": {"k": "lone surrogate \ud800"}}, "invalid_characters"),
        ({"attributes": {"k": ["ok", "nul \x00"]}}, "invalid_characters"),
        ({"attributes": {"nul\x00key": 1}}, "invalid_characters"),
        ({"severity": "LOUD"}, "invalid_severity"),
        ({"service": "Pay Ments"}, "invalid_service"),
        ({"service": "other-service"}, "unknown_service"),
        ({"environment": "../prod"}, "invalid_environment"),
        ({"trace_id": "xyz"}, "invalid_trace_id"),
        ({"trace_id": "0" * 32}, "invalid_trace_id"),
        ({"span_id": "abcd"}, "invalid_span_id"),
        ({"commit_sha": "not-a-sha"}, "invalid_commit_sha"),
        ({"event_id": "has space"}, "invalid_event_id"),
        ({"error_type": "Bad\nType"}, "invalid_error_type"),
        ({"project_id": "project-other"}, "project_mismatch"),
        ({"attributes": {f"k{i}": i for i in range(65)}}, "attributes_too_large"),
        ({"attributes": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}}, "attributes_too_deep"),
        ({"attributes": {"blob": "x" * 20000}}, "attributes_too_large"),
    ],
)
def test_invalid_values_are_rejected_not_coerced(
    patch, reason, raw_event, project, settings, redactor
) -> None:
    raw_event.update(patch)
    with pytest.raises(Rejected) as info:
        prep(raw_event, project, settings, redactor)
    assert info.value.reason == reason


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"message": ""}, "invalid_message"),
        ({"message": "x" * 9000}, "message_too_long"),
        ({"surprise": 1}, "unknown_field"),
        ({"severity": 5}, "invalid_severity"),
        ({"attributes": "nope"}, "invalid_attributes"),
    ],
)
def test_schema_level_rejections(patch, reason, raw_event) -> None:
    raw_event.update(patch)
    with pytest.raises(Rejected) as info:
        validate_ingest(raw_event)
    assert info.value.reason == reason


def test_missing_required_field_names_the_field(raw_event) -> None:
    del raw_event["message"]
    with pytest.raises(Rejected) as info:
        validate_ingest(raw_event)
    assert (info.value.reason, info.value.field) == ("missing_field", "message")


def test_non_object_event_is_rejected() -> None:
    with pytest.raises(Rejected) as info:
        validate_ingest("a string")
    assert info.value.reason == "invalid_event"


def test_rejection_never_echoes_input() -> None:
    with pytest.raises(Rejected) as info:
        normalize_severity("super-secret-value-123")
    assert "super-secret" not in str(info.value) + info.value.reason


def test_absent_correlation_fields_stay_null(raw_event, project, settings, redactor) -> None:
    ev = prep(raw_event, project, settings, redactor).event
    assert (ev.trace_id, ev.span_id, ev.commit_sha, ev.release) == (None, None, None, None)


def test_error_type_is_never_guessed(raw_event, project, settings, redactor) -> None:
    del raw_event["error_type"]
    raw_event["message"] = "ValueError: boom"
    assert prep(raw_event, project, settings, redactor).event.error_type is None


def test_derived_event_id_is_stable_and_identity_sensitive(
    raw_event, project, settings, redactor
) -> None:
    a = prep(raw_event, project, settings, redactor).event.event_id
    b = prep(dict(raw_event), project, settings, redactor).event.event_id
    raw_event["message"] = "Different failure"
    c = prep(raw_event, project, settings, redactor).event.event_id
    assert a == b != c
    assert a.startswith("derived-")


def test_client_event_id_is_kept(raw_event, project, settings, redactor) -> None:
    raw_event["event_id"] = "evt-42"
    assert prep(raw_event, project, settings, redactor).event.event_id == "evt-42"


@pytest.mark.parametrize(
    ("severity", "eligible"),
    [("DEBUG", False), ("INFO", False), ("WARNING", False), ("ERROR", True), ("CRITICAL", True)],
)
def test_incident_eligibility(severity, eligible, raw_event, project, settings, redactor) -> None:
    raw_event["severity"] = severity
    assert prep(raw_event, project, settings, redactor).incident_eligible is eligible


def test_summary_is_deterministic_and_bounded(raw_event, project, settings, redactor) -> None:
    raw_event["message"] = "Timeout after 30 seconds " + "x" * 500
    summary = prep(raw_event, project, settings, redactor).summary
    assert summary.startswith("ConnectionError: Timeout after <num> seconds")
    assert len(summary) <= len("ConnectionError: ") + 200
