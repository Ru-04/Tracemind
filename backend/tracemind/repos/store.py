"""PostgreSQL access for repositories, scans, change records and scan events.

Every read is scoped by `project_id`. Server file paths are stored but never returned by the API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from tracemind.storage.postgres import _decode_cursor, _encode_cursor, _iso
from tracemind.storage.tables import repositories


class AlreadyRegisteredError(Exception):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


class RepositoryStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        return self._engine

    # ---- repositories -------------------------------------------------------------------

    def create_repository(
        self,
        *,
        project_id: str,
        display_name: str,
        path: str,
        identity: str,
        default_branch: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        repository_id = new_id()
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    pg_insert(repositories).values(
                        repository_id=repository_id,
                        project_id=project_id,
                        display_name=display_name,
                        path=path,
                        identity=identity,
                        default_branch=default_branch,
                        created_at=now,
                    )
                )
        except IntegrityError:
            raise AlreadyRegisteredError from None
        found = self.get_repository(project_id, repository_id)
        assert found is not None
        return found

    def get_repository(self, project_id: str, repository_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(repositories).where(
                        repositories.c.project_id == project_id,
                        repositories.c.repository_id == repository_id,
                    )
                )
                .mappings()
                .first()
            )
        return _repository_dict(row) if row is not None else None

    def list_repositories(
        self, project_id: str, *, limit: int, cursor: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        q = sa.select(repositories).where(repositories.c.project_id == project_id)
        if cursor:
            created, repo_id = _decode_cursor(cursor)
            q = q.where(
                sa.tuple_(repositories.c.created_at, repositories.c.repository_id)
                < sa.tuple_(created, repo_id)
            )
        q = q.order_by(repositories.c.created_at.desc(), repositories.c.repository_id.desc()).limit(
            limit + 1
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(q).mappings())
        page = rows[:limit]
        next_cursor = (
            _encode_cursor(page[-1]["created_at"], page[-1]["repository_id"])
            if len(rows) > limit
            else None
        )
        return [_repository_dict(r) for r in page], next_cursor


def _repository_dict(row: Any) -> dict[str, Any]:
    """Public view. `path` is deliberately omitted: server file layout is not exposed."""
    return {
        "repository_id": row["repository_id"],
        "project_id": row["project_id"],
        "display_name": row["display_name"],
        "identity": row["identity"],
        "default_branch": row["default_branch"],
        "indexed_sha": row["indexed_sha"],
        "indexed_scan_id": row["indexed_scan_id"],
        "graph_sha256": row["graph_sha256"],
        "created_at": _iso(row["created_at"]),
    }


def repository_path(engine: Engine, project_id: str, repository_id: str) -> str | None:
    """Server-side only: the stored path of a project's repository."""
    with engine.connect() as conn:
        return conn.execute(
            sa.select(repositories.c.path).where(
                repositories.c.project_id == project_id,
                repositories.c.repository_id == repository_id,
            )
        ).scalar_one_or_none()
