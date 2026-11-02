# TraceMind threat model (M0)

Scope: V1 as defined in [sow.md](../sow.md) §13 and the frozen [PLAN.md](../PLAN.md). V1 is read-only: it inspects data and code and never changes production systems or source.

## Assets

1. Application telemetry (may contain secrets or personal data by accident).
2. Source code and Git history of registered repositories.
3. Credentials: Snowflake, Groq API key, database, repository access.
4. Integrity of incident records and investigation reports.
5. Warehouse and LLM spend.

## Trust boundaries

| Boundary | Untrusted side |
|---|---|
| Ingestion API | Every event field, including `message`, `stack_trace`, `attributes` |
| Repository content | Source files, commit messages, READMEs, diffs |
| Retrieved history | Previously stored events and reports |
| LLM output | Everything the model returns |
| Dashboard/API clients | All requests |

## Threats and controls

| # | Threat | Control | Milestone | Verified by |
|---|---|---|---|---|
| T1 | Secrets in logs persisted or sent to the LLM | Redact configured patterns/fields before persistence and again before any LLM call | M1 (store), M4 (LLM) | redaction unit tests with seeded fake secrets |
| T2 | Malformed or oversized payloads | Schema validation, size limits, bounded batch size, rate limits; reject, never coerce silently | M1 | invalid-payload and 413 tests |
| T3 | Duplicate delivery creating duplicate incidents | Idempotent event ids, dedup key, deterministic fingerprint | M1 | duplicate tests |
| T4 | Prompt injection via logs, code, commit messages, docs | Retrieved content labelled as data in a fixed template; model output has no execution path; report schema validation; evidence ids must resolve | M4 | injection fixtures in the evaluation set |
| T5 | Arbitrary SQL from the model | No model-authored SQL. Named, parameterized query templates only; read-only role; timeout and row cap | M3 | query-function tests, role permissions check |
| T6 | Shell or code execution from model output | No shell tool exists. Tool registry is an allowlist of read-only operations with schemas, timeouts and size limits | M4 | registry rejection tests |
| T7 | Unauthorized project/repository access | Authentication and project-level authorization on every route; repository paths restricted to registered locations | M2 (path rules), M5 (auth) | authz tests |
| T8 | Path traversal / option injection through the Git adapter | Fixed argv (no shell), validated revisions and paths, repository root confinement, output size caps | M2 | adapter tests with hostile inputs |
| T9 | Hallucinated or overconfident conclusions | Evidence-id citation, resolvable references, explicit confidence scale, "unverified" default, missing/contradicting evidence fields | M4 | schema and traceability tests |
| T10 | Wrong deployment-to-commit claim | `verified / estimated / unknown` confidence; timestamp proximity is never "verified" | M2, M5 | mapping tests |
| T11 | Runaway Snowflake or LLM cost | Query limits, timeouts, warehouse auto-suspend and resource monitor, token budgets, usage recording | M3, M4 | consumption-monitor tests |
| T12 | Credentials leaked to source control or logs | `.env` ignored, `.env.example` placeholders only, secrets redacted from logs | M0 onward | repo scan before each milestone close |
| T13 | Internal details exposed in API errors | Generic error envelope; no stack traces or config | M0 (demo), M1 | demo 500-body test, API error tests |
| T14 | Vulnerable dependencies | Pinned minimums, CI dependency audit | M1 (CI) | CI |
| T15 | Audit gaps for sensitive operations | Audit events for tool calls, investigations, feedback, resolution changes | M4 | audit tests |

## Residual risks and limits

- Redaction is pattern-based; unknown secret shapes can slip through. Mitigated by field allowlists for LLM prompts, not eliminated.
- Prompt injection cannot be fully prevented, only contained: the model has no side-effecting tools and output is validated.
- Graphify output is derived and may be stale or wrong; it is labelled with its commit SHA and graph version.
- M0 has no authentication. The demo service must only be run locally.

## Out of scope for V1

Automatic remediation, production writes, arbitrary command execution, enterprise SSO/RBAC (SOW §3.2).

## M1 implementation status (2026-10-28)

| Threat | State after M1 | Evidence |
|---|---|---|
| T1 secrets in logs | Mitigated for the ingestion path: key-name and pattern redaction before fingerprinting, storage and logging. Pattern-based, so unknown secret shapes remain a residual risk. LLM-bound redaction is M4 | `tests/unit/test_redact.py`, `tests/integration/test_api.py::test_secrets_never_reach_db_responses_or_logs` |
| T2 malformed / oversized input | Mitigated: strict models, size, count, depth and attribute limits, NUL/surrogate and `NaN` rejection, timestamp overflow handled, rate limit | `tests/unit/test_normalize.py`, `tests/integration/test_api.py` |
| T3 duplicate delivery | Mitigated: unique `(project_id, event_id)`, content-hash conflict detection, single transaction; 20-way concurrency test | `tests/integration/test_pipeline_store.py` |
| T7 unauthorized access | Partly mitigated (temporary): per-project key on every M1 route, reads and writes scoped to the key's project, constant-time comparison. Real auth is M5 | `tests/integration/test_api.py` (M1-11) |
| T12 credentials in source | Mitigated: keys and DB password only in environment / gitignored `.env`; tests generate their own keys; pre-push scan required | repo scan in the M1 report |
| T13 internal details in errors | Mitigated: envelope with stable codes; storage and unhandled errors log only the exception class name | `tests/integration/test_api.py` |
| T14 vulnerable dependencies | `pip-audit` found no known vulnerabilities on 2026-10-28; CI step exists but has not run | M1 report |
| T4–T6, T8–T11, T15 | Not applicable yet (no LLM, shell tool, Git adapter, Snowflake or investigation audit in M1) | — |

New M1 risks recorded:

- **T16 untrusted adapter input.** Adapters receive arbitrary JSON. Controls: adapter contract (no I/O, never raises beyond `AdapterFormatError`, bounded output, no value echo) enforced by contract tests including seeded fuzzing; adapter output still passes the same validation, redaction and fingerprinting.
- **T17 stored XSS in a future UI.** Event text is untrusted even after redaction; M5 must escape it.
- **T18 shipper trust.** `tools/ship_logs.py` forwards whatever JSON lines it reads; the API validates everything again, so the shipper is not a trust boundary.

## M2 implementation status (2026-10-28)

| Threat | State after M2 | Evidence |
|---|---|---|
| T7 unauthorized access | Repository, scan, change and code endpoints are project-scoped; another project's key gets 404 on every route; repository paths are confined to configured allowed roots with symlink/junction and `.git`-link escapes refused, re-validated on every use. Real authentication is still M5 | `tests/integration/test_repositories_api.py`, `test_repo_endpoints.py`, `tests/repos/test_validation.py` |
| T8 path traversal / option injection in the Git adapter | Mitigated: revisions and paths validated before any git call (asserted by a spy that fails if git is invoked); `git` runs with a fixed argv, no shell, no user or system config, literal pathspecs, no optional locks, no external diff or textconv programs (a hostile `diff.external` is proven not to run, with a control showing it would otherwise), time and output caps | `tests/repos/test_git_adapter.py`, `test_validation.py` |
| T10 wrong deployment-to-commit claim | Mitigated for M2: `verified` only for a telemetry-supplied full SHA that exists in the repository; everything else `unknown` with a reason; nothing inferred from release names, tags, branches or time | `tests/repos/test_resolver.py` |
| T6 shell execution | No model path exists. The only subprocesses are `git` and the pinned Graphify CLI with fixed argv and a scrubbed environment | code review; `test_graphify_runner.py` |
| T12 credentials | Graphify and git children receive no API keys, tokens, database URLs or proxies from the parent | `test_graphify_runner.py`, `test_git_adapter.py` |
| T14 dependencies | Graphify's whole dependency tree is installed with `--require-hashes` from a lock file into its own environment, and its installed files are re-verified against their recorded hashes before every worker start | `tools/install_graphify.py`, `infra/graphify/requirements.lock`, `test_graphify_runner.py` |
| T15 audit gaps | Scan history and change records are append-only (database triggers); no author names or e-mails are stored or returned | `tests/integration/test_repositories_api.py`, `test_scans_recovery.py` |

New M2 risks recorded:

- **T19 hostile repository content.** Source, commit messages and diffs are untrusted. Controls: no repository code is executed (snapshots are read from Git objects, Python files are only parsed, never imported); symlinks, submodules, unsafe, non-UTF-8, oversized and case-colliding entries are skipped and counted; commit subjects are redacted before storage; diffs are not exposed by any M2 endpoint. Residual: Graphify parses hostile source with tree-sitter; a parser bug is possible and is contained only by running it as a time-limited subprocess with a scrubbed environment inside a TraceMind-owned directory.
- **T20 stale or tampered derived data.** Graphs are content-addressed and hash-checked on load; a mismatch is refused; the status endpoint states which commit and tool version a graph describes. Residual: the data directory is trusted local storage; anyone who can write to it as the worker's user can alter snapshots (detected by hash for graphs and by blob comparison for snapshots on the next scan).
- **T21 misleading relationships.** Graphify marks some edges INFERRED and resolves some method calls to their class. Controls: confidence, file and line on every relationship; an absent edge or absent file is never reported as irrelevance (`not_in_graph` says so); coverage and Python syntax errors are surfaced. Residual: relevance ranking built on these edges (M4) must keep treating them as hints, not proof.
- **T22 worker resource exhaustion.** Caps on files, bytes, file size, graph size, node/edge counts, scan time and output; bounded retries. Residual: no memory limit on Graphify's process (not available portably on Windows).

## M3 preparation status (2026-11-02) — design only; nothing implemented, nothing run against Snowflake

Controls below are **designed and drafted** (setup guide, SQL drafts, M3 plan); none is verified by a test. "Verified by" names the test that would verify it. Documentation facts behind each entry: [docs/setup/snowflake-account-setup.md](setup/snowflake-account-setup.md) §4.

| Threat | Planned control | Residual risk / status | Verified by (all NOT RUN) |
|---|---|---|---|
| T5 arbitrary SQL from the model | Named, parameterized templates over reader **views** only; the reader role has `SELECT` on views and no table or write privilege; no SQL text accepted from callers | Design only. A view grant sufficing without table grants is documented, not tested | M3-2; READ-1/READ-2 |
| T11 runaway Snowflake cost | Provisional 20-credit allowance (not a cap); X-Small warehouse, 60 s auto-suspend, created suspended; resource monitor with 10/50/75 % notify, 90 % suspend, 100 % suspend-immediate, attached before any role can use the warehouse; exporter off by default with a 14,400 s default / 7,200 s floor interval; soft budget from TraceMind's own log; owner review of metering | **The monitor is one safeguard, not a guarantee:** not precise, may lag, covers warehouses only (not serverless, AI services or storage), **cannot suspend cloud-services credit usage**, behaviour on a trial account is undocumented, notifications are off by default, metering lags up to 3–6 h. The trial's free balance is a separate limit (its unit is not stated by the docs). A stolen key can spend until a control acts | M3-5; monitor and notification checks B1–B2, N0–N1 |
| T12 credentials | Two encrypted key pairs outside the repository; only the two passphrases are secrets; `.env.example` holds names only; migrator, cleanup and admin roles have no key and no variable | Keys are long-lived (T23) | secret scan; M3-7 |

New M3 risks recorded:

- **T23 long-lived service keys and the deferred network policy.** Two key pairs, role-restricted and expiring after 90 days; least-privilege roles. Residual: until a network policy exists, a stolen key **and** its passphrase work from any IP; whether a policy even applies to key-pair logins is not documented. Follow-up **F20** lists the triggers for revisiting this. Key expiry causes an outage if unnoticed (F22).
- **T24 duplicate or drifting exported data.** Snowflake does not enforce `PRIMARY KEY`/`UNIQUE` on standard tables. Controls: insert-only `MERGE` with deduplicated batches, a single PostgreSQL-enforced exporter, a short `LOCK_TIMEOUT`, a post-load duplicate audit. Residual: `MERGE` privileges and concurrent behaviour are **unverified** (F21); a failed test stops M3 and goes to the owner.
- **T25 accidental deletion of development data during test cleanup.** Controls: separate test database; `tmtest-` run ids with a format guard inside the `DELETE`; a cleanup role with **no privilege on `TRACEMIND_DEV`**; secondary roles switched off in every cleanup session; leak check; transaction with an exact-count check. Residual: the migrator (held by the owner) owns the development tables and holds every privilege on them; **the `CREATE USER` default for secondary roles is `ALL`**, so unless the owner's session turns them off, a "cleaner" session can act through the migrator — the `NONE` step is a **procedure, not a privilege boundary** (open decision O9, F23); the cleaner can delete any row inside `TRACEMIND_TEST`, and another test run's rows are protected only by the human-read count check. The same secondary-role weakness invalidates any permission test run on a user that also holds administrative roles.
- **T26 writer privilege creep.** `MERGE` might turn out to need `UPDATE`/`DELETE`. Control: the writer is `INSERT`+`SELECT` only and nothing is widened automatically; the owner decides (O7).
- **T27 silent notification failure.** Resource-monitor notifications need a verified e-mail and enabled preferences. Control: a 10 % canary trigger; until its e-mail arrives, notifications are treated as not working.
- **T28 human-held privileged roles.** The migrator and the cleaner are granted to the owner's user only, have no key pair, are not granted to `SYSADMIN`, and are never configured for the application. Residual: the owner's session holds them (and probably `ACCOUNTADMIN`); the protocols in script 99 depend on the owner following them.
