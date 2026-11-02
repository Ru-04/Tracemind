# TraceMind — Frozen Project Plan

**Status:** FROZEN (baseline set 2026-10-28)
**Authority:** This file defines the milestone sequence and per-milestone scope. It is followed strictly.
**Companion document:** [sow.md](sow.md) — product scope, principles, security rules and definition of done. Where this plan is silent, the SOW governs. Where they conflict on milestone sequence or milestone content, **this plan wins** (it supersedes SOW §15).

## Change control

- The frozen milestone text below (between the `FROZEN BEGIN` / `FROZEN END` markers) must not be edited, reordered, or reinterpreted without explicit approval from the project owner.
- Only the **Status tracker** and **Change log** sections may be updated as work progresses.
- Work outside the current milestone's scope is not started. Anything discovered mid-milestone that belongs elsewhere goes to [docs/backlog-parking-lot.md](docs/backlog-parking-lot.md), not into code.
- A milestone is closed only when every deliverable exists, its acceptance tests (see [docs/acceptance-tests.md](docs/acceptance-tests.md)) have actually been run and pass, and limitations are documented (SOW §14.5, §19.4).
- At the end of each milestone, a progress report is produced per SOW §19.3.

<!-- FROZEN BEGIN -->
## M0 — Week 1 — Foundation and design

- Create the GitHub repository and folder structure.
- Write the architecture decision record and threat model.
- Define the incident schema, event schema, API contracts, and acceptance tests.
- Create a demo FastAPI service with deliberately reproducible failures.

**Deliverable:** architecture, repository setup, test scenarios.

## M1 — Weeks 2–3 — Telemetry and ingestion

- Emit structured JSON logs from the demo app.
- Implement ingestion endpoints and validation.
- Normalize timestamps, severity, service, exception type, and trace IDs.
- Store events and add deterministic error fingerprints.
- Track ingestion health, duplicate events, and malformed payloads.

**Deliverable:** trigger a known error and see its normalized event stored.

## M2 — Weeks 3–4 — Repository intelligence

- Run the initial Graphify scan.
- Record repository identity, commit SHA, graph version, and scan status.
- Retrieve code relationships and relevant file context.
- Integrate Git history and real commit diffs.
- Test file additions, edits, deletions, retries, and graph-update recovery.

**Deliverable:** a code change updates the index and produces an auditable change record.

## M3 — Weeks 4–5 — Snowflake and incident lifecycle

- Create the telemetry and incident-knowledge schemas.
- Connect through the Python connector.
- Implement approved, read-only query functions.
- Store incident history, deployment metadata, and investigation results.
- Add limits, query timeouts, and consumption monitoring.

**Deliverable:** historical incidents can be queried programmatically with evidence references.

## M4 — Weeks 5–7 — AI investigation engine

- Implement a bounded tool registry.
- Retrieve telemetry, deployment details, code relationships, Git diffs, and verified incidents.
- Generate competing cause hypotheses and collect supporting or contradictory evidence.
- Produce a validated, structured investigation report using Groq.
- Capture developer feedback and verified outcomes separately.

**Deliverable:** an evidence-backed investigation with explicit uncertainty and suggested tests.

## M5 — Weeks 7–10 — Dashboard, integration and evaluation

- Build incident lists, detail pages, event timelines, and report views.
- Map releases to commit SHAs where possible.
- Add authentication and project-level authorization.
- Run end-to-end tests against known failure scenarios.
- Measure detection quality, evidence traceability, investigation latency, and cost.
- Prepare a deployed demo, architecture diagram, and technical documentation.

**Deliverable:** a complete, demonstrable V1 rather than isolated prototypes.
<!-- FROZEN END -->

## Status tracker (mutable)

| Milestone | Window | Status | Closed on | Notes |
|---|---|---|---|---|
| M0 | Week 1 | Closed | 2026-10-28 | Approved by project owner; remote `origin` = github.com/Ru-04/Tracemind (connected, nothing pushed yet) |
| M1 | Weeks 2–3 | Closed | 2026-10-28 | Approved by project owner after review of [docs/reports/M1-report.md](docs/reports/M1-report.md): 242 tests pass, security fixes and limitations accepted. Work remains uncommitted (see push gate below) |
| M2 | Weeks 3–4 | Closed | 2026-10-28 | Approved by project owner after review of [docs/reports/M2-report.md](docs/reports/M2-report.md) (owner review of the report, not an independent rerun). 592 tests passed at closure (350 new). Decisions and follow-ups F1–F19 recorded below. Uncommitted; no push |
| M3 | Weeks 4–5 | **Blocked** — Snowflake preparation completed (documents and unrun drafts); implementation pending; awaiting the Snowflake setup, the secure configuration and separate owner authorization | — | **Not complete.** Prerequisites and plan: [docs/plans/M3-plan.md](docs/plans/M3-plan.md); setup guide and SQL drafts: [docs/setup/snowflake-account-setup.md](docs/setup/snowflake-account-setup.md), `infra/snowflake/`. Nothing was run against Snowflake and no M3 implementation has started (checklist below) |
| M4 | Weeks 5–7 | Not started | — | |
| M5 | Weeks 7–10 | Not started | — | |

### M0 checklist

- [x] Local git repository initialised; folder structure created (no commit yet)
- [x] GitHub remote connected — https://github.com/Ru-04/Tracemind.git (exists, empty, reachable)
- [x] Architecture decision record — [ADR-0001](docs/adr/0001-modular-monolith-with-worker.md)
- [x] Threat model — [docs/threat-model.md](docs/threat-model.md)
- [x] Event schema — [docs/contracts/](docs/contracts/)
- [x] Incident schema — [docs/contracts/](docs/contracts/)
- [x] API contracts — [docs/contracts/api-v1.md](docs/contracts/api-v1.md)
- [x] Acceptance tests defined — [docs/acceptance-tests.md](docs/acceptance-tests.md)
- [x] Demo FastAPI service with reproducible failures, and tests proving them (18 tests pass; ruff and mypy clean)
- [x] M0 progress report (SOW §19.3) delivered 2026-10-28 and approved by the owner

## Change log

| Date | Change | Approved by |
|---|---|---|
| 2026-10-28 | Plan frozen from owner's M0–M5 milestone list | Project owner |
| 2026-10-28 | M0 closed; `origin` set to github.com/Ru-04/Tracemind | Project owner |
| 2026-10-28 | M1 plan approved. Clarifications: ingest key kept in env only and never logged/returned; PostgreSQL for transactional ingestion with Snowflake integration documented for later; a real adapter contract with contract tests (not only a made-up format); read-only boundary; synthetic data only; no commit/push until repo is private and identity confirmed | Project owner |
| 2026-10-28 | M1 implementation finished; report delivered; closing requires owner review | — |
| 2026-10-28 | M1 closed; M2 approved to start with the Graphify verification spike, findings and task sequence to be presented before any M2 implementation. All work stays uncommitted; push blocked until repository is private and commit identity confirmed | Project owner |
| 2026-10-28 | M2 plan approved with decisions: Graphify adopted as isolated pinned CLI; local repositories under an allowed root only; PostgreSQL job queue + worker (ADR-0002); no author names/e-mails stored, logged or returned; `verified` release mapping only for a telemetry-supplied SHA that exists in the repository (no timestamp/tag/branch inference); `POST …/scans` and `GET …/code` added; audit = structured logs + append-only records until M4. Uncommitted, no push, no M3 before the M2 report is reviewed | Project owner |
| 2026-10-28 | M2 implementation finished; evidence and acceptance report ready for review; closing requires owner review; M3 not started | — |
| 2026-10-28 | **M2 closed** after owner review of [docs/reports/M2-report.md](docs/reports/M2-report.md) (a review of the report, not an independent rerun of the suite). Decisions: (1) release mapping via optional `commit_sha` and `release` on `GET …/changes` approved; (2) abbreviated SHAs remain `unknown`, only a telemetry-supplied full 40-character SHA that exists in the registered repository may be `verified`; (3) `ls-tree` + `cat-file --batch` snapshots approved. Follow-ups F1–F19 recorded, including a recurring audit of Graphify's isolated environment. Still uncommitted; push gate unchanged | Project owner |
| 2026-10-28 | **M3 is blocked** until the owner provides the Snowflake configuration through the secure configuration process (never pasted into chat, never committed). Prerequisites and a proposed plan were prepared ([docs/plans/M3-plan.md](docs/plans/M3-plan.md)); no M3 implementation has started | Project owner |
| 2026-10-29 | Snowflake prerequisite preparation reviewed: separate writer and reader service users with separate encrypted key pairs; 20-credit provisional development allowance (not a guaranteed cap); manual migrations as a human-held migrator role; `MERGE` retained; network policy deferred; exporter off by default | Project owner |
| 2026-11-02 | O1–O8 applied (the owner stated the decisions had been made without restating them; they were applied as the assistant's recommendations — see the assumption in setup guide §0.0): account-level monitor **deferred** until the read-only preflight; **F20** recorded; `TRACEMIND_TEST_CLEANER` approved; export interval default 14,400 s with floor 7,200 s; 90-day key expiry; `.env.example` variable set applied; `MERGE` fallback conditional on tests MERGE-1/MERGE-4; credit price information requested from the owner. Documentation and example configuration only (plus the four SQL drafts): no application code changed, no Snowflake connection, no SQL executed, nothing committed or pushed. A new open decision **O9** (secondary roles on the owner's user) and the assistant's own 7,200 s export-interval floor are recorded for confirmation. **M3 is not complete** | O1 (defer): project owner. O2–O7: **applied by the assistant as its own recommendations; owner confirmation pending** |
| 2026-11-02 | **Preparation approved** by the owner: the 11-file preparation changes, the 91/91 static checks, the ruff/mypy results, the independent-review findings with their resolutions, and the status wording "Snowflake preparation completed; M3 implementation pending". **M3 is not complete; M4 is not authorized** and may start only after M3 is demonstrably complete and independently reviewed. The owner set ten required steps (resolve O1–O9, run the database tests, preflight authorization, preflight report, separate authorization for live changes and implementation, implementation, acceptance checks M3-1 to M3-14, evidence and detection/grouping verification, independent review, one consolidated closure report). The assistant read the approval of the reported changes as covering O2–O7 and the 7,200 s floor as applied; that reading is recorded, not asserted by the owner. Same day, the 592-test suite was run with PostgreSQL 16 up and `TRACEMIND_REQUIRE_DB=1`: **592 passed, 0 skipped, 0 failed** (no application code changed). No commit, no push | Project owner |

## Open follow-ups carried forward from M2

Recorded at M2 closure (2026-10-28). None blocks M3; each has an owner milestone. Nothing here is claimed as verified.

| # | Follow-up | Type | Target |
|---|---|---|---|
| F1 | **Audit Graphify's isolated dependency environment for known vulnerabilities, on a schedule.** Hash pinning proves *which* bytes are installed, not that they are free of vulnerabilities. A single point-in-time `pip-audit` of `infra/graphify/requirements.lock` on 2026-10-28 reported no known vulnerabilities; that is not an ongoing guarantee. Add the audit to CI, re-run it before each release and whenever the lock changes, and record results and exceptions | Security | CI at first push; before M5 release; then recurring |
| F2 | Review Graphify's own security posture and governance (maintainers, release cadence, advisory history); it is a young 0.9.x project that parses hostile source | Security | Before M5 |
| F3 | Run the CI workflow for the first time and fix what it reveals (it has never executed; action versions and the Graphify and Postgres steps are unverified) | Quality | At first push |
| F4 | **Platform:** verified on Windows 11 only. Verify Linux (and macOS if wanted), including the POSIX process-group kill branch, path handling and the install script | Verification | Before any non-Windows deployment claim (M5) |
| F5 | **Languages:** only Python and JavaScript are verified; everything else is reported `not_assessed`. Add languages one at a time with evidence | Verification | Per language, as needed |
| F6 | **Scale:** no large-repository evidence (fixtures of 4 to 47 files). Measure time, memory and graph size on real repositories and validate or change the caps (20,000 files, 256 MiB total, 2 MiB per file, graph size limits) | Verification | M5 evaluation |
| F7 | **Memory limit:** the Graphify process has no memory cap (no portable mechanism on Windows). Evaluate a Windows Job Object limit, cgroups or a container, and document the result | Hardening | M5 |
| F8 | **Hostile-source parsing:** Graphify's tree-sitter parsers handle untrusted code; evaluate stronger sandboxing (low-privilege user, Job Object, container) beyond today's time limit, scrubbed environment and separate directory | Hardening | M5 |
| F9 | Verify Graphify makes no network calls by **traffic capture** (today: dead proxy and scrubbed environment only) | Verification | Before M5 |
| F10 | Protect the data directory (permissions, optionally encryption at rest); it is trusted storage for snapshots and graphs | Hardening | M5 |
| F11 | Retention and garbage collection for content-addressed graph files and old snapshots | Operations | M3 (retention design) |
| F12 | Multi-host deployments: lease expiry assumes one host's clock; polling latency (2 s); consider `LISTEN/NOTIFY` | Operations | If deployed across hosts |
| F13 | Unsupported repository features: SHA-256 object format, submodules, symlinks (skipped and counted), Git LFS pointers (treated as plain files), non-UTF-8 and case-colliding paths (skipped) | Limitation | Document in M5 docs; extend only on demand |
| F14 | Method calls through instances can resolve to the class; some edges are `INFERRED`. M4 must keep treating graph relationships as hints, never proof | Limitation | M4 |
| F15 | The Python `ast` fallback indexer (ADR-0003) is documented, not implemented | Contingency | Only if Graphify fails a future check |
| F16 | Per-file diff text exists in the Git adapter but no endpoint exposes it; exposing it requires redaction and an explicit decision | Decision | M4 or M5 |
| F17 | Replace static per-project keys with real authentication and authorization; rate limiter is per-process | Security | M5 |
| F18 | Starlette deprecates `httpx` in `TestClient` (use `httpx2`) | Maintenance | Next dependency pass |
| F19 | Remote repository cloning is out of scope; local paths only | Scope | Later, needs its own design and threat model |

## Follow-ups recorded for M3 (2026-11-02)

None is claimed as verified. Each names what would close it.

| # | Follow-up | Type | Target |
|---|---|---|---|
| F20 | **Network-policy review for the Snowflake service users** (deferred for initial local development). Re-open **before the first of:** (a) any non-synthetic data enters Snowflake; (b) the account is upgraded from trial; (c) TraceMind runs from any machine other than the development machine, including CI; (d) M3 closes; (e) a key or passphrase may have been exposed. First test on a throwaway service user whether a user-level policy restricts key-pair logins (not stated by the documentation), keeping an administrator login path the policy does not cover. Residual risk meanwhile: a stolen key plus passphrase works from any IP | Security | Before the first trigger above |
| F21 | **Demonstrate the `MERGE` design before relying on the exporter:** MERGE-1 (privileges `INSERT`+`SELECT` suffice), MERGE-2/3 (retry and duplicate-source behaviour), MERGE-4a/4b (concurrent run waits, times out, adds 0 rows), duplicate audit. A failure stops M3 and goes to the owner; no `UPDATE`/`DELETE` grant is added automatically | Verification | M3 S2/S3, live, `TRACEMIND_TEST` |
| F22 | **Key expiry and rotation:** 90-day expiry cannot be altered without removing and re-adding the key; calendar reminder at day 75; connectivity check warns under 14 days; write the rotation procedure (`ROTATE KEY PAIR` keeps the old key valid 24 h by default) | Operations | M3 S2; before day 75 of the first keys |
| F23 | **Cleanup role and secondary roles:** verify future grants reach migrator-created tables; verify the cleaner has no development access with secondary roles off and record the demonstration with them on; verify whether `DELETE … WHERE` also needs `SELECT`; record the user's default secondary-role setting (the `CREATE USER` default is `ALL`) and decide O9; run every runtime-role test with secondary roles off or as the real service users | Verification | M3 S2 live checks (M3-14); O9 before any cleanup |

### M3 preparation checklist (2026-11-02)

- [x] Snowflake setup guide (revision 4) and SQL drafts `infra/snowflake/00`, `01`, `02`, `99` — **drafts, never executed**
- [x] Documentation review against docs.snowflake.com (citations in the setup guide, section 4)
- [x] M3 plan, `.env.example`, dependency verification, threat model and ADR-0007 note updated and checked for internal consistency by 91 offline static checks (alignment with the owner's *confirmation* of O2–O7 is pending)
- [x] Offline checks and independent review of the preparation (2026-11-02): 91 static consistency checks pass; ruff and mypy clean; 423 of 592 existing tests passed in that pass (the 169 database-backed integration tests were not run then because PostgreSQL was not running; **superseded: all 592 passed on 2026-11-02 with the database up, 0 skipped**); two independent reviewers found 1 blocker, 6 major and about 13 minor/nit issues, all addressed in the documents and SQL drafts except the open owner decision O9. The fixes were verified by the static checks and by re-reading, **not re-reviewed independently**. Nothing was run against Snowflake
- [x] Preparation approved by the owner (2026-11-02); PostgreSQL integration tests run and passing (592/592, 0 skipped)
- [ ] Owner approval to run the read-only preflight; preflight results; O1 decided from them
- [ ] Owner supplies credit price, edition, region, trial balance and the unit its tile shows, trial dates (O8); decides O9 (secondary roles); explicitly confirms O2–O7 and the 7,200 s floor (currently only covered by the approval of the reported changes)
- [ ] Owner runs setup parts A–D with the gates; verification ledger (setup guide section 11) filled in as PASS / FAIL / NOT RUN
- [ ] Owner authorizes M3 implementation; slices S1–S5 (S1 needs no Snowflake)
- [ ] **Unverified, gating:** `MERGE` privileges and concurrency; future grants on migrator tables; connector role conveyance; network policy on key-pair logins; monitors and `FREQUENCY = NEVER` on a trial
- [ ] **Blocked:** S2–S5, live acceptance checks M3-1 to M3-14, M3 closure, any push (repository must be private, identity confirmed, staged diff and secret scan reviewed)

### Secrets handling (standing)

The owner never pastes passwords, tokens, private keys or other secrets into chat, and none are committed. Credentials are provided through the secure configuration process (git-ignored environment file or an OS/cloud secret store, key files outside the repository). Documentation lists only variable *names*.

### Push gate (standing)

Nothing is committed or pushed. Before any push: repository visibility set to private by the owner, commit identity confirmed, staged diff re-reviewed, secrets scan re-run.

### M2 slice status

- [x] S1 Git adapter, validators, snapshot export/apply/verify, hostile-input corpus (161 new tests)
- [x] S2 migration 0002, repository registry, allowed roots, project-scoped register/list API (23 tests)
- [x] S3 Graphify runner (hash-pinned isolated install, scrubbed env, timeout + tree kill), CodeGraph loader/validator, bounded queries (60 tests)
- [x] S4 scan service + PostgreSQL job queue + worker: full/incremental scans, change records, retries, recovery, idempotence, concurrency ([ADR-0002](docs/adr/0002-postgresql-job-queue-and-worker.md)) (53 tests)
- [x] S5 scans/status/changes/code endpoints, release→commit resolver, end-to-end demo (automated and live), docs, M2 report (33 tests + resolver 22)

### M1 slice status

- [x] S1 pure logic: models, normalization, redaction, fingerprint
- [x] S2 Postgres schema, store, pipeline, dedup, incident grouping
- [x] S3 API: auth, limits, errors, endpoints, health/ready, adapter contract
- [x] S4 demo JSON logs, shipper, synthetic events, end-to-end
- [x] S5 CI, docs, security review, M1 report
