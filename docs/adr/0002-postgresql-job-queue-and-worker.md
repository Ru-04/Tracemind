# ADR-0002: PostgreSQL-backed job queue with a separate worker

- Status: **ACCEPTED** by the project owner on 2026-10-28; implemented in M2 (`repos/scan_store.py`, `repos/scan_service.py`, `worker.py`)
- Milestone: M2 (first slow job: repository scans). M4 investigations are expected to reuse the pattern.
- Related: ADR-0001 (modular monolith plus worker), ADR-0003 (Graphify), SOW §4.2

## Context

Repository scans (Git export, Graphify, validation) take seconds to minutes and must not block HTTP requests. The system needs bounded retries, safe recovery when a worker dies, and no corruption of the last valid graph or the audit history. It must not add infrastructure for a solo-developer V1.

## Decision

The `repository_scans` table **is** the queue. A separate worker process (`python -m tracemind.worker`) claims and runs scans; the API only enqueues and reads. No broker or new service.

### Concurrency

- **Claim:** `SELECT … FOR UPDATE SKIP LOCKED`, oldest first. Two workers can never receive the same row (tested with 8 simultaneous claimers).
- **One scan per repository at a time:** after selecting a candidate the claimer takes `pg_advisory_xact_lock(hash(repository_id))`, re-checks that no other scan of that repository holds a valid lease, and otherwise skips the candidate. Different repositories run in parallel (tested: peak per repository 1, overall peak ≥ 2).
- **Lease and heartbeat:** a claim sets `lease_expires_at`; a background heartbeat in the running worker renews it every third of the lease. Defaults: 900 s lease.
- **Fencing:** completion and failure updates are conditional on `status = running AND locked_by = <this worker>`. A worker that lost its lease (stalled, partitioned, or slow) cannot publish a result or alter state; its attempt is reported `abandoned` and its work directory is removed.

### Idempotence

- Enqueueing is idempotent per target commit. A partial unique index allows one *live* (queued/running) scan and another allows one *succeeded* scan per `(repository, target_sha)`. Re-triggering returns the existing scan (`created = false`); ten concurrent enqueues produce one row (tested).
- A successful scan writes exactly one change record (`UNIQUE(scan_id)` and `UNIQUE(repository, base, target)`).
- Graph files are content-addressed (`graphs/<sha256>.json`); Graphify's output is byte-identical for identical input (verified), so re-running a scan reproduces the same hash.

### Retries and failure

- Bounded: `scan_max_attempts` (default 3) per scan. A retryable failure requeues the scan with exponential backoff (`scan_backoff_seconds`, doubling, capped at 300 s) via `available_at`; after the last attempt the scan is `failed` with a stable `error_code`.
- Non-retryable codes fail immediately (missing repository or revision, hostile path, Graphify not installed/wrong version/modified, empty graph, hard size limits, already indexed).
- Error text is never stored or logged; only stable codes and the exception *class*.

### Recovery

| Situation | Behaviour |
|---|---|
| Worker dies mid-scan | Lease expires → next claim records `lease_expired` and re-runs the scan (attempt + 1). After the last attempt the scan fails with `worker_lost`. |
| Graphify update fails or produces an unusable graph | The work copy's Graphify output is discarded and the same commit is rebuilt from scratch once (`graph_rebuilt` event). If that also fails the attempt fails. |
| Snapshot diverges from Git (tampering, partial copy) | Detected by comparing every file's Git blob hash with the commit; the snapshot is rebuilt from Git (`snapshot_rebuilt` event). |
| `current/` or the published graph file is missing | A full scan from the last good commit's perspective; the change record notes `baseline_missing`. |
| Crash between database commit and directory swap | `current.sha` no longer matches the repository pointer → next scan does a full rebuild. Queries read the content-addressed graph, so they stay correct meanwhile. |
| Leftover work directories | Swept after every attempt and when a scan for the same id starts. |

### What can never be lost or corrupted

- **Last known-good graph:** the repository pointer (`indexed_sha`, `graph_sha256`), `current/`, and `graphs/` change only after Graphify succeeded **and** the graph validated **and** the database transaction committed. Every failure scenario in the tests asserts these are byte-identical afterwards.
- **Audit history:** `scan_events` and `change_records` are append-only (database triggers reject UPDATE and DELETE). Every transition (queued, claimed, lease_expired, graph_rebuilt, snapshot_rebuilt, retry_scheduled, succeeded, failed) is recorded with attempt and worker.

## Alternatives considered

- **Celery/RQ/Redis:** new infrastructure and another failure mode; unjustified for V1's volume.
- **In-process threads in the API:** scans would die with the API and block its lifecycle.
- **`LISTEN/NOTIFY`:** possible later to cut polling latency; polling (default 2 s) is sufficient now.

## Consequences and limits

- Polling latency up to the poll interval; the queue shares PostgreSQL's capacity (fine for tens of repositories, not thousands).
- Heartbeats use the database clock only through timestamps the worker supplies; large clock skew between hosts would distort lease expiry. Single-host assumption for V1.
- The advisory lock is per repository id hash; a hash collision only causes harmless extra serialization.
- Verified on Windows with PostgreSQL 16 only.
