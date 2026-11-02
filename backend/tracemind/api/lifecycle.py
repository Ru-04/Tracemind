"""Incident lifecycle routes. Every route requires the project key and is scoped to its project;
an incident that belongs to another project is a 404, never a 403.

`resolved` is a status only. Nothing here can record a verified resolution (see
`tracemind.incidents.lifecycle`)."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field

from tracemind.api.errors import ApiError
from tracemind.incidents.lifecycle import MAX_NOTE, LifecycleError, clean_note
from tracemind.incidents.store import IncidentLifecycleStore
from tracemind.ingestion.redact import Redactor
from tracemind.projects import Project
from tracemind.storage.base import InvalidCursorError

_STATUS: dict[str, tuple[int, str]] = {
    "not_found": (404, "not_found"),
    "invalid_transition": (409, "invalid_transition"),
    "note_required": (422, "note_required"),
    "invalid_note": (422, "invalid_note"),
}


def lifecycle_error_to_api(exc: LifecycleError) -> ApiError:
    status, code = _STATUS.get(exc.code, (500, "internal_error"))
    if status == 404:
        return ApiError(404, "not_found", "Incident not found")
    details = [{"reason": exc.detail}] if exc.detail else None
    return ApiError(status, code, "Lifecycle request was rejected", details=details)


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    to: Literal["open", "investigating", "resolved", "ignored"]
    note: str | None = Field(default=None, max_length=MAX_NOTE)


def install_lifecycle_routes(
    app: FastAPI,
    *,
    require_project: Callable[..., Project],
    store: IncidentLifecycleStore,
    throttle: Callable[[Project, int], None],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    redactor: Redactor | None = None,
) -> None:
    Authed = Annotated[Project, Depends(require_project)]  # noqa: N806
    redact = redactor or Redactor()
    base = "/api/v1/incidents/{incident_id}/transitions"

    @app.post(base)
    def transition_incident(
        incident_id: str, body: TransitionRequest, project: Authed
    ) -> dict[str, Any]:
        throttle(project, 1)
        try:
            note = clean_note(body.note, target=body.to, redactor=redact)
            incident, transition = store.transition(
                project.project_id,
                incident_id[:128],
                body.to,
                note=note,
                actor_id=project.project_id,
                now=clock(),
            )
        except LifecycleError as exc:
            raise lifecycle_error_to_api(exc) from None
        return {"incident": incident, "transition": transition}

    @app.get(base)
    def incident_transitions(
        incident_id: str,
        project: Authed,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=64)] = None,
    ) -> dict[str, Any]:
        try:
            found = store.list_transitions(
                project.project_id, incident_id[:128], limit=limit, cursor=cursor
            )
        except InvalidCursorError:
            raise ApiError(400, "invalid_cursor", "Invalid pagination cursor") from None
        if found is None:
            raise ApiError(404, "not_found", "Incident not found")
        items, next_cursor = found
        return {"items": items, "next_cursor": next_cursor}
