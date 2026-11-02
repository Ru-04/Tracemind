# M3 plan — Snowflake and incident lifecycle

**Status:** **Snowflake preparation completed (documents and unrun drafts); M3 implementation pending and still BLOCKED.** No M3 implementation has started, no Snowflake operation has been performed, and M3 is **not** complete. M3 begins only after (a) the owner completes the Snowflake setup and supplies the configuration through the secure configuration process (section 2), and (b) the owner separately authorizes implementation. Revision 2026-11-02: sections 2.2–2.4, 3, 4.4, 4.5, 4.7, 5, 6, 7, 8 and 9 were updated to match [docs/setup/snowflake-account-setup.md](../setup/snowflake-account-setup.md). **Assumption to confirm:** the owner said the O1–O8 decisions "have been made" without restating them; the assistant applied its own recommendations, so section 9 items 12–17 are "applied, owner confirmation pending"; only O1 (defer the account-level monitor) is the owner's own instruction. The 7,200 s export-interval floor is the assistant's change.
**Frozen scope (PLAN.md):** create the telemetry and incident-knowledge schemas · connect through the Python connector · implement approved, read-only query functions · store incident history, deployment metadata and investigation results · add limits, query timeouts and consumption monitoring.
**Deliverable:** historical incidents can be queried programmatically with evidence references.
**Also in the milestone title:** incident lifecycle (open, investigating, resolved, ignored), deferred from M1 by the owner.
**Constraints carried forward:** read-only toward monitored systems; no LLM-authored SQL; credentials never in chat or in the repository; nothing committed or pushed until the repository is private and the commit identity is confirmed; claims limited to what is verified.

## 1. What can and cannot happen before the configuration arrives

| Can happen now (no Snowflake access) | Cannot happen until configuration is provided |
|---|---|
| This plan; owner decisions in section 9 | Any connection to Snowflake, including "just testing" |
| After approval and **only** with explicit go-ahead: the Postgres-side lifecycle and deployment work, and connector-independent code against a fake (S1) | Creating schemas, tables, roles, warehouses or monitors in Snowflake |
| Static design of query templates and their tests | Verification of connector behaviour, privileges, costs, timeouts against a real account |

The owner stated that M3 does not start until the Snowflake configuration is provided, so even the connector-independent slice waits.

## 2. Prerequisites: what I need from the owner, and how it must be provided

**Rule:** the owner never pastes passwords, tokens, private keys or passphrases into chat, and none are committed. Values reach the application only through the secure configuration process: the git-ignored `.env` file on the machine that runs TraceMind, or an OS/cloud secret store that exports environment variables. Key material lives in a file **outside** the repository with restricted permissions. The repository documents variable *names* only ([.env.example](../../.env.example) lists them: secrets and the account identifier empty; object names and non-secret defaults filled in).

### 2.1 Non-secret facts (may be shared in chat or in a document)

| Item | Why |
|---|---|
| Account edition (Standard/Enterprise/Business Critical), cloud and region | Features and failure modes differ (for example Time Travel length, resource monitor options) |
| Whether a **dedicated development database** may be created, and its name | Tests and the first export must never touch production data. I will only ever write to this database |
| Preferred database/schema/role/warehouse naming conventions, if any | Avoid clashes |
| Who will run the one-time administrator setup (section 2.3) | TraceMind never needs `ACCOUNTADMIN` at runtime |
| Retention policy wishes for telemetry and incident history | Drives table design and cost |
| Credit budget for development (for example "at most N credits per month") | Sets the resource monitor quota |
| Data-handling constraints (is storing redacted telemetry in Snowflake acceptable; any region or compliance limits) | Telemetry is redacted before storage, but this is still a policy decision |

### 2.2 Configuration variables (only the two passphrases and the key files are secret; provided only through the secure process; I never ask to see them)

| Variable | Content | Where it lives |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` | Account identifier, preferred form `orgname-account_name` (not a secret, but account-specific and kept private) | `.env` or secret store |
| `SNOWFLAKE_AUTHENTICATOR` | `SNOWFLAKE_JWT` (key-pair) | `.env` |
| `SNOWFLAKE_WRITER_USER` / `SNOWFLAKE_READER_USER` | **Two dedicated service users** (`TYPE = SERVICE`), writer = exporter, reader = query service; not people | `.env` |
| `SNOWFLAKE_WRITER_ROLE` / `SNOWFLAKE_READER_ROLE` | One role each; the key pairs are **role-restricted**, so the role must always be requested explicitly | `.env` |
| `SNOWFLAKE_WRITER_PRIVATE_KEY_FILE` / `SNOWFLAKE_READER_PRIVATE_KEY_FILE` | **Paths** to two separate encrypted private key files, outside the repository, readable only by the account running TraceMind | key files on disk; paths in `.env` |
| `SNOWFLAKE_WRITER_PRIVATE_KEY_PASSPHRASE` / `SNOWFLAKE_READER_PRIVATE_KEY_PASSPHRASE` | One passphrase per key. **The only secrets in the variable set** | `.env` or secret store |
| `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_TEST_DATABASE` | `TRACEMIND_WH`, `TRACEMIND_DEV`, `TRACEMIND_TEST` | `.env` |
| `TRACEMIND_SNOWFLAKE_ENABLED` and the limit settings | Non-secret; the exporter is **off by default**; see [.env.example](../../.env.example) | `.env` |

Password authentication is not planned (and SERVICE users cannot use one). If key-pair auth is impossible in the owner's environment, that is a decision to raise, not something to work around silently. **Migrator, test-cleanup and administrator credentials never appear in this set:** those roles are held by a human in an interactive session and have no key pair.

### 2.3 Snowflake-side setup (one time, by an administrator; I will provide a reviewable SQL script for the administrator to read and run — I do not run it)

1. **Two service users** (`TYPE = SERVICE`, no password), each with its own encrypted, **named, role-restricted** key pair that expires after **90 days**: `TRACEMIND_EXPORTER_SVC`→writer, `TRACEMIND_QUERY_SVC`→reader. The **network policy is deferred** for initial local development (residual risk and review triggers: follow-up F20, setup guide §10).
2. Roles with least privilege (the exact script is `infra/snowflake/01_admin_setup.sql`, a draft for the owner to run; I never run it):
   - `TRACEMIND_WRITER`: `USAGE` on the warehouse, database and the two data schemas; `INSERT` and `SELECT` on the export tables and **nothing else** (no `UPDATE`, `DELETE`, `TRUNCATE`, `CREATE`). This is the documented-by-inference minimum for an insert-only `MERGE` (4.5); it is UNVERIFIED until test MERGE-1 passes.
   - `TRACEMIND_READER`: `USAGE` on the warehouse, database and data schemas; `SELECT` only on the approved **views**.
   - `TRACEMIND_MIGRATOR`: `USAGE` plus `CREATE TABLE`/`CREATE VIEW` on the schemas; **held by the owner's user, run by hand**, no key pair, never configured for the application. It owns what it creates.
   - `TRACEMIND_TEST_CLEANER`: `USAGE`, and `SELECT`+`DELETE` on tables in `TRACEMIND_TEST` only; **no privilege of any kind on `TRACEMIND_DEV`**; held by the owner's user; used only by the guarded procedure in script 99 section T, always after `USE SECONDARY ROLES NONE`.
3. A dedicated **X-Small warehouse**, `AUTO_SUSPEND` of 60 seconds, `AUTO_RESUME`, created suspended, with **its own resource monitor** (one warehouse per monitor): a **provisional 20-credit development allowance** (not a guaranteed cap), `FREQUENCY = NEVER`, `NOTIFY` at 10 % (a canary that proves notifications work), 50 % and 75 %, `SUSPEND` at 90 %, `SUSPEND_IMMEDIATE` at 100 %. Creating the monitor and attaching it are `ACCOUNTADMIN`-only (verified) and must happen **before any TraceMind role can use the warehouse**. The monitor is one safeguard among several; its limits (not precise, warehouses only, cannot suspend cloud-services credit usage, undocumented on trials) are in setup guide §9. An optional account-level monitor is **deferred** until the read-only preflight lists the account's other compute.
4. A statement timeout set **on the warehouse** (300 s) as a server-side backstop to the client-side timeouts; a user-level setting is not documented. The application also sets a short `LOCK_TIMEOUT` on every session (the default is 12 hours).
5. A dedicated development database (`TRACEMIND_DEV`) and a test database (`TRACEMIND_TEST`), separate from anything production.

### 2.4 Acceptance of the prerequisite step

Done when the owner confirms that the secure configuration is in place (without sending values). My first action will then be a read-only connectivity check that prints only *whether* it connected, which role/warehouse the session reports, and that the privileges are as narrow as intended (the writer cannot drop; the reader cannot insert). If those checks fail, M3 stops and I report.

The check, per service user, must also: connect **requesting the restricted role explicitly** (how the connector conveys it is UNVERIFIED); confirm the key's expiry date is in the future and warn when fewer than 14 days remain; confirm the warehouse suspends after use; confirm no credential appears in any output or log; and then run the canary and `MERGE` tests of `infra/snowflake/02_verify_permissions.sql` section 4 (M3-12, M3-13). The owner's own preparation steps come first and are recorded PASS / FAIL / NOT RUN in the setup guide (§11); an item not run is never counted as passed.

## 3. Verified facts, assumptions and unknowns

Documentation read on 2026-10-28 and **not yet exercised** against any account (details in [dependency-verification.md](../dependency-verification.md)): key-pair authentication parameters, server-side binding with `paramstyle='qmark'`, per-`execute` `timeout`, resource monitors are warehouse-only with `NOTIFY`/`SUSPEND` triggers, warehouses auto-suspend. **Unknown until verified:** connector behaviour on Windows with this Python version (5.0 release candidates exist), per-statement `QUERY_TAG` mechanics, `STATEMENT_TIMEOUT_IN_SECONDS` handling, how consumption can be observed per query, service-user and network-policy behaviour, the owner's edition and region.

**Added 2026-11-02 (documentation review of 44 opened pages, still not exercised against any account; full list with URLs in setup guide §4):** `PRIMARY KEY`/`UNIQUE` are **not enforced** on standard tables; `UPDATE`/`DELETE`/`MERGE` hold locks that stop them running in parallel with each other while plain `INSERT` generally does not serialize; `LOCK_TIMEOUT` defaults to 12 hours; SERVICE users cannot log in with a password; a role can query a view without privileges on its tables; a temporary table needs no `CREATE TABLE`; future grants are not applied when a table is renamed or swapped; privileges of active secondary roles count towards authorization; `DROP ROLE` transfers the role's objects to the role that drops it. **Still UNVERIFIED and gating:** what privileges `MERGE` needs (no official page states it), whether a blocked `MERGE` re-reads the target after the lock, whether future grants reach tables created by the migrator, how the connector conveys the restricted role, whether a network policy applies to key-pair logins, and everything about resource monitors on a trial account.

## 4. Proposed design

### 4.1 PostgreSQL stays the source of truth (ADR-0007)

Snowflake holds a **derived, one-way, idempotent copy** for history and similarity. Ingestion and incident grouping never wait for Snowflake; if it is unavailable the export backlog grows and the system reports a degraded state. All data exported is already redacted.

### 4.2 Incident lifecycle (PostgreSQL, no Snowflake needed)

Status stays separate from resolution (SOW 7.4). Proposed transitions, each recorded in an **append-only** `incident_transitions` table (who, when, from, to, note) and exposed through a validated endpoint:

| From → To | Allowed | Notes |
|---|---|---|
| open → investigating | yes | set when an investigation starts (M4) or manually |
| investigating → open | yes | investigation cancelled |
| open / investigating → resolved | yes | **does not set resolution verified**; `resolution.state` changes only through its own evidence-carrying operation |
| open / investigating → ignored | yes | requires a note |
| resolved → open, ignored → open | yes | manual reopen |
| any other transition | rejected with a stable error | |

New occurrence of a `resolved` incident: proposed to **reopen automatically** (status `open`, transition recorded as system-initiated, `reopen_count` incremented); a new occurrence of an `ignored` incident only increments counters. Both are decisions for the owner (section 9).

### 4.3 Deployment metadata (PostgreSQL, mirrored to Snowflake)

A `deployments` table (project, environment, release, optional commit SHA, deployed_at, source) filled through a project-scoped `POST …/deployments` (new endpoint, needs approval) and read with the planned `GET …/deployments`. The commit link goes through the M2 resolver, so it is `verified` only for a full SHA that exists in a registered repository and `unknown` otherwise. No inference from timestamps.

### 4.4 Snowflake schemas (draft, to be reviewed before any DDL is written)

- `TELEMETRY.EVENTS` — normalized, redacted events (project, event_id, service, environment, occurred_at, severity, error_type, message, fingerprint, fingerprint_version, trace_id, commit_sha, release, attributes as VARIANT); logical key `(project_id, event_id)`; a nullable `test_run_id` column (NULL for real data; a `tmtest-YYYYMMDDTHHMMSSZ-xxxxxxxx` id on test rows, never written to `TRACEMIND_DEV`, never absent on `TRACEMIND_TEST`). **Snowflake does not enforce the key** (VERIFIED: constraints are "not enforced" on standard tables), so uniqueness comes from the loader (4.5), a single exporter and a post-load duplicate audit, not from the table. `MERGE` makes loads idempotent *only under those conditions*.
- `TELEMETRY.EXPORT_WATERMARK` or an export marker kept in PostgreSQL (preferred, so Snowflake stays purely derived).
- `KNOWLEDGE.INCIDENT_SUMMARIES` — one row per incident version (fingerprint, service, environment, first/last seen, count, severity, status, summary).
- `KNOWLEDGE.INVESTIGATION_RESULTS` — structured results stored by M4 (empty until then); `KNOWLEDGE.DEPLOYMENTS`.
- Reader **views** expose only what query functions need.
- Clustering, retention and warehouse size are decided from the data-volume and budget answers in 2.1 (SOW 5.2); no clustering keys are added without evidence.

### 4.5 Export worker

Reuses the ADR-0002 queue pattern: a separate job reads unexported events and incident versions in bounded batches, loads them with an insert-only `MERGE` keyed by `(project_id, event_id)`, and records progress in PostgreSQL. **Exact operation and design requirements (owner decision 7, 2026-11-02; `MERGE` is retained):**

```sql
MERGE INTO <db>.TELEMETRY.EVENTS AS t
USING ( <deduplicated bounded batch: bound VALUES, or a session TEMPORARY table> ) AS s
ON t.project_id = s.project_id AND t.event_id = s.event_id
WHEN NOT MATCHED THEN INSERT (...) VALUES (...);     -- no WHEN MATCHED clause: no UPDATE, no DELETE
```

1. **Minimum privilege `INSERT` + `SELECT` on the target — an INFERENCE.** No official Snowflake page states what `MERGE` requires. The writer holds exactly that and nothing more; test **MERGE-1** must pass before the exporter is trusted. If it fails, M3 stops and the owner decides; **no `UPDATE` or `DELETE` is ever granted automatically.**
2. **One exporter at a time, enforced in PostgreSQL** (the ADR-0002 lease/advisory-lock pattern). This is the primary duplicate guard because Snowflake cannot enforce the key.
3. **Deduplicate every batch** before sending and in the `USING` subquery: with only `WHEN NOT MATCHED … INSERT`, each duplicate source row becomes a target row (documented; MERGE-3a/3b).
4. **Short `LOCK_TIMEOUT` on every session** (60 s proposed; default 12 h), so a blocked exporter fails and retries with backoff.
5. **`MERGE` locks** (documented to prevent parallel `MERGE`/`UPDATE`/`DELETE`) are defense in depth, not the guard; whether a blocked `MERGE` re-reads the target after the lock is UNVERIFIED and is what test **MERGE-4a/4b** demonstrates.
6. **Duplicate audit after every export** (the writer has `SELECT`): `GROUP BY project_id, event_id HAVING COUNT(*) > 1` must return nothing, otherwise the exporter reports a degraded state.
7. **Why not `INSERT … WHERE NOT EXISTS`:** plain `INSERT` statements generally do not serialize, so it cannot be shown safe under concurrency; it is not substituted unless a later, tested design is approved.
8. **No table rename or swap in migrations** (future grants are not applied on rename/swap) unless the migration re-grants.
9. **Cadence:** off by default; `TRACEMIND_SNOWFLAKE_EXPORT_MIN_INTERVAL_SECONDS` default 14,400 s with a code-enforced floor of 7,200 s (start-up refused below it); export only when a backlog exists; manual trigger preferred during development. Arithmetic in setup guide §8.3 (exact X-Small rate 1/3600 credits per second; estimates, not measurements): roughly 3–15 credits per 30 days at the default (base case 6) and 6–30 at the floor (base 12) — so in the pessimistic case the floor **alone** exceeds the 20-credit allowance, and these figures exclude reader queries, migrations and tests.

Re-running or crashing mid-batch must not duplicate rows (M3-9, M3-12, M3-13), but **that is a property to be demonstrated, not assumed.** Failures use bounded retries with backoff; Snowflake outage leaves ingestion untouched.

### 4.6 Approved, read-only query functions

Named functions only; each has a static SQL template, typed validated parameters, a hard row cap, a time-window cap, a statement timeout, and returns structured rows **with evidence references** (`event:<event_id>`, `incident:<id>`, `history:<incident_id>`). Nothing accepts SQL text, table names, column names or free-form predicates.

| Function | Purpose |
|---|---|
| `events_for_fingerprint(project, fingerprint, window, limit)` | Occurrences of an error pattern |
| `events_for_service_window(project, service, environment, start, end, limit)` | Telemetry around an incident |
| `similar_incidents(project, fingerprint \| (service, error_type), window, limit)` | Deterministic similarity: same fingerprint, else same service and error type; no embeddings, no LLM |
| `documented_resolutions(project, fingerprint, limit)` | Previously recorded resolutions — shown as *historical suggestions*, never as verified fixes for the current incident |
| `events_for_verified_commit(project, commit_sha, window, limit)` | Telemetry for a release whose commit mapping is `verified` |

Binding uses server-side `qmark`. Identifiers are never interpolated. A test asserts every template is a single `SELECT` over the reader views with only bind placeholders. The runtime reader role could not write even if a template were wrong.

### 4.7 Limits, timeouts, consumption monitoring

Client timeout per statement plus the server-side backstop; row, window and concurrency caps; a per-process semaphore on concurrent Snowflake statements; every statement tagged with a TraceMind query tag. For each call, record in PostgreSQL: function, project, duration, rows, outcome and the Snowflake query id (no SQL text with parameters, no result content). Consumption is monitored from warehouse-level metering and the resource monitor, with the caveat that **per-query credit cost is not directly available**; exact mechanisms are to be verified in the account. A configurable soft budget (`TRACEMIND_SNOWFLAKE_SOFT_BUDGET_CREDITS`, proposed default 15 = 75 % of the provisional 20-credit allowance) raises a degraded/warning state before the hard resource-monitor suspension. **The soft budget is computed from TraceMind's own call log** (statement durations and warehouse resume counts at the documented X-Small rate), because the runtime roles deliberately hold no `MONITOR` privilege and cannot read Snowflake metering, which in any case lags up to 3 hours (cloud services up to 6). The owner reviews actual consumption with `02_verify_permissions.sql` section 3 after every working session. Neither the soft budget nor the resource monitor is a guaranteed cap: the monitor is not precise, covers warehouses only, and its behaviour on a trial account is undocumented.

### 4.8 Degraded mode

If Snowflake is unreachable, slow or suspended: ingestion, incident grouping, lifecycle and repository features keep working; query functions return a structured `unavailable` result with a stable reason; `/ready` reports the dependency as degraded without hiding it or exposing details; the export backlog is visible in a health endpoint.

## 5. Acceptance checks (M3-1 to M3-7 from the M0 list, made concrete; M3-8 to M3-14 are additions for approval)

**Status of every check below: NOT STARTED / NOT RUN.** None has been run; none can be until the owner authorizes M3 and the Snowflake setup exists.

| ID | Check | How |
|---|---|---|
| M3-1 | Telemetry and incident-knowledge schemas exist from **versioned scripts**; applying them twice is harmless; a fresh database reaches the same structure | migration tests against the dev database (`snowflake`-marked) |
| M3-2 | Query functions are named, parameterized, read-only; there is no code path that takes SQL text; templates are single `SELECT`s over reader views | static template tests (no account needed) plus live check that the reader role cannot write |
| M3-3 | Known synthetic data loaded into the test database returns exactly the expected rows, each with a resolvable evidence reference | live tests, deterministic fixtures |
| M3-4 | Window and row caps enforced; invalid parameters rejected before any statement is sent; a deliberately slow statement is aborted at the timeout and reported cleanly | unit tests with a fake connector; one live timeout test |
| M3-5 | Every call records its metadata (function, duration, rows, query id, outcome); consumption/budget state is visible | tests with a fake connector and live check |
| M3-6 | With Snowflake unavailable (unreachable, bad credentials, suspended warehouse) the API stays usable, queries report `unavailable`, `/ready` shows degraded, export backlog grows and then drains after recovery without duplicates | fault-injection tests |
| M3-7 | No credential appears in the repository, logs, errors, API responses or query metadata; key material is read from a path outside the repository | secret scan, log capture tests |
| M3-8 *(add)* | Incident lifecycle: only the allowed transitions succeed; invalid ones are rejected with stable errors; every transition is appended to an append-only table; `resolved` never implies a verified resolution; reopen behaviour as decided | PostgreSQL integration tests |
| M3-9 *(add)* | Export idempotence: re-running or crashing mid-batch never duplicates rows (insert-only `MERGE`, deduplicated batch, single exporter, duplicate audit clean); PostgreSQL wins on conflict because an existing key is never updated; only redacted data is exported | tests with a fake connector and live (M3-12, M3-13) |
| M3-10 *(add)* | Project isolation: every query function and endpoint is scoped to the caller's project; another project's key cannot read its history | integration tests |
| M3-11 *(add)* | Deployments: records are validated and project-scoped; commit links are `verified` only for existing full SHAs, else `unknown`, never inferred from time | tests reusing the M2 resolver |
| M3-12 *(add)* | Writer privileges and `MERGE`: the insert-only `MERGE` succeeds with `INSERT`+`SELECT` only (MERGE-1); a re-run inserts 0 rows (MERGE-2); duplicate source rows are shown to duplicate and the dedupe fixes it (MERGE-3a/b); the writer cannot `UPDATE`/`DELETE`/`TRUNCATE` (MERGE-6); the duplicate audit is clean (MERGE-7). **Outcome decides O7; nothing is granted automatically** | live, `TRACEMIND_TEST`, canary table (`02_verify_permissions.sql` §4.1) |
| M3-13 *(add)* | Concurrency: a second identical `MERGE` started while the first holds its lock waits, times out under a short `LOCK_TIMEOUT`, and after the first commits adds 0 rows (MERGE-4a/4b). A different outcome is recorded as a FAIL and reported | live, two sessions (`02_verify_permissions.sql` §4.2) |
| M3-14 *(add)* | Test-data safety: no `tmtest-` row exists in `TRACEMIND_DEV` (LEAK-1); the cleanup role has no access to development data with secondary roles off (CLEAN-0), and the demonstration with secondary roles on is recorded (CLEAN-2); the guarded cleanup deletes exactly the logged count on the canary table | live (`02_verify_permissions.sql` §4.4, `99_teardown_DESTRUCTIVE.sql` §T) |

Live (`snowflake`-marked) tests run **only** against the dedicated test database named by `SNOWFLAKE_TEST_DATABASE`, are skipped without configuration locally, and are required in CI only if the owner wants CI to hold Snowflake credentials (a decision).

## 6. Work breakdown (vertical slices, tests after each)

| Slice | Content | Needs Snowflake access |
|---|---|---|
| S1 | Incident lifecycle (transitions, append-only history, reopen rule) and deployments with the M2 resolver, in PostgreSQL | no |
| S2 | Snowflake connection layer (key-pair, `qmark`, timeouts, tags, secret handling), connectivity and least-privilege checks, versioned schema scripts | **yes** |
| S3 | Export worker: bounded batches, deduplicated insert-only `MERGE`, single-exporter lease, short `LOCK_TIMEOUT`, duplicate audit, minimum interval and off-by-default switch, watermark in PostgreSQL, retries, degraded state | yes |
| S4 | Approved query functions with caps, timeouts, evidence references, consumption monitoring | yes |
| S5 | API endpoints, degraded-mode tests, documentation (ADR for the Snowflake integration and retention), threat-model update, M3 report | partly |

Effort: frozen window weeks 4 to 5 (about two weeks) from the day the configuration is provided.

## 7. Security considerations

Least-privilege roles and key-pair authentication; credentials only from environment or secret store, never logged; the private key outside the repository; query tags without sensitive data; templates are static and bound; reader role is `SELECT`-only on views; project scoping on every function; redacted data only; no LLM involvement; error messages expose stable codes only. Residual risks to record: the service keys are long-lived credentials (90-day expiry; rotation procedure needed; `ROTATE KEY PAIR` keeps the old key valid for 24 hours by default); the **network policy is deferred**, so a stolen key plus passphrase works from any IP until it is revisited (follow-up F20); costs depend on the account's configuration and on a resource monitor an administrator must create, which is **one safeguard, not a cap** and is undocumented on trials; Snowflake does not enforce the event key, so duplicate protection rests on the single exporter and the audit; the human-held migrator owns the development tables and secondary roles can widen a session's reach (hence the cleanup protocol); connector behaviour, including how the restricted role is conveyed, is unverified. Threat-model entries T23–T28.

## 8. Risks

| Risk | Mitigation |
|---|---|
| No account, or wrong edition or privileges | Prerequisites check (2.4) before any work; stop and report |
| Cost overrun | X-Small warehouse, 60 s auto-suspend, resource monitor, caps, soft budget |
| Connector 5.x release-candidate churn | Pin a verified version; contract tests around a small adapter |
| Per-query cost unobservable | Monitor at warehouse and monitor level; state the limitation |
| Snowflake outage | Degraded mode; ingestion never waits |
| Data volume unknown | Start minimal, measure, then decide clustering and retention |
| Export duplicates or drift | Insert-only `MERGE` with deduplicated batches, single exporter in PostgreSQL, short `LOCK_TIMEOUT`, duplicate audit; PostgreSQL authoritative; **MERGE privileges and concurrency to be demonstrated (M3-12, M3-13), not assumed** |
| `MERGE` needs more than `INSERT`+`SELECT` | The writer is not widened automatically; stop and report (decision O7) |
| Accidental deletion of development data during test cleanup | Dedicated cleanup role with no development privilege, secondary roles off, run-id format guard inside the `DELETE`, leak check, transaction with count check |
| Exporter exhausts the provisional allowance | Off by default, 14,400 s default / 7,200 s floor interval, soft budget from own log, monitor, owner review of metering after every session |
| Key expires unnoticed (90 days) | Calendar reminder at day 75; connectivity check warns under 14 days; degraded mode keeps the API usable |
| Notifications silently not delivered | 10 % canary trigger; the 10 % e-mail must be seen before relying on notifications |

## 9. Decisions (items 2, 3 and 10 updated 2026-11-02; items 11–19 record the owner's O1–O8 decisions)

1. **Approve this plan** and the order S1 to S5, once the configuration is available. *(Still pending: no implementation is authorized.)*
2. **Authentication:** key-pair with encrypted key files outside the repository; **two** dedicated service users, one per role, with role-restricted keys that expire after 90 days. *(Decided 2026-10-29/11-02.)*
3. **Environment:** a dedicated development database and an X-Small warehouse with its own resource monitor; budget: a **provisional 20-credit development allowance, not a guaranteed cap**. *(Decided.)*
4. **What is exported:** events of severity `ERROR` and above plus all incident versions (not every `INFO` event) — cheaper and less sensitive. Say if you want everything.
5. **Retention defaults:** 30 days of raw events in PostgreSQL after export is confirmed, 180 days in Snowflake, incident summaries and resolutions kept longer. These are proposals pending your budget and policy.
6. **Reopen rule:** a new occurrence of a `resolved` incident reopens it automatically; an `ignored` incident only counts occurrences.
7. **Similarity:** deterministic matching only (same fingerprint, else same service and error type); no embeddings.
8. **New endpoints:** `POST …/deployments`, lifecycle transition endpoint, and a history endpoint for the planned `GET /incidents/{id}/history`.
9. **CI:** keep Snowflake credentials out of CI (live tests run only locally) unless you decide otherwise.
10. **Administrator script:** I draft the setup SQL for an administrator to review and run; I never run administrator operations. Drafts: `infra/snowflake/00`–`02` and `99`; guide: `docs/setup/snowflake-account-setup.md`. *(Done as drafts; nothing executed.)*
11. **O1 account-level monitor:** deferred until the read-only preflight lists existing warehouses and monitors. *(The owner's own instruction.)*
12. **O2 follow-up F20** (network-policy review before non-synthetic data, trial upgrade, non-development hosts including CI, M3 closure, or suspected key exposure): recorded in `PLAN.md`. *(Applied as recommended; owner confirmation pending.)*
13. **O3 `TRACEMIND_TEST_CLEANER`:** with the secondary-role protocol (`USE SECONDARY ROLES NONE` at the start of every cleanup/test session). *(Applied as recommended; owner confirmation pending.)*
14. **O4 export interval:** default 14,400 s, code floor 7,200 s, off by default. *(Applied; owner confirmation pending. The 7,200 s floor is the assistant's change from the earlier 3,600 s proposal; a floor equal to the default is the alternative.)*
15. **O5 key expiry:** 90 days; cannot be altered afterwards without removing and re-adding the key; implications in setup guide §10.1. *(Applied; owner confirmation pending.)*
16. **O6 variable names:** applied to `.env.example` as listed in setup guide §13.1. *(Applied; owner confirmation pending.)*
17. **O7 `MERGE` fallback:** conditional on tests MERGE-1 and MERGE-4; never an automatic grant, never `DELETE`. *(Conditional by design; owner confirmation pending.)*
18. **O8 credit price and trial balance:** information the owner supplies after the preflight (edition, region, price per credit from Snowflake's Pricing Guide, the balance tile and its unit, trial dates). *(Open: owner input.)*
19. **O9 secondary roles on the owner's own user** (default `ALL` for users created with `CREATE USER`): rely on the `USE SECONDARY ROLES NONE` procedure, or set the user's default to `()`, or use a separate human user for the cleanup role. *(Open owner decision; the files assume the procedure.)*
20. **Assumption to confirm:** the O1–O8 decisions were communicated as "made" without being restated; they were applied as the assistant's recommendations of the 2026-11-02 report (items 12–17 above). Tell me if any differs.
