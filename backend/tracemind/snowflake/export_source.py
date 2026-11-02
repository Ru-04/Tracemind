"""What still has to be exported, read from PostgreSQL (the source of truth).

Selection is an anti-join against `export_marks`: a row is exported when it has no mark, and an
incident is exported again when its `updated_at` no longer equals the version that was marked.
Marks are written only AFTER Snowflake confirmed the batch, so a crash between the two just
re-sends the batch; the insert-only MERGE (when demonstrated live) makes that harmless.

Only redacted columns are selected (events are redacted at ingestion; stack traces are not
exported at all). The project id is part of every key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from tracemind.snowflake.config import SEVERITIES
from tracemind.snowflake.sql import STREAMS
from tracemind.storage.postgres import _iso
from tracemind.storage.tables import (
    deployments,
    events,
    export_marks,
    incident_occurrences,
    incidents,
)

BACKLOG_CAP = 100_000


@dataclass(frozen=True)
class Candidate:
    stream: str
    project_id: str
    key: str  # event_id | incident_id | deployment_id (the mark key)
    sf_key: tuple[str, ...]  # the key as Snowflake sees it (used to de-duplicate a batch)
    params: tuple[Any, ...]  # all temporary-table columns except test_run_id, in column order
    version_at: datetime | None = None


def severities_at_least(minimum: str) -> list[str]:
    return list(SEVERITIES[SEVERITIES.index(minimum) :])


def _unmarked(stream: str, project_col: Any, key_col: Any, *extra: Any) -> Any:
    return ~sa.exists().where(
        export_marks.c.stream == stream,
        export_marks.c.project_id == project_col,
        export_marks.c.key == key_col,
        *extra,
    )


def _events_query(severities: list[str]) -> Any:
    return sa.select(events).where(
        events.c.severity.in_(severities),
        _unmarked("events", events.c.project_id, events.c.event_id),
    )


def _incidents_query() -> Any:
    return sa.select(incidents).where(
        _unmarked(
            "incident_summaries",
            incidents.c.project_id,
            incidents.c.incident_id,
            export_marks.c.version_at == incidents.c.updated_at,
        )
    )


def _deployments_query() -> Any:
    return sa.select(deployments).where(
        _unmarked("deployments", deployments.c.project_id, deployments.c.deployment_id)
    )


def events_batch(conn: Connection, *, limit: int, severities: list[str]) -> list[Candidate]:
    rows = conn.execute(
        _events_query(severities)
        .order_by(events.c.received_at, events.c.project_id, events.c.event_id)
        .limit(limit)
    ).mappings()
    out: list[Candidate] = []
    for r in rows:
        attrs = json.dumps(r["attributes"], sort_keys=True, separators=(",", ":"))
        out.append(
            Candidate(
                "events",
                r["project_id"],
                r["event_id"],
                (r["project_id"], r["event_id"]),
                (
                    r["project_id"],
                    r["event_id"],
                    r["service"],
                    r["environment"],
                    _iso(r["occurred_at"]),
                    r["severity"],
                    r["error_type"],
                    r["message"],
                    r["fingerprint"],
                    r["fingerprint_version"],
                    r["trace_id"],
                    r["commit_sha"],
                    r["release"],
                    attrs,
                ),
            )
        )
    return out


def incident_batch(conn: Connection, *, limit: int) -> list[Candidate]:
    first_error = (
        sa.select(events.c.error_type)
        .select_from(
            incident_occurrences.join(
                events,
                sa.and_(
                    events.c.project_id == incident_occurrences.c.project_id,
                    events.c.event_id == incident_occurrences.c.event_id,
                ),
            )
        )
        .where(
            incident_occurrences.c.incident_id == incidents.c.incident_id,
            incident_occurrences.c.project_id == incidents.c.project_id,
        )
        .order_by(events.c.occurred_at, events.c.event_id)
        .limit(1)
        .scalar_subquery()
    )
    query = _incidents_query().add_columns(first_error.label("error_type"))
    rows = conn.execute(
        query.order_by(incidents.c.updated_at, incidents.c.incident_id).limit(limit)
    ).mappings()
    out: list[Candidate] = []
    for r in rows:
        version = _iso(r["updated_at"])
        out.append(
            Candidate(
                "incident_summaries",
                r["project_id"],
                r["incident_id"],
                (r["project_id"], r["incident_id"], str(version)),
                (
                    r["project_id"],
                    r["incident_id"],
                    version,
                    r["service"],
                    r["environment"],
                    r["fingerprint"],
                    r["fingerprint_version"],
                    r["error_type"],
                    r["severity"],
                    r["status"],
                    _iso(r["first_seen"]),
                    _iso(r["last_seen"]),
                    r["occurrence_count"],
                    r["reopen_count"],
                    r["summary"],
                ),
                version_at=r["updated_at"],
            )
        )
    return out


def deployment_batch(conn: Connection, *, limit: int) -> list[Candidate]:
    rows = conn.execute(
        _deployments_query()
        .order_by(deployments.c.created_at, deployments.c.project_id, deployments.c.deployment_id)
        .limit(limit)
    ).mappings()
    return [
        Candidate(
            "deployments",
            r["project_id"],
            r["deployment_id"],
            (r["project_id"], r["deployment_id"]),
            (
                r["project_id"],
                r["deployment_id"],
                r["environment"],
                r["service"],
                r["release"],
                _iso(r["deployed_at"]),
                r["commit_sha"],  # only a verified commit is ever non-null
                r["commit_confidence"],
            ),
        )
        for r in rows
    ]


def backlog(
    conn: Connection, *, severities: list[str], project_id: str | None = None
) -> dict[str, int]:
    """Unexported rows per stream, each capped at BACKLOG_CAP so the check stays cheap."""

    def count(query: Any, project_col: Any) -> int:
        q = query.with_only_columns(sa.literal(1), maintain_column_froms=True)
        if project_id is not None:
            q = q.where(project_col == project_id)
        capped = q.limit(BACKLOG_CAP).subquery()
        return int(conn.execute(sa.select(sa.func.count()).select_from(capped)).scalar_one())

    return {
        "events": count(_events_query(severities), events.c.project_id),
        "incident_summaries": count(_incidents_query(), incidents.c.project_id),
        "deployments": count(_deployments_query(), deployments.c.project_id),
    }


def mark_exported(
    conn: Connection, candidates: list[Candidate], *, batch_id: str, now: datetime
) -> None:
    for c in candidates:
        stmt = pg_insert(export_marks).values(
            stream=c.stream,
            project_id=c.project_id,
            key=c.key,
            exported_at=now,
            batch_id=batch_id,
            version_at=c.version_at,
        )
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    export_marks.c.stream,
                    export_marks.c.project_id,
                    export_marks.c.key,
                ],
                set_={
                    "exported_at": stmt.excluded.exported_at,
                    "batch_id": stmt.excluded.batch_id,
                    "version_at": stmt.excluded.version_at,
                },
            )
        )


def columns_without_run_id(stream: str) -> tuple[str, ...]:
    return tuple(c.name for c in STREAMS[stream].columns if c.name != "test_run_id")
