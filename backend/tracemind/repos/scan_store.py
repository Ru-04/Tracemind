"""The PostgreSQL job queue for scans, plus change records and the append-only scan history.

Concurrency model
- A scan row *is* the job. Workers claim with `FOR UPDATE SKIP LOCKED`, so two workers never
  receive the same row, and a per-repository advisory lock keeps two scans of one repository
  from running at once.
- A claim sets a lease. A worker that dies simply stops renewing it; after the lease expires
  the scan is claimed again (event `lease_expired`) until `max_attempts` is exhausted, after
  which it is failed with `worker_lost`.
- Completion is *fenced*: it only succeeds while the row is still `running` and still locked by
  the completing worker. A worker that lost its lease can therefore never publish a result.
- Success is one transaction: scan row, change record, repository pointer, commit cache and
  history event commit together or not at all.
- `scan_events` and `change_records` are append-only (database triggers).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from tracemind.repos.git_adapter import CommitInfo
from tracemind.repos.store import new_id
from tracemind.storage.postgres import _decode_cursor, _encode_cursor, _iso
from tracemind.storage.tables import (
    change_records,
    repositories,
    repository_commits,
    repository_scans,
    scan_events,
)

scans = repository_scans


class LostLeaseError(Exception):
    """This worker no longer owns the scan (lease expired and was re-claimed, or it finished)."""


@dataclass(frozen=True)
class ScanOutcome:
    """Everything a successful scan publishes, committed atomically."""

    graph_tool: str
    graph_tool_version: str
    graph_schema_version: int
    graph_sha256: str
    node_count: int
    edge_count: int
    inferred_edge_count: int
    files_in_snapshot: int
    files_in_graph: int
    coverage: dict[str, Any]
    kind: str
    base_sha: str | None
    files: dict[str, Any]
    graph_delta: dict[str, Any]
    commits: Sequence[CommitInfo] = field(default_factory=tuple)


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _scan_public(row: Any) -> dict[str, Any]:
    return {
        "scan_id": row["scan_id"],
        "repository_id": row["repository_id"],
        "status": row["status"],
        "kind": row["kind"],
        "target_sha": row["target_sha"],
        "base_sha": row["base_sha"],
        "graph_tool": row["graph_tool"],
        "graph_tool_version": row["graph_tool_version"],
        "graph_schema_version": row["graph_schema_version"],
        "graph_sha256": row["graph_sha256"],
        "node_count": row["node_count"],
        "edge_count": row["edge_count"],
        "inferred_edge_count": row["inferred_edge_count"],
        "files_in_snapshot": row["files_in_snapshot"],
        "files_in_graph": row["files_in_graph"],
        "coverage": row["coverage"],
        "attempt": row["attempt"],
        "max_attempts": row["max_attempts"],
        "error_code": row["error_code"],
        "queued_at": _iso(row["queued_at"]),
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
    }


def _record_public(row: Any) -> dict[str, Any]:
    return {
        "record_id": row["record_id"],
        "repository_id": row["repository_id"],
        "scan_id": row["scan_id"],
        "base_sha": row["base_sha"],
        "target_sha": row["target_sha"],
        "files": row["files"],
        "commits": row["commits"],
        "graph_sha256": row["graph_sha256"],
        "graph_delta": row["graph_delta"],
        "created_at": _iso(row["created_at"]),
        "content_hash": row["content_hash"],
    }


class ScanStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- enqueue (idempotent) -----------------------------------------------------------

    def enqueue_scan(
        self,
        *,
        project_id: str,
        repository_id: str,
        target_sha: str,
        max_attempts: int,
        now: datetime,
    ) -> tuple[dict[str, Any], bool]:
        """Queue a scan of `target_sha`. If one is already queued/running/succeeded for that
        commit, return it instead (created=False): re-triggering never duplicates work."""
        existing = self._existing(project_id, repository_id, target_sha)
        if existing is not None:
            return existing, False
        scan_id = new_id()
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.insert(scans).values(
                        scan_id=scan_id,
                        repository_id=repository_id,
                        project_id=project_id,
                        kind="full",  # the real kind and base are decided when the scan starts
                        status="queued",
                        target_sha=target_sha,
                        attempt=0,
                        max_attempts=max_attempts,
                        available_at=now,
                        queued_at=now,
                    )
                )
                self._event(conn, scan_id, repository_id, project_id, now, "queued", 0)
        except IntegrityError:
            raced = self._existing(project_id, repository_id, target_sha)
            if raced is None:
                raise
            return raced, False
        created = self.get_scan(project_id, scan_id)
        assert created is not None
        return created, True

    def _existing(
        self, project_id: str, repository_id: str, target_sha: str
    ) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(scans)
                    .where(
                        scans.c.project_id == project_id,
                        scans.c.repository_id == repository_id,
                        scans.c.target_sha == target_sha,
                        scans.c.status.in_(("queued", "running", "succeeded")),
                    )
                    .order_by(scans.c.queued_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return _scan_public(row) if row is not None else None

    # ---- claim / heartbeat --------------------------------------------------------------

    def claim_next(
        self, *, worker_id: str, now: datetime, lease_seconds: float
    ) -> dict[str, Any] | None:
        excluded: list[str] = []
        while True:
            with self._engine.begin() as conn:
                result = self._claim_once(conn, worker_id, now, lease_seconds, excluded)
            if result == "retry":
                continue
            return result if isinstance(result, dict) else None

    def _claim_once(
        self,
        conn: Connection,
        worker_id: str,
        now: datetime,
        lease_seconds: float,
        excluded: list[str],
    ) -> dict[str, Any] | str | None:
        candidate = (
            sa.select(scans)
            .where(
                sa.or_(
                    sa.and_(scans.c.status == "queued", scans.c.available_at <= now),
                    sa.and_(scans.c.status == "running", scans.c.lease_expires_at < now),
                ),
                scans.c.scan_id.notin_(excluded) if excluded else sa.true(),
            )
            .order_by(scans.c.queued_at, scans.c.scan_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = conn.execute(candidate).mappings().first()
        if row is None:
            return None
        # One scan per repository at a time: serialize on an advisory lock, then re-check.
        conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": row["repository_id"]},
        )
        busy = conn.execute(
            sa.select(sa.func.count())
            .select_from(scans)
            .where(
                scans.c.repository_id == row["repository_id"],
                scans.c.status == "running",
                scans.c.lease_expires_at >= now,
                scans.c.scan_id != row["scan_id"],
            )
        ).scalar_one()
        if busy:
            excluded.append(row["scan_id"])
            return "retry"

        project_id, repository_id, scan_id = row["project_id"], row["repository_id"], row["scan_id"]
        if row["status"] == "running":  # the previous worker stopped renewing its lease
            self._event(
                conn,
                scan_id,
                repository_id,
                project_id,
                now,
                "lease_expired",
                row["attempt"],
                worker_id=row["locked_by"],
            )
            if row["attempt"] >= row["max_attempts"]:
                conn.execute(
                    sa.update(scans)
                    .where(scans.c.scan_id == scan_id)
                    .values(
                        status="failed",
                        error_code="worker_lost",
                        finished_at=now,
                        locked_by=None,
                        lease_expires_at=None,
                    )
                )
                self._event(
                    conn,
                    scan_id,
                    repository_id,
                    project_id,
                    now,
                    "failed",
                    row["attempt"],
                    error_code="worker_lost",
                )
                excluded.append(scan_id)
                return "retry"
        attempt = row["attempt"] + 1
        conn.execute(
            sa.update(scans)
            .where(scans.c.scan_id == scan_id)
            .values(
                status="running",
                attempt=attempt,
                locked_by=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                started_at=now,
                error_code=None,
            )
        )
        self._event(
            conn, scan_id, repository_id, project_id, now, "claimed", attempt, worker_id=worker_id
        )
        claimed = conn.execute(sa.select(scans).where(scans.c.scan_id == scan_id)).mappings().one()
        return dict(claimed)

    def heartbeat(
        self, *, scan_id: str, worker_id: str, now: datetime, lease_seconds: float
    ) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(scans)
                .where(
                    scans.c.scan_id == scan_id,
                    scans.c.status == "running",
                    scans.c.locked_by == worker_id,
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
        return bool(result.rowcount)

    def set_plan(self, *, scan_id: str, worker_id: str, kind: str, base_sha: str | None) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(scans)
                .where(
                    scans.c.scan_id == scan_id,
                    scans.c.status == "running",
                    scans.c.locked_by == worker_id,
                )
                .values(kind=kind, base_sha=base_sha)
            )
        if not result.rowcount:
            raise LostLeaseError

    def record_event(
        self,
        scan: dict[str, Any],
        *,
        event: str,
        now: datetime,
        worker_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            self._event(
                conn,
                scan["scan_id"],
                scan["repository_id"],
                scan["project_id"],
                now,
                event,
                scan["attempt"],
                worker_id=worker_id,
                error_code=error_code,
            )

    # ---- completion (fenced) ------------------------------------------------------------

    def complete_success(
        self, *, scan: dict[str, Any], worker_id: str, now: datetime, outcome: ScanOutcome
    ) -> dict[str, Any]:
        scan_id, repository_id, project_id = (
            scan["scan_id"],
            scan["repository_id"],
            scan["project_id"],
        )
        record_id = new_id()
        content_hash = hashlib.sha256(
            canonical(
                [
                    repository_id,
                    outcome.base_sha,
                    scan["target_sha"],
                    outcome.files,
                    [c.sha for c in outcome.commits],
                    outcome.graph_sha256,
                    outcome.graph_delta,
                ]
            ).encode("utf-8")
        ).hexdigest()
        with self._engine.begin() as conn:
            updated = conn.execute(
                sa.update(scans)
                .where(
                    scans.c.scan_id == scan_id,
                    scans.c.status == "running",
                    scans.c.locked_by == worker_id,
                )
                .values(
                    status="succeeded",
                    kind=outcome.kind,
                    base_sha=outcome.base_sha,
                    graph_tool=outcome.graph_tool,
                    graph_tool_version=outcome.graph_tool_version,
                    graph_schema_version=outcome.graph_schema_version,
                    graph_sha256=outcome.graph_sha256,
                    node_count=outcome.node_count,
                    edge_count=outcome.edge_count,
                    inferred_edge_count=outcome.inferred_edge_count,
                    files_in_snapshot=outcome.files_in_snapshot,
                    files_in_graph=outcome.files_in_graph,
                    coverage=outcome.coverage,
                    finished_at=now,
                    locked_by=None,
                    lease_expires_at=None,
                    error_code=None,
                )
            )
            if not updated.rowcount:
                raise LostLeaseError
            conn.execute(
                sa.insert(change_records).values(
                    record_id=record_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    scan_id=scan_id,
                    base_sha=outcome.base_sha,
                    target_sha=scan["target_sha"],
                    files=outcome.files,
                    commits=[c.sha for c in outcome.commits],
                    graph_sha256=outcome.graph_sha256,
                    graph_delta=outcome.graph_delta,
                    created_at=now,
                    content_hash=content_hash,
                )
            )
            for commit in outcome.commits:
                conn.execute(
                    pg_insert(repository_commits)
                    .values(
                        repository_id=repository_id,
                        sha=commit.sha,
                        parent_shas=list(commit.parents),
                        authored_at=commit.authored_at,
                        committed_at=commit.committed_at,
                        subject=commit.subject,
                        retrieved_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["repository_id", "sha"])
                )
            conn.execute(
                sa.update(repositories)
                .where(repositories.c.repository_id == repository_id)
                .values(
                    indexed_sha=scan["target_sha"],
                    indexed_scan_id=scan_id,
                    graph_sha256=outcome.graph_sha256,
                )
            )
            self._event(
                conn,
                scan_id,
                repository_id,
                project_id,
                now,
                "succeeded",
                scan["attempt"],
                worker_id=worker_id,
            )
            row = (
                conn.execute(
                    sa.select(change_records).where(change_records.c.record_id == record_id)
                )
                .mappings()
                .one()
            )
        return _record_public(row)

    def fail_attempt(
        self,
        *,
        scan: dict[str, Any],
        worker_id: str,
        now: datetime,
        error_code: str,
        retryable: bool,
        retry_delay_seconds: float,
    ) -> str:
        """Record a failed attempt: requeue with backoff while attempts remain, else fail for good.
        Returns the resulting status. Fenced like completion."""
        scan_id, repository_id, project_id = (
            scan["scan_id"],
            scan["repository_id"],
            scan["project_id"],
        )
        with self._engine.begin() as conn:
            current = (
                conn.execute(
                    sa.select(scans.c.attempt, scans.c.max_attempts)
                    .where(
                        scans.c.scan_id == scan_id,
                        scans.c.status == "running",
                        scans.c.locked_by == worker_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if current is None:
                raise LostLeaseError
            if retryable and current["attempt"] < current["max_attempts"]:
                conn.execute(
                    sa.update(scans)
                    .where(scans.c.scan_id == scan_id)
                    .values(
                        status="queued",
                        error_code=error_code,
                        locked_by=None,
                        lease_expires_at=None,
                        available_at=now + timedelta(seconds=retry_delay_seconds),
                    )
                )
                self._event(
                    conn,
                    scan_id,
                    repository_id,
                    project_id,
                    now,
                    "retry_scheduled",
                    current["attempt"],
                    worker_id=worker_id,
                    error_code=error_code,
                )
                return "queued"
            conn.execute(
                sa.update(scans)
                .where(scans.c.scan_id == scan_id)
                .values(
                    status="failed",
                    error_code=error_code,
                    finished_at=now,
                    locked_by=None,
                    lease_expires_at=None,
                )
            )
            self._event(
                conn,
                scan_id,
                repository_id,
                project_id,
                now,
                "failed",
                current["attempt"],
                worker_id=worker_id,
                error_code=error_code,
            )
            return "failed"

    # ---- reads (project-scoped) ---------------------------------------------------------

    def get_scan(self, project_id: str, scan_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(scans).where(
                        scans.c.project_id == project_id, scans.c.scan_id == scan_id
                    )
                )
                .mappings()
                .first()
            )
        return _scan_public(row) if row is not None else None

    def latest_scan(self, project_id: str, repository_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(scans)
                    .where(scans.c.project_id == project_id, scans.c.repository_id == repository_id)
                    .order_by(scans.c.queued_at.desc(), scans.c.scan_id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return _scan_public(row) if row is not None else None

    def scan_history(self, project_id: str, scan_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(scan_events)
                .where(scan_events.c.project_id == project_id, scan_events.c.scan_id == scan_id)
                .order_by(scan_events.c.event_id)
            ).mappings()
            return [
                {
                    "at": _iso(r["at"]),
                    "event": r["event"],
                    "attempt": r["attempt"],
                    "worker_id": r["worker_id"],
                    "error_code": r["error_code"],
                }
                for r in rows
            ]

    def list_change_records(
        self,
        project_id: str,
        repository_id: str,
        *,
        limit: int,
        cursor: str | None,
        contains_commit: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        q = sa.select(change_records).where(
            change_records.c.project_id == project_id,
            change_records.c.repository_id == repository_id,
        )
        if contains_commit:
            q = q.where(change_records.c.commits.contains([contains_commit]))
        if cursor:
            created, record_id = _decode_cursor(cursor)
            q = q.where(
                sa.tuple_(change_records.c.created_at, change_records.c.record_id)
                < sa.tuple_(created, record_id)
            )
        q = q.order_by(change_records.c.created_at.desc(), change_records.c.record_id.desc()).limit(
            limit + 1
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(q).mappings())
        page = rows[:limit]
        nxt = (
            _encode_cursor(page[-1]["created_at"], page[-1]["record_id"])
            if len(rows) > limit
            else None
        )
        return [_record_public(r) for r in page], nxt

    def commit_details(self, repository_id: str, shas: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Cached metadata (id, parents, dates, redacted subject) for commits of one repository.
        No author name or e-mail is stored, so none can be returned."""
        if not shas:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(repository_commits).where(
                    repository_commits.c.repository_id == repository_id,
                    repository_commits.c.sha.in_(list(shas)),
                )
            ).mappings()
            return {
                r["sha"]: {
                    "sha": r["sha"],
                    "parents": r["parent_shas"],
                    "authored_at": _iso(r["authored_at"]),
                    "committed_at": _iso(r["committed_at"]),
                    "subject": r["subject"],
                }
                for r in rows
            }

    # ---- internals ----------------------------------------------------------------------

    @staticmethod
    def _event(
        conn: Connection,
        scan_id: str,
        repository_id: str,
        project_id: str,
        now: datetime,
        event: str,
        attempt: int,
        *,
        worker_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        conn.execute(
            sa.insert(scan_events).values(
                scan_id=scan_id,
                repository_id=repository_id,
                project_id=project_id,
                at=now,
                event=event,
                attempt=attempt,
                worker_id=worker_id,
                error_code=error_code,
            )
        )
