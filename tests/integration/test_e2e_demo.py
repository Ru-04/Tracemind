"""The M1 deliverable, end to end: trigger a known error in the demo app, ship its log line to
TraceMind, and see the normalized event and its incident stored."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tracemind.api.app import create_app
from tracemind.config import Settings
from tracemind.projects import ConfigProjectRegistry

from demo_app.main import app as demo_app
from demo_app.scenarios import SCENARIOS
from tests.conftest import NOW
from tools import ship_logs

pytestmark = pytest.mark.integration


@pytest.fixture
def key() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def tracemind(store, tmp_path: Path, key: str) -> TestClient:
    path = tmp_path / "projects.json"
    path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "project_id": "project-demo",
                        "allowed_services": ["demo-payments"],
                        "ingest_key_env": "K",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    registry = ConfigProjectRegistry.from_file(path, {"K": key})
    # `now` sits just after the demo's real wall-clock events so none is "in the future".
    from datetime import UTC, datetime

    app = create_app(
        settings=Settings(),
        registry=registry,
        store=store,
        clock=lambda: max(NOW, datetime.now(UTC)),
    )
    return TestClient(app, raise_server_exceptions=False, base_url="http://tracemind.test")


@pytest.fixture(autouse=True)
def _clean_demo_env(monkeypatch):
    for name in ("DEMO_COMMIT_SHA", "DEMO_RELEASE", "DEMO_PAYMENT_GATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)


def trigger(route: str, capsys) -> list[str]:
    TestClient(demo_app, raise_server_exceptions=False).get(route)
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def test_known_error_becomes_a_stored_normalized_event_and_open_incident(
    tracemind, key, capsys
) -> None:
    lines = trigger("/demo/controlled-exception", capsys)
    summary = ship_logs.ship(lines, client=tracemind, key=key)
    assert (summary.accepted, summary.rejected, summary.failed_requests) == (1, 0, 0)

    h = {"X-TraceMind-Key": key}
    incidents = tracemind.get("/api/v1/incidents", headers=h).json()["items"]
    assert len(incidents) == 1
    incident = tracemind.get(f"/api/v1/incidents/{incidents[0]['incident_id']}", headers=h).json()
    assert incident["status"] == "open" and incident["occurrence_count"] == 1
    assert incident["summary"] == "InvoiceError: Invoice total cannot be negative: <num>"
    event_id = incident["evidence_ids"][0].removeprefix("event:")
    event = tracemind.get(f"/api/v1/events/{event_id}", headers=h).json()
    assert event["error_type"] == "InvoiceError" and event["severity"] == "ERROR"
    assert event["service"] == "demo-payments" and event["source"] == "tracemind-json"
    assert event["trace_id"] and len(event["trace_id"]) == 32
    assert event["commit_sha"] is None  # nothing was invented
    assert event["fingerprint"] == incident["fingerprint"]


def test_full_demo_script_grouping_dedup_and_distinct_fingerprints(tracemind, key, capsys) -> None:
    h = {"X-TraceMind-Key": key}
    first = trigger(SCENARIOS[0].route, capsys)
    ship_logs.ship(first, client=tracemind, key=key)

    again = trigger(SCENARIOS[0].route, capsys)  # a second occurrence of the same failure
    assert again != first  # different event id, trace id and timestamp...
    ship_logs.ship(again, client=tracemind, key=key)

    redelivered = ship_logs.ship(first, client=tracemind, key=key)  # ...and a re-shipped log line
    assert (redelivered.accepted, redelivered.duplicate) == (0, 1)

    for scenario in SCENARIOS[1:]:
        ship_logs.ship(trigger(scenario.route, capsys), client=tracemind, key=key)

    items = tracemind.get("/api/v1/incidents", headers=h).json()["items"]
    assert len(items) == 4  # S1–S4 give four distinct fingerprints
    assert len({i["fingerprint"] for i in items}) == 4
    by_summary = {i["summary"].split(":")[0]: i for i in items}
    assert by_summary["InvoiceError"]["occurrence_count"] == 2  # grouped despite new ids/times
    assert all(
        i["occurrence_count"] == 1 for name, i in by_summary.items() if name != "InvoiceError"
    )
    health = tracemind.get("/api/v1/ingestion/health", headers=h).json()
    assert (health["accepted"], health["duplicate"], health["rejected"]) == (5, 1, 0)


def test_fingerprints_do_not_depend_on_trace_ids_or_machine_paths(tracemind, key, capsys) -> None:
    for _ in range(3):
        ship_logs.ship(trigger("/demo/regression", capsys), client=tracemind, key=key)
    items = tracemind.get("/api/v1/incidents", headers={"X-TraceMind-Key": key}).json()["items"]
    assert len(items) == 1 and items[0]["occurrence_count"] == 3
