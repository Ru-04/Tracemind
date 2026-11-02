"""Storage interface used by the pipeline and API. PostgreSQL is the only implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from tracemind.ingestion.normalize import PreparedEvent


@dataclass(frozen=True)
class RecordResult:
    status: Literal["accepted", "duplicate"]
    incident_id: str | None = None
    conflict: bool = False


class InvalidCursorError(ValueError):
    pass


class EventStore(Protocol):
    def ping(self) -> None: ...

    def record_event(self, prepared: PreparedEvent, *, now: datetime) -> RecordResult: ...

    def record_rejections(
        self, project_id: str, reasons: Mapping[str, int], *, now: datetime
    ) -> None: ...

    def get_event(self, project_id: str, event_id: str) -> dict[str, Any] | None: ...

    def list_incidents(
        self,
        project_id: str,
        *,
        service: str | None,
        environment: str | None,
        severity: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def get_incident(self, project_id: str, incident_id: str) -> dict[str, Any] | None: ...

    def ingestion_health(
        self, project_id: str, *, window_hours: int, now: datetime
    ) -> dict[str, Any]: ...
