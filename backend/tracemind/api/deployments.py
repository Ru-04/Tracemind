"""Deployment routes. Every route requires the project key; a `{project_id}` that differs from the
key's project is a 404 (never a 403), so other projects' existence is not revealed.

The commit link of a record is `verified` only for a full SHA that exists in a registered
repository of the same project, otherwise `unknown` with a stable reason. It is never inferred
from time (see `tracemind.deployments.service`)."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from tracemind.api.errors import ApiError
from tracemind.deployments.service import DeploymentError, DeploymentService
from tracemind.projects import Project
from tracemind.storage.base import InvalidCursorError


class DeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    environment: str = Field(min_length=1, max_length=128)
    release: str = Field(min_length=1, max_length=128)
    deployed_at: str = Field(min_length=1, max_length=64)
    service: str | None = Field(default=None, max_length=128)
    commit_sha: str | None = Field(default=None, max_length=128)
    repository_id: str | None = Field(default=None, max_length=128)
    deployment_id: str | None = Field(default=None, max_length=128)


def deployment_error_to_api(exc: DeploymentError) -> ApiError:
    if exc.code == "repository_not_found":
        return ApiError(404, "not_found", "Not found")
    details = [{"field": exc.field}] if exc.field else None
    return ApiError(422, exc.code, "Deployment was rejected", details=details)


def install_deployment_routes(
    app: FastAPI,
    *,
    require_project: Callable[..., Project],
    service: DeploymentService,
    throttle: Callable[[Project, int], None],
) -> None:
    Authed = Annotated[Project, Depends(require_project)]  # noqa: N806

    def scoped(project: Project, project_id: str) -> None:
        if project_id != project.project_id:
            raise ApiError(404, "not_found", "Not found")

    base = "/api/v1/projects/{project_id}/deployments"

    @app.post(base, status_code=201, response_model=None)
    def record_deployment(
        project_id: str, body: DeploymentRequest, project: Authed
    ) -> JSONResponse:
        scoped(project, project_id)
        throttle(project, 1)
        try:
            result = service.record(
                project,
                environment=body.environment,
                release=body.release,
                deployed_at=body.deployed_at,
                service=body.service,
                commit_sha=body.commit_sha,
                repository_id=body.repository_id,
                deployment_id=body.deployment_id,
            )
        except DeploymentError as exc:
            raise deployment_error_to_api(exc) from None
        if result.status == "conflict":
            raise ApiError(
                409, "deployment_conflict", "A different deployment already uses this deployment_id"
            )
        # 201 for a new record, 200 when the same record was already stored (idempotent).
        return JSONResponse(
            status_code=201 if result.status == "created" else 200, content=result.record
        )

    @app.get(base)
    def list_deployments(
        project_id: str,
        project: Authed,
        environment: Annotated[str | None, Query(max_length=128)] = None,
        service_name: Annotated[str | None, Query(alias="service", max_length=128)] = None,
        release: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> dict[str, Any]:
        scoped(project, project_id)
        try:
            items, next_cursor = service.store.list(
                project.project_id,
                environment=environment.lower() if environment else None,
                service=service_name.lower() if service_name else None,
                release=release,
                limit=limit,
                cursor=cursor,
            )
        except (InvalidCursorError, ValueError):
            raise ApiError(400, "invalid_cursor", "Invalid pagination cursor") from None
        return {"items": items, "next_cursor": next_cursor}
