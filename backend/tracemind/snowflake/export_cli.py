"""Manual trigger for the Snowflake export (preferred during development, owner O4).

    python -m tracemind.snowflake.export_cli --status
    python -m tracemind.snowflake.export_cli --once
    python -m tracemind.snowflake.export_cli --clear-halt

Nothing starts this automatically. `--once` still obeys every rule of the exporter: it does
nothing unless TRACEMIND_SNOWFLAKE_ENABLED is true, never runs sooner than the minimum
interval after the previous attempt, and never touches the warehouse when there is no backlog.

There is no real connector adapter yet (the live integration, S2, is not authorized), so with
Snowflake enabled `--once` reports `failed` with `connector_not_available`. It prints only
status words, counts and stable error codes - never configuration, SQL or credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from tracemind.config import ConfigError, Settings
from tracemind.observability.logging import configure_logging
from tracemind.snowflake.config import SnowflakeSettings
from tracemind.snowflake.consumption import CallLog
from tracemind.snowflake.export import Exporter
from tracemind.snowflake.session import UnavailableFactory

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracemind.snowflake.export_cli")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="run one export attempt")
    group.add_argument("--status", action="store_true", help="print the export status")
    group.add_argument("--clear-halt", action="store_true", help="clear a halted exporter")
    args = parser.parse_args(argv)

    configure_logging()
    try:
        settings = Settings.from_env()
        sf = SnowflakeSettings.from_env()
        if sf.enabled:
            sf.key_files_outside([_PROJECT_ROOT])
        if not settings.database_url:
            raise ConfigError("TRACEMIND_DATABASE_URL is not set")
    except ConfigError as exc:
        print(json.dumps({"error": "configuration", "detail": str(exc)}))
        return 2

    engine = sa.create_engine(settings.database_url, pool_pre_ping=True)
    try:
        exporter = Exporter(
            engine, sf, UnavailableFactory(), CallLog(engine), clock=lambda: datetime.now(UTC)
        )
        if args.status:
            print(json.dumps(exporter.status(), default=str))
            return 0
        if args.clear_halt:
            exporter.clear_halt()
            print(json.dumps({"status": "cleared"}))
            return 0
        outcome = exporter.run_once()
        print(
            json.dumps(
                {
                    "status": outcome.status,
                    "error_code": outcome.error_code,
                    "streams": {
                        k: {"sent": v.sent, "inserted": v.inserted, "batches": v.batches}
                        for k, v in outcome.streams.items()
                    },
                }
            )
        )
        return 0 if outcome.status in ("exported", "no_backlog", "disabled", "too_soon") else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
