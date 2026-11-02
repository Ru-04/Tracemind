"""Snowflake integration (M3): a derived, one-way copy of PostgreSQL data and read-only query
functions. PostgreSQL stays the source of truth (ADR-0007); nothing in ingestion, grouping or
the lifecycle ever waits for this package.

Status of this package: the logic is exercised against an in-memory fake of the session
boundary (`session.py`). **No real connector adapter exists yet** and nothing here has run
against a Snowflake account; behaviour that depends on Snowflake itself is listed as UNVERIFIED
in docs/plans/M3-plan.md and docs/setup/snowflake-account-setup.md and is covered by the live
checks M3-12 to M3-14, which have NOT been run.
"""
