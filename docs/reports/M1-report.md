# M1 progress report — Telemetry and ingestion

Date: 2026-10-28 · Status: **implementation complete, awaiting owner review. M1 is not closed.** Nothing has been committed or pushed.

Deliverable (frozen plan): *trigger a known error and see its normalized event stored.* Demonstrated live over real HTTP (see "Live run") and by automated tests.

## What was implemented

| Slice | Content |
|---|---|
| S1 | Pydantic ingest/normalized models; normalization (UTC timestamps, severity enum with aliases, service/environment, trace/span ids, commit SHA, derived event ids); redaction; fingerprint v1; config-seeded project registry |
| S2 | PostgreSQL schema (Alembic migration `0001`, with tested downgrade), `PostgresStore`, ingestion pipeline, deduplication with conflict detection, incident grouping, ingestion counters |
| S3 | FastAPI app: per-project key auth, size/rate limits, error envelope, ingest + read endpoints, `/health`, `/ready`; adapter contract; native and OTLP-logs adapters; TraceMind's own JSON logging |
| S4 | Demo app structured JSON logs; `tools/ship_logs.py`; `tools/synthetic_events.py`; end-to-end tests |
| S5 | CI workflow, ADR-0007, contract/threat-model/acceptance/README updates, security review, this report |

## Files and modules

- `backend/tracemind/`: `config.py`, `errors.py`, `fingerprint.py`, `projects/`, `ingestion/{models,normalize,redact,pipeline,adapters,native,otlp}.py`, `storage/{base,postgres,tables}.py` + `storage/migrations/`, `api/{app,errors,ratelimit}.py`, `observability/logging.py`
- `demo_app/telemetry.py` (new), `demo_app/main.py` (now emits JSON lines)
- `tools/ship_logs.py`, `tools/synthetic_events.py`
- `infra/docker-compose.yml`, `infra/postgres-init.sql`, `alembic.ini`, `config/projects.example.json`, `config/projects.json` (names env vars only), `.github/workflows/ci.yml`
- Docs: `docs/plans/M1-plan.md`, `docs/adr/0007-…`, `docs/contracts/{api-v1,fingerprint}.md`, `docs/acceptance-tests.md`, `docs/threat-model.md`, `docs/backlog-parking-lot.md`

## Architecture decisions

- ADR-0007: PostgreSQL holds transactional ingestion state in one transaction per event; Snowflake is a planned one-way idempotent export in M3 (documented, **not implemented or verified**).
- Adapter contract defined in code and enforced for every adapter by tests; a second real format (OTLP/JSON logs) proves it. A third adapter registers without touching the pipeline.
- Temporary seams isolated: `tracemind/projects` (config-seeded projects) and the `require_project` dependency (per-project key). Both are replaced in M5.
- Incident `status` is always `open` in M1 and nothing changes it (lifecycle is M3). Commit association is always `unknown` (mapping is M2/M5).

## Tests actually run (2026-10-28, Windows 11, Python 3.12.10, PostgreSQL 16 in Docker)

| Check | Result |
|---|---|
| `pytest` (full) | **242 passed**, 1 warning (Starlette deprecation of `httpx` in `TestClient`) |
| `ruff check` / `ruff format --check` | clean / 58 files formatted |
| `mypy` (strict) | no issues in 46 source files |
| `pip-audit --skip-editable` | no known vulnerabilities |
| Acceptance M1-1 … M1-12 | all automated and passing (mapping in `docs/acceptance-tests.md`) |

Test counts: normalize 50, adapter contract 52, demo telemetry/tools 26, fingerprint 13, isolation 13, redact 8, API 38, store/pipeline 18, adapters API 3, end-to-end 3, M0 demo 11, M0 schemas 7.

### Live run (real servers, not test clients)

Started the API (`uvicorn … --factory`) and the demo app, triggered `/demo/controlled-exception` twice and `/demo/dependency-timeout` once, shipped the three stdout log lines with `tools.ship_logs`: 3 accepted. Re-shipping the same lines: 3 duplicates. `GET /api/v1/incidents` showed two open incidents (`InvoiceError` ×2, `DependencyTimeoutError` ×1); ingestion health showed accepted 3, duplicate 3, rejected 0; no key → 401; the ingest key appeared in neither server's output.

## Integrations verified

- PostgreSQL 16 (Docker): migrations up and down, constraints, `ON CONFLICT`, row-level concurrency (20 threads, one winner).
- OTLP/JSON logs: verified only against fixtures I wrote from the OTLP JSON structure; **not** against output of a real OpenTelemetry exporter.
- Not touched in M1 (as planned): Graphify, Snowflake, Groq, Git.

## Security review (self-review, M1 scope)

Found and fixed during review, each with a regression test: (1) a timestamp such as `0001-01-01T00:00:00+05:00` overflowed on UTC conversion and returned 500; (2) NUL or lone-surrogate characters would make PostgreSQL error and fail the whole batch — now a per-event `invalid_characters` rejection; (3) `NaN`/`Infinity` JSON numbers were accepted by the parser and would break JSONB — now `400 invalid_json`. Also fixed while testing: the validation-error handler used a FastAPI API that does not exist.

Verified by tests: secrets absent from the database, responses and logs; ingest keys never returned, logged or stored; cross-project read/write impossible; no value echoed in errors; storage and unhandled errors log only the exception class; no LLM, network library or subprocess import in the deterministic modules; pipeline runs with sockets blocked.

Repository scan: the actual local `.env` values (database password, ingest key) appear in **no** file other than the git-ignored `.env`. The only secret-shaped literals are two PEM header strings in the redaction test fixture (fake, intentionally).

Residual risks: pattern redaction misses unknown secret shapes; static keys are coarse and un-rotatable; rate limiter is per-process; the CI workflow has never run; the future dashboard must escape event text.

## Deviations from the approved plan

1. **No in-memory store.** The plan listed `storage/memory.py` for unit tests. Dropped: pure logic is unit-tested without a store, and everything that touches storage runs against real PostgreSQL, which gave stronger concurrency and constraint coverage.
2. **Added rejection `invalid_characters`, timestamp-overflow handling and `NaN` rejection** (from the security review).
3. **Reason codes** use the family `invalid_<field>` / `<field>_too_long` rather than a hand-listed code per field.
4. **ADR numbering:** the PostgreSQL/Snowflake decision is ADR-0007 (0002–0006 stay reserved for the deferred decisions listed in ADR-0001).
5. **OTLP logs adapter** is beyond the original plan; added per the owner's clarification that the adapter contract be real and tested.
6. Test fixtures needed a local `.env` (git-ignored) and a Docker Postgres; without `TRACEMIND_TEST_DATABASE_URL` the integration tests skip locally and fail in CI.

## Known limitations

- Fingerprint v1 parses Python tracebacks only; other languages group by message.
- Identical events with no client `event_id` in the same millisecond collapse into one.
- No retention or pruning of events or counters (M3).
- Verified on Windows only; Linux and the CI workflow are untested.
- No performance or load measurement was done and none is claimed.
- `config/projects.json` and the ingest keys are the temporary mechanism; project APIs and real authentication arrive in M5.

## Scope or estimate changes

None to the frozen plan. M1 took the planned shape; the time estimate is unaffected.

## Proposed next: M2 — Repository intelligence

Acceptance (from the frozen plan and `docs/acceptance-tests.md` M2-1…M2-7): Graphify verification spike first (ADR on adopt vs. documented fallback); scan records repository identity, commit SHA, graph version and scan status; real commits and diffs from a fixture repository; file addition/edit/deletion update the index and create auditable change records; bounded retry and graph-update recovery; hostile revision/path inputs rejected; unknown release-to-commit mappings stay unknown. Needs from the owner: approval to start M2 and, before any push, the repository set to private with the commit identity confirmed.
