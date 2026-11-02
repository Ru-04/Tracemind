# M2 plan — Repository intelligence

**Status:** PROPOSED — awaiting owner approval. No M2 implementation code has been written. The only M2 work so far is the Graphify verification spike ([ADR-0003](../adr/0003-graphify-adoption.md), [verification record](../dependency-verification.md)), done in a throwaway environment outside the repository.
**Frozen scope (PLAN.md):** run the initial Graphify scan · record repository identity, commit SHA, graph version and scan status · retrieve code relationships and relevant file context · integrate Git history and real commit diffs · test file additions, edits, deletions, retries and graph-update recovery.
**Deliverable:** a code change updates the index and produces an auditable change record.
**Owner requirements added:** real Git commits and diffs; auditable changes; tested indexing updates and recovery; hostile revision/path inputs rejected; unknown release-to-commit mappings stay explicitly unknown; everything uncommitted and unpushed.

## 1. Verification findings (summary)

Full detail in the ADR. In one paragraph: Graphify 0.9.97 runs **headless and without an LLM** (`extract --code-only`, then `update`), writes a NetworkX-style `graph.json` with per-edge `confidence` and `file:line`, updates incrementally for add/edit/delete/rename, refuses to overwrite a corrupt graph, and is deterministic. Python and JavaScript were verified; other languages were not. Cautions that shape the design: it writes `graphify-out/` **into the scanned directory** (so we scan snapshots, never the monitored repo); `built_at_commit` is unreliable (so provenance is ours); syntax-error files are indexed silently (so we track coverage ourselves); some edges are `INFERRED`; method calls through instances can resolve to the class. **Recommendation: adopt as an isolated, hash-pinned CLI.**

## 2. Architecture

```
registered local repo (read-only)         TraceMind-owned data dir
 monitored/.git ──► GitAdapter ──────────► snapshots/<repo_id>/tree   (git archive of an exact SHA, then
   (never written)   fixed argv, no shell                               changed files only, symlinks rejected)
                     validated rev/paths                                      │
                                                                              ▼
                                        GraphifyRunner (subprocess, pinned venv, timeout, scrubbed env)
                                                                              │ graph.json
                                                                              ▼
 Postgres: repositories · repository_scans · change_records ◄── ScanService ──► CodeGraph (validated, queryable)
                                                  ▲
                                           worker process (claims queued scans, bounded retries)
```

| Module (`backend/tracemind/repos/`) | Responsibility |
|---|---|
| `git_adapter.py` | The only code that runs `git`. Read-only commands: `rev-parse`, `log`, `show`/`diff --name-status -z`, `diff` per file, `cat-file`, `archive`. Validators for revisions and paths. Output and time caps |
| `snapshot.py` | Exports a commit with `git archive` into a managed directory using a safe tar reader (rejects symlinks, absolute paths, `..`, devices, oversize entries); applies a diff (add/modify/delete) to move an existing snapshot to a new SHA |
| `graphify_runner.py` | Runs `extract --code-only` / `update` via subprocess; validates the binary version and hash pin; timeout; environment scrubbed of API keys and credentials; captures bounded output |
| `code_graph.py` | Loads and validates `graph.json` (schema_version 1, shapes), exposes `file_context(path)`, `callers(symbol)`, `callees(symbol)`, `neighbors`, each result carrying `confidence`, `source_file`, line and the graph version. Internal format, so a fallback indexer can replace Graphify (ADR-0003) |
| `scan_service.py` | Scan lifecycle, change records, retries, recovery, audit events |
| `resolver.py` | Release/commit association with explicit confidence (see 3.6) |
| `worker/` | Separate process: claims queued scans with `SELECT … FOR UPDATE SKIP LOCKED` |
| `api/repos.py` | Routes (see 3.5) |

New configuration: `TRACEMIND_REPO_ROOTS` (allowed parent directories for registered repositories), `TRACEMIND_DATA_DIR`, `TRACEMIND_GRAPHIFY_BIN` (path to the pinned CLI), scan timeout and size limits. No new Python runtime dependency in TraceMind itself: `git` is invoked as a subprocess; Graphify lives in its own environment.

## 3. Design details

### 3.1 Git adapter safety (M2-6)

- No shell; argv list only; every call starts with `git --no-pager` plus `-c` overrides (`core.fsmonitor=`, `core.hooksPath=` empty, `protocol.allow=never`, `safe.directory` only for the registered path), environment reduced to `PATH` and a fixed set, `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, no pager, locale pinned.
- **Revisions** accepted only as 7–40 hex characters, or a ref name matching a strict allowlist pattern that is then resolved with `git rev-parse --verify --end-of-options <value>^{commit}`. Anything starting with `-`, containing whitespace, `..`, `@{`, `:`, `^`/`~` expressions, control characters, or longer than 255 characters is rejected before git is invoked. Resolution result must be a 40-hex commit id that exists.
- **Paths** must be relative, normalized, free of `..`, drive letters, NUL, leading `-`, and are always passed after `--`. A path must exist in the commit's tree (`git ls-tree`) before its diff or content is requested.
- **Repository path** must resolve (after symlink resolution) inside an allowed root and contain a `.git` directory or file; no registration of the TraceMind data directory itself.
- Output is bounded (bytes and lines); diffs of binary or oversized files are summarized, not returned; commit messages and diffs are untrusted data (stored for evidence, never executed or interpreted as instructions).
- Tests use real temporary Git repositories, plus a hostile-input corpus (option injection `--upload-pack=…`, `HEAD~1;rm`, `..\..\`, absolute paths, NUL, very long values, refs that look like flags).

### 3.2 Data model (Alembic migration `0002`)

| Table | Purpose / key columns |
|---|---|
| `repositories` | `repository_id`, `project_id`, `display_name`, `path` (canonical), `default_branch`, `identity` (SHA of the root commit(s) — survives moves), `created_at`; unique `(project_id, path)` |
| `repository_scans` | `scan_id`, `repository_id`, `status` (`queued`/`running`/`succeeded`/`failed`), `kind` (`full`/`incremental`), `target_sha`, `base_sha` (null for full), `graph_tool`, `graph_tool_version`, `graph_schema_version`, `graph_sha256`, `node_count`, `edge_count`, `files_seen`, `files_in_graph`, `attempt`, `error_code`, `queued_at`/`started_at`/`finished_at` |
| `change_records` | **Append-only.** `record_id`, `repository_id`, `scan_id`, `base_sha`, `target_sha`, `files` (JSON: path, status A/M/D/R, old path, additions, deletions, binary flag), `graph_delta` (nodes/edges added/removed counts), `created_at`, `content_hash`. No UPDATE/DELETE path in code; a database trigger rejects updates and deletes |
| `repository_commits` | Cached commit metadata actually retrieved: `sha`, `parent_shas`, `authored_at`, `committed_at`, `subject` (≤200 chars, redacted). **No author name or email is stored** (not needed for M2; avoids personal data — see decisions) |
| audit | Reuses the structured logger; a minimal `audit_events` table is introduced only if the owner wants DB-level audit in M2 (see decisions) |

The graph itself is **not** copied into Postgres: it is derived data, stored as a content-addressed file (`graph.json` named by its SHA-256) in the data directory and loaded on demand. Postgres records only its identity and counts. This avoids a second source of truth for derived data.

### 3.3 Scan lifecycle, retries, recovery (M2-2, M2-4, M2-5)

1. **Register** a repository (validates path/allowed root, resolves identity). Registration does not scan.
2. **Trigger scan** (`POST …/scans`, returns `202` + `scan_id`): resolves the target revision (default: default branch tip) to a SHA; queues a `full` scan if none succeeded before, else `incremental` from the last succeeded SHA.
3. **Worker** claims the job, then: full → export snapshot of `target_sha`, `extract --code-only`; incremental → `git diff --name-status -z base target`, apply to the snapshot, `update`.
4. **Validate** `graph.json`; compute coverage (files in the snapshot vs files in the graph); compute `graph_delta` against the previous graph; write the graph file; in **one transaction** insert the change record, mark the scan `succeeded`, advance the repository's "indexed SHA".
5. **Failure** (timeout, non-zero exit, invalid graph, git error): the previous graph and indexed SHA are untouched; the scan is `failed` with a stable `error_code`; retried up to a bounded count with backoff (default 3 attempts); after the limit it stays `failed` and is visible in status.
6. **Recovery:** if `update` reports a corrupt or unreadable graph, the worker deletes that snapshot's `graphify-out` and performs a full rebuild of the same `target_sha`; if the snapshot itself is inconsistent (verified by comparing its file hashes to `git ls-tree`), it is re-exported from Git.
7. **Idempotence:** re-running the same `(repository, target_sha)` scan produces an identical `graph_sha256` (verified determinism) and does not create a second change record.

### 3.4 Change record (the M2 deliverable)

A change record states, from real Git data: base and target SHA, the list of changed files with A/M/D/R status and line counts, and the effect on the graph (nodes/edges added or removed, with the symbol names). It references the scan that produced it. It never states causation, only what changed. Every file or symbol shown can be resolved back to a commit and path (M2 acceptance: "every displayed change references a real revision or file").

### 3.5 API (project-scoped by the M1 key; replaced by real authorization in M5)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/projects/{project_id}/repositories` | Register (path under an allowed root) |
| GET | `/api/v1/projects/{project_id}/repositories` | List |
| GET | `/api/v1/projects/{project_id}/repositories/{repository_id}/status` | Identity, indexed SHA, graph tool/version/hash, last scan status and error code, coverage |
| GET | `/api/v1/projects/{project_id}/repositories/{repository_id}/changes` | Change records, paginated; optional `from`/`to` revisions (validated) |
| POST *(addition)* | `…/repositories/{repository_id}/scans` | Queue a scan (`202`) |
| GET *(addition)* | `…/repositories/{repository_id}/code?file=<path>` | File context and relationships for a file or `symbol=` (callers/callees), each with confidence, file:line and graph version |

The two additions are not in the original M0 API list; they are needed to trigger scans and to satisfy "retrieve code relationships and relevant file context". A project key can only reach its own repositories; the path in `{project_id}` must equal the key's project or the request is 404.

### 3.5a Release/commit resolver (M2-7)

`resolve(project, repository, release=None, commit_sha=None) → {commit_sha, confidence, source, reason}`:

- `verified` **only** when a commit SHA supplied by the application's own telemetry or deployment metadata resolves to a real commit in the registered repository (`source = "telemetry.commit_sha"`). Existence in the repo is checked, correctness of deployment is the source's claim and is recorded as such.
- `unknown` in every other case, with a reason (`no_commit_in_telemetry`, `commit_not_in_repository`, `no_repository`). **A release string is never turned into a SHA by guessing, and no timestamp-based estimate is produced in M2.**
- Tag matching (release string equal to a tag) is *not* implemented in M2 unless the owner asks; if added it would be `estimated`, never `verified`.

### 3.6 Security

Read-only against monitored repositories (a test snapshot-hashes the monitored repo's `.git` before and after a full scan/update cycle and asserts it is unchanged, and `git status` stays clean); no model-authored git/shell input exists; Graphify is run with a scrubbed environment and timeouts; snapshot extraction rejects traversal and symlinks; repository paths confined to allowed roots; per-project scoping; secrets that appear in source files are not copied into Postgres (only symbol names, paths and line numbers from the graph, and redacted commit subjects); the pinned wheel hash is checked before first use; scan size/time caps protect the worker. Threat-model entries T7, T8, T10 move from "planned" to "implemented" at close.

## 4. Acceptance checks

M2-1 … M2-7 are the checks defined in M0 ([acceptance-tests.md](../acceptance-tests.md)), made concrete. M2-8 to M2-10 are additions needed by the approved scope.

| ID | Check | How |
|---|---|---|
| **M2-1** | Graphify verification result and decision recorded (ADR-0003 accepted); the adapter refuses a binary whose version/hash differs from the pin; a documented fallback path exists | ADR review; unit test of the pin check |
| **M2-2** | Initial scan of a fixture repository stores repository identity, commit SHA, Graphify version, `graph.json` schema version, graph SHA-256, node/edge counts, coverage and status `succeeded`; re-scanning the same SHA yields the same graph hash and no new change record | pytest + real git + real Graphify (marked `graphify`, skipped if the pinned CLI is absent; required in CI) |
| **M2-3** | Commits and diffs come from real Git: log, per-commit changed files, per-file diff; every returned change cites an existing SHA and path; missing revision/path → clear `not_found`/`invalid_revision` errors, nothing invented | pytest with temporary repos (including merge, rename, binary, empty-tree cases) |
| **M2-4** | File **addition**, **edit** and **deletion** (and rename) each update the index incrementally and create exactly one change record whose files and graph delta match the commit | pytest e2e on a fixture repo |
| **M2-5** | Failed scans retry with a bounded count then stay `failed` without altering the previous graph; corrupt graph and killed-mid-run scenarios recover to a consistent state; a failed update never leaves a half-written graph visible | pytest with fault injection (fake runner failures, truncated `graph.json`, timeout) and one real-Graphify corruption test |
| **M2-6** | Hostile revisions and paths are rejected before any git call: option injection, `..`, absolute/drive paths, NUL, control characters, over-length, flag-like refs, paths not in the tree; repository paths outside allowed roots, symlinked escapes and the data directory are refused; tar entries with symlinks/traversal rejected | pytest parametrized corpus; asserts the git runner was never called for rejected input |
| **M2-7** | Resolver returns `unknown` (with reason) for missing metadata, unknown SHAs, unregistered repository, and a bare release string; `verified` only for a SHA that exists in the repository; never `estimated` in M2; API/UI-facing fields expose the confidence | pytest |
| M2-8 *(added)* | Read-only boundary: the monitored repository's files and `.git` are byte-identical before and after register → scan → update → status; Graphify only ever runs inside the snapshot directory | pytest hashing the tree |
| M2-9 *(added)* | No LLM or network use: scrubbed child environment contains no API keys/proxies; Graphify invoked only with `extract --code-only` / `update`; the scan runs with outbound sockets blocked at the process level in a test using a deny-all proxy setting | pytest |
| M2-10 *(added)* | Project isolation and graph honesty: another project's key cannot see a repository; code-context responses always carry `confidence`, `source_file`, line and graph version; an `INFERRED` edge is returned labelled; a syntax-error file shows up in coverage as unverified rather than as understood | pytest |

**End-to-end demo (the M2 deliverable):** register a fixture repo → scan (graph + status) → commit a change that adds a function, edits another and deletes a file → trigger a scan → `GET …/changes` shows one change record listing the real files, line counts and the graph delta → `GET …/code?symbol=` shows updated callers/callees with confidence → status shows the new indexed SHA.

## 5. Work breakdown (vertical slices, in order; tests run after each)

| Slice | Content | Covers |
|---|---|---|
| S1 | Git adapter, validators, snapshot export/apply, hostile-input corpus, temporary-repo test helpers | M2-3 (part), M2-6, M2-8 (part) |
| S2 | Migration `0002`, repository registry, allowed roots, project-scoped register/list API | M2-6 (paths), M2-10 (isolation) |
| S3 | Graphify runner (pin check, scrubbed env, timeout), `CodeGraph` loader/validator, golden `graph.json` fixture from the spike, queries | M2-1, M2-2 (part), M2-9, M2-10 (part) |
| S4 | Scan service + worker: full/incremental scans, change records (append-only), retries, recovery, idempotence | M2-2, M2-4, M2-5, M2-8 |
| S5 | `changes`/`status`/`code` endpoints, resolver, e2e demo, docs (ADR-0002 job queue, API contract, threat model, acceptance), security review, M2 report | M2-3, M2-7, M2-10, closing |

Effort: frozen window weeks 3–4 (about two weeks). Dependencies: `git` (verified 2.55), an isolated Graphify environment installed from the hash-pinned wheel, Docker Postgres from M1. No external credentials.

## 6. Risks

- Graphify is young (0.9.x) and its JSON is not documented as stable → schema check, pin, golden-file tests; fallback indexer documented.
- Only Python and JavaScript verified; M2 claims support for exactly those until others are tested.
- `INFERRED` edges and class-level call resolution can mislead → always labelled; never used as proof (SOW §8.5).
- Worker, queue and snapshot handling add moving parts → bounded retries, idempotent scans, failure leaves the last good state.
- Large repositories untested for time/size → caps and `failed` with a clear code rather than hanging.
- Windows only so far; path-handling bugs are the most likely class → hostile-path corpus includes Windows forms.

## 7. Decisions requested (recommended defaults first)

1. **Adopt Graphify** as an isolated, hash-pinned CLI per ADR-0003 (alternative: the documented fallback indexer).
2. **Local-path repositories only** in M2, under `TRACEMIND_REPO_ROOTS`; remote cloning (network, credentials) deferred.
3. **Postgres-backed job queue** with a separate worker (`SKIP LOCKED`, bounded retries) — no new infrastructure; recorded as ADR-0002.
4. **No author name/email stored** in M2 (commit SHA, parents, dates and redacted subject only); say so if you want authorship for investigations.
5. **Scan snapshots of exact commits** (not the working tree), so the recorded SHA is true.
6. **Resolver:** `verified` only for a telemetry/deployment-supplied SHA that exists in the repository; everything else `unknown`; no timestamp or tag estimates in M2.
7. **API additions** `POST …/scans` and `GET …/code` beyond the M0 list — approve, or tell me to trigger scans another way.
8. **DB-level audit table** in M2, or structured logs only until M4's audit requirements? (Recommendation: structured logs plus the append-only `change_records`; add `audit_events` in M4 when tools exist.)
