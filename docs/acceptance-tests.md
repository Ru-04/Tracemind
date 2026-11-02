# Acceptance tests

Derived from the frozen [PLAN.md](PLAN.md) deliverables and the SOW. A test is "passed" only when it has been run and the result recorded in the milestone report. Status: `defined` (not yet runnable), `automated` (a test exists), `manual` (scripted manual check).

## Demo failure scenarios (shared fixtures for every milestone)

| ID | Route | Failure | Error type | Deterministic | Used from |
|---|---|---|---|---|---|
| S1-controlled-exception | `/demo/controlled-exception` | Invoice total negative | `InvoiceError` | yes | M0 |
| S2-dependency-timeout | `/demo/dependency-timeout` | Simulated DB timeout after 3 attempts | `DependencyTimeoutError` | yes | M0 |
| S3-config-error | `/demo/config-error` | Required `DEMO_PAYMENT_GATEWAY_URL` unset | `ConfigurationError` | yes (env) | M0 |
| S4-regression | `/demo/regression` | Division by zero on empty cart | `ZeroDivisionError` | yes | M0; real commit history added in M2 |

## M0

| ID | Check | Status |
|---|---|---|
| M0-1 | Repo has the documented folder structure and a clean local git history | manual |
| M0-2 | ADR and threat model exist and cover the SOW §13.1 threat list | manual |
| M0-3 | Event, ingest-event and incident JSON schemas are valid, accept the example payloads and reject malformed ones (`tests/test_schemas.py`) | automated |
| M0-4 | Each scenario raises its expected error on every call, and its endpoint returns a generic 500 without leaking the error type (`tests/test_demo_app.py`) | automated |
| M0-5 | `ruff` and `mypy` pass | automated |
| M0-6 | Acceptance tests for M1–M5 are defined below | manual |

## M1 — trigger a known error and see its normalized event stored

| ID | Check | Status |
|---|---|---|
Status as of 2026-10-28: all twelve are **automated and passing** (full run: 242 passed — see [docs/reports/M1-report.md](reports/M1-report.md)). Integration tests need PostgreSQL (`infra/docker-compose.yml`); without it they skip locally and fail in CI (`TRACEMIND_REQUIRE_DB=1`). M1-10 to M1-12 were added by the approved M1 plan; they are not part of the original M0 list.

| ID | Check | Covered by |
|---|---|---|
| M1-1 | Demo app emits one structured JSON log line per failure with the fields of the ingest schema | `tests/unit/test_demo_telemetry_and_tools.py` |
| M1-2 | Valid event → 202 and a stored normalized event with UTC `occurred_at`, enum severity, lower-cased service/environment | `tests/integration/test_api.py`, `test_pipeline_store.py`, `test_e2e_demo.py` |
| M1-3 | Malformed payloads → clear 4xx errors using the error envelope; oversized → 413; none stored; malformed counter increments | `tests/integration/test_api.py`, `tests/unit/test_normalize.py` |
| M1-4 | Seeded fake secrets are absent from stored data, API responses and TraceMind logs | `tests/unit/test_redact.py`, `tests/integration/test_api.py` |
| M1-5 | Re-sending the same `event_id` → idempotent duplicate; duplicate counter increments; occurrence count unchanged; same id with different content keeps the original; 20 concurrent identical submissions store exactly one | `tests/integration/test_pipeline_store.py` |
| M1-6 | Same failure twice ⇒ identical fingerprint; S1–S4 produce four distinct fingerprints; stable across runs and machine paths; golden value | `tests/unit/test_fingerprint.py`, `tests/integration/test_e2e_demo.py` |
| M1-7 | Missing `trace_id` stays null — nothing fabricated; invalid ids rejected | `tests/unit/test_normalize.py`, `tests/integration/test_api.py` |
| M1-8 | Ingestion health endpoint reports accepted/duplicate/duplicate-conflict/rejected counts and reasons | `tests/integration/test_api.py`, `test_pipeline_store.py` |
| M1-9 | No LLM/network-library import in the deterministic modules; pipeline runs with sockets blocked | `tests/unit/test_isolation.py` |
| M1-10 *(added)* | Incident grouping: open incident on first ERROR; repeats group; out-of-order timestamps; different fingerprints split; WARNING stored but no incident; duplicates never bump the count; schema-valid; commit association `unknown` | `tests/integration/test_pipeline_store.py` |
| M1-11 *(added)* | Auth and isolation: 401 without/with wrong key; project A's key cannot read or write project B; body project mismatch → 403; unknown service rejected; 429 with `Retry-After`; key never in responses, logs or database | `tests/integration/test_api.py` |
| M1-12 *(added)* | Extensibility and degradation: a real adapter contract enforced for every adapter (native + OTLP logs, with fuzzing); OTLP end to end; a new adapter works by registration alone; `/ready` 503 naming the database while `/health` stays up | `tests/unit/test_adapter_contract.py`, `tests/integration/test_adapters_api.py`, `test_api.py` |

## M2 — a code change updates the index and produces an auditable change record

Status as of 2026-10-28: all ten are **automated and passing** against real Git repositories, real PostgreSQL 16 and the real pinned Graphify 0.9.97 (see [docs/reports/M2-report.md](reports/M2-report.md)). Tests marked `graphify` need the hash-pinned CLI (`python -m tools.install_graphify`); locally they skip when it is absent, in CI (`TRACEMIND_REQUIRE_GRAPHIFY=1`) they fail. M2-8 to M2-10 were added by the approved M2 plan.

| ID | Check | Covered by |
|---|---|---|
| M2-1 | Graphify verified and decision recorded (ADR-0003 accepted); the runner refuses a CLI whose version differs or whose installed files do not match their recorded hashes; documented fallback exists (not implemented) | [ADR-0003](adr/0003-graphify-adoption.md), `tests/repos/test_graphify_runner.py` (verification outcomes, real install) |
| M2-2 | Initial scan stores repository identity, commit SHA, Graphify version, graph schema version, graph SHA-256, counts, coverage and status; re-triggering the same commit is idempotent; an independent scan of the same commit gives the same graph hash | `tests/integration/test_scans_lifecycle.py`, `test_scans_concurrency.py` |
| M2-3 | Commits, diffs and trees come from real Git: log, changed files with A/M/D/R and line counts, per-file diff, merge commits, empty tree, binary files; every change cites a real SHA and path; missing revisions and paths are clean errors | `tests/repos/test_git_adapter.py`, `test_snapshot.py`, `tests/integration/test_scans_lifecycle.py`, `test_repo_endpoints.py` |
| M2-4 | File addition, edit, deletion and rename each update the index incrementally and create exactly one change record whose files, line counts and graph delta match the commit; the snapshot verifies against the commit | `tests/integration/test_scans_lifecycle.py`, `tests/repos/test_graphify_runner.py` |
| M2-5 | Retries are bounded with backoff; transient failure succeeds on retry; permanent failure fails without altering the last good graph; corrupt, truncated, partial, empty or wrong-schema Graphify output never becomes current; timeouts kill the process tree; a corrupt graph or tampered snapshot is rebuilt; dead workers are reclaimed; a worker that lost its lease cannot publish; history is never lost | `tests/integration/test_scans_recovery.py`, `tests/repos/test_graphify_runner.py`, `test_code_graph.py` |
| M2-6 | Hostile revisions, paths and repository locations are rejected before any git call or filesystem access: option injection, `..`, absolute and drive paths, NUL/control characters, over-length, flag-like refs, paths outside allowed roots, symlink/junction escapes, `.git` links, the data directory; unsafe tree entries are skipped | `tests/repos/test_validation.py`, `test_git_adapter.py`, `test_snapshot.py`, `tests/integration/test_repositories_api.py`, `test_repo_endpoints.py` |
| M2-7 | Release-to-commit mapping is `unknown` (with a reason) unless a telemetry-supplied full 40-character SHA exists in the registered repository; release names, tags, branches, abbreviations and timestamps are never used; rejected input never reaches git | `tests/repos/test_resolver.py`, `tests/integration/test_repo_endpoints.py` |
| M2-8 *(added)* | Read-only boundary: the monitored repository (files and `.git`) is byte-identical after register, scan, update and queries; Graphify only ever runs inside TraceMind's data directory; git runs with no user config, no external diff/textconv programs and no optional locks | `tests/repos/test_git_adapter.py`, `test_snapshot.py`, `tests/integration/test_scans_lifecycle.py` |
| M2-9 *(added)* | No LLM or network use: Graphify runs only as `extract . --code-only --no-gitignore` or `update .`, with API keys, credentials and proxies scrubbed and deny-all proxies set; a real scan succeeds under those conditions | `tests/repos/test_graphify_runner.py` |
| M2-10 *(added)* | Project isolation and graph honesty: another project's key cannot see, queue or query a repository; no filesystem path, author name or e-mail appears in any response; every relationship carries confidence, file, line and graph version; INFERRED is labelled; syntax-error files and missing coverage are flagged | `tests/integration/test_repo_endpoints.py`, `test_scans_lifecycle.py`, `tests/repos/test_code_graph.py` |

**End-to-end demo (the M2 deliverable)** — automated as `test_demo_script_code_change_updates_index_and_produces_a_change_record` and also run by hand against live API and worker processes (see the M2 report): register a fixture repo, scan, commit a change that adds a function, edits one file and deletes another, scan again; `GET .../changes` shows a record listing the real files, line counts, commit subject and graph delta; `GET .../code?symbol=` shows the new callers with confidence; `GET .../status` shows the new indexed SHA.

## M3 — historical incidents queryable with evidence references

| ID | Check | Status |
|---|---|---|
| M3-1 | Telemetry and incident-knowledge schemas created from versioned scripts | defined |
| M3-2 | Query functions are named, parameterized, read-only; no free-form SQL path exists | defined |
| M3-3 | Known sample data returns the expected rows, each with an evidence reference | defined |
| M3-4 | Time window and row limits enforced; timeout produces a handled error | defined |
| M3-5 | Consumption (query count/duration) recorded | defined |
| M3-6 | App stays usable with Snowflake unavailable and reports degraded state | defined |
| M3-7 | No credentials in source | defined |

## M4 — evidence-backed investigation with explicit uncertainty

| ID | Check | Status |
|---|---|---|
| M4-1 | Tools are registered with schemas, timeouts and size limits; unregistered or invalid calls are rejected | defined |
| M4-2 | Report validates against the schema; invalid model output is retried then fails safely without corrupting the incident | defined |
| M4-3 | Every evidence id in a report resolves to a stored record | defined |
| M4-4 | At least two competing hypotheses, each with supporting/contradicting/missing evidence and suggested checks | defined |
| M4-5 | Report is marked unverified; feedback and verified outcome are stored separately | defined |
| M4-6 | Prompt-injection fixtures in logs/code do not change behavior or produce actions | defined |
| M4-7 | Redaction applied before the Groq call; provider metadata and token usage recorded | defined |

## M5 — complete, demonstrable V1

| ID | Check | Status |
|---|---|---|
| M5-1 | Unauthenticated requests rejected; users cannot read another project's data | defined |
| M5-2 | A developer navigates overview → incident → investigation using real API data | defined |
| M5-3 | E2E run over S1–S4: ingestion → incident → investigation → dashboard | defined |
| M5-4 | Measured and reported: detection recall, false positives, grouping accuracy, evidence traceability, investigation latency, invalid-output rate, token and Snowflake cost | defined |
| M5-5 | Evidence and AI hypotheses are visually distinct; loading/empty/error states exist | defined |
| M5-6 | Documented setup works from a clean environment; deployed demo reachable | defined |
