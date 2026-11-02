"""PostgreSQL access for incident transitions. Every read and write is scoped by `project_id`.

A transition locks the incident row (`SELECT ... FOR UPDATE`), validates against the locked
status, updates the status and appends the history row in ONE transaction. Two concurrent
requests therefore cannot both succeed from the same state, and a history row can never exist
without the status change it describes (or the other way round)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from tracemind.incidents.lifecycle import LifecycleError, check_transition
from tracemind.observability.logging import LOGGER_NAME, log
from tracemind.storage.base import InvalidCursorError
from tracemind.storage.postgres import PostgresStore, _iso
from tracemind.storage.tables import incident_transitions, incidents

_logger = logging.getLogger(LOGGER_NAME + ".lifecycle")


def transition_dict(row: Any) -> dict[str, Any]:
    trigger = row["trigger_event_id"]
    return {
        "transition_id": int(row["transition_id"]),
        "incident_id": row["incident_id"],
        "at": _iso(row["at"]),
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "actor": {"kind": row["actor_kind"], "id": row["actor_id"]},
        "note": row["note"],
        # An automatic reopen names the occurrence that caused it, as a resolvable reference.
        "evidence_ids": [f"event:{trigger}"] if trigger else [],
    }


class IncidentLifecycleStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._incidents = PostgresStore(engine)

    def transition(
        self,
        project_id: str,
        incident_id: str,
        target: str,
        *,
        note: str | None,
        actor_id: str,
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Apply one validated transition; returns (incident document, transition document)."""
        with self._engine.begin() as conn:
            current = conn.execute(
                sa.select(incidents.c.status)
                .where(incidents.c.project_id == project_id, incidents.c.incident_id == incident_id)
                .with_for_update()
            ).scalar_one_or_none()
            if current is None:
                raise LifecycleError("not_found", "incident")
            check_transition(current, target)
            values: dict[str, Any] = {"status": target, "updated_at": now}
            if current == "resolved" and target == "open":
                values["reopen_count"] = incidents.c.reopen_count + 1
            conn.execute(
                sa.update(incidents)
                .where(incidents.c.project_id == project_id, incidents.c.incident_id == incident_id)
                .values(**values)
            )
            row = (
                conn.execute(
                    sa.insert(incident_transitions)
                    .values(
                        incident_id=incident_id,
                        project_id=project_id,
                        at=now,
                        from_status=current,
                        to_status=target,
                        actor_kind="project_key",
                        actor_id=actor_id,
                        note=note,
                        trigger_event_id=None,
                    )
                    .returning(*incident_transitions.c)
                )
                .mappings()
                .one()
            )
        log(
            _logger,
            logging.INFO,
            "incident transition",
            project_id=project_id,
            incident_id=incident_id,
            from_status=current,
            to_status=target,
        )
        doc = self._incidents.get_incident(project_id, incident_id)
        assert doc is not None
        return doc, transition_dict(row)

    def list_transitions(
        self, project_id: str, incident_id: str, *, limit: int, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str | None] | None:
        """Oldest first, so the list reads as a history. None if the incident is not the
        project's (never revealing whether another project has it)."""
        after = _decode_position(cursor) if cursor else 0
        with self._engine.connect() as conn:
            exists = conn.execute(
                sa.select(incidents.c.incident_id).where(
                    incidents.c.project_id == project_id, incidents.c.incident_id == incident_id
                )
            ).first()
            if exists is None:
                return None
            rows = list(
                conn.execute(
                    sa.select(incident_transitions)
                    .where(
                        incident_transitions.c.project_id == project_id,
                        incident_transitions.c.incident_id == incident_id,
                        incident_transitions.c.transition_id > after,
                    )
                    .order_by(incident_transitions.c.transition_id)
                    .limit(limit + 1)
                ).mappings()
            )
        page = rows[:limit]
        nxt = _encode_position(page[-1]["transition_id"]) if len(rows) > limit else None
        return [transition_dict(r) for r in page], nxt


def _encode_position(transition_id: int) -> str:
    return f"t{int(transition_id)}"


def _decode_position(cursor: str) -> int:
    if not cursor.startswith("t") or not cursor[1:].isdigit() or len(cursor) > 20:
        raise InvalidCursorError("invalid cursor")
    return int(cursor[1:])
