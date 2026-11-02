"""Runs events through validate → normalize → redact → fingerprint → store.

No LLM, network provider or web framework is involved (acceptance check M1-9)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tracemind.config import Settings
from tracemind.errors import Rejected
from tracemind.ingestion.adapters import AdapterItem
from tracemind.ingestion.models import validate_ingest
from tracemind.ingestion.normalize import prepare_event
from tracemind.ingestion.redact import Redactor
from tracemind.projects import Project
from tracemind.storage.base import EventStore


@dataclass(frozen=True)
class EventOutcome:
    index: int
    status: str  # accepted | duplicate | rejected
    event_id: str | None = None
    incident_id: str | None = None
    reason: str | None = None
    field: str | None = None
    conflict: bool = False

    def to_api(self) -> dict[str, Any]:
        out: dict[str, Any] = {"index": self.index, "status": self.status}
        if self.status == "rejected":
            out["reason"] = self.reason
            if self.field:
                out["field"] = self.field
            return out
        out["event_id"] = self.event_id
        if self.status == "accepted":
            out["incident_id"] = self.incident_id
        return out


class IngestionPipeline:
    def __init__(
        self,
        store: EventStore,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._settings = settings
        self._redactor = Redactor(redact_emails=settings.redact_emails)
        self._clock = clock

    def process(
        self, project: Project, items: Sequence[AdapterItem], *, source: str | None = None
    ) -> list[EventOutcome]:
        now = self._clock()
        outcomes: list[EventOutcome] = []
        rejections: Counter[str] = Counter()
        for index, item in enumerate(items):
            try:
                if item.rejection is not None:
                    raise item.rejection
                raw = validate_ingest(item.event)
                prepared = prepare_event(
                    raw,
                    project,
                    now=now,
                    settings=self._settings,
                    redactor=self._redactor,
                    source=source,
                )
                result = self._store.record_event(prepared, now=now)
                outcomes.append(
                    EventOutcome(
                        index,
                        result.status,
                        event_id=prepared.event.event_id,
                        incident_id=result.incident_id,
                        conflict=result.conflict,
                    )
                )
            except Rejected as rej:
                rejections[rej.reason] += 1
                outcomes.append(EventOutcome(index, "rejected", reason=rej.reason, field=rej.field))
        self._store.record_rejections(project.project_id, rejections, now=now)
        return outcomes
