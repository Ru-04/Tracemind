"""Snowflake-related routes (M3 S5). Every route requires the project key and is scoped to it.

These routes never fail because Snowflake is unavailable: the answer says so, with a stable
reason (`availability: "unavailable"`), and the rest of the API is unaffected. An `ok` answer
with no items means "asked, and nothing was found"; it is never returned when the question
could not be asked."""

from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query

from tracemind.api.errors import ApiError
from tracemind.projects import Project
from tracemind.snowflake.export import Exporter
from tracemind.snowflake.queries import QueryRejected, QueryResult, QueryService
from tracemind.storage.postgres import PostgresStore


def similar_incidents(
    queries: QueryService,
    store: PostgresStore,
    project_id: str,
    incident: dict[str, Any],
    *,
    window_days: int,
    limit: int,
) -> tuple[QueryResult, str | None]:
    """The same fingerprint; only if that finds nothing, the same service and error type.
    Returns the result and which rule matched (None when nothing matched)."""
    start = queries.now() - timedelta(days=window_days)
    primary = queries.similar_incidents(
        project_id,
        fingerprint=incident["fingerprint"],
        exclude_incident_id=incident["incident_id"],
        start=start,
        limit=limit,
    )
    if primary.availability != "ok" or primary.rows:
        return primary, "fingerprint" if primary.rows else None
    error_type = store.incident_error_type(project_id, incident["incident_id"])
    if error_type is None:
        return primary, None
    fallback = queries.similar_incidents(
        project_id,
        service=incident["service"],
        error_type=error_type,
        exclude_incident_id=incident["incident_id"],
        start=start,
        limit=limit,
    )
    return fallback, "service_and_error_type" if fallback.rows else None


def install_snowflake_routes(
    app: FastAPI,
    *,
    require_project: Callable[..., Project],
    store: PostgresStore,
    exporter: Exporter,
    queries: QueryService,
    throttle: Callable[[Project, int], None],
) -> None:
    Authed = Annotated[Project, Depends(require_project)]  # noqa: N806

    @app.get("/api/v1/projects/{project_id}/export/status")
    def export_status(project_id: str, project: Authed) -> dict[str, Any]:
        if project_id != project.project_id:
            raise ApiError(404, "not_found", "Not found")
        return exporter.status(project.project_id)

    @app.get("/api/v1/incidents/{incident_id}/history")
    def incident_history(
        incident_id: str,
        project: Authed,
        window_days: Annotated[int, Query(ge=1, le=30)] = 30,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        """Historical incidents similar to this one. Each answer states which rule matched, and
        every item is a lead to look at, not a diagnosis."""
        throttle(project, 1)
        incident = store.get_incident(project.project_id, incident_id[:128])
        if incident is None:
            raise ApiError(404, "not_found", "Incident not found")
        try:
            result, basis = similar_incidents(
                queries, store, project.project_id, incident, window_days=window_days, limit=limit
            )
        except QueryRejected as exc:
            raise ApiError(
                422,
                "invalid_parameters",
                "Parameters were rejected",
                details=[{"field": exc.field}],
            ) from None
        body = result.to_json()
        body["incident_id"] = incident["incident_id"]
        body["match_basis"] = basis
        return body
