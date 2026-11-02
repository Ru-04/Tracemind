"""M1-2, M1-5, M1-7, M1-10: storage, deduplication and incident grouping on real PostgreSQL."""

# mypy: disable-error-code="no-untyped-def"
from __future__ import annotations

import copy
import threading
from datetime import timedelta
from typing import Any

import jsonschema
import pytest
import sqlalchemy as sa
from tracemind.ingestion.adapters import AdapterItem
from tracemind.storage.tables import events, incident_occurrences, incidents

from tests.conftest import NOW, ROOT

pytestmark = pytest.mark.integration


def items(*raws: dict[str, Any]) -> list[AdapterItem]:
    return [AdapterItem(event=copy.deepcopy(r)) for r in raws]


def ev(raw_event: dict[str, Any], **patch: Any) -> dict[str, Any]:
    return {**copy.deepcopy(raw_event), **patch}


def count(engine, table) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def test_valid_event_is_stored_normalized(pipeline, project, store, raw_event) -> None:
    raw_event.update(
        service="Payments-API", severity="error", timestamp="2026-10-28T12:15:30+02:00"
    )
    out = pipeline.process(project, items(raw_event))[0]
    assert out.status == "accepted" and out.incident_id
    stored = store.get_event("project-demo", out.event_id)
    assert stored["service"] == "payments-api"
    assert stored["occurred_at"] == "2026-10-28T10:15:30Z"
    assert stored["received_at"] == "2026-10-28T12:00:00Z"
    assert stored["severity"] == "ERROR"
    schema = (ROOT / "docs/contracts/event.schema.json").read_text(encoding="utf-8")
    import json

    jsonschema.Draft202012Validator(
        json.loads(schema), format_checker=jsonschema.FormatChecker()
    ).validate(stored)


def test_events_are_scoped_to_their_project(pipeline, project, store, raw_event) -> None:
    out = pipeline.process(project, items(raw_event))[0]
    assert store.get_event("project-other", out.event_id) is None


def test_absent_trace_stays_null(pipeline, project, store, raw_event) -> None:
    out = pipeline.process(project, items(raw_event))[0]
    stored = store.get_event("project-demo", out.event_id)
    assert stored["trace_id"] is None and stored["span_id"] is None


def test_resend_with_same_event_id_is_duplicate_and_count_unchanged(
    pipeline, project, store, engine, raw_event
) -> None:
    raw_event["event_id"] = "evt-1"
    first = pipeline.process(project, items(raw_event))[0]
    again = pipeline.process(project, items(raw_event))[0]
    assert (first.status, again.status) == ("accepted", "duplicate")
    assert not again.conflict
    assert count(engine, events) == 1
    inc = store.get_incident("project-demo", first.incident_id)
    assert inc["occurrence_count"] == 1
    health = store.ingestion_health("project-demo", window_hours=24, now=NOW)
    assert (health["accepted"], health["duplicate"], health["duplicate_conflict"]) == (1, 1, 0)


def test_derived_id_resend_deduplicates(pipeline, project, engine, raw_event) -> None:
    pipeline.process(project, items(raw_event))
    assert pipeline.process(project, items(raw_event))[0].status == "duplicate"
    assert count(engine, events) == 1


def test_same_id_different_content_keeps_original_and_counts_conflict(
    pipeline, project, store, raw_event
) -> None:
    raw_event["event_id"] = "evt-1"
    first = pipeline.process(project, items(raw_event))[0]
    changed = ev(raw_event, message="A completely different message")
    again = pipeline.process(project, items(changed))[0]
    assert again.status == "duplicate" and again.conflict
    assert store.get_event("project-demo", "evt-1")["message"] == "Database connection failed"
    assert store.get_incident("project-demo", first.incident_id)["occurrence_count"] == 1
    assert (
        store.ingestion_health("project-demo", window_hours=24, now=NOW)["duplicate_conflict"] == 1
    )


def test_concurrent_identical_submissions_store_exactly_one(
    store, settings, project, engine, raw_event
) -> None:
    from tracemind.ingestion.pipeline import IngestionPipeline

    raw_event["event_id"] = "evt-race"
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            p = IngestionPipeline(store, settings, clock=lambda: NOW)
            results.append(p.process(project, items(raw_event))[0].status)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(results) == ["accepted"] + ["duplicate"] * 19
    assert count(engine, events) == 1
    with engine.connect() as conn:
        assert conn.execute(sa.select(incidents.c.occurrence_count)).scalar_one() == 1


def test_first_error_creates_open_incident_and_repeats_group(
    pipeline, project, store, raw_event
) -> None:
    a = pipeline.process(project, items(ev(raw_event, event_id="a")))[0]
    b = pipeline.process(
        project, items(ev(raw_event, event_id="b", timestamp="2026-10-28T11:00:00Z"))
    )[0]
    assert a.incident_id == b.incident_id
    inc = store.get_incident("project-demo", a.incident_id)
    assert inc["status"] == "open"
    assert inc["occurrence_count"] == 2
    assert inc["first_seen"] == "2026-10-28T10:15:30Z"
    assert inc["last_seen"] == "2026-10-28T11:00:00Z"
    assert inc["commit_association"] == {
        "commit_sha": None,
        "confidence": "unknown",
        "source": None,
    }
    assert inc["evidence_ids"] == ["event:b", "event:a"]
    import json

    schema = json.loads((ROOT / "docs/contracts/incident.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(inc)


def test_out_of_order_delivery_gets_correct_first_and_last_seen(
    pipeline, project, store, raw_event
) -> None:
    late = pipeline.process(
        project, items(ev(raw_event, event_id="late", timestamp="2026-10-28T11:30:00Z"))
    )[0]
    pipeline.process(
        project, items(ev(raw_event, event_id="early", timestamp="2026-10-28T09:00:00Z"))
    )
    pipeline.process(
        project, items(ev(raw_event, event_id="mid", timestamp="2026-10-28T10:00:00Z"))
    )
    inc = store.get_incident("project-demo", late.incident_id)
    assert (inc["first_seen"], inc["last_seen"], inc["occurrence_count"]) == (
        "2026-10-28T09:00:00Z",
        "2026-10-28T11:30:00Z",
        3,
    )


def test_different_fingerprints_make_different_incidents(
    pipeline, project, engine, raw_event
) -> None:
    a = pipeline.process(project, items(ev(raw_event, event_id="a")))[0]
    b = pipeline.process(project, items(ev(raw_event, event_id="b", error_type="TimeoutError")))[0]
    c = pipeline.process(project, items(ev(raw_event, event_id="c", environment="production")))[0]
    assert len({a.incident_id, b.incident_id, c.incident_id}) == 3
    assert count(engine, incidents) == 3


def test_warning_events_are_stored_but_create_no_incident(
    pipeline, project, engine, raw_event
) -> None:
    out = pipeline.process(project, items(ev(raw_event, severity="WARNING")))[0]
    assert out.status == "accepted" and out.incident_id is None
    assert count(engine, events) == 1 and count(engine, incidents) == 0


def test_incident_severity_escalates_to_critical(pipeline, project, store, raw_event) -> None:
    a = pipeline.process(project, items(ev(raw_event, event_id="a")))[0]
    pipeline.process(project, items(ev(raw_event, event_id="b", severity="CRITICAL")))
    pipeline.process(project, items(ev(raw_event, event_id="c")))
    assert store.get_incident("project-demo", a.incident_id)["severity"] == "CRITICAL"


def test_duplicates_never_bump_occurrence_links(pipeline, project, engine, raw_event) -> None:
    raw_event["event_id"] = "x"
    for _ in range(3):
        pipeline.process(project, items(raw_event))
    assert count(engine, incident_occurrences) == 1


def test_batch_mixes_outcomes_and_counts_rejections(pipeline, project, store, raw_event) -> None:
    good = ev(raw_event, event_id="g")
    bad_sev = ev(raw_event, event_id="b1", severity="LOUD")
    bad_ts = ev(raw_event, event_id="b2", timestamp="yesterday")
    outs = pipeline.process(project, items(good, bad_sev, bad_ts, good))
    assert [o.status for o in outs] == ["accepted", "rejected", "rejected", "duplicate"]
    assert [o.reason for o in outs[1:3]] == ["invalid_severity", "invalid_timestamp"]
    health = store.ingestion_health("project-demo", window_hours=24, now=NOW)
    assert health["rejected"] == 2
    assert health["rejection_reasons"] == {"invalid_severity": 1, "invalid_timestamp": 1}


def test_rejected_payloads_are_never_persisted(pipeline, project, engine, raw_event) -> None:
    bad = ev(raw_event, severity="LOUD", message="password=topsecretvalue123")
    pipeline.process(project, items(bad))
    assert count(engine, events) == 0
    with engine.connect() as conn:
        dump = " ".join(
            str(row)
            for t in ("ingestion_stats", "ingestion_rejections")
            for row in conn.execute(sa.text(f"SELECT * FROM {t}"))
        )
    assert "topsecretvalue123" not in dump


def test_health_window_excludes_old_buckets(pipeline, project, store, raw_event) -> None:
    pipeline.process(project, items(raw_event))
    later = NOW + timedelta(hours=30)
    assert store.ingestion_health("project-demo", window_hours=24, now=later)["accepted"] == 0
    assert store.ingestion_health("project-demo", window_hours=48, now=later)["accepted"] == 1


def test_list_incidents_filters_and_paginates(pipeline, project, store, raw_event) -> None:
    for i in range(5):
        pipeline.process(
            project,
            items(
                ev(
                    raw_event,
                    event_id=f"e{i}",
                    error_type=f"Err{i}",
                    timestamp=f"2026-10-28T10:0{i}:00Z",
                )
            ),
        )
    pipeline.process(project, items(ev(raw_event, event_id="p", environment="production")))
    page1, cur = store.list_incidents(
        "project-demo", service=None, environment="development", severity=None, limit=2, cursor=None
    )
    page2, cur2 = store.list_incidents(
        "project-demo", service=None, environment="development", severity=None, limit=2, cursor=cur
    )
    page3, cur3 = store.list_incidents(
        "project-demo", service=None, environment="development", severity=None, limit=2, cursor=cur2
    )
    seen = [i["incident_id"] for i in page1 + page2 + page3]
    assert len(seen) == len(set(seen)) == 5 and cur3 is None
    assert [i["last_seen"] for i in page1] == ["2026-10-28T10:04:00Z", "2026-10-28T10:03:00Z"]
    other, _ = store.list_incidents(
        "project-other", service=None, environment=None, severity=None, limit=10, cursor=None
    )
    assert other == []


def test_migration_creates_expected_tables(engine) -> None:
    names = set(sa.inspect(engine).get_table_names())
    assert {
        "events",
        "incidents",
        "incident_occurrences",
        "ingestion_stats",
        "ingestion_rejections",
    } <= names
