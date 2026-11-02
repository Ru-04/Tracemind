"""PostgreSQL store. Event insert, incident upsert and counters share one transaction, so a
duplicate delivery can never increment an occurrence count. All SQL is parameterized."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from tracemind.ingestion.normalize import PreparedEvent
from tracemind.storage.base import InvalidCursorError, RecordResult
from tracemind.storage.tables import (
    events,
    incident_occurrences,
    incident_transitions,
    incidents,
    ingestion_rejections,
    ingestion_stats,
)

EVIDENCE_LIMIT = 20


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hour(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class PostgresStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, url: str) -> PostgresStore:
        engine = sa.create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        return cls(engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    def dispose(self) -> None:
        self._engine.dispose()

    def ping(self) -> None:
        with self._engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    # ---- writes -------------------------------------------------------------------------

    def record_event(self, prepared: PreparedEvent, *, now: datetime) -> RecordResult:
        e = prepared.event
        values = e.model_dump(exclude={"ingest_status"})
        values["content_hash"] = prepared.content_hash
        with self._engine.begin() as conn:
            inserted = conn.execute(
                pg_insert(events)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[events.c.project_id, events.c.event_id])
                .returning(events.c.event_id)
            ).first()
            if inserted is None:
                existing = conn.execute(
                    sa.select(events.c.content_hash).where(
                        events.c.project_id == e.project_id, events.c.event_id == e.event_id
                    )
                ).scalar_one()
                conflict = existing != prepared.content_hash
                self._bump(
                    conn, e.project_id, now, duplicate=1, duplicate_conflict=1 if conflict else 0
                )
                return RecordResult("duplicate", None, conflict)

            incident_id: str | None = None
            if prepared.incident_eligible:
                incident_id = self._upsert_incident(conn, prepared, now)
                conn.execute(
                    sa.insert(incident_occurrences).values(
                        incident_id=incident_id, project_id=e.project_id, event_id=e.event_id
                    )
                )
            self._bump(conn, e.project_id, now, accepted=1)
            return RecordResult("accepted", incident_id)

    def _upsert_incident(self, conn: Connection, prepared: PreparedEvent, now: datetime) -> str:
        """Create the incident or count one more occurrence on it.

        A NEW occurrence of a `resolved` incident reopens it (owner decision, M3 plan 4.2) and the
        reopen is appended to the transition history as a system action naming the occurrence.
        `ignored` and `investigating` incidents only count. Duplicates never get here
        (`record_event` returns before), so a re-delivery cannot reopen anything.

        Two statements instead of one upsert: the row is locked before its status is read, so
        the reopen decision cannot race with a concurrent API transition."""
        e = prepared.event
        created = conn.execute(
            pg_insert(incidents)
            .values(
                incident_id=str(uuid.uuid4()),
                project_id=e.project_id,
                service=e.service,
                environment=e.environment,
                fingerprint=e.fingerprint,
                fingerprint_version=e.fingerprint_version,
                severity=e.severity,
                status="open",
                first_seen=e.occurred_at,
                last_seen=e.occurred_at,
                occurrence_count=1,
                summary=prepared.summary,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_incident_group")
            .returning(incidents.c.incident_id)
        ).scalar_one_or_none()
        if created is not None:
            return str(created)

        group = sa.and_(
            incidents.c.project_id == e.project_id,
            incidents.c.service == e.service,
            incidents.c.environment == e.environment,
            incidents.c.fingerprint == e.fingerprint,
            incidents.c.fingerprint_version == e.fingerprint_version,
        )
        existing = conn.execute(
            sa.select(incidents.c.incident_id, incidents.c.status).where(group).with_for_update()
        ).one()
        incident_id = str(existing.incident_id)
        reopen = existing.status == "resolved"
        changes: dict[str, Any] = {
            "occurrence_count": incidents.c.occurrence_count + 1,
            "first_seen": sa.func.least(incidents.c.first_seen, e.occurred_at),
            "last_seen": sa.func.greatest(incidents.c.last_seen, e.occurred_at),
            "updated_at": now,
        }
        if e.severity == "CRITICAL":
            changes["severity"] = "CRITICAL"
        if reopen:
            changes["status"] = "open"
            changes["reopen_count"] = incidents.c.reopen_count + 1
        conn.execute(
            sa.update(incidents).where(incidents.c.incident_id == incident_id).values(**changes)
        )
        if reopen:
            conn.execute(
                sa.insert(incident_transitions).values(
                    incident_id=incident_id,
                    project_id=e.project_id,
                    at=now,
                    from_status="resolved",
                    to_status="open",
                    actor_kind="system",
                    actor_id="new_occurrence",
                    note=None,
                    trigger_event_id=e.event_id,
                )
            )
        return incident_id

    def _bump(self, conn: Connection, project_id: str, now: datetime, **counts: int) -> None:
        stmt = pg_insert(ingestion_stats).values(
            project_id=project_id, hour_bucket=_hour(now), **counts
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ingestion_stats.c.project_id, ingestion_stats.c.hour_bucket],
            set_={k: ingestion_stats.c[k] + stmt.excluded[k] for k in counts},
        )
        conn.execute(stmt)

    def record_rejections(
        self, project_id: str, reasons: Mapping[str, int], *, now: datetime
    ) -> None:
        total = sum(reasons.values())
        if not total:
            return
        with self._engine.begin() as conn:
            self._bump(conn, project_id, now, rejected=total)
            for reason, count in reasons.items():
                stmt = pg_insert(ingestion_rejections).values(
                    project_id=project_id, hour_bucket=_hour(now), reason=reason, count=count
                )
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[
                            ingestion_rejections.c.project_id,
                            ingestion_rejections.c.hour_bucket,
                            ingestion_rejections.c.reason,
                        ],
                        set_={"count": ingestion_rejections.c.count + stmt.excluded.count},
                    )
                )

    # ---- reads (always scoped to one project) -------------------------------------------

    def get_event(self, project_id: str, event_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(events).where(
                        events.c.project_id == project_id, events.c.event_id == event_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "service": row["service"],
            "environment": row["environment"],
            "occurred_at": _iso(row["occurred_at"]),
            "received_at": _iso(row["received_at"]),
            "severity": row["severity"],
            "message": row["message"],
            "error_type": row["error_type"],
            "stack_trace": row["stack_trace"],
            "trace_id": row["trace_id"],
            "span_id": row["span_id"],
            "release": row["release"],
            "commit_sha": row["commit_sha"],
            "attributes": row["attributes"],
            "fingerprint": row["fingerprint"],
            "fingerprint_version": row["fingerprint_version"],
            "ingest_status": "accepted",
            "redactions_applied": row["redactions_applied"],
            "source": row["source"],
        }

    @staticmethod
    def _incident_dict(row: Mapping[Any, Any], evidence: list[str] | None = None) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "incident_id": row["incident_id"],
            "project_id": row["project_id"],
            "service": row["service"],
            "environment": row["environment"],
            "severity": row["severity"],
            "status": row["status"],
            "fingerprint": row["fingerprint"],
            "fingerprint_version": row["fingerprint_version"],
            "first_seen": _iso(row["first_seen"]),
            "last_seen": _iso(row["last_seen"]),
            "occurrence_count": row["occurrence_count"],
            "reopen_count": row["reopen_count"],
            "summary": row["summary"],
            "deployment_id": None,
            # M1 never claims a commit association; deployment mapping arrives in M2/M5.
            "commit_association": {"commit_sha": None, "confidence": "unknown", "source": None},
            "investigation_status": "none",
            "resolution": {"state": "none"},
        }
        if evidence is not None:
            doc["evidence_ids"] = evidence
        return doc

    def get_incident(self, project_id: str, incident_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(incidents).where(
                        incidents.c.project_id == project_id, incidents.c.incident_id == incident_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            ev_rows = conn.execute(
                sa.select(events.c.event_id)
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
                    incident_occurrences.c.incident_id == incident_id,
                    incident_occurrences.c.project_id == project_id,
                )
                .order_by(events.c.occurred_at.desc(), events.c.event_id)
                .limit(EVIDENCE_LIMIT)
            ).scalars()
            evidence = [f"event:{eid}" for eid in ev_rows]
        return self._incident_dict(row, evidence)

    def incident_error_type(self, project_id: str, incident_id: str) -> str | None:
        """The error type of an incident's earliest occurrence (all share one fingerprint)."""
        with self._engine.connect() as conn:
            return conn.execute(
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
                    incident_occurrences.c.incident_id == incident_id,
                    incident_occurrences.c.project_id == project_id,
                )
                .order_by(events.c.occurred_at, events.c.event_id)
                .limit(1)
            ).scalar_one_or_none()

    def list_incidents(
        self,
        project_id: str,
        *,
        service: str | None,
        environment: str | None,
        severity: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        q = sa.select(incidents).where(incidents.c.project_id == project_id)
        if service:
            q = q.where(incidents.c.service == service)
        if environment:
            q = q.where(incidents.c.environment == environment)
        if severity:
            q = q.where(incidents.c.severity == severity)
        if cursor:
            last_seen, incident_id = _decode_cursor(cursor)
            q = q.where(
                sa.tuple_(incidents.c.last_seen, incidents.c.incident_id)
                < sa.tuple_(last_seen, incident_id)
            )
        q = q.order_by(incidents.c.last_seen.desc(), incidents.c.incident_id.desc()).limit(
            limit + 1
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(q).mappings())
        page = rows[:limit]
        next_cursor = (
            _encode_cursor(page[-1]["last_seen"], page[-1]["incident_id"])
            if len(rows) > limit
            else None
        )
        return [self._incident_dict(r) for r in page], next_cursor

    def ingestion_health(
        self, project_id: str, *, window_hours: int, now: datetime
    ) -> dict[str, Any]:
        since = _hour(now - timedelta(hours=window_hours))
        with self._engine.connect() as conn:
            totals = (
                conn.execute(
                    sa.select(
                        sa.func.coalesce(sa.func.sum(ingestion_stats.c.accepted), 0).label(
                            "accepted"
                        ),
                        sa.func.coalesce(sa.func.sum(ingestion_stats.c.duplicate), 0).label(
                            "duplicate"
                        ),
                        sa.func.coalesce(
                            sa.func.sum(ingestion_stats.c.duplicate_conflict), 0
                        ).label("duplicate_conflict"),
                        sa.func.coalesce(sa.func.sum(ingestion_stats.c.rejected), 0).label(
                            "rejected"
                        ),
                    ).where(
                        ingestion_stats.c.project_id == project_id,
                        ingestion_stats.c.hour_bucket >= since,
                    )
                )
                .mappings()
                .one()
            )
            reasons = conn.execute(
                sa.select(ingestion_rejections.c.reason, sa.func.sum(ingestion_rejections.c.count))
                .where(
                    ingestion_rejections.c.project_id == project_id,
                    ingestion_rejections.c.hour_bucket >= since,
                )
                .group_by(ingestion_rejections.c.reason)
            ).all()
            last = conn.execute(
                sa.select(sa.func.max(events.c.received_at)).where(
                    events.c.project_id == project_id
                )
            ).scalar_one()
        return {
            "window_hours": window_hours,
            "accepted": int(totals["accepted"]),
            "duplicate": int(totals["duplicate"]),
            "duplicate_conflict": int(totals["duplicate_conflict"]),
            "rejected": int(totals["rejected"]),
            "rejection_reasons": {r: int(c) for r, c in reasons},
            "last_event_received_at": _iso(last),
        }


def _encode_cursor(last_seen: datetime, incident_id: str) -> str:
    raw = f"{last_seen.astimezone(UTC).isoformat()}|{incident_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, _, incident_id = raw.partition("|")
        return datetime.fromisoformat(ts), incident_id
    except (ValueError, UnicodeError) as exc:
        raise InvalidCursorError("invalid cursor") from exc
