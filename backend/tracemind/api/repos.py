"""Repository routes. Every route requires the project key and is scoped to that project:
a `{project_id}` that differs from the key's project, or a repository that belongs to another
project, is a 404 (never a 403), so other projects' existence is not revealed.

Nothing here accepts or returns a server filesystem path (registration takes one, once, and it
is never echoed), and no endpoint exposes a free-form graph or git query: `code` answers for one
file or one symbol name, `changes` lists stored change records."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from tracemind.api.errors import ApiError
from tracemind.projects import Project
from tracemind.repos.errors import RepoError
from tracemind.repos.scan_service import ScanService
from tracemind.repos.service import RepositoryService
from tracemind.storage.base import InvalidCursorError

_STATUS: dict[str, tuple[int, str]] = {
    "invalid_repository_path": (422, "invalid_repository_path"),
    "invalid_revision": (422, "invalid_revision"),
    "invalid_path": (422, "invalid_path"),
    "invalid_display_name": (422, "invalid_display_name"),
    "invalid_query": (422, "invalid_query"),
    "empty_repository": (422, "empty_repository"),
    "unsupported_object_format": (422, "unsupported_object_format"),
    "not_found": (404, "not_found"),
    "not_in_graph": (404, "not_in_graph"),
    "not_indexed": (409, "not_indexed"),
    "already_registered": (409, "already_registered"),
    "timeout": (504, "repository_timeout"),
    "output_too_large": (422, "repository_too_large"),
    "git_failed": (502, "repository_error"),
}
_MESSAGES = {
    "not_in_graph": "Not found in the current graph. That does not mean it is irrelevant:"
    " the graph is derived data and may not cover it.",
    "not_indexed": "This repository has no successfully indexed commit yet.",
}


def repo_error_to_api(exc: RepoError) -> ApiError:
    """Map a RepoError to the error envelope. Only stable codes are exposed, never input."""
    status, code = _STATUS.get(exc.code, (500, "internal_error"))
    details = [{"reason": exc.detail}] if exc.detail and status < 500 else None
    return ApiError(
        status, code, _MESSAGES.get(exc.code, "Repository request was rejected"), details=details
    )


class RegisterRepositoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: str | None = Field(default=None, min_length=1, max_length=255)


def install_repository_routes(
    app: FastAPI,
    *,
    require_project: Callable[..., Project],
    service: RepositoryService,
    scans: ScanService | None,
    throttle: Callable[[Project, int], None],
) -> None:
    Authed = Annotated[Project, Depends(require_project)]  # noqa: N806

    def scoped(project: Project, project_id: str) -> None:
        if project_id != project.project_id:
            raise ApiError(404, "not_found", "Not found")

    base = "/api/v1/projects/{project_id}/repositories"

    @app.post(base, status_code=201)
    def register_repository(
        project_id: str, body: RegisterRepositoryRequest, project: Authed
    ) -> dict[str, Any]:
        scoped(project, project_id)
        throttle(project, 1)
        try:
            return service.register(project.project_id, body.display_name, body.path)
        except RepoError as exc:
            raise repo_error_to_api(exc) from None

    @app.get(base)
    def list_repositories(
        project_id: str,
        project: Authed,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> dict[str, Any]:
        scoped(project, project_id)
        try:
            items, next_cursor = service.store.list_repositories(
                project.project_id, limit=limit, cursor=cursor
            )
        except (InvalidCursorError, ValueError):
            raise ApiError(400, "invalid_cursor", "Invalid pagination cursor") from None
        return {"items": items, "next_cursor": next_cursor}

    if scans is None:
        return

    @app.post(base + "/{repository_id}/scans", status_code=202, response_model=None)
    def queue_scan(
        project_id: str,
        repository_id: str,
        project: Authed,
        body: ScanRequest | None = None,
    ) -> JSONResponse:
        scoped(project, project_id)
        throttle(project, 1)
        try:
            scan, created = scans.enqueue(
                project.project_id, str(repository_id), body.revision if body else None
            )
        except RepoError as exc:
            raise repo_error_to_api(exc) from None
        # 202 for new work, 200 when the commit was already queued/running/indexed (idempotent).
        return JSONResponse(
            status_code=202 if created else 200, content={"scan": scan, "created": created}
        )

    @app.get(base + "/{repository_id}/status")
    def repository_status(project_id: str, repository_id: str, project: Authed) -> dict[str, Any]:
        scoped(project, project_id)
        doc = scans.repository_status(project.project_id, repository_id)
        if doc is None:
            raise ApiError(404, "not_found", "Not found")
        return doc

    @app.get(base + "/{repository_id}/changes")
    def repository_changes(
        project_id: str,
        repository_id: str,
        project: Authed,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        commit_sha: Annotated[str | None, Query(max_length=128)] = None,
        release: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, Any]:
        scoped(project, project_id)
        try:
            doc = scans.changes(
                project.project_id,
                repository_id,
                limit=limit,
                cursor=cursor,
                commit_sha=commit_sha,
                release=release,
            )
        except (InvalidCursorError, ValueError):
            raise ApiError(400, "invalid_cursor", "Invalid pagination cursor") from None
        if doc is None:
            raise ApiError(404, "not_found", "Not found")
        return doc

    @app.get(base + "/{repository_id}/code")
    def repository_code(
        project_id: str,
        repository_id: str,
        project: Authed,
        file: Annotated[str | None, Query(max_length=1024)] = None,
        symbol: Annotated[str | None, Query(max_length=200)] = None,
    ) -> dict[str, Any]:
        scoped(project, project_id)
        try:
            return scans.code_context(project.project_id, repository_id, file=file, symbol=symbol)
        except RepoError as exc:
            raise repo_error_to_api(exc) from None
