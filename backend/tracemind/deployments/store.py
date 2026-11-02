"""PostgreSQL access for deployment records. Records are immutable (an append-only trigger
rejects UPDATE and DELETE) and every read and write is scoped by `project_id`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from tracemind.storage.postgres import _decode_cursor, _encode_cursor, _iso
from tracemind.storage.tables import deployments


@dataclass(frozen=True)
class DeploymentRow:
    project_id: str
    deployment_id: str
    environment: str
    service: str | None
    release: str
    deployed_at: datetime
    repository_id: str | None
    commit_sha_reported: str | None
    commit_sha: str | None
    commit_confidence: str
    commit_source: str | None
    commit_reason: str | None
    source: str
    created_at: datetime
    content_hash: str


@dataclass(frozen=True)
class InsertResult:
    status: Literal["created", "duplicate", "conflict"]
    record: dict[str, Any]


def deployment_dict(row: Any) -> dict[str, Any]:
    return {
        "deployment_id": row["deployment_id"],
        "project_id": row["project_id"],
        "environment": row["environment"],
        "service": row["service"],
        "release": row["release"],
        "deployed_at": _iso(row["deployed_at"]),
        "repository_id": row["repository_id"],
        "source": row["source"],
        "commit_association": {
            "commit_sha": row["commit_sha"],
            "confidence": row["commit_confidence"],
            "source": row["commit_source"],
            "reason": row["commit_reason"],
            "reported": row["commit_sha_reported"],
        },
        "created_at": _iso(row["created_at"]),
    }


class DeploymentStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def insert(self, row: DeploymentRow) -> InsertResult:
        values = {
            "project_id": row.project_id,
            "deployment_id": row.deployment_id,
            "environment": row.environment,
            "service": row.service,
            "release": row.release,
            "deployed_at": row.deployed_at,
            "repository_id": row.repository_id,
            "commit_sha_reported": row.commit_sha_reported,
            "commit_sha": row.commit_sha,
            "commit_confidence": row.commit_confidence,
            "commit_source": row.commit_source,
            "commit_reason": row.commit_reason,
            "source": row.source,
            "created_at": row.created_at,
            "content_hash": row.content_hash,
        }
        with self._engine.begin() as conn:
            inserted = (
                conn.execute(
                    pg_insert(deployments)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=[deployments.c.project_id, deployments.c.deployment_id]
                    )
                    .returning(*deployments.c)
                )
                .mappings()
                .first()
            )
            if inserted is not None:
                return InsertResult("created", deployment_dict(inserted))
            existing = (
                conn.execute(
                    sa.select(deployments).where(
                        deployments.c.project_id == row.project_id,
                        deployments.c.deployment_id == row.deployment_id,
                    )
                )
                .mappings()
                .one()
            )
        same = existing["content_hash"] == row.content_hash
        return InsertResult("duplicate" if same else "conflict", deployment_dict(existing))

    def list(
        self,
        project_id: str,
        *,
        environment: str | None,
        service: str | None,
        release: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        q = sa.select(deployments).where(deployments.c.project_id == project_id)
        if environment:
            q = q.where(deployments.c.environment == environment)
        if service:
            q = q.where(deployments.c.service == service)
        if release:
            q = q.where(deployments.c.release == release)
        if cursor:
            deployed_at, deployment_id = _decode_cursor(cursor)
            q = q.where(
                sa.tuple_(deployments.c.deployed_at, deployments.c.deployment_id)
                < sa.tuple_(deployed_at, deployment_id)
            )
        q = q.order_by(deployments.c.deployed_at.desc(), deployments.c.deployment_id.desc()).limit(
            limit + 1
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(q).mappings())
        page = rows[:limit]
        nxt = (
            _encode_cursor(page[-1]["deployed_at"], page[-1]["deployment_id"])
            if len(rows) > limit
            else None
        )
        return [deployment_dict(r) for r in page], nxt
