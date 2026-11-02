# ADR-0007: PostgreSQL for transactional ingestion; planned path to Snowflake

- Status: Accepted for M1 (2026-10-28). The Snowflake part is a **plan only**: nothing in it is implemented or verified until M3.
- Milestone: M1 (decision), M3 (Snowflake implementation)

## Context

M1 must accept events, deduplicate them, and create or update incidents atomically. The SOW makes PostgreSQL the source of truth for operational state and Snowflake the home for analytical and historical evidence (SOW §4.4, §5).

## Decision (M1)

PostgreSQL stores events, incidents, occurrence links and ingestion counters. One transaction covers the event insert, the incident upsert, the occurrence link and the counters, so a duplicate delivery cannot change an occurrence count. SQLAlchemy 2 Core with psycopg 3, schema managed by Alembic (`backend/tracemind/storage/migrations`). The schema is created from versioned migrations only.

Reasons: unique constraints and `INSERT … ON CONFLICT` give correct idempotency and race behaviour (proved by the 20-way concurrency test); Snowflake is not suited to row-level transactional upserts.

## Planned path to Snowflake (M3, not implemented)

1. **Postgres remains the source of truth.** Snowflake holds a derived, append-oriented copy of normalized events and incident summaries for historical queries.
2. **One-way, idempotent export.** A worker process (ADR-0001) reads events that have not been exported, keyed by `(project_id, event_id)`, and loads them into Snowflake; re-running the export cannot create duplicates because the key is unique there too. Export progress is stored in Postgres (a watermark or per-row exported marker added by an M3 migration — not part of M1).
3. **Only redacted data leaves Postgres.** Events are already redacted before storage, so the export carries no raw secrets. The fingerprint, `fingerprint_version` and `event_id` travel with each row, so Snowflake history joins cleanly to incidents.
4. **Ingestion never waits for Snowflake.** If Snowflake is unavailable, ingestion and incident grouping continue; the export backlog grows and is reported as a degraded state (SOW §M3 acceptance).
5. **Read access goes through named, parameterized, read-only query functions** with time-window, row and timeout limits (SOW §9.3). No model-authored SQL.
6. **Evidence references stay stable.** Query results carry `event:<event_id>` references that resolve back to the Postgres record.
7. **Cost controls** (resource monitor, auto-suspend, query-duration recording) are M3 deliverables.

Unverified assumptions to settle in M3: Snowflake account/role/warehouse, connector behaviour on this platform, table design, retention, and export batching.

### Amendment 2026-11-02 (documentation review; nothing implemented)

Item 2 above says re-running the export "cannot create duplicates because the key is unique there too". **That reasoning does not hold in Snowflake.** The official constraints page states that `PRIMARY KEY` and `UNIQUE` are "Optional, not enforced" on standard tables (they are enforced only on hybrid tables), so the target table cannot guarantee uniqueness. Idempotence must instead come from the loader: an insert-only `MERGE` keyed by `(project_id, event_id)` over a **deduplicated** batch (with only `WHEN NOT MATCHED … INSERT`, duplicate source rows each produce a target row), a **single exporter** enforced in PostgreSQL, a short `LOCK_TIMEOUT`, and a post-load duplicate audit. What `MERGE` requires in privileges and how it behaves under concurrent execution are **unverified** and are demonstrated by tests M3-12 and M3-13 before the exporter is trusted. Design and evidence: [docs/plans/M3-plan.md](../plans/M3-plan.md) §4.5 and [docs/setup/snowflake-account-setup.md](../setup/snowflake-account-setup.md) §6. The rest of this ADR is unchanged.

## Consequences

- M1 data model already carries everything the export needs (stable ids, fingerprint, UTC timestamps), so M3 adds a worker and migrations without changing ingestion.
- Two stores can drift; the export is therefore one-way and idempotent, and Postgres wins on conflict.
