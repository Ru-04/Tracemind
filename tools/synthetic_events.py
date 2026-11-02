"""Deterministic synthetic events for tests and local demos. No real telemetry, no network.

    python -m tools.synthetic_events --kind valid --count 20 --seed 1 | python -m tools.ship_logs

All randomness comes from a seeded generator, so the same arguments always give the same lines.
Fake credentials are assembled at runtime and are obviously fake.
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

PROJECT = "project-demo"
SERVICE = "demo-payments"
DEFAULT_BASE = datetime(2026, 10, 28, 9, 0, 0, tzinfo=UTC)

# (error_type, message template, innermost frame). The template's numbers vary per event but
# must not split the incident group; the three templates must produce three distinct incidents.
FAILURES: tuple[tuple[str, str, str], ...] = (
    ("ConnectionError", "Database connection failed after {n} retries", "db/pool.py:acquire"),
    ("TimeoutError", "orders-db did not respond within {n}.0s", "clients/orders.py:fetch"),
    ("KeyError", "Missing field 'customer_{n}' in payload", "api/handlers.py:parse_order"),
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stack(frame: str, error_type: str, message: str, line: int) -> str:
    path, func = frame.split(":")
    return (
        "Traceback (most recent call last):\n"
        f'  File "src/{path}", line {line}, in {func}\n'
        f"{error_type}: {message}"
    )


def valid_error_events(
    count: int, *, seed: int = 0, base: datetime = DEFAULT_BASE
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    events: list[dict[str, Any]] = []
    for i in range(count):
        error_type, template, frame = FAILURES[i % len(FAILURES)]
        message = template.format(n=rng.randint(1, 99))
        events.append(
            {
                "event_id": f"synthetic-{seed}-{i:05d}",
                "project_id": PROJECT,
                "service": SERVICE,
                "environment": "development",
                "timestamp": _iso(base + timedelta(seconds=i * 7 + rng.randint(0, 5))),
                "severity": "ERROR",
                "message": message,
                "error_type": error_type,
                "stack_trace": _stack(frame, error_type, message, rng.randint(10, 400)),
                "trace_id": f"{rng.getrandbits(128):032x}",
                "attributes": {"attempt": rng.randint(1, 3)},
            }
        )
    return events


def secret_bearing_event(*, base: datetime = DEFAULT_BASE) -> dict[str, Any]:
    """An event whose fields all carry fake secrets, for redaction tests."""
    return {
        "event_id": "synthetic-secrets-1",
        "project_id": PROJECT,
        "service": SERVICE,
        "environment": "development",
        "timestamp": _iso(base),
        "severity": "ERROR",
        "message": "login failed for "
        + "jane.doe"
        + "@example.com password="
        + "hunter2-correct-horse",
        "error_type": "AuthError",
        "stack_trace": "Authorization: Bearer " + "q9w8e7r6t5y4u3i2o1p0",
        "attributes": {
            "api_key": "not-a-real-key",
            "db_url": "postgres://u:" + "pw123456" + "@h/db",
        },
    }


# (name, mutation applied to a valid event, expected rejection reason)
MALFORMED: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("timestamp without offset", {"timestamp": "2026-10-28T09:00:00"}, "invalid_timestamp"),
    ("unparseable timestamp", {"timestamp": "yesterday"}, "invalid_timestamp"),
    ("unknown severity", {"severity": "LOUD"}, "invalid_severity"),
    ("invalid trace id", {"trace_id": "not-hex"}, "invalid_trace_id"),
    ("unregistered service", {"service": "ghost-service"}, "unknown_service"),
    ("unknown field", {"surprise": 1}, "unknown_field"),
    ("empty message", {"message": ""}, "invalid_message"),
    ("oversized message", {"message": "x" * 9000}, "message_too_long"),
)


def malformed_events(*, base: datetime = DEFAULT_BASE) -> list[tuple[str, dict[str, Any], str]]:
    template = valid_error_events(1, base=base)[0]
    return [(name, {**template, **patch}, reason) for name, patch, reason in MALFORMED]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print synthetic events as JSON lines")
    parser.add_argument("--kind", choices=["valid", "secrets", "malformed"], default="valid")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    if args.kind == "valid":
        docs = valid_error_events(args.count, seed=args.seed)
    elif args.kind == "secrets":
        docs = [secret_bearing_event()]
    else:
        docs = [event for _, event, _ in malformed_events()]
    for doc in docs:
        print(json.dumps(doc, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
