"""M3-5 (arithmetic half): the soft-budget estimate, tied to the cost model in the setup guide
(section 8.3). The figures are an ESTIMATE of what TraceMind caused, not a measurement of what
the account used."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tracemind.snowflake.consumption import (
    AUTO_SUSPEND_SECONDS,
    CREDITS_PER_SECOND,
    MIN_BILLED_SECONDS,
    classify,
    session_credits,
    warehouse_windows,
)

T0 = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_constants_are_the_documented_x_small_figures() -> None:
    assert CREDITS_PER_SECOND == pytest.approx(1 / 3600)
    assert (MIN_BILLED_SECONDS, AUTO_SUSPEND_SECONDS) == (60, 60)


@pytest.mark.parametrize(
    ("busy", "billed_seconds", "credits"),
    [
        (5, 65, 0.018),  # guide 8.3, optimistic: ~65 s per use
        (60, 120, 0.033),  # base: ~120 s per use
        (240, 300, 0.083),  # pessimistic: ~300 s per use
    ],
)
def test_per_use_cost_matches_the_guide_scenarios(
    busy: float, billed_seconds: int, credits: float
) -> None:
    assert busy + AUTO_SUSPEND_SECONDS == billed_seconds
    assert session_credits(busy) == pytest.approx(billed_seconds / 3600)
    assert session_credits(busy) == pytest.approx(credits, abs=0.0005)


def test_a_resume_is_never_billed_for_less_than_the_minimum() -> None:
    assert session_credits(0) == pytest.approx(60 / 3600) or session_credits(0) == pytest.approx(
        (0 + AUTO_SUSPEND_SECONDS) / 3600
    )
    assert session_credits(0) >= MIN_BILLED_SECONDS * CREDITS_PER_SECOND


def test_one_statement_is_one_window() -> None:
    assert warehouse_windows([(at(0), 4000)]) == [4.0]


def test_statements_within_the_idle_window_share_one_warehouse_run() -> None:
    # ends at 4 s; the next starts 50 s later (< 60 s auto-suspend): the warehouse never suspended
    assert warehouse_windows([(at(0), 4000), (at(54), 2000)]) == [56.0]


def test_a_gap_longer_than_auto_suspend_starts_a_new_window() -> None:
    windows = warehouse_windows([(at(0), 4000), (at(65), 2000)])  # gap 61 s
    assert windows == [4.0, 2.0]
    assert warehouse_windows([(at(0), 4000), (at(64), 2000)]) == [66.0]  # gap exactly 60 s: shared


def test_overlapping_statements_do_not_double_count() -> None:
    # an export and a query running at the same time
    assert warehouse_windows([(at(0), 30000), (at(5), 10000), (at(10), 5000)]) == [30.0]


def test_no_calls_no_windows() -> None:
    assert warehouse_windows([]) == []


def test_many_close_calls_cost_less_than_many_separate_resumes() -> None:
    close = sum(
        session_credits(b) for b in warehouse_windows((at(i * 10), 1000) for i in range(30))
    )
    apart = sum(
        session_credits(b) for b in warehouse_windows((at(i * 3600), 1000) for i in range(30))
    )
    assert close < apart / 10


def test_budget_states() -> None:
    assert classify(0, 15) == "ok"
    assert classify(11.99, 15) == "ok"
    assert classify(12.0, 15) == "warning"  # 80 %
    assert classify(14.99, 15) == "warning"
    assert classify(15.0, 15) == "exceeded"
    assert classify(40, 15) == "exceeded"
