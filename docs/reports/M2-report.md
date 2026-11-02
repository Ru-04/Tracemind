# M2 progress report — Repository intelligence

Date: 2026-10-28 · Status: **CLOSED by owner decision on 2026-10-28.** The owner reviewed this report and accepted its test and live-demo evidence for the purpose of the review; that was a review of the report, not an independent rerun of the test suite. Nothing has been committed or pushed. M3 has not been started and is blocked pending Snowflake configuration.

Deliverable (frozen plan): *a code change updates the index and produces an auditable change record.* Demonstrated by an automated end-to-end test and by hand against live API and worker processes (below).

## What was implemented

| Slice | Content |
|---|---|
| S1 | Read-only Git adapter; validators for revisions, paths and repository locations; snapshot export, incremental apply and verification against Git blob hashes |
| S2 | Migration `0002` (repositories, scans = the job queue, append-only change records and scan events, commit cache); registration under allowed roots; project-scoped register/list API |
| S3 | Hash-pinned isolated Graphify install; Graphify runner (install verification, scrubbed environment, timeout with process-tree kill); `CodeGraph` loader/validator with coverage and bounded queries |
| S4 | Scan service and PostgreSQL job queue with a separate worker: full and incremental scans, change records, retries with backoff, recovery, fenced completion, idempotence ([ADR-0002](../adr/0002-postgresql-job-queue-and-worker.md)) |
| S5 | `POST …/scans`, `GET …/status`, `GET …/changes`, `GET …/code`; release-to-commit resolver; documentation and this report |

## Files and modules

- `backend/tracemind/repos/`: `errors.py`, `validate.py`, `git_adapter.py`, `snapshot.py`, `graphify_runner.py`, `code_graph.py`, `store.py`, `service.py`, `scan_store.py`, `scan_service.py`, `resolver.py`
- `backend/tracemind/worker.py`, `backend/tracemind/api/repos.py` (and wiring in `api/app.py`), `config.py` additions
- `backend/tracemind/storage/migrations/versions/0002_repository_intelligence.py`, `storage/tables.py`
- `tools/install_graphify.py`, `infra/graphify/requirements.{in,lock}`
- Docs: [ADR-0002](../adr/0002-postgresql-job-queue-and-worker.md), [ADR-0003](../adr/0003-graphify-adoption.md) (accepted, with addendum), [M2 plan](../plans/M2-plan.md), `docs/contracts/api-v1.md`, `docs/acceptance-tests.md`, `docs/threat-model.md`, `docs/dependency-verification.md`, `docs/backlog-parking-lot.md`

## Architecture decisions

- Graphify runs as an isolated, hash-pinned CLI (`graphifyy==0.9.97`, whole dependency tree installed with `--require-hashes`); before use the runner verifies the version and that all 244 installed files match their recorded hashes. Only `extract . --code-only --no-gitignore` and `update .` are ever invoked, inside a TraceMind-owned snapshot, with a scrubbed environment and deny-all proxies.
- Snapshots are exact commits read from Git objects (`ls-tree` + `cat-file --batch`), never the working tree. Provenance (commit, tool version, schema version, graph SHA-256, coverage) is TraceMind's own; Graphify's `built_at_commit` and `.graphify_root` are ignored.
- The queue is the `repository_scans` table; claims use `SKIP LOCKED` plus a per-repository advisory lock; leases and heartbeats; **fenced completion**; one transaction publishes scan, change record and repository pointer.
- Graphs are content-addressed and hash-checked on load. The published pointer, snapshot and graph change only after Graphify succeeded, the graph validated, and the database committed.
- Only code nodes are modelled; relationships carry confidence, file and line; coverage and Python syntax errors are reported; "not in the graph" never means "irrelevant".
- Release-to-commit: `verified` only for a telemetry-supplied full 40-character SHA that exists in the repository; everything else `unknown` with a reason; no `estimated`.
- No author name or e-mail is collected, stored, logged or returned. No endpoint returns a server path or diff text.

## Tests actually run (2026-10-28, Windows 11, Python 3.12.10, Git 2.55.0, PostgreSQL 16 in Docker, Graphify 0.9.97)

| Check | Result |
|---|---|
| `pytest` (full) | **592 passed**, 0 skipped, 1 warning (Starlette deprecation of `httpx` in `TestClient`); 5 min 47 s. 242 are the M0/M1 tests (unchanged and passing); **350 are new in M2**, of which 92 are marked `graphify` and run the real CLI |
| `ruff check` / `ruff format --check` | clean / 90 files formatted |
| `mypy` (strict) | no issues in 74 source files |
| `pip-audit --skip-editable` (TraceMind's environment) | no known vulnerabilities. **Graphify's own environment was not audited** (hash-pinned, not scanned) |
| Acceptance M2-1 … M2-10 | all automated and passing; mapping in [acceptance-tests.md](../acceptance-tests.md) |

New test files (count): `tests/repos/` validation 102, code graph 35, snapshot 32, git adapter 27, graphify runner 25, resolver 22; `tests/integration/` repository registration 23, scan lifecycle 18, scan recovery 28, scan concurrency 5, repository endpoints 33.

### Evidence for the specific requirements you listed

| Requirement | Evidence |
|---|---|
| Hostile paths and revisions | 102 validation cases; a spy proves git is never invoked for rejected input (adapter, resolver, API); symlink **or junction** escapes, `.git` links, the data directory and prefix-sibling directories refused; API hostile `file=`/`symbol=` inputs rejected without opening any file |
| Project isolation | Every repository route returns 404 for another project's key or URL; scans, history, change records and graphs unreachable across projects |
| Corrupted or incomplete Graphify output | 16 corruption cases at the loader (truncated, wrong schema, dangling edge = partial write, duplicates, outside snapshot, oversize…); six runner-level failure modes leave pointer, `current/`, `graphs/` and records byte-identical |
| Timeouts | Real 10 ms timeout kills the CLI; fake CLI proves the **grandchild process is killed** too |
| Failed scans, retries | Exactly `max_attempts` attempts, backoff doubling and honoured by the claimer, configuration failures fail fast |
| Idempotence | Re-enqueue returns the existing scan (10 concurrent enqueues → one row); one change record per commit pair; independent scans of the same commit give **byte-identical graph hashes** |
| Concurrent jobs | 8 simultaneous claimers → one winner; 4 workers over 6 scans in 3 repositories → each scan claimed once, never two scans of one repository at once, different repositories in parallel |
| Recovery | Dead worker reclaimed after lease expiry (`lease_expired` recorded); exhausted attempts → `worker_lost`; a late worker's completion and failure are rejected; a lost lease mid-scan abandons without publishing; corrupt incremental baseline → automatic full rebuild whose hash equals a clean build; tampered snapshot rebuilt from Git; missing `current/` or graph file → full scan |
| Last good graph preserved | Asserted after every failure scenario: repository pointer, graph hash, snapshot files, `graphs/` files, `current.sha`, change-record ids all unchanged, and the old graph still answers queries |
| Cleanup | `work/` and `trash/` empty after success, failure, abandonment and crash-leftovers |
| Worker cannot modify the registered repository | Fingerprint of every file of the monitored repository, including `.git`, is identical before and after registration, scans, updates and queries; no `graphify-out` appears in it; a hostile `diff.external` config is proven not to run (with a control showing it would run without the adapter's flags) |
| Unknown release mappings | 15-case table at the resolver and the API: release names, same-named tags, branch names, abbreviations, `HEAD`, unknown SHAs, missing repository, timestamps → `unknown` with a reason; only an exact existing 40-character SHA is `verified` |
| Audit history | `scan_events` and `change_records` reject UPDATE and DELETE at the database; every transition is recorded with attempt and worker |
| Author data | Distinctive author name and e-mail committed in every test repository; asserted absent from all API responses, change records, scan history, commit cache and logs |

### Live run (real processes over HTTP, not test clients)

API (`uvicorn … --factory`) and worker (`python -m tracemind.worker`) started as separate processes against the dev database; real Git repository; real Graphify. Results: hostile registrations → three 422s with reasons; registration 201; first scan queued (202), re-queue 200 idempotent, worker indexed it (`kind full`, 8 nodes, 9 edges, 0 inferred, coverage complete); a real commit that **added** `app/audit.py`, **edited** `app/pricing.py` and **deleted** `app/util.js` was scanned incrementally; `GET …/changes` listed one record with base and target SHA, `[('app/audit.py','A',5,0), ('app/pricing.py','M',1,1), ('app/util.js','D',0,1)]`, the real commit subject and the graph delta (`+app/audit.py:audit()`, `-app/util.js:fmt()`); `GET …/code?symbol=regression` showed `audit()` as an `EXTRACTED` caller with its line; hostile `file=` queries → 422; release-only and abbreviated mappings → `unknown`; the full SHA → `verified` with one record; no key → 401, another project's URL → 404; no author name, e-mail, local path or `graphify-out` in any response.

## Integrations verified

- Git 2.55.0 (Windows): all adapter operations against real repositories, including merges, renames, binary files, empty trees, symlink and submodule index entries.
- Graphify 0.9.97: install (hash-verified), extraction, incremental update, corruption refusal, determinism, behaviours recorded in [dependency-verification.md](../dependency-verification.md).
- PostgreSQL 16: migrations up/down, partial unique indexes, `SKIP LOCKED`, advisory locks, append-only triggers, concurrency.

## Security review (self-review, M2 scope) — issues found and fixed

1. `diff_files` resolved the target (a git call) **before** validating `base`; the "git never invoked for rejected input" test caught it; all inputs are now validated first.
2. A missing or broken repository was reported as "commit not in repository" (a false claim); only git's "no such revision" exit status now means that, anything else is `repository_unavailable`.
3. `git archive` honours `.gitattributes export-ignore`, which would make snapshots differ from the commit; replaced by `ls-tree` + `cat-file --batch`.
4. Graphify path handling differed by working directory and `.gitignore` handling differed between commands; fixed by fixed cwd, `--no-gitignore`, path mapping that rejects unmapped nodes, and coverage comparison.
5. `update` adds document nodes that `extract` omits (different graphs for one commit); loader keeps code nodes only.
6. Code queries validated user input *after* loading the graph; validation now comes first, and a test proves rejected input opens no file.

Residual risks: Graphify parses hostile source (parser bugs are contained only by a time-limited, scrubbed subprocess); no memory cap on the Graphify process; static project keys until M5; the data directory is trusted local storage; Graphify's own environment is not vulnerability-scanned.

## Deviations from the approved plan

1. Snapshots use `ls-tree` + `cat-file --batch`, not `git archive` + a tar reader (reason above; also removes tar parsing).
2. **Release mapping is exposed through optional `commit_sha` and `release` query parameters of the approved `GET …/changes`** (returning `release_mapping`), rather than a new endpoint. Needs your confirmation.
3. Change records also store `commits` and `graph_sha256`; a `scan_events` append-only table records every scan transition (your "append-only scan/change records").
4. Coverage includes Python syntax-error detection with `ast.parse` (parse only, never execution), because Graphify indexes broken files silently.
5. Abbreviated SHAs from telemetry map to `unknown` (not exact). An application that only emits short SHAs will never be `verified`; say if this is too strict.
6. A failing incremental `update` is followed by one automatic full rebuild in the same attempt, so a failing attempt makes two Graphify calls.
7. Repository `indexed_scan_id` is returned; the repository path never is.

## Known limitations (and what is not claimed)

- **Verified on Windows 11 only.** Linux and macOS are untested; the POSIX branches (process-group kill) were not executed. The CI workflow has not run.
- **Languages: Python and JavaScript only** (`.py`, `.js`, `.mjs`, `.cjs`). Other languages are counted as `not_assessed`; no support is claimed.
- **No large-repository evidence.** Fixtures are 4–47 files; per-scan time was about one second there. Caps (20,000 files, 256 MiB total, 2 MiB per file) are unvalidated.
- Method calls through instances can resolve to the class; some edges are `INFERRED`; only the `EXTRACTED`/`INFERRED` labels Graphify provides are available.
- Only SHA-1 repositories; submodules and symlinks are skipped (and counted); non-UTF-8 and case-colliding paths are skipped; Git LFS pointers are treated as ordinary small files.
- Local repositories only; no remote cloning; no per-file diff endpoint.
- The worker polls (2 s); the lease assumes one host's clock.
- Graph files are never garbage-collected yet.
- Graphify network behaviour was checked with a dead proxy and by environment scrubbing, **not** by traffic capture.


## Owner review and decisions (2026-10-28)

| # | Decision |
|---|---|
| 1 | Release mapping through optional `commit_sha` and `release` query parameters on `GET …/changes` is **approved**; response semantics are documented in [api-v1.md](../contracts/api-v1.md) |
| 2 | Abbreviated SHAs remain `unknown`. Only a telemetry-supplied, full 40-character SHA that exists in the registered repository can be `verified` |
| 3 | The `ls-tree` + `cat-file --batch` snapshot implementation is **approved** (reasons: `git archive` honours `.gitattributes` `export-ignore`, which would make a snapshot differ from the commit; verified by the snapshot tests and per-file blob-hash comparison against Git) |
| 4 | M2 is closed, subject to the limitations and follow-ups below being recorded; they are also tracked in [PLAN.md](../../PLAN.md) |
| 5 | M3 does not start until the owner supplies the Snowflake configuration through the secure configuration process. Secrets are never pasted into chat or committed |

Supplementary evidence gathered at closure (informational, **not** a vulnerability clearance): a point-in-time `pip-audit` of the hash-locked Graphify environment (`infra/graphify/requirements.lock`) on 2026-10-28 reported no known vulnerabilities. Hash pinning identifies the installed bytes; it does not establish vulnerability status, so recurring auditing remains follow-up F1.

## Open limitations and security follow-ups (explicit)

None blocks M3. Platform, language, scale, memory-limit and operational limits stated in this report stand: nothing beyond Windows 11, Python and JavaScript, small repositories and a single host is claimed.

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

## Scope or estimate changes

None to the frozen plan. M2 fits the planned window.

## Proposed next: M3 — Snowflake and incident lifecycle (not started)

Per the frozen plan: Snowflake schemas, connector, approved read-only query functions, incident history, deployment metadata, limits/timeouts/consumption monitoring; incident lifecycle states (open, investigating, resolved, ignored). It needs from you before work starts: approval of this M2 report, the answers to deviations 2 and 5, Snowflake account/role/warehouse details (none are in the repository), and — before any push — the repository set to private with the commit identity confirmed.
