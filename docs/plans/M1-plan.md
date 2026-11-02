# M1 plan — Telemetry and ingestion

**Status:** PROPOSED — awaiting owner approval. No M1 code has been written.
**Frozen scope (PLAN.md):** emit structured JSON logs from the demo app · ingestion endpoints and validation · normalize timestamps, severity, service, exception type, trace IDs · store events with deterministic fingerprints · track ingestion health, duplicates and malformed payloads.
**Deliverable:** trigger a known error and see its normalized event stored.
**Owner decisions already taken:** (1) M1 also creates/groups incidents by fingerprint; lifecycle transitions stay in M3. (2) Projects are seeded from configuration until M5, isolated so they can be replaced. (3) No dependency on Sentry, Datadog or any external platform; TraceMind owns its ingestion API and is tested with synthetic JSON events and the demo app; external integrations must be addable later.

## 1. Architecture

```
demo_app ──JSON log line (stdout)──► tools/ship_logs.py ──HTTP──┐
tools/synthetic_events.py ───────────────────────────────HTTP──┤
(future: Sentry/OTLP/... adapters) ──► convert to IngestEvent ──┘
                                                                 ▼
 POST /api/v1/telemetry/events ─► auth(project key) ─► size/rate limits
   ─► per event: validate ─► normalize ─► redact ─► fingerprint ─► derive/validate event_id
   ─► one DB transaction: insert event (dedup) ─► upsert incident + occurrence link ─► update counters
```

Module layout (all under `backend/tracemind/`, boundaries enforced by interfaces):

| Module | Responsibility |
|---|---|
| `config.py` | Typed settings from environment (limits, severity threshold, DB URL, projects file path) |
| `projects/` | `ProjectRegistry` interface + `ConfigProjectRegistry`. **The only place that knows projects come from config**; M5 swaps the implementation |
| `ingestion/models.py` | Pydantic `IngestEvent` (input) and `NormalizedEvent` (output), mirroring the JSON schemas |
| `ingestion/adapters.py` | `EventAdapter` protocol (foreign payload → `IngestEvent`). M1 ships only the native-JSON adapter. This is the extension seam for external platforms |
| `ingestion/normalize.py` | Timestamp, severity, service/environment, error type, trace/span ID normalization |
| `ingestion/redact.py` | Secret/PII redaction |
| `fingerprint.py` | v1 fingerprint per [docs/contracts/fingerprint.md](../contracts/fingerprint.md) |
| `ingestion/pipeline.py` | Orchestrates one event; returns a per-event result; no I/O besides the store interface |
| `incidents/service.py` | Create/group incidents from stored events |
| `storage/` | `EventStore` protocol; `postgres.py` (SQLAlchemy 2 + psycopg 3 + Alembic migrations) and `memory.py` (unit tests only) |
| `api/` | FastAPI routers, auth dependency, error envelope, health/ready |
| `observability/logging.py` | TraceMind's own JSON logs — never contain event payloads |

Pure logic (normalize, redact, fingerprint, pipeline) has no web or DB imports and is unit-testable on its own.

New dependencies (all verified to exist on PyPI 2026-10-28): `sqlalchemy` 2.1, `psycopg[binary]` 3.3, `alembic` 1.20, plus dev `pip-audit`. Postgres runs from Docker Compose (`infra/docker-compose.yml`); the Docker daemon on this machine is running (29.8.2).

## 2. Contracts

### 2.1 Ingest request

`POST /api/v1/telemetry/events`, header `X-TraceMind-Key: <project key>`, body:

```json
{ "events": [ { ...IngestEvent per event-ingest.schema.json... } ] }
```

Limits (configurable, defaults): body ≤ 1 MiB, 1–100 events, message ≤ 8 KiB, stack trace ≤ 32 KiB, ≤ 64 attributes, attribute nesting ≤ 6 levels, serialized attributes ≤ 16 KiB, timestamp not more than 10 minutes in the future.

### 2.2 Ingest response

| Condition | Status | Body |
|---|---|---|
| Missing/invalid key | 401 | error envelope |
| Body not JSON / wrong envelope / 0 or >100 events | 400 or 422 | error envelope |
| Body over limit | 413 | error envelope |
| Rate limit exceeded | 429 + `Retry-After` | error envelope |
| ≥ 1 event accepted or duplicate | 202 | `{ "results": [ {"index":0,"status":"accepted","event_id":"…","incident_id":"…"|null}, {"index":1,"status":"duplicate","event_id":"…"}, {"index":2,"status":"rejected","reason":"invalid_severity","field":"severity"} ] }` |
| Every event rejected | 422 | same `results` shape |

`reason` is a stable machine code; no free-text echo of submitted values (they may contain secrets). A batch never partially hides failures: each rejected event is listed.

### 2.3 Other M1 endpoints (all require the project key and are scoped to that key's project)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/events/{event_id}` | The stored normalized event ([event.schema.json](../contracts/event.schema.json)) |
| `GET /api/v1/incidents` | List; filters `service`, `environment`, `severity`; `limit`/`cursor` |
| `GET /api/v1/incidents/{incident_id}` | Incident ([incident.schema.json](../contracts/incident.schema.json)) with up to 20 most recent `evidence_ids` (`event:<event_id>`) |
| `GET /api/v1/ingestion/health` | Accepted / duplicate / duplicate-conflict / rejected counts for a window (default 24 h), rejection reasons, last event received time |
| `GET /health`, `GET /ready` | Liveness; readiness checks the database and returns 503 with the failing dependency named (no internals) |

`docs/contracts/api-v1.md` is updated to match when this plan is approved. Error envelope and pagination follow that file.

### 2.4 Temporary seams (to be replaced, isolated on purpose)

- **Projects:** `config/projects.json` (not secret) lists `project_id`, `allowed_services`, and `ingest_key_env` — the *name* of an environment variable holding the key. Keys are never in the file. Replaced in M5 by the project API and database table.
- **Authentication:** the per-project ingest key stands in until M5 adds real authentication and authorization. One FastAPI dependency (`require_project`) resolves key → project; M5 replaces it. Keys are compared in constant time.

### 2.5 Storage (PostgreSQL, Alembic migration `0001`)

| Table | Key columns |
|---|---|
| `events` | `event_id`+`project_id` (unique), all normalized-event fields, `content_hash`, `fingerprint`, `received_at`; indexes on `(project_id, fingerprint)`, `(project_id, occurred_at)` |
| `incidents` | `incident_id` (UUID), unique `(project_id, service, environment, fingerprint, fingerprint_version)`, severity, status (`open` only in M1), first/last seen, `occurrence_count`, summary, `commit_association` (unknown unless supplied by events) |
| `incident_occurrences` | `(incident_id, event_id)` unique |
| `ingestion_stats` | `(project_id, hour_bucket)` → accepted, duplicate, duplicate_conflict, rejected |
| `ingestion_rejections` | `(project_id, hour_bucket, reason)` → count |

`project_id` is a plain column (no FK) while projects are config-seeded; M5 adds the table and constraint. **Rejected payloads are never stored** — only the reason code and a count — because they may contain secrets.

## 3. Rules

### 3.1 Normalization

| Field | Rule |
|---|---|
| `timestamp` → `occurred_at` | RFC 3339 with explicit offset required; converted to UTC. No offset, unparsable, or > 10 min in the future → rejected. `received_at` is set by the server and kept separately |
| `severity` | Case-insensitive. `TRACE/DEBUG→DEBUG`, `INFO/NOTICE→INFO`, `WARN/WARNING→WARNING`, `ERR/ERROR→ERROR`, `FATAL/CRITICAL/CRIT→CRITICAL`. Anything else → rejected (never defaulted) |
| `service`, `environment` | Trimmed, lower-cased, must match `^[a-z0-9][a-z0-9._-]{0,127}$`. Service must be in the project's `allowed_services` |
| `error_type` | Trimmed; kept as given (exception class name). Absent stays `null` — never guessed from the message |
| `trace_id` / `span_id` | Lower-cased hex; trace 32 chars, span 16 chars, not all zeros. Absent stays `null`. **Invalid → rejected** (not silently nulled) |
| `commit_sha` | 7–40 hex, lower-cased, stored only as supplied; never inferred |
| `event_id` | Client-supplied (≤128 chars) or server-derived `derived-<sha256 of project|service|environment|occurred_at|severity|redacted message|error_type|trace_id|span_id>` so an identical resend deduplicates |

### 3.2 Redaction (runs before fingerprinting, persistence and logging)

- **By key** (recursive, case-insensitive substring on attribute keys): `password`, `passwd`, `secret`, `token`, `api_key`, `apikey`, `authorization`, `cookie`, `set-cookie`, `private_key`, `credential`, `session` → value replaced.
- **By pattern** in `message`, `stack_trace`, `error_type` and every string attribute value: `Bearer` tokens, JWTs, AWS access key IDs, GitHub / Groq / Slack token prefixes, PEM private-key blocks, `key=value` / `key: value` pairs with the key names above, URL userinfo (`scheme://user:pass@`), and email addresses (default on; configurable).
- Replacement marker is `[REDACTED:<kind>]`; `redactions_applied` counts replacements. Markers are left alone by fingerprinting so redaction never changes grouping.
- Limitation, documented: pattern redaction cannot catch unknown secret shapes (threat model residual risk).

### 3.3 Fingerprint v1

As specified in [fingerprint.md](../contracts/fingerprint.md), with two precisions added when this plan is approved: (a) "top application frame" = innermost frame of a Python traceback whose path contains neither `site-packages` nor the standard library, falling back to the innermost frame; (b) the path component used is the last two path segments with `/` separators, so the fingerprint is identical across machines and operating systems. V1 parses Python tracebacks only; other formats fall back to the normalized message.

### 3.4 Duplicates

- Unique key `(project_id, event_id)`; insert uses `ON CONFLICT DO NOTHING`, and event + incident update + counters commit in one transaction, so a duplicate can never increment an occurrence count.
- Same id, same `content_hash` → `duplicate`. Same id, different content → `duplicate_conflict`: original kept, never overwritten, counted and reported as `duplicate`, with the conflict visible in ingestion health.
- Known limitation: two genuinely distinct, identical events with no client `event_id` in the same millisecond collapse into one.

### 3.5 Incident creation and grouping

- Events with severity ≥ `ERROR` (configurable threshold) create or update an incident keyed by `(project, service, environment, fingerprint, fingerprint_version)`; lower severities are stored but create none.
- Update in the same transaction as the event: `occurrence_count + 1`, `first_seen = min`, `last_seen = max` (out-of-order delivery safe), severity = highest seen.
- `status` is `open` and nothing changes it in M1 (no transitions — M3). `investigation_status` = `none`, `resolution.state` = `none`.
- `summary` is deterministic: `<error_type or "Error">: <redacted message, normalized, ≤200 chars>`. No LLM.
- `commit_association`: always `{commit_sha: null, confidence: "unknown"}` in M1. A `commit_sha` supplied by the source is kept on the event, but deciding whether it verifies a deployment mapping is M2/M5 work.

### 3.6 Demo app, logs and shipping

- `demo_app` emits exactly one JSON line per failure to stdout, using the ingest-schema field names: `service="demo-payments"`, `environment` from `DEMO_ENVIRONMENT` (default `development`), `error_type` = exception class, redacted-by-construction `stack_trace` with repo-relative paths, `trace_id` generated per request (or taken from a valid incoming `traceparent`), `release`/`commit_sha` only from `DEMO_RELEASE` / `DEMO_COMMIT_SHA` when set (else `null`).
- `tools/ship_logs.py` reads JSONL (stdin or file), batches, and POSTs to the ingest API with the key from the environment. This keeps the demo decoupled from TraceMind exactly as a real app + log shipper would be.
- `tools/synthetic_events.py` generates seeded, deterministic events (valid, malformed, duplicate, secret-bearing) for tests and local demos.

## 4. Security considerations

| Risk | M1 treatment |
|---|---|
| Unauthenticated ingestion / cross-project access (T7) | Project key required on every M1 endpoint; project derived from the key; body `project_id` must match or → 403 `project_mismatch`; reads are scoped to that project |
| Secrets in telemetry (T1) | §3.2; redaction precedes fingerprint, DB write and logging; rejected payloads not stored |
| Malformed / oversized / abusive input (T2) | Size, count, depth and attribute limits; strict validation with `extra="forbid"`; in-process token-bucket rate limit per project (single-process only — documented) |
| SQL injection | SQLAlchemy parameterized statements only; no string-built SQL |
| Log injection | TraceMind's own logs are JSON-serialized; payload content is never logged, only ids, counts and reason codes |
| Information disclosure (T13) | Error envelope with stable codes; no stack traces, SQL or config; rejection reasons never echo input values |
| Key handling (T12) | Keys only in environment variables; constant-time comparison; never logged; `.env` stays ignored |
| Prompt injection (T4) | Not exercised in M1 (no LLM), but event text is stored verbatim-after-redaction and must be treated as untrusted data by M4 |
| Dependency vulnerabilities (T14) | CI runs `pip-audit` |
| Residual | Pattern redaction gaps; in-memory rate limiter resets on restart and is per-process; static keys are coarse (replaced in M5) |

## 5. Acceptance checks

M1-1 … M1-9 are the checks defined in M0 ([acceptance-tests.md](../acceptance-tests.md)), made concrete below. M1-10 … M1-12 are additions needed by the approved scope; they are not in the M0 list and are marked as such.

| ID | Check | How it is run |
|---|---|---|
| **M1-1** | Demo failure emits one JSON log line per scenario S1–S4 with the ingest field names; line validates against `event-ingest.schema.json`; `trace_id` valid or null; no fabricated `commit_sha` | pytest, captured stdout, jsonschema |
| **M1-2** | Valid event → 202 `accepted`; stored event has UTC `occurred_at` (offset input converted), enum severity, lower-cased service/environment, separate `received_at`; `GET /events/{id}` returns it and it validates against `event.schema.json` | pytest + Postgres |
| **M1-3** | Malformed JSON, wrong envelope, missing field, unknown field, bad timestamp (no offset), future timestamp, unknown severity, bad trace/span id, bad `commit_sha`, unknown service, too-long fields, too many events → clear 4xx with error envelope / per-event `reason`; > 1 MiB → 413; nothing stored; rejected counter and reason counter increase; response never echoes the bad value | pytest parametrized |
| **M1-4** | Seeded fake secrets (bearer token, JWT, AWS key, `password=` in message, `Authorization` attribute, URL with credentials, email, PEM block) absent from the DB rows, API responses, fingerprint inputs and TraceMind logs; `redactions_applied` correct; redaction deterministic | pytest scanning DB dump + captured logs |
| **M1-5** | Resend same `event_id` → `duplicate`, occurrence count unchanged, duplicate counter +1; derived-id resend dedups; same id with different content → original kept, `duplicate_conflict` counted; 20 concurrent identical submissions → exactly one stored, count = 1 | pytest, threaded test on Postgres |
| **M1-6** | Same failure → identical fingerprint across runs and with different paths/line numbers/numbers/UUIDs in the message; S1–S4 → four distinct fingerprints; different service or environment → different fingerprint; missing `error_type` handled; golden-value test locks v1 | pytest unit |
| **M1-7** | Events without trace/span stay `null` end-to-end; invalid ids rejected; no trace data is ever generated server-side | pytest |
| **M1-8** | `GET /ingestion/health` reports accepted / duplicate / duplicate-conflict / rejected totals, rejection reasons and last-received time that match what the test submitted; scoped to the key's project | pytest |
| **M1-9** | No LLM/network provider imported by `ingestion/`, `incidents/`, `fingerprint.py`; pipeline runs with all external access blocked | pytest import-graph check + run with sockets disabled except the test DB |
| M1-10 *(addition)* | Incident grouping: first ERROR event creates an `open` incident; repeats raise `occurrence_count`, update `last_seen`; out-of-order timestamps give correct `first_seen`/`last_seen`; different fingerprints → different incidents; WARNING events store but create none; duplicates never bump the count; incident validates against `incident.schema.json`; `commit_association` is `unknown` | pytest + Postgres |
| M1-11 *(addition)* | Auth/isolation: no key / wrong key → 401; project A key cannot read or write project B; body `project_id` mismatch → 403; unknown project/service rejected; rate limit → 429 with `Retry-After` | pytest |
| M1-12 *(addition)* | Extensibility: a test-only adapter converting a made-up foreign format into `IngestEvent` flows through the same pipeline with no pipeline changes; `/ready` returns 503 naming the database when it is down and the API stays up | pytest |

**End-to-end demo script (the M1 deliverable):** start Postgres + API → run the demo app → hit `/demo/controlled-exception` → ship the log line → `GET /api/v1/incidents` shows one open incident → `GET /api/v1/events/{id}` shows the normalized event → hit it again → count is 2 → re-ship the same line → still 2, duplicate counter 1.

## 6. Work breakdown (vertical slices, in order)

| Slice | Content | Covers |
|---|---|---|
| S1 | Models, normalization, redaction, fingerprint — pure code + unit tests | M1-4 (unit), M1-6, M1-7, M1-9 |
| S2 | Postgres schema + migration, store, pipeline, dedup, incident grouping, counters | M1-2, M1-5, M1-10 |
| S3 | API: auth, limits, error envelope, endpoints, health/ready | M1-3, M1-8, M1-11, M1-12 |
| S4 | Demo JSON logs, `ship_logs.py`, `synthetic_events.py`, end-to-end test | M1-1, e2e script |
| S5 | GitHub Actions CI (ruff, mypy, pytest with a Postgres service, pip-audit), docs updates (`api-v1.md`, `fingerprint.md`, `acceptance-tests.md`, README), security review, M1 report | closing |

Effort: about two weeks (frozen window: weeks 2–3). Dependencies: Docker running for Postgres integration tests; no external credentials needed.

## 7. Risks and unknowns

- Strict rejection of invalid trace IDs/timestamps loses events from sloppy clients; chosen over silent coercion to honour the SOW rule that invalid payloads must not silently become valid events. Rejections are counted so the cost is visible.
- Fingerprint v1 is Python-traceback specific; other languages group by message only.
- Rate limiting is per process; multi-worker deployments need a shared limiter (M5 hardening).
- SQLAlchemy 2.1 and psycopg 3.3 are newer than my reference experience; versions are verified to exist, behaviour is verified by the tests rather than assumed.

## 8. Decisions requested

1. Approve this plan as written (or list changes).
2. Confirm the defaults I chose where you hadn't specified: PostgreSQL via Docker Compose with SQLAlchemy + Alembic; per-project ingest key as the temporary auth; strict rejection of invalid trace IDs; email redaction on by default; M1-10 to M1-12 added to the acceptance list.
