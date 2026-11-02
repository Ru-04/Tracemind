"""Metadata-only call log and the soft-budget estimate (M3 plan 4.7, check M3-5).

The runtime roles hold no MONITOR privilege and Snowflake's own metering lags by hours, so the
soft budget is computed from TraceMind's OWN log of what it ran. It is an estimate of what
TraceMind caused, not a measurement of what the account used: it excludes migrations, the
owner's own worksheets, the verification scripts and anything else on the warehouse. The
resource monitor and the owner's review of real metering remain the authoritative checks.

Arithmetic (X-Small, setup guide 8.3): 1 credit per hour = 1/3600 credit per second; every
resume bills at least 60 seconds; the warehouse then idles for AUTO_SUSPEND seconds before
suspending. Statements are merged into warehouse run-windows (a gap of at most AUTO_SUSPEND
seconds keeps the warehouse running), and each window is estimated as

    max(60, busy_seconds + AUTO_SUSPEND_SECONDS) / 3600  credits

How idle time is billed is not stated by the documentation (UNVERIFIED), so this is a model,
not a measurement.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from tracemind.storage.tables import snowflake_calls

CREDITS_PER_SECOND = 1.0 / 3600.0  # X-Small
MIN_BILLED_SECONDS = 60
AUTO_SUSPEND_SECONDS = 60  # TRACEMIND_WH (infra/snowflake/01_admin_setup.sql, Part A)
WINDOW_DAYS = 30
WARNING_FRACTION = 0.8

BudgetState = Literal["ok", "warning", "exceeded"]
BASIS = "estimate from TraceMind's own call log; excludes all other use of the warehouse"


@dataclass(frozen=True)
class BudgetEstimate:
    estimated_credits: float
    windows: int
    window_days: int
    soft_budget_credits: float
    state: BudgetState
    basis: str = BASIS

    def to_json(self) -> dict[str, Any]:
        return {
            "estimated_credits": round(self.estimated_credits, 4),
            "soft_budget_credits": self.soft_budget_credits,
            "state": self.state,
            "warehouse_windows": self.windows,
            "window_days": self.window_days,
            "basis": self.basis,
        }


def session_credits(busy_seconds: float) -> float:
    """Credits for one warehouse run-window that was busy for `busy_seconds`."""
    return max(MIN_BILLED_SECONDS, busy_seconds + AUTO_SUSPEND_SECONDS) * CREDITS_PER_SECOND


def warehouse_windows(calls: Iterable[tuple[datetime, int]]) -> list[float]:
    """Busy seconds of each warehouse run-window. Statements (start, duration in ms, ordered by
    start) whose gap to the previous statement's end is at most AUTO_SUSPEND_SECONDS share one
    window, because the warehouse had not yet suspended; overlapping statements (exporter and
    queries at once) merge the same way."""
    windows: list[float] = []
    window_start: datetime | None = None
    window_end: datetime | None = None
    for started, duration_ms in calls:
        ended = started + timedelta(milliseconds=duration_ms)
        if (
            window_start is None
            or window_end is None
            or (started - window_end).total_seconds() > AUTO_SUSPEND_SECONDS
        ):
            if window_start is not None and window_end is not None:
                windows.append((window_end - window_start).total_seconds())
            window_start, window_end = started, ended
        else:
            window_end = max(window_end, ended)
    if window_start is not None and window_end is not None:
        windows.append((window_end - window_start).total_seconds())
    return windows


def classify(estimated: float, soft_budget: float) -> BudgetState:
    if estimated >= soft_budget:
        return "exceeded"
    if estimated >= soft_budget * WARNING_FRACTION:
        return "warning"
    return "ok"


class CallLog:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(snowflake_calls).values(
                    session_id=session_id,
                    started_at=started_at,
                    duration_ms=max(0, duration_ms),
                    function=function,
                    project_id=project_id,
                    rows=rows,
                    outcome=outcome,
                    query_id=query_id,
                )
            )

    def estimate(self, now: datetime, soft_budget_credits: float) -> BudgetEstimate:
        since = now - timedelta(days=WINDOW_DAYS)
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(snowflake_calls.c.started_at, snowflake_calls.c.duration_ms)
                .where(snowflake_calls.c.started_at >= since)
                .order_by(snowflake_calls.c.started_at)
            ).all()
        windows = warehouse_windows((r.started_at, r.duration_ms) for r in rows)
        total = sum(session_credits(busy) for busy in windows)
        return BudgetEstimate(
            estimated_credits=total,
            windows=len(windows),
            window_days=WINDOW_DAYS,
            soft_budget_credits=soft_budget_credits,
            state=classify(total, soft_budget_credits),
        )
