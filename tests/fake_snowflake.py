"""An in-memory stand-in for the Snowflake session boundary, for OFFLINE tests only.

What this proves and what it does not: it lets the tests exercise TraceMind's own logic (batching,
de-duplication, leases, halting, back-off, validation, caps, evidence references, degraded
answers, metadata logging). It encodes the Snowflake behaviour the DOCUMENTATION describes, as
switches the tests can flip. It does NOT prove that Snowflake behaves that way: whether MERGE needs
only INSERT+SELECT, how it behaves under concurrency, and whether the statements are even
accepted are live checks (M3-12, M3-13), not run.

The fake is deliberately strict about what it accepts: any statement that is not one of
TraceMind's static templates, or that is sent on the wrong role, is a test failure. That makes
"there is no path for free-form SQL" (M3-2) something the whole suite keeps checking.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tracemind.snowflake import sql as S
from tracemind.snowflake.session import (
    Role,
    SnowflakeError,
    SnowflakeRejected,
    SnowflakeSession,
    SnowflakeTimeout,
    StatementResult,
)

EVENT_COLUMNS = (
    "EVENT_ID,SERVICE,ENVIRONMENT,OCCURRED_AT,SEVERITY,ERROR_TYPE,MESSAGE,"
    "FINGERPRINT,RELEASE,COMMIT_SHA,TRACE_ID"
).split(",")
INCIDENT_COLUMNS = (
    "INCIDENT_ID,SERVICE,ENVIRONMENT,FINGERPRINT,ERROR_TYPE,SEVERITY,STATUS,"
    "FIRST_SEEN,LAST_SEEN,OCCURRENCE_COUNT,REOPEN_COUNT,SUMMARY"
).split(",")


@dataclass(frozen=True)
class Statement:
    role: str
    kind: str  # create_tmp | insert_tmp | merge | audit_batch | audit_all | <reader function>
    sql: str
    params: tuple[Any, ...]


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class FakeSnowflake:
    """The shared 'account' (tables, switches, a log) and the session factory."""

    available = True

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {s.target: [] for s in S.STREAMS.values()}
        self.statements: list[Statement] = []
        self.opened: list[tuple[Role, int, int]] = []
        self.closed = 0
        # switches
        self.unreachable: SnowflakeError | None = None
        self.refuse_merge: SnowflakeError | None = None
        self.merge_ignores_existing = False  # a MERGE that does not see rows already there
        self.timeout_functions: set[str] = set()
        self.fail_next: dict[str, list[Exception]] = {}
        self.delay_seconds = 0.0
        self._tmp: dict[str, list[dict[str, Any]]] = {}

    # ---- factory ------------------------------------------------------------------------------

    def open(
        self, role: Role, *, lock_timeout_seconds: int, statement_timeout_seconds: int
    ) -> SnowflakeSession:
        if self.unreachable is not None:
            raise self.unreachable
        self.opened.append((role, lock_timeout_seconds, statement_timeout_seconds))
        return FakeSession(self, role)

    # ---- helpers for tests ------------------------------------------------------------------------

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.tables[table]

    def kinds(self, role: str | None = None) -> list[str]:
        return [s.kind for s in self.statements if role is None or s.role == role]

    def key_counts(self, table: str, *key: str) -> dict[tuple[Any, ...], int]:
        counts: dict[tuple[Any, ...], int] = {}
        for row in self.tables[table]:
            k = tuple(row[c] for c in key)
            counts[k] = counts.get(k, 0) + 1
        return counts

    def duplicates(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for spec in S.STREAMS.values():
            dupes = [k for k, n in self.key_counts(spec.target, *spec.key).items() if n > 1]
            out[spec.stream] = len(dupes)
        return out


class FakeSession:
    def __init__(self, account: FakeSnowflake, role: Role) -> None:
        self._a = account
        self._role = role
        self._open = True

    def close(self) -> None:
        self._a.closed += 1
        self._open = False

    # ---- dispatch -------------------------------------------------------------------------------------

    def _kind(self, sql: str) -> tuple[str, S.StreamSql | None]:
        for spec in S.STREAMS.values():
            for kind in ("create_tmp", "insert_tmp", "merge", "audit_batch", "audit_all"):
                if sql == getattr(spec, kind):
                    return kind, spec
        for name, template in S.READER_TEMPLATES.items():
            if sql == template:
                return name, None
        raise AssertionError("statement is not one of TraceMind's static templates")

    def _record(self, kind: str, sql: str, params: Sequence[Any]) -> None:
        assert self._open, "statement on a closed session"
        assert sql.count("?") == len(params), f"{kind}: wrong number of bind parameters"
        self._a.statements.append(Statement(self._role, kind, sql, tuple(params)))
        if self._a.delay_seconds:
            time.sleep(self._a.delay_seconds)

    def _maybe_fail(self, kind: str) -> None:
        queue = self._a.fail_next.get(kind)
        if queue:
            raise queue.pop(0)

    def execute(
        self, sql: str, params: Sequence[Any] = (), *, timeout_seconds: float, query_tag: str
    ) -> StatementResult:
        assert timeout_seconds > 0 and query_tag.startswith("tracemind:")
        kind, spec = self._kind(sql)
        self._record(kind, sql, params)
        self._maybe_fail(kind)
        writer_kinds = {"create_tmp", "insert_tmp", "merge", "audit_batch", "audit_all"}
        if (kind in writer_kinds) != (self._role == "writer"):
            # the reader holds SELECT on views only; the writer is not a reader of views
            raise SnowflakeRejected("insufficient_privileges")
        if spec is not None:
            return self._writer(kind, spec)
        if kind in self._a.timeout_functions:
            raise SnowflakeTimeout()
        return self._reader(kind, params)

    def executemany(
        self,
        sql: str,
        rows: Sequence[Sequence[Any]],
        *,
        timeout_seconds: float,
        query_tag: str,
    ) -> StatementResult:
        assert timeout_seconds > 0 and query_tag.startswith("tracemind:")
        kind, spec = self._kind(sql)
        assert kind == "insert_tmp" and spec is not None, "executemany only loads a batch table"
        for row in rows:
            assert len(row) == sql.count("?")
        self._record(kind, sql, rows[0] if rows else ())
        self._maybe_fail(kind)
        if self._role != "writer":
            raise SnowflakeRejected("insufficient_privileges")
        names = [c.name for c in spec.columns]
        self._a._tmp[spec.tmp] = [dict(zip(names, row, strict=True)) for row in rows]
        return StatementResult(rowcount=len(rows), query_id="q-insert")

    # ---- the writer's statements ----------------------------------------------------------------------

    def _writer(self, kind: str, spec: S.StreamSql) -> StatementResult:
        a = self._a
        if kind == "create_tmp":
            a._tmp[spec.tmp] = []
            return StatementResult(query_id="q-create")
        if kind == "merge":
            if a.refuse_merge is not None:
                raise a.refuse_merge
            source: dict[tuple[Any, ...], dict[str, Any]] = {}
            for row in sorted(a._tmp[spec.tmp], key=lambda r: str(r.get(_order(spec)))):
                source.setdefault(tuple(row[k] for k in spec.key), row)  # QUALIFY ROW_NUMBER() = 1
            present = (
                set()
                if a.merge_ignores_existing
                else {tuple(r[k] for k in spec.key) for r in a.tables[spec.target]}
            )
            inserted = 0
            for key, row in source.items():
                if key in present:
                    continue
                target = {c.target_name: row[c.name] for c in spec.columns}
                if "attributes" in target:
                    target["attributes"] = json.loads(target["attributes"])
                target["exported_at"] = datetime.now(UTC).isoformat()
                a.tables[spec.target].append(target)
                inserted += 1
            return StatementResult(rows=((inserted,),), rowcount=inserted, query_id="q-merge")
        counts = self._counts(spec, only_batch=(kind == "audit_batch"))
        dupes = tuple((*k, n) for k, n in counts.items() if n > 1)
        return StatementResult(
            columns=(*spec.key, "N"), rows=dupes, rowcount=len(dupes), query_id="q-audit"
        )

    def _counts(self, spec: S.StreamSql, *, only_batch: bool) -> dict[tuple[Any, ...], int]:
        wanted = {tuple(r[k] for k in spec.key) for r in self._a._tmp.get(spec.tmp, [])}
        counts: dict[tuple[Any, ...], int] = {}
        for row in self._a.tables[spec.target]:
            key = tuple(row[k] for k in spec.key)
            if only_batch and key not in wanted:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    # ---- the reader's statements (a Python rendering of the documented SQL semantics) ------

    def _reader(self, function: str, params: Sequence[Any]) -> StatementResult:
        a = self._a
        if function.startswith("events_for_"):
            rows = a.tables["TELEMETRY.EVENTS"]
            project, *rest = params
            limit = rest[-1]
            start, end = _ts(rest[-3]), _ts(rest[-2])
            match = {
                "events_for_fingerprint": lambda r: r["fingerprint"] == rest[0],
                "events_for_service_window": lambda r: (
                    r["service"] == rest[0] and r["environment"] == rest[1]
                ),
                "events_for_verified_commit": lambda r: r["commit_sha"] == rest[0],
            }[function]
            hits = [
                r
                for r in rows
                if r["project_id"] == project and match(r) and start <= _ts(r["occurred_at"]) < end
            ]
            hits.sort(key=lambda r: (_ts(r["occurred_at"]), r["event_id"]), reverse=True)
            out = tuple(
                tuple(
                    _ts(r[c.lower()]) if c == "OCCURRED_AT" else r.get(c.lower())
                    for c in EVENT_COLUMNS
                )
                for r in hits[:limit]
            )
            return StatementResult(tuple(EVENT_COLUMNS), out, len(out), "q-read")
        if function.startswith("similar_incidents"):
            project = params[0]
            limit = params[-1]
            start, end = _ts(params[-3]), _ts(params[-2])
            skip = params[-4]
            latest: dict[str, dict[str, Any]] = {}
            for r in a.tables["KNOWLEDGE.INCIDENT_SUMMARIES"]:
                if r["project_id"] != project:
                    continue
                cur = latest.get(r["incident_id"])
                if cur is None or r["summary_version"] > cur["summary_version"]:
                    latest[r["incident_id"]] = r
            if function.endswith("by_fingerprint"):
                keep = lambda r: r["fingerprint"] == params[1]  # noqa: E731
            else:
                keep = lambda r: r["service"] == params[1] and r["error_type"] == params[2]  # noqa: E731
            hits = [
                r
                for r in latest.values()
                if keep(r) and r["incident_id"] != skip and start <= _ts(r["last_seen"]) < end
            ]
            hits.sort(key=lambda r: (_ts(r["last_seen"]), r["incident_id"]), reverse=True)
            out = tuple(
                tuple(
                    _ts(r[c.lower()]) if c in ("FIRST_SEEN", "LAST_SEEN") else r.get(c.lower())
                    for c in INCIDENT_COLUMNS
                )
                for r in hits[:limit]
            )
            return StatementResult(tuple(INCIDENT_COLUMNS), out, len(out), "q-read")
        # documented_resolutions: INVESTIGATION_RESULTS stays empty until M4
        return StatementResult(
            ("INCIDENT_ID", "FINGERPRINT", "RESOLUTION_STATE", "NOTE", "RECORDED_AT"),
            (),
            0,
            "q-read",
        )


def _order(spec: S.StreamSql) -> str:
    return {
        "events": "occurred_at",
        "incident_summaries": "last_seen",
        "deployments": "deployed_at",
    }[spec.stream]
