"""M1-1 (demo JSON logs), synthetic events, and the shipper, without needing a database."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient
from tracemind.ingestion.models import validate_ingest
from tracemind.ingestion.normalize import prepare_event

from demo_app import telemetry
from demo_app.main import app
from demo_app.scenarios import SCENARIOS
from tests.conftest import NOW, ROOT
from tools import ship_logs, synthetic_events

INGEST_SCHEMA = json.loads(
    (ROOT / "docs/contracts/event-ingest.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = jsonschema.Draft202012Validator(
    INGEST_SCHEMA, format_checker=jsonschema.FormatChecker()
)


@pytest.fixture(autouse=True)
def _demo_env(monkeypatch):
    for name in ("DEMO_COMMIT_SHA", "DEMO_RELEASE", "DEMO_PAYMENT_GATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def demo() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def stdout_events(capsys) -> list[dict[str, Any]]:
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ---- M1-1 --------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_each_failure_emits_exactly_one_valid_json_line(scenario, demo, capsys) -> None:
    response = demo.get(scenario.route)
    events = stdout_events(capsys)
    assert response.status_code == 500 and len(events) == 1
    event = events[0]
    VALIDATOR.validate(event)
    assert event["error_type"] == scenario.expected_error_type
    assert event["severity"] == "ERROR"
    assert event["trace_id"] == response.headers["X-Trace-Id"]
    assert len(event["trace_id"]) == 32
    assert "commit_sha" not in event and "release" not in event and "span_id" not in event
    assert "Traceback (most recent call last)" in event["stack_trace"]
    assert str(ROOT) not in event["stack_trace"]  # repo-relative, no machine paths
    assert "demo_app/scenarios.py" in event["stack_trace"]


def test_emitted_event_passes_the_real_pipeline_validation(
    demo, capsys, project, settings, redactor
) -> None:
    demo.get("/demo/controlled-exception")
    raw = stdout_events(capsys)[0]
    prepared = prepare_event(
        validate_ingest(raw),
        project,
        now=datetime.now(UTC),
        settings=settings,
        redactor=redactor,
    )
    assert prepared.event.error_type == "InvoiceError" and prepared.incident_eligible


def test_valid_traceparent_is_honoured_and_invalid_is_replaced(demo, capsys) -> None:
    good = "4bf92f3577b34da6a3ce929d0e0e4736"
    demo.get("/demo/dependency-timeout", headers={"traceparent": f"00-{good}-00f067aa0ba902b7-01"})
    demo.get("/demo/dependency-timeout", headers={"traceparent": "garbage"})
    demo.get(
        "/demo/dependency-timeout", headers={"traceparent": f"00-{'0' * 32}-00f067aa0ba902b7-01"}
    )
    a, b, c = stdout_events(capsys)
    assert a["trace_id"] == good
    assert b["trace_id"] != "garbage" and len(b["trace_id"]) == 32
    assert c["trace_id"] != "0" * 32


def test_release_and_commit_are_only_emitted_when_provided_and_valid(
    demo, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("DEMO_RELEASE", "1.2.0")
    monkeypatch.setenv("DEMO_COMMIT_SHA", "ABCDEF1234567")
    demo.get("/demo/controlled-exception")
    monkeypatch.setenv("DEMO_COMMIT_SHA", "not-a-sha")
    demo.get("/demo/controlled-exception")
    with_sha, bad_sha = stdout_events(capsys)
    assert (with_sha["release"], with_sha["commit_sha"]) == ("1.2.0", "abcdef1234567")
    assert "commit_sha" not in bad_sha


def test_response_body_stays_generic(demo) -> None:
    r = demo.get("/demo/regression")
    assert r.json() == {"error": "internal_error"} and "Zero" not in r.text


def test_build_event_is_pure_given_its_inputs() -> None:
    now = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        e = telemetry.build_event(exc, trace_id="a" * 32, env={}, method="GET", path="/x", now=now)
    assert e["timestamp"] == "2026-10-28T10:00:00Z"
    assert (e["project_id"], e["service"], e["environment"]) == (
        "project-demo",
        "demo-payments",
        "development",
    )


# ---- synthetic events --------------------------------------------------------------------


def test_synthetic_events_are_deterministic_and_seed_sensitive() -> None:
    a = synthetic_events.valid_error_events(30, seed=7)
    assert a == synthetic_events.valid_error_events(30, seed=7)
    assert a != synthetic_events.valid_error_events(30, seed=8)
    for event in a:
        VALIDATOR.validate(event)


def test_synthetic_valid_events_group_into_exactly_three_incidents(
    project, settings, redactor
) -> None:
    fingerprints = {
        prepare_event(
            validate_ingest(e), project, now=NOW, settings=settings, redactor=redactor
        ).event.fingerprint
        for e in synthetic_events.valid_error_events(60, seed=3)
    }
    assert len(fingerprints) == len(synthetic_events.FAILURES) == 3


@pytest.mark.parametrize(
    ("name", "event", "reason"), synthetic_events.malformed_events(), ids=lambda v: str(v)[:30]
)
def test_malformed_synthetic_events_are_rejected_with_the_expected_reason(
    name, event, reason, project, settings, redactor
) -> None:
    from tracemind.errors import Rejected

    with pytest.raises(Rejected) as info:
        prepare_event(
            validate_ingest(event), project, now=NOW, settings=settings, redactor=redactor
        )
    assert info.value.reason == reason, name


def test_synthetic_secret_event_is_fully_redacted(project, settings, redactor) -> None:
    ev = prepare_event(
        validate_ingest(synthetic_events.secret_bearing_event()),
        project,
        now=NOW,
        settings=settings,
        redactor=redactor,
    ).event
    dumped = ev.model_dump_json()
    for needle in ("hunter2", "jane.doe", "q9w8e7r6t5y4", "pw123456", "not-a-real-key"):
        assert needle not in dumped


def test_synthetic_cli_prints_jsonl(capsys) -> None:
    assert synthetic_events.main(["--kind", "valid", "--count", "3", "--seed", "1"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3 and all(json.loads(ln)["project_id"] for ln in lines)


# ---- shipper -----------------------------------------------------------------------------


def mock_client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://tracemind.test", transport=httpx.MockTransport(handler))


def test_shipper_batches_counts_and_skips_non_json(make_key) -> None:
    seen: list[tuple[str | None, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events = json.loads(request.content)["events"]
        seen.append((request.headers.get("x-tracemind-key"), len(events)))
        results = [{"index": i, "status": "accepted", "event_id": "e"} for i in range(len(events))]
        results[0] = {"index": 0, "status": "duplicate", "event_id": "e"}
        return httpx.Response(202, json={"results": results})

    lines = [
        "plain text line",
        "",
        json.dumps({"a": 1}),
        json.dumps({"b": 2}),
        "[1,2]",
        json.dumps({"c": 3}),
    ]
    summary = ship_logs.ship(lines, client=mock_client(handler), key=make_key, batch_size=2)
    assert seen == [(make_key, 2), (make_key, 1)]
    assert (summary.read, summary.skipped_non_events) == (3, 2)
    assert (summary.accepted, summary.duplicate, summary.rejected) == (1, 2, 0)


def test_shipper_reports_rejections_without_values(make_key) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"results": [{"index": 0, "status": "rejected", "reason": "invalid_severity"}]},
        )

    summary = ship_logs.ship([json.dumps({"x": 1})], client=mock_client(handler), key=make_key)
    assert summary.rejected == 1 and summary.rejection_reasons == {"invalid_severity": 1}


def test_shipper_stops_on_auth_failure_and_never_leaks_the_key(make_key, capsys) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"code": "unauthorized"}})

    lines = [json.dumps({"i": i}) for i in range(5)]
    summary = ship_logs.ship(lines, client=mock_client(handler), key=make_key, batch_size=1)
    assert calls == 1 and summary.failed_requests == 1 and summary.last_error == "unauthorized"
    assert make_key not in json.dumps(summary.__dict__)


def test_shipper_survives_network_errors(make_key) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    summary = ship_logs.ship([json.dumps({"x": 1})], client=mock_client(handler), key=make_key)
    assert summary.failed_requests == 1 and summary.last_error == "ConnectError"


def test_shipper_cli_requires_a_key_and_never_takes_it_as_an_argument(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TRACEMIND_INGEST_KEY", raising=False)
    assert ship_logs.main([]) == 2
    assert "TRACEMIND_INGEST_KEY" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        ship_logs.main(["--key", "abc"])
