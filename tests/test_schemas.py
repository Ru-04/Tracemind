import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

CONTRACTS = Path(__file__).resolve().parent.parent / "docs" / "contracts"
FP = "a" * 64


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


VALID_INGEST: dict[str, Any] = {
    "project_id": "project-demo",
    "service": "payments-api",
    "environment": "development",
    "timestamp": "2026-10-28T10:15:30Z",
    "severity": "ERROR",
    "message": "Database connection failed",
    "error_type": "ConnectionError",
    "trace_id": None,
    "span_id": None,
    "release": "1.2.0",
    "commit_sha": None,
    "attributes": {"endpoint": "/payments", "retry_count": 3},
}

VALID_EVENT: dict[str, Any] = {
    "event_id": "evt-1",
    "project_id": "project-demo",
    "service": "payments-api",
    "environment": "development",
    "occurred_at": "2026-10-28T10:15:30Z",
    "received_at": "2026-10-28T10:15:31Z",
    "severity": "ERROR",
    "message": "Database connection failed",
    "fingerprint": FP,
    "fingerprint_version": 1,
    "ingest_status": "accepted",
    "redactions_applied": 0,
}

VALID_INCIDENT: dict[str, Any] = {
    "incident_id": "inc-1",
    "project_id": "project-demo",
    "service": "payments-api",
    "environment": "development",
    "severity": "ERROR",
    "status": "open",
    "fingerprint": FP,
    "fingerprint_version": 1,
    "first_seen": "2026-10-28T10:15:30Z",
    "last_seen": "2026-10-28T10:20:00Z",
    "occurrence_count": 3,
    "summary": "ConnectionError: Database connection failed",
    "commit_association": {"commit_sha": None, "confidence": "unknown", "source": None},
    "investigation_status": "none",
    "resolution": {"state": "none"},
}


@pytest.mark.parametrize(
    ("schema", "doc"),
    [
        ("event-ingest.schema.json", VALID_INGEST),
        ("event.schema.json", VALID_EVENT),
        ("incident.schema.json", VALID_INCIDENT),
    ],
)
def test_valid_examples_pass(schema: str, doc: dict[str, Any]) -> None:
    assert list(_validator(schema).iter_errors(doc)) == []


def test_ingest_rejects_missing_required_and_unknown_fields() -> None:
    v = _validator("event-ingest.schema.json")
    assert list(v.iter_errors({k: x for k, x in VALID_INGEST.items() if k != "message"}))
    assert list(v.iter_errors({**VALID_INGEST, "surprise": 1}))


def test_ingest_rejects_bad_timestamp_and_commit_sha() -> None:
    v = _validator("event-ingest.schema.json")
    assert list(v.iter_errors({**VALID_INGEST, "timestamp": "yesterday"}))
    assert list(v.iter_errors({**VALID_INGEST, "commit_sha": "not-a-sha"}))


def test_incident_status_and_resolution_are_independent_fields() -> None:
    v = _validator("incident.schema.json")
    closed_unverified = {**VALID_INCIDENT, "status": "resolved", "resolution": {"state": "none"}}
    assert list(v.iter_errors(closed_unverified)) == []
    assert list(v.iter_errors({**VALID_INCIDENT, "status": "verified"}))
    assert list(v.iter_errors({**VALID_INCIDENT, "resolution": {"state": "resolved"}}))


def test_incident_commit_confidence_is_restricted() -> None:
    v = _validator("incident.schema.json")
    association = {"commit_sha": "abc1234", "confidence": "likely"}
    bad = {**VALID_INCIDENT, "commit_association": association}
    assert list(v.iter_errors(bad))
