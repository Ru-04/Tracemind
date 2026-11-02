"""Forward JSON log lines to the TraceMind ingestion API.

    python -m demo_app ... | python -m tools.ship_logs
    python -m tools.ship_logs --file app.log --batch-size 50

The ingest key is read from TRACEMIND_INGEST_KEY (never a command-line argument, so it stays out
of shell history and process lists) and is never printed. Lines that are not JSON objects are
skipped and counted. Read-only with respect to the monitored application: it only reads logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

import httpx

INGEST_PATH = "/api/v1/telemetry/events"


@dataclass
class ShipSummary:
    read: int = 0
    skipped_non_events: int = 0
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    failed_requests: int = 0
    last_error: str | None = None


def parse_lines(lines: Iterable[str]) -> Iterator[dict[str, Any] | None]:
    """Yield a dict per JSON-object line, None per skipped line."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            yield None
            continue
        yield doc if isinstance(doc, dict) else None


def _batches(events: Sequence[dict[str, Any]], size: int) -> Iterator[Sequence[dict[str, Any]]]:
    for i in range(0, len(events), size):
        yield events[i : i + size]


def ship(
    lines: Iterable[str],
    *,
    client: httpx.Client,
    key: str,
    batch_size: int = 50,
    fmt: str = "tracemind-json",
) -> ShipSummary:
    summary = ShipSummary()
    events: list[dict[str, Any]] = []
    for parsed in parse_lines(lines):
        if parsed is None:
            summary.skipped_non_events += 1
        else:
            events.append(parsed)
    summary.read = len(events)

    for batch in _batches(events, max(1, batch_size)):
        try:
            response = client.post(
                INGEST_PATH,
                params={"format": fmt},
                json={"events": list(batch)},
                headers={"X-TraceMind-Key": key},
            )
        except httpx.HTTPError as exc:
            summary.failed_requests += 1
            summary.last_error = type(exc).__name__
            continue
        if response.status_code not in (202, 422) or "results" not in _json(response):
            summary.failed_requests += 1
            summary.last_error = _error_code(response)
            if response.status_code in (401, 403, 429):
                break  # retrying the remaining batches cannot succeed
            continue
        for result in _json(response)["results"]:
            status = result.get("status")
            if status == "accepted":
                summary.accepted += 1
            elif status == "duplicate":
                summary.duplicate += 1
            else:
                summary.rejected += 1
                reason = str(result.get("reason"))
                summary.rejection_reasons[reason] = summary.rejection_reasons.get(reason, 0) + 1
    return summary


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        doc = response.json()
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _error_code(response: httpx.Response) -> str:
    err = _json(response).get("error")
    code = err.get("code") if isinstance(err, dict) else None
    return str(code or f"http_{response.status_code}")


def main(argv: Sequence[str] | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument(
        "--url", default=os.environ.get("TRACEMIND_INGEST_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--file", help="read this file instead of stdin")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--format", default="tracemind-json")
    args = parser.parse_args(argv)

    key = os.environ.get("TRACEMIND_INGEST_KEY", "")
    if not key:
        print("TRACEMIND_INGEST_KEY is not set", file=sys.stderr)
        return 2
    source: Iterable[str]
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            source = handle.readlines()
    else:
        source = (stdin or sys.stdin).readlines()
    with httpx.Client(base_url=args.url, timeout=10.0) as client:
        summary = ship(source, client=client, key=key, batch_size=args.batch_size, fmt=args.format)
    print(json.dumps(summary.__dict__, sort_keys=True))
    return 0 if summary.rejected == 0 and summary.failed_requests == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
