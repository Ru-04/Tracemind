"""Approved, read-only query functions (M3 plan 4.6, checks M3-2 to M3-6).

There is no way to send SQL through this module: each function selects one static statement from
`sql.READER_TEMPLATES` and binds validated values. Nothing accepts SQL text, table names, column
names or predicates. Validation happens BEFORE any statement is sent, so a rejected request
costs nothing. Every row carries an evidence reference that resolves to a PostgreSQL record.

When Snowflake cannot be used (disabled, over the soft budget, circuit open, busy, unreachable,
timed out) the function returns an `unavailable` result with a stable reason. It never raises
for that, and it never returns an empty "ok" that could be mistaken for "no history".
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from tracemind.snowflake.config import SnowflakeSettings
from tracemind.snowflake.consumption import BudgetEstimate, CallLog
from tracemind.snowflake.session import (
    MeteredSession,
    SessionFactory,
    SnowflakeError,
    SnowflakeSession,
    SnowflakeTimeout,
    StatementResult,
)
from tracemind.snowflake.sql import READER_TEMPLATES
from tracemind.storage.postgres import _iso

MAX_WINDOW = timedelta(days=30)
DEFAULT_WINDOW = timedelta(days=7)
FUTURE_SKEW = timedelta(minutes=10)
DEFAULT_LIMIT = 100
BREAKER_FAILURES = 3
BREAKER_OPEN_SECONDS = 60
BUDGET_CACHE_SECONDS = 30
SEMAPHORE_WAIT_SECONDS = 1.0

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}\Z")
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\Z")
_COMMIT = re.compile(r"^[0-9a-f]{40}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class QueryRejected(Exception):  # noqa: N818 - a domain outcome
    """The request is invalid. Raised before any statement is sent. `field` names the problem,
    never the value."""

    def __init__(self, field_name: str) -> None:
        super().__init__("invalid_parameters")
        self.code = "invalid_parameters"
        self.field = field_name


@dataclass(frozen=True)
class QueryResult:
    function: str
    availability: Literal["ok", "unavailable"]
    reason: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "availability": self.availability,
            "reason": self.reason,
            "items": self.rows,
            "truncated": self.truncated,
            # Everything here is a derived copy of PostgreSQL data. It is a lead, not a fact.
            "derived_data": True,
            "source": "snowflake",
        }


# ---- parameter validation (pure) ----------
def _project(value: object) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise QueryRejected("project_id")
    return value


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.match(value):
        raise QueryRejected("fingerprint")
    return value


def _name(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise QueryRejected(field_name)
    return value


def _error_type(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or _CONTROL.search(value):
        raise QueryRejected("error_type")
    return value


def _commit(value: object) -> str:
    if not isinstance(value, str) or not _COMMIT.match(value):
        raise QueryRejected("commit_sha")  # only a full, lower-case SHA; callers verify it first
    return value


def _limit(value: object, maximum: int) -> int:
    if value is None:
        return min(DEFAULT_LIMIT, maximum)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise QueryRejected("limit")
    return value


def resolve_window(start: object, end: object, *, now: datetime) -> tuple[datetime, datetime]:
    """A half-open window [start, end) in UTC, at most MAX_WINDOW long, not in the future."""
    if end is None:
        end = now
    if not isinstance(end, datetime) or end.tzinfo is None:
        raise QueryRejected("end")
    if start is None:
        start = end - DEFAULT_WINDOW
    if not isinstance(start, datetime) or start.tzinfo is None:
        raise QueryRejected("start")
    s, e = start.astimezone(UTC), end.astimezone(UTC)
    if e <= s:
        raise QueryRejected("window")
    if e - s > MAX_WINDOW:
        raise QueryRejected("window_too_long")
    if e > now + FUTURE_SKEW:
        raise QueryRejected("end")
    return s, e


# ---- row mapping ----------
def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value if value.tzinfo else value.replace(tzinfo=UTC))
    return value


def map_rows(
    result: StatementResult, evidence: Callable[[dict[str, Any]], str], cap: int
) -> tuple[list[dict[str, Any]], bool]:
    names = [c.lower() for c in result.columns]
    rows = [{n: _plain(v) for n, v in zip(names, row, strict=True)} for row in result.rows]
    truncated = len(rows) > cap
    rows = rows[:cap]
    for row in rows:
        row["evidence_id"] = evidence(row)
    return rows, truncated


def _event_ref(row: dict[str, Any]) -> str:
    return f"event:{row['event_id']}"


def _history_ref(row: dict[str, Any]) -> str:
    return f"history:{row['incident_id']}"


# ---- the service ----------
class QueryService:
    def __init__(
        self,
        settings: SnowflakeSettings,
        sessions: SessionFactory,
        calls: CallLog,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._s = settings
        self._sessions = sessions
        self._calls = calls
        self._clock = clock
        self._slots = threading.BoundedSemaphore(settings.max_concurrency)
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until: datetime | None = None
        self._budget: tuple[datetime, BudgetEstimate] | None = None

    def now(self) -> datetime:
        return self._clock()

    # ---- named functions ----------
    def events_for_fingerprint(
        self,
        project_id: str,
        fingerprint: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        pid, fp = _project(project_id), _fingerprint(fingerprint)
        s, e = resolve_window(start, end, now=self._clock())
        n = _limit(limit, self._s.max_rows)
        return self._run(
            "events_for_fingerprint", pid, (pid, fp, _iso(s), _iso(e), n + 1), _event_ref, n
        )

    def events_for_service_window(
        self,
        project_id: str,
        service: str,
        environment: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        pid = _project(project_id)
        svc, env = _name(service, "service"), _name(environment, "environment")
        s, e = resolve_window(start, end, now=self._clock())
        n = _limit(limit, self._s.max_rows)
        return self._run(
            "events_for_service_window",
            pid,
            (pid, svc, env, _iso(s), _iso(e), n + 1),
            _event_ref,
            n,
        )

    def events_for_verified_commit(
        self,
        project_id: str,
        commit_sha: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        """The caller must have verified `commit_sha` (M2 resolver); this only checks its shape."""
        pid, sha = _project(project_id), _commit(commit_sha)
        s, e = resolve_window(start, end, now=self._clock())
        n = _limit(limit, self._s.max_rows)
        return self._run(
            "events_for_verified_commit", pid, (pid, sha, _iso(s), _iso(e), n + 1), _event_ref, n
        )

    def similar_incidents(
        self,
        project_id: str,
        *,
        fingerprint: str | None = None,
        service: str | None = None,
        error_type: str | None = None,
        exclude_incident_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> QueryResult:
        """Deterministic similarity, no embeddings and no model: same fingerprint when one is
        given, otherwise the same service and error type."""
        pid = _project(project_id)
        by_fingerprint = fingerprint is not None
        if by_fingerprint == (service is not None or error_type is not None):
            raise QueryRejected("selector")  # exactly one of: fingerprint | (service, error_type)
        skip = ""
        if exclude_incident_id is not None:
            skip = exclude_incident_id
            if not isinstance(skip, str) or not 1 <= len(skip) <= 128 or _CONTROL.search(skip):
                raise QueryRejected("exclude_incident_id")
        s, e = resolve_window(start, end, now=self._clock())
        n = _limit(limit, self._s.max_rows)
        if by_fingerprint:
            fp = _fingerprint(fingerprint)
            return self._run(
                "similar_incidents_by_fingerprint",
                pid,
                (pid, fp, skip, _iso(s), _iso(e), n + 1),
                _history_ref,
                n,
            )
        if service is None or error_type is None:
            raise QueryRejected("selector")
        return self._run(
            "similar_incidents_by_service_error",
            pid,
            (
                pid,
                _name(service, "service"),
                _error_type(error_type),
                skip,
                _iso(s),
                _iso(e),
                n + 1,
            ),
            _history_ref,
            n,
        )

    def documented_resolutions(
        self, project_id: str, fingerprint: str, *, limit: int | None = None
    ) -> QueryResult:
        """Previously recorded resolutions: historical SUGGESTIONS, never verified fixes for the
        incident being looked at."""
        pid, fp = _project(project_id), _fingerprint(fingerprint)
        n = _limit(limit, self._s.max_rows)
        return self._run("documented_resolutions", pid, (pid, fp, n + 1), _history_ref, n)

    # ---- execution ----------
    def _unavailable(self, function: str, reason: str) -> QueryResult:
        return QueryResult(function, "unavailable", reason)

    def _budget_state(self, now: datetime) -> BudgetEstimate:
        cached = self._budget
        if cached is not None and (now - cached[0]).total_seconds() < BUDGET_CACHE_SECONDS:
            return cached[1]
        fresh = self._calls.estimate(now, self._s.soft_budget_credits)
        self._budget = (now, fresh)
        return fresh

    def _breaker_open(self, now: datetime) -> bool:
        with self._lock:
            return self._open_until is not None and now < self._open_until

    def _note(self, ok: bool, now: datetime) -> None:
        with self._lock:
            if ok:
                self._failures, self._open_until = 0, None
                return
            self._failures += 1
            if self._failures >= BREAKER_FAILURES:
                self._open_until = now + timedelta(seconds=BREAKER_OPEN_SECONDS)

    def _run(
        self,
        function: str,
        project_id: str,
        params: tuple[Any, ...],
        evidence: Callable[[dict[str, Any]], str],
        cap: int,
    ) -> QueryResult:
        template = READER_TEMPLATES[function]
        assert template.count("?") == len(params), function  # a programming error, not input
        now = self._clock()
        if not self._s.enabled:
            return self._unavailable(function, "snowflake_disabled")
        if self._budget_state(now).state == "exceeded":
            return self._unavailable(function, "soft_budget_exceeded")
        if self._breaker_open(now):
            return self._unavailable(function, "temporarily_unavailable")
        if not self._slots.acquire(timeout=SEMAPHORE_WAIT_SECONDS):
            return self._unavailable(function, "busy")
        raw: SnowflakeSession | None = None
        try:
            raw = self._sessions.open(
                "reader",
                lock_timeout_seconds=self._s.lock_timeout_seconds,
                statement_timeout_seconds=self._s.query_timeout_seconds,
            )
            session = MeteredSession(
                raw,
                self._calls,
                session_id=uuid.uuid4().hex,
                function=function,
                project_id=project_id,
                clock=self._clock,
            )
            result = session.execute(
                template,
                params,
                timeout_seconds=float(self._s.query_timeout_seconds),
                query_tag=f"tracemind:{function}",
            )
        except SnowflakeTimeout:
            self._note(False, now)
            return self._unavailable(function, "query_timeout")
        except SnowflakeError as exc:
            self._note(False, now)
            return self._unavailable(function, exc.code)
        finally:
            if raw is not None:
                with suppress(Exception):  # closing must never mask the result
                    raw.close()
            self._slots.release()
        self._note(True, now)
        rows, truncated = map_rows(result, evidence, cap)
        return QueryResult(function, "ok", None, rows, truncated)
