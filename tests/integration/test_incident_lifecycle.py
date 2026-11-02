"""M3-8, M3-10 (lifecycle half): transitions, append-only history, reopen rules, concurrency and
project isolation, through the HTTP API on real PostgreSQL."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import copy
import itertools
import json
import secrets
import threading
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from tracemind.api.app import create_app
from tracemind.config import Settings
from tracemind.incidents.lifecycle import ALLOWED, STATUSES
from tracemind.ingestion.adapters import AdapterItem
from tracemind.projects import ConfigProjectRegistry
from tracemind.storage.tables import incident_transitions, incidents

from tests.conftest import NOW, ROOT

pytestmark = pytest.mark.integration


@pytest.fixture
def keys() -> dict[str, str]:
    return {"alpha": secrets.token_urlsafe(32), "beta": secrets.token_urlsafe(32)}


@pytest.fixture
def client(store, tmp_path: Path, keys: dict[str, str]) -> TestClient:
    cfg = {
        "projects": [
            {
                "project_id": "project-demo",
                "allowed_services": ["payments-api"],
                "ingest_key_env": "KA",
            },
            {
                "project_id": "project-other",
                "allowed_services": ["payments-api"],
                "ingest_key_env": "KB",
            },
        ]
    }
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    registry = ConfigProjectRegistry.from_file(path, {"KA": keys["alpha"], "KB": keys["beta"]})
    app = create_app(settings=Settings(), registry=registry, store=store, clock=lambda: NOW)
    return TestClient(app, raise_server_exceptions=False)


def h(keys: dict[str, str], who: str = "alpha") -> dict[str, str]:
    return {"X-TraceMind-Key": keys[who]}


def make_incident(pipeline, project, raw_event, event_id: str = "evt-1") -> str:
    raw = {**copy.deepcopy(raw_event), "event_id": event_id}
    out = pipeline.process(project, [AdapterItem(event=raw)])[0]
    assert out.status == "accepted" and out.incident_id
    return str(out.incident_id)


def move(client, keys, incident_id: str, to: str, note: str | None = None, who: str = "alpha"):
    body: dict[str, Any] = {"to": to}
    if note is not None:
        body["note"] = note
    return client.post(
        f"/api/v1/incidents/{incident_id}/transitions", json=body, headers=h(keys, who)
    )


def history(client, keys, incident_id: str, who: str = "alpha", **params: Any):
    return client.get(
        f"/api/v1/incidents/{incident_id}/transitions", params=params, headers=h(keys, who)
    )


def history_rows(engine, incident_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(incident_transitions)
                .where(incident_transitions.c.incident_id == incident_id)
                .order_by(incident_transitions.c.transition_id)
            ).mappings()
        )


def status_of(engine, incident_id: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.select(incidents.c.status).where(incidents.c.incident_id == incident_id)
            ).scalar_one()
        )


# A valid route from "open" to each state, so any (from, to) pair can be tested.
ROUTE: dict[str, list[tuple[str, str | None]]] = {
    "open": [],
    "investigating": [("investigating", None)],
    "resolved": [("resolved", None)],
    "ignored": [("ignored", "known noisy")],
}


def drive_to(client, keys, incident_id: str, state: str) -> None:
    for to, note in ROUTE[state]:
        assert move(client, keys, incident_id, to, note).status_code == 200


# ---- the table, end to end ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "dst"), [(s, d) for s, d in itertools.product(STATUSES, STATUSES) if d in ALLOWED[s]]
)
def test_allowed_transitions_succeed_and_are_recorded(
    client, keys, pipeline, project, raw_event, engine, src: str, dst: str
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    drive_to(client, keys, iid, src)
    before = len(history_rows(engine, iid))
    r = move(client, keys, iid, dst, "because tests")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["incident"]["status"] == dst
    assert (body["transition"]["from_status"], body["transition"]["to_status"]) == (src, dst)
    assert body["transition"]["actor"] == {"kind": "project_key", "id": "project-demo"}
    rows = history_rows(engine, iid)
    assert len(rows) == before + 1
    assert (rows[-1]["from_status"], rows[-1]["to_status"]) == (src, dst)
    assert status_of(engine, iid) == dst


@pytest.mark.parametrize(
    ("src", "dst"),
    [(s, d) for s, d in itertools.product(STATUSES, STATUSES) if d not in ALLOWED[s]],
)
def test_invalid_transitions_are_rejected_and_change_nothing(
    client, keys, pipeline, project, raw_event, engine, src: str, dst: str
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    drive_to(client, keys, iid, src)
    before = len(history_rows(engine, iid))
    r = move(client, keys, iid, dst, "because tests")
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "invalid_transition"
    assert err["details"] == [{"reason": f"{src}->{dst}"}]
    assert status_of(engine, iid) == src
    assert len(history_rows(engine, iid)) == before


# ---- notes, resolution, history --------------------------------------------------------------


def test_ignoring_needs_a_note_and_a_rejected_request_changes_nothing(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    for body in ({"to": "ignored"}, {"to": "ignored", "note": "   "}):
        r = client.post(f"/api/v1/incidents/{iid}/transitions", json=body, headers=h(keys))
        assert r.status_code == 422 and r.json()["error"]["code"] == "note_required"
    assert status_of(engine, iid) == "open" and history_rows(engine, iid) == []
    assert move(client, keys, iid, "ignored", "expected during the migration").status_code == 200


def test_resolved_status_never_records_a_verified_resolution(
    client, keys, pipeline, project, raw_event
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    doc = move(client, keys, iid, "resolved", "deployed a fix").json()["incident"]
    assert doc["status"] == "resolved"
    assert doc["resolution"] == {"state": "none"}
    again = client.get(f"/api/v1/incidents/{iid}", headers=h(keys)).json()
    assert again["resolution"] == {"state": "none"} and again["status"] == "resolved"


def test_incident_documents_stay_schema_valid(client, keys, pipeline, project, raw_event) -> None:
    schema = json.loads((ROOT / "docs/contracts/incident.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    iid = make_incident(pipeline, project, raw_event)
    validator.validate(client.get(f"/api/v1/incidents/{iid}", headers=h(keys)).json())
    validator.validate(move(client, keys, iid, "resolved").json()["incident"])
    validator.validate(move(client, keys, iid, "open").json()["incident"])


def test_history_is_ordered_paginated_and_complete(
    client, keys, pipeline, project, raw_event
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    path = ["investigating", "open", "resolved", "open", "ignored", "open"]
    for to in path:
        assert move(client, keys, iid, to, "n").status_code == 200
    first = history(client, keys, iid, limit=4).json()
    assert len(first["items"]) == 4 and first["next_cursor"]
    rest = history(client, keys, iid, limit=4, cursor=first["next_cursor"]).json()
    assert rest["next_cursor"] is None
    items = first["items"] + rest["items"]
    assert [i["to_status"] for i in items] == path
    assert [i["transition_id"] for i in items] == sorted(i["transition_id"] for i in items)
    # each step starts where the previous one ended
    assert [i["from_status"] for i in items] == ["open", *path[:-1]]


def test_bad_history_cursor_is_a_clean_400(client, keys, pipeline, project, raw_event) -> None:
    iid = make_incident(pipeline, project, raw_event)
    for bad in ("zzz", "t-1", "t1;drop", "t" + "9" * 40):
        r = history(client, keys, iid, cursor=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cursor"


def test_history_rows_cannot_be_updated_or_deleted(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    move(client, keys, iid, "resolved")
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE incident_transitions SET note = 'edited'"))
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM incident_transitions"))
    assert len(history_rows(engine, iid)) == 1


def test_notes_are_redacted_and_validated(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    r = move(client, keys, iid, "resolved", "rotated: password=hunter2 reported by ops@example.com")
    assert r.status_code == 200
    stored = history_rows(engine, iid)[0]["note"]
    assert "hunter2" not in stored and "ops@example.com" not in stored
    assert "hunter2" not in r.text
    for bad in ("two\nlines", "x" * 501):
        r = move(client, keys, iid, "open", bad)
        assert r.status_code == 422, bad[:10]
        assert bad not in r.text
    assert status_of(engine, iid) == "resolved"


def test_request_shape_is_strict(client, keys, pipeline, project, raw_event) -> None:
    iid = make_incident(pipeline, project, raw_event)
    url = f"/api/v1/incidents/{iid}/transitions"
    for body in (
        {},
        {"to": "closed"},
        {"to": "OPEN"},
        {"to": "open", "actor": "someone"},
        {"to": 1},
    ):
        r = client.post(url, json=body, headers=h(keys))
        assert r.status_code == 422, body
    assert client.post(url, content=b"not json", headers=h(keys)).status_code == 422


# ---- reopen counter and the automatic reopen ---------------------------------------------------


def test_manual_reopen_counts_only_from_resolved(
    client, keys, pipeline, project, raw_event
) -> None:
    iid = make_incident(pipeline, project, raw_event)

    def reopens() -> int:
        return int(client.get(f"/api/v1/incidents/{iid}", headers=h(keys)).json()["reopen_count"])

    assert reopens() == 0
    move(client, keys, iid, "ignored", "noise")
    move(client, keys, iid, "open")
    assert reopens() == 0  # un-ignoring is not a reopen
    move(client, keys, iid, "resolved")
    move(client, keys, iid, "open")
    assert reopens() == 1
    move(client, keys, iid, "investigating")
    move(client, keys, iid, "open")
    assert reopens() == 1  # cancelling an investigation is not a reopen


def test_new_occurrence_reopens_a_resolved_incident_and_records_why(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event, "evt-1")
    move(client, keys, iid, "resolved", "fixed")
    out = pipeline.process(
        project, [AdapterItem(event={**copy.deepcopy(raw_event), "event_id": "evt-2"})]
    )[0]
    assert out.incident_id == iid
    doc = client.get(f"/api/v1/incidents/{iid}", headers=h(keys)).json()
    assert doc["status"] == "open" and doc["reopen_count"] == 1 and doc["occurrence_count"] == 2
    assert doc["resolution"] == {"state": "none"}
    last = history(client, keys, iid).json()["items"][-1]
    assert (last["from_status"], last["to_status"]) == ("resolved", "open")
    assert last["actor"] == {"kind": "system", "id": "new_occurrence"}
    assert last["evidence_ids"] == ["event:evt-2"]  # a resolvable reference to the cause
    # the reference resolves to a real stored event
    assert client.get("/api/v1/events/evt-2", headers=h(keys)).status_code == 200


def test_duplicate_delivery_does_not_reopen(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event, "evt-1")
    move(client, keys, iid, "resolved")
    again = pipeline.process(
        project, [AdapterItem(event={**copy.deepcopy(raw_event), "event_id": "evt-1"})]
    )[0]
    assert again.status == "duplicate"
    assert status_of(engine, iid) == "resolved"
    assert len(history_rows(engine, iid)) == 1


@pytest.mark.parametrize("state", ["open", "investigating", "ignored"])
def test_other_states_only_count_new_occurrences(
    client, keys, pipeline, project, raw_event, engine, state: str
) -> None:
    iid = make_incident(pipeline, project, raw_event, "evt-1")
    drive_to(client, keys, iid, state)
    before = len(history_rows(engine, iid))
    pipeline.process(
        project, [AdapterItem(event={**copy.deepcopy(raw_event), "event_id": "evt-2"})]
    )
    doc = client.get(f"/api/v1/incidents/{iid}", headers=h(keys)).json()
    assert doc["status"] == state and doc["reopen_count"] == 0 and doc["occurrence_count"] == 2
    assert len(history_rows(engine, iid)) == before


def test_warning_events_never_reopen(client, keys, pipeline, project, raw_event, engine) -> None:
    iid = make_incident(pipeline, project, raw_event, "evt-1")
    move(client, keys, iid, "resolved")
    warn = {**copy.deepcopy(raw_event), "event_id": "evt-w", "severity": "WARNING"}
    out = pipeline.process(project, [AdapterItem(event=warn)])[0]
    assert out.status == "accepted" and out.incident_id is None
    assert status_of(engine, iid) == "resolved"


# ---- concurrency -----------------------------------------------------------------------------


def run_threads(n: int, work) -> list[Any]:
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n

    def target(i: int) -> None:
        barrier.wait()
        results[i] = work(i)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_concurrent_identical_transitions_succeed_exactly_once(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    codes = run_threads(8, lambda i: move(client, keys, iid, "resolved", f"racer {i}").status_code)
    assert sorted(codes) == [200] + [409] * 7
    assert status_of(engine, iid) == "resolved"
    assert len(history_rows(engine, iid)) == 1


def test_concurrent_conflicting_transitions_leave_a_consistent_history(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    targets = ["resolved", "investigating", "ignored", "resolved", "investigating", "ignored"]
    codes = run_threads(6, lambda i: move(client, keys, iid, targets[i], "race").status_code)
    assert set(codes) <= {200, 409} and 200 in codes
    rows = history_rows(engine, iid)
    # Serialized by the row lock: one history row per success, each starting where the previous
    # one ended (so the chain is valid under the table), and the last one is the final status.
    assert len(rows) == codes.count(200)
    assert rows[0]["from_status"] == "open"
    for prev, nxt in itertools.pairwise(rows):
        assert nxt["from_status"] == prev["to_status"]
    for row in rows:
        assert row["to_status"] in ALLOWED[row["from_status"]]
    assert rows[-1]["to_status"] == status_of(engine, iid)


def test_concurrent_new_occurrences_reopen_exactly_once(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event, "evt-0")
    move(client, keys, iid, "resolved")

    def ingest(i: int) -> str:
        raw = {**copy.deepcopy(raw_event), "event_id": f"evt-r{i}"}
        return str(pipeline.process(project, [AdapterItem(event=raw)])[0].status)

    assert run_threads(8, ingest) == ["accepted"] * 8
    doc = client.get(f"/api/v1/incidents/{iid}", headers=h(keys)).json()
    assert doc["status"] == "open" and doc["reopen_count"] == 1 and doc["occurrence_count"] == 9
    system = [r for r in history_rows(engine, iid) if r["actor_kind"] == "system"]
    assert len(system) == 1


def test_resolve_racing_with_new_occurrences_never_corrupts_the_count(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event, "evt-0")

    def work(i: int) -> Any:
        if i == 0:
            return move(client, keys, iid, "resolved").status_code
        raw = {**copy.deepcopy(raw_event), "event_id": f"evt-x{i}"}
        return pipeline.process(project, [AdapterItem(event=raw)])[0].status

    run_threads(6, work)
    doc = client.get(f"/api/v1/incidents/{iid}", headers=h(keys)).json()
    assert doc["occurrence_count"] == 6
    rows = history_rows(engine, iid)
    # whatever the interleaving, the history explains the final state and the counter
    final = rows[-1]["to_status"] if rows else "open"
    assert doc["status"] == final
    assert doc["reopen_count"] == sum(
        1 for r in rows if (r["from_status"], r["to_status"]) == ("resolved", "open")
    )


# ---- isolation and authentication ----------------------------------------------------------------


def test_another_projects_key_cannot_read_or_change_an_incident(
    client, keys, pipeline, project, raw_event, engine
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    r = move(client, keys, iid, "resolved", who="beta")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert history(client, keys, iid, who="beta").status_code == 404
    assert status_of(engine, iid) == "open" and history_rows(engine, iid) == []
    # indistinguishable from an incident that does not exist
    missing = move(client, keys, "no-such-incident", "resolved", who="beta")
    assert missing.status_code == r.status_code
    assert {k: missing.json()["error"][k] for k in ("code", "message", "details")} == {
        k: r.json()["error"][k] for k in ("code", "message", "details")
    }


def test_unknown_incident_is_404_and_requests_need_a_key(
    client, keys, pipeline, project, raw_event
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    assert move(client, keys, "nope", "resolved").status_code == 404
    assert history(client, keys, "nope").status_code == 404
    url = f"/api/v1/incidents/{iid}/transitions"
    assert client.post(url, json={"to": "resolved"}).status_code == 401
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-TraceMind-Key": "wrong"}).status_code == 401


def test_status_constraint_rejects_invalid_values_at_the_database(
    engine, pipeline, project, raw_event
) -> None:
    iid = make_incident(pipeline, project, raw_event)
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE incidents SET status = 'closed' WHERE incident_id = :i"), {"i": iid}
        )
