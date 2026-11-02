"""M1-12: a real adapter format flows through the same API and pipeline, and a *new* adapter can
be registered without touching the pipeline."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from tracemind.api.app import create_app, default_adapters
from tracemind.config import Settings
from tracemind.errors import Rejected
from tracemind.ingestion.adapters import AdapterContext, AdapterFormatError, AdapterItem
from tracemind.projects import ConfigProjectRegistry

from tests.conftest import NOW
from tests.unit.test_adapter_contract import otlp_body, otlp_record

pytestmark = pytest.mark.integration


@pytest.fixture
def key() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def registry(tmp_path: Path, key: str) -> ConfigProjectRegistry:
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "project_id": "project-demo",
                        "allowed_services": ["payments-api"],
                        "ingest_key_env": "K",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return ConfigProjectRegistry.from_file(path, {"K": key})


def test_otlp_logs_are_ingested_end_to_end(store, registry, key) -> None:
    app = create_app(settings=Settings(), registry=registry, store=store, clock=lambda: NOW)
    client = TestClient(app, raise_server_exceptions=False)
    h = {"X-TraceMind-Key": key}
    r = client.post(
        "/api/v1/telemetry/events?format=otlp-logs", json=otlp_body(otlp_record()), headers=h
    )
    assert r.status_code == 202, r.text
    res = r.json()["results"][0]
    stored = client.get(f"/api/v1/events/{res['event_id']}", headers=h).json()
    assert stored["project_id"] == "project-demo"  # taken from the authenticated key
    assert stored["error_type"] == "ConnectionError"
    assert stored["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert stored["attributes"] == {"retry_count": 3}
    assert stored["source"] == "otlp-logs"
    assert client.get(f"/api/v1/incidents/{res['incident_id']}", headers=h).status_code == 200
    # Resending the same OTLP record is deduplicated through the derived event id.
    again = client.post(
        "/api/v1/telemetry/events?format=otlp-logs", json=otlp_body(otlp_record()), headers=h
    )
    assert again.json()["results"][0]["status"] == "duplicate"


def test_otlp_envelope_errors_use_the_error_envelope(store, registry, key) -> None:
    app = create_app(settings=Settings(), registry=registry, store=store, clock=lambda: NOW)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/api/v1/telemetry/events?format=otlp-logs",
        json={"resourceLogs": 1},
        headers={"X-TraceMind-Key": key},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_envelope"


class LineProtocolAdapter:
    """A new source added purely by registration: `severity|service|env|iso-time|message` lines."""

    name: ClassVar[str] = "pipe-lines"

    def parse(self, body: Any, ctx: AdapterContext) -> list[AdapterItem]:
        if not isinstance(body, dict) or not isinstance(body.get("lines"), list):
            raise AdapterFormatError("invalid_envelope")
        if len(body["lines"]) > ctx.max_events:
            raise AdapterFormatError("too_many_events")
        items: list[AdapterItem] = []
        for line in body["lines"]:
            parts = line.split("|", 4) if isinstance(line, str) else []
            if len(parts) != 5:
                items.append(AdapterItem(rejection=Rejected("invalid_event")))
                continue
            sev, svc, env, ts, msg = parts
            items.append(
                AdapterItem(
                    event={
                        "project_id": ctx.project_id,
                        "service": svc,
                        "environment": env,
                        "timestamp": ts,
                        "severity": sev,
                        "message": msg,
                    }
                )
            )
        return items


def test_a_new_adapter_works_by_registration_alone(store, registry, key) -> None:
    adapters = default_adapters()
    adapters.register(LineProtocolAdapter())
    app = create_app(
        settings=Settings(), registry=registry, store=store, adapters=adapters, clock=lambda: NOW
    )
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/api/v1/telemetry/events?format=pipe-lines",
        json={
            "lines": ["error|payments-api|development|2026-10-28T10:00:00Z|disk full", "bad line"]
        },
        headers={"X-TraceMind-Key": key},
    )
    assert r.status_code == 202
    assert [x["status"] for x in r.json()["results"]] == ["accepted", "rejected"]
    event_id = r.json()["results"][0]["event_id"]
    assert (
        client.get(f"/api/v1/events/{event_id}", headers={"X-TraceMind-Key": key}).json()["source"]
        == "pipe-lines"
    )
