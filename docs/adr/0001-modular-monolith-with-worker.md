# ADR-0001: Modular monolith plus a separate worker

- Status: Accepted (2026-10-28, as proposed in SOW §4.2)
- Milestone: M0

## Context

TraceMind is a solo-developer V1 delivered over about ten weeks. It has ingestion, incident management, repository analysis, Snowflake retrieval and an LLM investigation engine. Repository scans and investigations are slow and must not block HTTP requests.

## Decision

One Python codebase (FastAPI API) with enforced module boundaries, plus a separate background worker process that runs repository scans and investigations. React + TypeScript dashboard in M5.

```
frontend/   React + TypeScript (M5)
backend/    FastAPI app and modules: ingestion, incidents, repos, history, investigation, llm, audit
worker/     background process: scans, investigations
demo_app/   failure-generating demo service (not product code)
docs/       ADRs, contracts, threat model, acceptance tests
infra/      docker compose and CI helpers
```

Boundaries inside `backend/`: modules talk through typed interfaces; the investigation module may use data only through the registered tools (ADR-0005 below, SOW §10.6). Adapters (Git, Graphify, Snowflake, Groq) sit behind interfaces so they can be replaced or mocked.

## Data stores

- PostgreSQL: source of truth for operational state (projects, events as ingested, incidents, jobs, feedback, audit).
- Snowflake (M3): telemetry history and incident knowledge for analytical retrieval. Not a copy of every Postgres table.
- Git: authoritative source of commits and diffs. Graphify output is derived data and never overrides Git.

## Consequences

- Simple to run and test locally; one deployable plus one worker.
- Boundaries must be policed by tests and review, since the compiler will not.
- Splitting into services later is possible along the existing module lines, but only if real load justifies it.

## Decisions deferred (to be recorded as their own ADRs when taken)

| # | Topic | Decided in |
|---|---|---|
| 0002 | Job queue for the worker (Postgres-backed vs library) | M2 (first slow job) |
| 0003 | Graphify adoption vs documented fallback | M2, after the verification spike |
| 0004 | Concrete Groq model and structured-output mode | M4 |
| 0005 | Investigation tool-registry contract | M4 |
| 0006 | Authentication mechanism | M5 |
| [0007](0007-postgresql-transactional-store-and-snowflake-path.md) | PostgreSQL transactional store; planned Snowflake path | **Accepted in M1** (Snowflake part is a plan until M3) |
