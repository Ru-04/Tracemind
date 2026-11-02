"""The boundary between TraceMind and the Snowflake connector.

Everything else in this package talks to `SnowflakeSession` / `SessionFactory`, never to the
connector. A real adapter (S2, live) will implement these two protocols; until it exists the
only implementation is the in-memory fake used by the tests. Keeping the boundary this small is
also the mitigation for connector release churn (M3 plan, risks).

Contract an adapter must honour (each point is UNVERIFIED until the live checks run):
- statements use server-side binding (`paramstyle='qmark'`); values are never interpolated;
- `timeout_seconds` is enforced client side, with the server-side statement timeout as backstop;
- the session runs under the restricted role requested in `open()`;
- failures are mapped to the error classes below and never carry SQL text, bound values, or
  credentials in their message.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

Role = Literal["writer", "reader"]


class SnowflakeError(Exception):
    """Base class. `code` is a stable identifier; messages never contain SQL, values or secrets."""

    code = "snowflake_error"

    def __init__(self, code: str | None = None) -> None:
        super().__init__(code or self.code)
        if code:
            self.code = code


class SnowflakeUnavailable(SnowflakeError):  # noqa: N818 - a domain outcome
    """Cannot reach or use the service: network, authentication, suspended warehouse, expired
    key, or no adapter installed."""

    code = "unavailable"


class SnowflakeTimeout(SnowflakeError):  # noqa: N818
    code = "query_timeout"


class SnowflakeLockTimeout(SnowflakeError):  # noqa: N818
    """A statement waited for a lock longer than LOCK_TIMEOUT (e.g. a concurrent MERGE)."""

    code = "lock_timeout"


class SnowflakeRejected(SnowflakeError):  # noqa: N818
    """The service understood and refused: `insufficient_privileges` or `sql_error`. Retrying the
    same statement cannot help."""

    code = "sql_error"


@dataclass(frozen=True)
class StatementResult:
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    rowcount: int | None = None
    query_id: str | None = None


class SnowflakeSession(Protocol):
    def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        timeout_seconds: float,
        query_tag: str,
    ) -> StatementResult: ...

    def executemany(
        self,
        sql: str,
        rows: Sequence[Sequence[Any]],
        *,
        timeout_seconds: float,
        query_tag: str,
    ) -> StatementResult: ...

    def close(self) -> None: ...


class SessionFactory(Protocol):
    def open(
        self, role: Role, *, lock_timeout_seconds: int, statement_timeout_seconds: int
    ) -> SnowflakeSession: ...


class UnavailableFactory:
    """Used when Snowflake is enabled but no adapter is installed: every use degrades cleanly
    instead of failing at import time or pretending to work."""

    available = False

    def open(
        self, role: Role, *, lock_timeout_seconds: int, statement_timeout_seconds: int
    ) -> SnowflakeSession:
        del role, lock_timeout_seconds, statement_timeout_seconds
        raise SnowflakeUnavailable("connector_not_available")


class CallRecorder(Protocol):
    def record(
        self,
        *,
        session_id: str,
        started_at: datetime,
        duration_ms: int,
        function: str,
        project_id: str | None,
        rows: int | None,
        outcome: str,
        query_id: str | None,
    ) -> None: ...


class MeteredSession:
    """Wraps a session so every statement leaves a metadata-only record (function, duration,
    rows, outcome, query id). Never the SQL text, the parameters or the results."""

    def __init__(
        self,
        inner: SnowflakeSession,
        recorder: CallRecorder,
        *,
        session_id: str,
        function: str,
        project_id: str | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self.session_id = session_id
        self._function = function
        self._project_id = project_id
        self._clock = clock

    def _run(self, call: Callable[[], StatementResult]) -> StatementResult:
        started = self._clock()
        t0 = time.monotonic()
        outcome = "ok"
        result: StatementResult | None = None
        try:
            result = call()
            return result
        except SnowflakeError as exc:
            outcome = exc.code
            raise
        except Exception:
            outcome = "unexpected_error"
            raise
        finally:
            self._recorder.record(
                session_id=self.session_id,
                started_at=started,
                duration_ms=int((time.monotonic() - t0) * 1000),
                function=self._function,
                project_id=self._project_id,
                rows=(result.rowcount if result and result.rowcount is not None else None),
                outcome=outcome,
                query_id=result.query_id if result else None,
            )

    def execute(
        self, sql: str, params: Sequence[Any] = (), *, timeout_seconds: float, query_tag: str
    ) -> StatementResult:
        return self._run(
            lambda: self._inner.execute(
                sql, params, timeout_seconds=timeout_seconds, query_tag=query_tag
            )
        )

    def executemany(
        self,
        sql: str,
        rows: Sequence[Sequence[Any]],
        *,
        timeout_seconds: float,
        query_tag: str,
    ) -> StatementResult:
        return self._run(
            lambda: self._inner.executemany(
                sql, rows, timeout_seconds=timeout_seconds, query_tag=query_tag
            )
        )

    def close(self) -> None:
        self._inner.close()
