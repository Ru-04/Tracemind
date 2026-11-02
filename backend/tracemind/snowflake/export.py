"""The one-way export of PostgreSQL data to Snowflake (M3 plan 4.5).

Design, and what is NOT proven:

- OFF by default and never started by ingestion. A run happens only when enabled, at least
  `export_min_interval_seconds` (floor 7,200 s) after the previous attempt, and only when there
  is a backlog: with nothing to send the warehouse is never touched (cost control, owner O4).
- ONE exporter at a time, enforced in PostgreSQL by an advisory lock. This is the primary guard
  against duplicates, because Snowflake does not enforce the key (ADR-0007 amendment).
- Each batch: de-duplicate, load a session TEMPORARY table, run the insert-only MERGE, audit the
  batch's keys for duplicates, and only then mark the rows exported in PostgreSQL.
- A duplicate found by the audit, or a privilege refusal, HALTS the exporter until an operator
  clears it. It does not retry into a problem and it never asks for more privileges (O7).
- Failures back off; ingestion, grouping and the lifecycle never notice.

Everything above is exercised against an in-memory fake of the session boundary. That proves
this module's logic, NOT Snowflake's behaviour: MERGE privileges, MERGE under concurrency and
re-run idempotence are checks M3-12 and M3-13 and have NOT been run.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from tracemind.config import ConfigError
from tracemind.observability.logging import LOGGER_NAME, log
from tracemind.snowflake import export_source as src
from tracemind.snowflake.config import TEST_RUN_ID, SnowflakeSettings
from tracemind.snowflake.consumption import CallLog
from tracemind.snowflake.session import (
    MeteredSession,
    SessionFactory,
    SnowflakeError,
    SnowflakeRejected,
    SnowflakeSession,
    StatementResult,
)
from tracemind.snowflake.sql import STREAMS, StreamSql
from tracemind.storage.postgres import _iso
from tracemind.storage.tables import export_state

_logger = logging.getLogger(LOGGER_NAME + ".export")

STREAM_ORDER = ("events", "incident_summaries", "deployments")
MAX_BATCHES_PER_RUN = 10  # per stream; with 1,000 rows per batch a run sends at most 10,000 rows
BACKOFF_BASE_SECONDS = 900
BACKOFF_CAP_SECONDS = 6 * 3600
EXPORT_STATEMENT_TIMEOUT_SECONDS = 120
ADVISORY_LOCK_KEY = 0x54524D4E_D0E5  # "TRMN" + export; one key, one exporter
QUERY_TAG = "tracemind:export"

HALT_DUPLICATES = "duplicate_rows_detected"
HALT_PRIVILEGE = "privilege_denied"


@dataclass
class StreamResult:
    sent: int = 0
    inserted: int | None = 0  # None when the service did not report a count
    batches: int = 0


@dataclass
class ExportOutcome:
    status: str  # disabled|halted|backoff|too_soon|budget_exceeded|busy|no_backlog|exported|failed
    streams: dict[str, StreamResult] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True)
class ExportState:
    state: str = "idle"
    last_started_at: datetime | None = None
    last_success_at: datetime | None = None
    next_attempt_at: datetime | None = None
    consecutive_failures: int = 0
    error_code: str | None = None


class _Halt(Exception):  # noqa: N818 - control flow inside this module
    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ExportStateStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self) -> ExportState:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.select(export_state).where(export_state.c.id == 1))
                .mappings()
                .first()
            )
        if row is None:
            return ExportState()
        return ExportState(
            row["state"],
            row["last_started_at"],
            row["last_success_at"],
            row["next_attempt_at"],
            row["consecutive_failures"],
            row["error_code"],
        )

    def save(self, state: ExportState, now: datetime) -> None:
        values = {
            "id": 1,
            "state": state.state,
            "last_started_at": state.last_started_at,
            "last_success_at": state.last_success_at,
            "next_attempt_at": state.next_attempt_at,
            "consecutive_failures": state.consecutive_failures,
            "error_code": state.error_code,
            "updated_at": now,
        }
        stmt = pg_insert(export_state).values(**values)
        with self._engine.begin() as conn:
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[export_state.c.id],
                    set_={k: stmt.excluded[k] for k in values if k != "id"},
                )
            )


def _dedupe(candidates: list[src.Candidate]) -> list[src.Candidate]:
    seen: set[tuple[str, ...]] = set()
    unique: list[src.Candidate] = []
    for c in candidates:
        if c.sf_key not in seen:
            seen.add(c.sf_key)
            unique.append(c)
    return unique


def _inserted(result: StatementResult) -> int | None:
    """The number of rows a MERGE inserted. The result shape is UNVERIFIED (live check)."""
    if result.rows and result.rows[0] and isinstance(result.rows[0][0], int):
        return int(result.rows[0][0])
    return result.rowcount


class Exporter:
    def __init__(
        self,
        engine: Engine,
        settings: SnowflakeSettings,
        sessions: SessionFactory,
        calls: CallLog,
        *,
        clock: Callable[[], datetime],
        test_run_id: str | None = None,
    ) -> None:
        targets_test_database = settings.database == settings.test_database
        if targets_test_database and not (test_run_id and TEST_RUN_ID.match(test_run_id)):
            raise ConfigError("exporting to the test database requires a well-formed test run id")
        if not targets_test_database and test_run_id is not None:
            raise ConfigError("a test run id may only be used with the test database")
        self._engine = engine
        self._s = settings
        self._sessions = sessions
        self._calls = calls
        self._clock = clock
        self._test_run_id = test_run_id
        self._states = ExportStateStore(engine)

    # ---- public ---------------------------------------------------------------------------

    def run_once(self) -> ExportOutcome:
        now = self._clock()
        if not self._s.enabled:
            return ExportOutcome("disabled")
        with self._lease() as acquired:
            if not acquired:
                return ExportOutcome("busy")
            state = self._states.load()
            if state.state == "halted":
                return ExportOutcome("halted", error_code=state.error_code)
            if state.next_attempt_at is not None and now < state.next_attempt_at:
                return ExportOutcome("backoff", error_code=state.error_code)
            if state.last_started_at is not None and now - state.last_started_at < timedelta(
                seconds=self._s.export_min_interval_seconds
            ):
                return ExportOutcome("too_soon")
            if self._calls.estimate(now, self._s.soft_budget_credits).state == "exceeded":
                return ExportOutcome("budget_exceeded")
            severities = src.severities_at_least(self._s.export_min_severity)
            with self._engine.connect() as conn:
                if not any(src.backlog(conn, severities=severities).values()):
                    return ExportOutcome("no_backlog")  # the warehouse is never touched
            return self._run(now, state, severities)

    def health(self) -> str:
        """A cheap summary for /ready: disabled | ok | degraded | halted | unavailable."""
        if not self._s.enabled:
            return "disabled"
        if not getattr(self._sessions, "available", True):
            return "unavailable"  # enabled, but no connector adapter is installed
        state = self._states.load()
        if state.state == "halted":
            return "halted"
        return "degraded" if state.state == "degraded" else "ok"

    def clear_halt(self) -> None:
        now = self._clock()
        state = self._states.load()
        self._states.save(
            ExportState("idle", state.last_started_at, state.last_success_at, None, 0, None),
            now,
        )

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        now = self._clock()
        state = self._states.load()
        budget = self._calls.estimate(now, self._s.soft_budget_credits)
        severities = src.severities_at_least(self._s.export_min_severity)
        with self._engine.connect() as conn:
            counts = src.backlog(conn, severities=severities, project_id=project_id)
        if not self._s.enabled:
            overall = "disabled"
        elif state.state == "halted":
            overall = "halted"
        elif budget.state == "exceeded":
            overall = "budget_exceeded"
        elif state.state == "degraded":
            overall = "degraded"
        else:
            overall = "idle"
        return {
            "enabled": self._s.enabled,
            "state": overall,
            "backlog": counts,
            "backlog_cap": src.BACKLOG_CAP,
            "last_started_at": _iso(state.last_started_at),
            "last_success_at": _iso(state.last_success_at),
            "next_attempt_at": _iso(state.next_attempt_at),
            "consecutive_failures": state.consecutive_failures,
            "error_code": state.error_code,
            "min_interval_seconds": self._s.export_min_interval_seconds,
            "budget": budget.to_json(),
        }

    # ---- internals --------------------------------------------------------------------------

    @contextmanager
    def _lease(self) -> Iterator[bool]:
        """A session-level advisory lock on a dedicated, non-transactional connection."""
        conn = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            got = bool(
                conn.execute(
                    sa.text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
                ).scalar_one()
            )
            try:
                yield got
            finally:
                if got:
                    released = bool(
                        conn.execute(
                            sa.text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY}
                        ).scalar_one()
                    )
                    if not released:  # never hand a connection that may hold the lock back
                        conn.invalidate()
        finally:
            conn.close()

    def _run(self, now: datetime, state: ExportState, severities: list[str]) -> ExportOutcome:
        started = ExportState(
            state.state,
            now,
            state.last_success_at,
            state.next_attempt_at,
            state.consecutive_failures,
            state.error_code,
        )
        self._states.save(started, now)
        session_id = uuid.uuid4().hex
        raw: SnowflakeSession | None = None
        results: dict[str, StreamResult] = {}
        try:
            raw = self._sessions.open(
                "writer",
                lock_timeout_seconds=self._s.lock_timeout_seconds,
                statement_timeout_seconds=EXPORT_STATEMENT_TIMEOUT_SECONDS,
            )
            session = MeteredSession(
                raw,
                self._calls,
                session_id=session_id,
                function="export",
                project_id=None,
                clock=self._clock,
            )
            self._export_all(session, severities, results, now)
        except _Halt as halt:
            return self._halt(started, now, halt, results)
        except SnowflakeRejected as exc:
            if exc.code == "insufficient_privileges":
                return self._halt(started, now, _Halt(HALT_PRIVILEGE), results)
            return self._fail(started, now, exc, results)
        except SnowflakeError as exc:
            return self._fail(started, now, exc, results)
        finally:
            if raw is not None:
                with suppress(Exception):
                    raw.close()
        self._states.save(ExportState("idle", now, now, None, 0, None), now)
        log(
            _logger,
            logging.INFO,
            "export finished",
            sent=sum(r.sent for r in results.values()),
        )
        return ExportOutcome("exported", results)

    def _export_all(
        self,
        session: MeteredSession,
        severities: list[str],
        results: dict[str, StreamResult],
        now: datetime,
    ) -> None:
        limit = self._s.export_batch_max_rows
        fetchers: dict[str, Callable[[Any], list[src.Candidate]]] = {
            "events": lambda c: src.events_batch(c, limit=limit, severities=severities),
            "incident_summaries": lambda c: src.incident_batch(c, limit=limit),
            "deployments": lambda c: src.deployment_batch(c, limit=limit),
        }
        for name in STREAM_ORDER:
            results[name] = StreamResult()
            for _ in range(MAX_BATCHES_PER_RUN):
                with self._engine.connect() as conn:
                    candidates = fetchers[name](conn)
                if not candidates:
                    break
                self._export_batch(session, STREAMS[name], candidates, results[name], now)

    def _export_batch(
        self,
        session: MeteredSession,
        spec: StreamSql,
        candidates: list[src.Candidate],
        result: StreamResult,
        now: datetime,
    ) -> None:
        batch_id = uuid.uuid4().hex
        unique = _dedupe(candidates)
        rows = [c.params + (self._test_run_id,) for c in unique]
        timeout = float(EXPORT_STATEMENT_TIMEOUT_SECONDS)
        session.execute(spec.create_tmp, (), timeout_seconds=timeout, query_tag=QUERY_TAG)
        session.executemany(spec.insert_tmp, rows, timeout_seconds=timeout, query_tag=QUERY_TAG)
        merged = session.execute(spec.merge, (), timeout_seconds=timeout, query_tag=QUERY_TAG)
        audit = session.execute(spec.audit_batch, (), timeout_seconds=timeout, query_tag=QUERY_TAG)
        # The batch was delivered either way; record that before reacting to the audit, so a
        # halt never leads to the same rows being sent again.
        with self._engine.begin() as conn:
            src.mark_exported(conn, candidates, batch_id=batch_id, now=now)
        inserted = _inserted(merged)
        result.sent += len(unique)
        result.batches += 1
        result.inserted = (
            None if inserted is None or result.inserted is None else result.inserted + inserted
        )
        if audit.rows:
            raise _Halt(HALT_DUPLICATES, f"{spec.stream}:{len(audit.rows)}")

    def _halt(
        self, started: ExportState, now: datetime, halt: _Halt, results: dict[str, StreamResult]
    ) -> ExportOutcome:
        self._states.save(
            ExportState(
                "halted",
                started.last_started_at,
                started.last_success_at,
                None,
                started.consecutive_failures + 1,
                halt.reason,
            ),
            now,
        )
        log(_logger, logging.ERROR, "export halted", reason=halt.reason, detail=halt.detail)
        return ExportOutcome("halted", results, halt.reason)

    def _fail(
        self,
        started: ExportState,
        now: datetime,
        exc: SnowflakeError,
        results: dict[str, StreamResult],
    ) -> ExportOutcome:
        failures = started.consecutive_failures + 1
        wait = max(
            self._s.export_min_interval_seconds,
            min(BACKOFF_BASE_SECONDS * 2 ** (failures - 1), BACKOFF_CAP_SECONDS),
        )
        self._states.save(
            ExportState(
                "degraded",
                started.last_started_at,
                started.last_success_at,
                now + timedelta(seconds=wait),
                failures,
                exc.code,
            ),
            now,
        )
        log(_logger, logging.WARNING, "export failed", error_code=exc.code, failures=failures)
        return ExportOutcome("failed", results, exc.code)
