"""M1-2/3/4/8/11/12 through the HTTP API, against real PostgreSQL."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import copy
import json
import logging
import secrets
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from tracemind.api.app import create_app
from tracemind.config import Settings
from tracemind.projects import ConfigProjectRegistry
from tracemind.storage.postgres import PostgresStore

from tests.conftest import NOW, ROOT

pytestmark = pytest.mark.integration

URL = "/api/v1/telemetry/events"


@pytest.fixture
def keys() -> dict[str, str]:
    return {"alpha": secrets.token_urlsafe(32), "beta": secrets.token_urlsafe(32)}


@pytest.fixture
def registry(tmp_path: Path, keys: dict[str, str]) -> ConfigProjectRegistry:
    cfg = {
        "projects": [
            {
                "project_id": "project-demo",
                "allowed_services": ["payments-api"],
                "ingest_key_env": "K_ALPHA",
            },
            {
                "project_id": "project-other",
                "allowed_services": ["payments-api"],
                "ingest_key_env": "K_BETA",
            },
        ]
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return ConfigProjectRegistry.from_file(path, {"K_ALPHA": keys["alpha"], "K_BETA": keys["beta"]})


@pytest.fixture
def make_client(store, registry):
    def build(settings: Settings | None = None, db: Any = None) -> TestClient:
        app = create_app(
            settings=settings or Settings(),
            registry=registry,
            store=db or store,
            clock=lambda: NOW,
        )
        return TestClient(app, raise_server_exceptions=False)

    return build


@pytest.fixture
def client(make_client) -> TestClient:
    built: TestClient = make_client()
    return built


def auth(keys: dict[str, str], who: str = "alpha") -> dict[str, str]:
    return {"X-TraceMind-Key": keys[who]}


def body(*events: Any) -> dict[str, Any]:
    return {"events": [copy.deepcopy(e) for e in events]}


def table_dump(engine) -> str:
    with engine.connect() as conn:
        return " ".join(
            str(row)
            for t in ("events", "incidents", "ingestion_stats", "ingestion_rejections")
            for row in conn.execute(sa.text(f"SELECT * FROM {t}"))
        )


def validator(name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((ROOT / "docs/contracts" / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


# ---- M1-2 --------------------------------------------------------------------------------


def test_valid_event_is_accepted_stored_and_retrievable(client, keys, raw_event) -> None:
    raw_event["event_id"] = "evt-1"
    r = client.post(URL, json=body(raw_event), headers=auth(keys))
    assert r.status_code == 202
    res = r.json()["results"][0]
    assert res["status"] == "accepted" and res["event_id"] == "evt-1" and res["incident_id"]
    event = client.get("/api/v1/events/evt-1", headers=auth(keys)).json()
    validator("event.schema.json").validate(event)
    incident = client.get(f"/api/v1/incidents/{res['incident_id']}", headers=auth(keys)).json()
    validator("incident.schema.json").validate(incident)
    assert incident["evidence_ids"] == ["event:evt-1"]
    listing = client.get("/api/v1/incidents", headers=auth(keys)).json()
    assert [i["incident_id"] for i in listing["items"]] == [res["incident_id"]]


def test_incident_list_filters_and_validates_params(client, keys, raw_event) -> None:
    client.post(URL, json=body(raw_event), headers=auth(keys))
    h = auth(keys)
    assert len(client.get("/api/v1/incidents?service=Payments-API", headers=h).json()["items"]) == 1
    assert client.get("/api/v1/incidents?environment=production", headers=h).json()["items"] == []
    assert client.get("/api/v1/incidents?severity=critical", headers=h).json()["items"] == []
    assert client.get("/api/v1/incidents?severity=banana", headers=h).status_code == 422
    assert client.get("/api/v1/incidents?limit=0", headers=h).status_code == 422
    assert client.get("/api/v1/incidents?cursor=%%%", headers=h).status_code == 400


def test_unknown_event_and_incident_are_404_with_envelope(client, keys) -> None:
    for path in ("/api/v1/events/nope", "/api/v1/incidents/nope"):
        r = client.get(path, headers=auth(keys))
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# ---- M1-3 --------------------------------------------------------------------------------


def error_code(r) -> str:
    return str(r.json()["error"]["code"])


def test_bad_json_and_bad_envelopes(client, keys, raw_event) -> None:
    h = {**auth(keys), "Content-Type": "application/json"}
    assert error_code(client.post(URL, content=b"{not json", headers=h)) == "invalid_json"
    assert error_code(client.post(URL, content=b"", headers=h)) == "invalid_json"
    payloads: tuple[Any, ...] = (
        [],
        {"events": []},
        {"events": "x"},
        {"events": [raw_event], "extra": 1},
        {"nope": 1},
    )
    for payload in payloads:
        r = client.post(URL, json=payload, headers=auth(keys))
        assert r.status_code == 422 and error_code(r) == "invalid_envelope", payload


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_numbers_are_rejected_cleanly(literal, client, keys) -> None:
    raw = (
        '{"events":[{"project_id":"project-demo","service":"payments-api","environment":"d",'
        '"timestamp":"2026-10-28T10:00:00Z","severity":"ERROR","message":"m",'
        '"attributes":{"x":' + literal + "}}]}"
    )
    r = client.post(URL, content=raw, headers={**auth(keys), "Content-Type": "application/json"})
    assert r.status_code == 400 and error_code(r) == "invalid_json"


def test_unstorable_characters_reject_only_that_event(client, keys, raw_event) -> None:
    bad = dict(raw_event, event_id="nul", message="has a \u0000 byte")
    good = dict(raw_event, event_id="fine")
    r = client.post(URL, json=body(bad, good), headers=auth(keys))
    assert r.status_code == 202
    assert [x["status"] for x in r.json()["results"]] == ["rejected", "accepted"]
    assert r.json()["results"][0]["reason"] == "invalid_characters"


def test_too_many_events(client, keys, raw_event) -> None:
    r = client.post(URL, json=body(*[raw_event] * 101), headers=auth(keys))
    assert r.status_code == 422 and error_code(r) == "too_many_events"


def test_oversized_body_is_413_and_nothing_is_stored(client, keys, raw_event, engine) -> None:
    raw_event["message"] = "x" * 8000
    big = body(*[dict(raw_event, event_id=f"e{i}") for i in range(100)])
    assert len(json.dumps(big)) < 1_048_576  # a legitimate full batch is NOT too large
    huge = {"events": [raw_event], "pad": "y" * 1_100_000}
    r = client.post(URL, json=huge, headers=auth(keys))
    assert r.status_code == 413 and error_code(r) == "payload_too_large"
    chunked = client.post(
        URL,
        content=(chunk for chunk in [b'{"events":[', b"1," * 600_000, b"1]}"]),
        headers=auth(keys),
    )
    assert chunked.status_code == 413
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM events")).scalar_one() == 0


@pytest.mark.parametrize(
    ("patch", "reason", "field"),
    [
        ({"message": None}, "invalid_message", "message"),
        ({"surprise": "x"}, "unknown_field", None),
        ({"timestamp": "2026-10-28T10:15:30"}, "invalid_timestamp", "timestamp"),
        ({"timestamp": "2026-10-28T13:00:00Z"}, "timestamp_in_future", "timestamp"),
        ({"severity": "LOUD"}, "invalid_severity", "severity"),
        ({"trace_id": "nothex"}, "invalid_trace_id", "trace_id"),
        ({"span_id": "nothex"}, "invalid_span_id", "span_id"),
        ({"commit_sha": "zzz"}, "invalid_commit_sha", "commit_sha"),
        ({"service": "unregistered"}, "unknown_service", "service"),
        ({"message": "x" * 9000}, "message_too_long", "message"),
    ],
)
def test_per_event_rejection_reasons_and_no_echo(
    patch, reason, field, client, keys, raw_event, engine
) -> None:
    marker = "ZZ-NEVER-ECHO-ZZ"
    raw_event.update(patch)
    if "surprise" in patch:
        raw_event["surprise"] = marker
    r = client.post(URL, json=body(raw_event), headers=auth(keys))
    assert r.status_code == 422
    res = r.json()["results"][0]
    assert res["status"] == "rejected" and res["reason"] == reason
    assert res.get("field") == field
    assert marker not in r.text
    assert "ZZ-NEVER-ECHO" not in table_dump(engine)
    health = client.get("/api/v1/ingestion/health", headers=auth(keys)).json()
    assert health["rejected"] == 1 and health["rejection_reasons"] == {reason: 1}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM events")).scalar_one() == 0


def test_mixed_batch_reports_each_event(client, keys, raw_event) -> None:
    good = dict(raw_event, event_id="ok")
    r = client.post(
        URL, json=body(good, dict(raw_event, severity="LOUD"), "not-an-object"), headers=auth(keys)
    )
    assert r.status_code == 202
    assert [x["status"] for x in r.json()["results"]] == ["accepted", "rejected", "rejected"]
    assert r.json()["results"][2]["reason"] == "invalid_event"


def test_unknown_format_is_400(client, keys, raw_event) -> None:
    r = client.post(URL + "?format=sentry", json=body(raw_event), headers=auth(keys))
    assert r.status_code == 400 and error_code(r) == "unknown_format"


# ---- M1-4 --------------------------------------------------------------------------------

SECRETS = {
    "bearer": "Bearer " + "q9w8e7r6t5y4u3i2o1p0",
    "email": "jane.doe" + "@example.com",
    "pw": "hunter2-" + "correct-horse",
    "url": "postgres://admin:" + "s3cr3tPass" + "@db.internal/app",
    "aws": "AKIA" + "IOSFODNN7EXAMPLE",
}
NEEDLES = ["q9w8e7r6t5y4", "jane.doe", "hunter2", "s3cr3tPass", "IOSFODNN7", "cookie-value-123"]


def test_secrets_never_reach_db_responses_or_logs(client, keys, raw_event, engine, caplog) -> None:
    raw_event.update(
        event_id="sec-1",
        message=f"login failed for {SECRETS['email']} password={SECRETS['pw']} {SECRETS['bearer']}",
        stack_trace=f'File "a.py", line 1, in f\n  connect("{SECRETS["url"]}") {SECRETS["aws"]}',
        attributes={"Cookie": "cookie-value-123", "note": SECRETS["email"]},
    )
    with caplog.at_level(logging.DEBUG):
        r = client.post(URL, json=body(raw_event), headers=auth(keys))
        ev = client.get("/api/v1/events/sec-1", headers=auth(keys))
        inc = client.get("/api/v1/incidents", headers=auth(keys))
    assert r.status_code == 202
    surfaces = {
        "db": table_dump(engine),
        "post": r.text,
        "event": ev.text,
        "incidents": inc.text,
        "logs": caplog.text,
    }
    for name, text in surfaces.items():
        for needle in NEEDLES:
            assert needle not in text, (name, needle)
    assert ev.json()["redactions_applied"] >= 6
    assert "[REDACTED:" in ev.json()["message"]


def test_ingest_key_never_appears_in_responses_logs_or_db(
    client, keys, raw_event, engine, caplog
) -> None:
    with caplog.at_level(logging.DEBUG):
        responses = [
            client.post(URL, json=body(raw_event), headers=auth(keys)),
            client.get("/api/v1/incidents", headers=auth(keys)),
            client.get("/api/v1/ingestion/health", headers=auth(keys)),
            client.get("/api/v1/incidents", headers={"X-TraceMind-Key": keys["alpha"] + "x"}),
            client.get("/ready"),
        ]
    everything = " ".join(r.text + str(dict(r.headers)) for r in responses) + caplog.text
    everything += table_dump(engine)
    for key in keys.values():
        assert key not in everything


# ---- M1-7 / M1-8 -------------------------------------------------------------------------


def test_trace_ids_pass_through_and_absent_stay_null(client, keys, raw_event) -> None:
    with_trace = dict(
        raw_event,
        event_id="t1",
        trace_id="4BF92F3577B34DA6A3CE929D0E0E4736",
        span_id="00F067AA0BA902B7",
    )
    without = dict(raw_event, event_id="t2")
    client.post(URL, json=body(with_trace, without), headers=auth(keys))
    a = client.get("/api/v1/events/t1", headers=auth(keys)).json()
    b = client.get("/api/v1/events/t2", headers=auth(keys)).json()
    assert (a["trace_id"], a["span_id"]) == ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")
    assert (b["trace_id"], b["span_id"]) == (None, None)


def test_ingestion_health_matches_what_was_submitted(client, keys, raw_event) -> None:
    good = dict(raw_event, event_id="h1")
    client.post(URL, json=body(good, good, dict(good, message="changed")), headers=auth(keys))
    client.post(URL, json=body(dict(raw_event, severity="LOUD")), headers=auth(keys))
    client.post(URL, json=body(dict(raw_event, timestamp="nope")), headers=auth(keys))
    h = client.get("/api/v1/ingestion/health", headers=auth(keys)).json()
    assert h["accepted"] == 1
    assert h["duplicate"] == 2 and h["duplicate_conflict"] == 1
    assert h["rejected"] == 2
    assert h["rejection_reasons"] == {"invalid_severity": 1, "invalid_timestamp": 1}
    assert h["last_event_received_at"] == "2026-10-28T12:00:00Z"
    assert (
        client.get("/api/v1/ingestion/health", headers=auth(keys, "beta")).json()["accepted"] == 0
    )
    assert (
        client.get("/api/v1/ingestion/health?window_hours=0", headers=auth(keys)).status_code == 422
    )


# ---- M1-11 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", URL),
        ("get", "/api/v1/incidents"),
        ("get", "/api/v1/incidents/x"),
        ("get", "/api/v1/events/x"),
        ("get", "/api/v1/ingestion/health"),
    ],
)
def test_every_api_route_requires_a_valid_key(method, path, client, keys) -> None:
    for headers in ({}, {"X-TraceMind-Key": "wrong"}, {"X-TraceMind-Key": keys["alpha"][:-1]}):
        r = getattr(client, method)(path, headers=headers)
        assert r.status_code == 401 and error_code(r) == "unauthorized"


def test_one_projects_key_cannot_read_another_projects_data(client, keys, raw_event) -> None:
    raw_event["event_id"] = "private-1"
    res = client.post(URL, json=body(raw_event), headers=auth(keys, "alpha")).json()["results"][0]
    other = auth(keys, "beta")
    assert client.get("/api/v1/events/private-1", headers=other).status_code == 404
    assert client.get(f"/api/v1/incidents/{res['incident_id']}", headers=other).status_code == 404
    assert client.get("/api/v1/incidents", headers=other).json()["items"] == []
    assert client.get("/api/v1/events/private-1", headers=auth(keys, "alpha")).status_code == 200


def test_body_project_mismatch_is_403_and_stores_nothing(client, keys, raw_event, engine) -> None:
    mine = dict(raw_event, event_id="m1")
    theirs = dict(raw_event, event_id="m2", project_id="project-other")
    r = client.post(URL, json=body(mine, theirs), headers=auth(keys, "alpha"))
    assert r.status_code == 403 and error_code(r) == "project_mismatch"
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM events")).scalar_one() == 0
    # Writing as project B with B's key lands in B, never A.
    b = client.post(
        URL, json=body(dict(raw_event, project_id="project-other")), headers=auth(keys, "beta")
    )
    assert b.status_code == 202
    assert client.get("/api/v1/incidents", headers=auth(keys, "alpha")).json()["items"] == []
    assert len(client.get("/api/v1/incidents", headers=auth(keys, "beta")).json()["items"]) == 1


def test_rate_limit_returns_429_with_retry_after(make_client, keys, raw_event) -> None:
    c = make_client(Settings(rate_limit_burst=3, rate_limit_per_second=0.001))
    codes = [
        c.post(URL, json=body(dict(raw_event, event_id=f"r{i}")), headers=auth(keys))
        for i in range(5)
    ]
    assert [r.status_code for r in codes[:3]] == [202, 202, 202]
    assert codes[3].status_code == 429 and error_code(codes[3]) == "rate_limited"
    assert int(codes[3].headers["Retry-After"]) >= 1
    # The limit is per project: another project is unaffected.
    other = c.post(
        URL, json=body(dict(raw_event, project_id="project-other")), headers=auth(keys, "beta")
    )
    assert other.status_code == 202


def test_unauthenticated_requests_do_not_consume_rate_limit(make_client, keys, raw_event) -> None:
    c = make_client(Settings(rate_limit_burst=2, rate_limit_per_second=0.001))
    for _ in range(10):
        assert (
            c.post(URL, json=body(raw_event), headers={"X-TraceMind-Key": "bad"}).status_code == 401
        )
    assert c.post(URL, json=body(raw_event), headers=auth(keys)).status_code == 202


# ---- M1-12 (readiness / degraded behaviour) ----------------------------------------------


def test_ready_reports_database_and_api_stays_up_when_it_is_down(
    make_client, keys, raw_event
) -> None:
    down = PostgresStore.from_url("postgresql+psycopg://u:p@127.0.0.1:1/none")
    c = make_client(db=down)
    ready = c.get("/ready")
    assert ready.status_code == 503
    assert ready.json() == {"status": "degraded", "dependencies": {"database": "unavailable"}}
    assert c.get("/health").json() == {"status": "ok"}
    r = c.post(URL, json=body(raw_event), headers=auth(keys))
    assert r.status_code == 503 and error_code(r) == "storage_unavailable"
    assert "127.0.0.1" not in r.text and "psycopg" not in r.text


def test_ready_ok_when_database_is_up(client) -> None:
    assert client.get("/ready").json() == {"status": "ready", "dependencies": {"database": "ok"}}


def test_internal_errors_do_not_leak_details(
    make_client, keys, raw_event, store, monkeypatch
) -> None:
    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail postgres://u:p@h/db")

    monkeypatch.setattr(store, "get_incident", boom)
    r = make_client().get("/api/v1/incidents/x", headers=auth(keys))
    assert r.status_code == 500 and error_code(r) == "internal_error"
    assert "secret-internal-detail" not in r.text
