# TraceMind API v1 — contract

Status: M1 and M2 endpoints are **implemented** and described here as built; M3–M5 endpoints are planned intent. The generated FastAPI OpenAPI document (`/openapi.json`) is the source of truth for implemented routes. Endpoints with no use case in the frozen plan are not listed (SOW §11).

## Conventions

- Base path `/api/v1`. JSON only. Timestamps are RFC 3339 UTC.
- **Authentication (TEMPORARY, M1–M4):** header `X-TraceMind-Key: <project key>`. The key identifies exactly one project, and every request is scoped to it. Keys live only in environment variables named in `config/projects.json`; they are never returned, logged or stored in plaintext. Replaced by real authentication and project-level authorization in M5.
- **Error envelope** (all non-2xx except the per-event `results` bodies below):

```json
{ "error": { "code": "validation_error", "message": "human readable", "details": [], "request_id": "…" } }
```

  `details` holds field-level problems (`loc`, `type`) only. Nothing submitted by a client is echoed back, and no stack traces, SQL, paths or configuration are exposed. Responses carry `X-Request-ID`.
- **Pagination:** `?limit=` (1–200, default 50) and an opaque `?cursor=`; response `{ "items": [], "next_cursor": null }`. A bad cursor is `400 invalid_cursor`.
- Long-running work returns `202 Accepted` with a job id and never blocks on an LLM call (M4).

## M1 endpoints (implemented)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (no auth) |
| GET | `/ready` | Readiness; `503 {"status":"degraded","dependencies":{"database":"unavailable"}}` when the database is down (no auth, no internals) |
| POST | `/api/v1/telemetry/events` | Submit events |
| GET | `/api/v1/events/{event_id}` | A stored normalized event ([event.schema.json](event.schema.json)) |
| GET | `/api/v1/incidents` | List; filters `service`, `environment`, `severity` (`ERROR`/`CRITICAL`); pagination |
| GET | `/api/v1/incidents/{incident_id}` | Incident ([incident.schema.json](incident.schema.json)) plus up to 20 most recent `evidence_ids` (`event:<event_id>`) |
| GET | `/api/v1/ingestion/health` | Accepted / duplicate / duplicate_conflict / rejected counts, rejection reasons, last event time; `?window_hours=` 1–168, default 24 |

### `POST /api/v1/telemetry/events`

Query: `format` selects the adapter (default `tracemind-json`; also `otlp-logs`). Unknown format → `400 unknown_format`.

Native body (`tracemind-json`): `{ "events": [ <IngestEvent> ] }` with 1–100 events ([event-ingest.schema.json](event-ingest.schema.json)). `otlp-logs` takes an OTLP/JSON logs export (`resourceLogs`); `service.name` and `deployment.environment` resource attributes are required and are never defaulted.

| Condition | Status |
|---|---|
| Missing/invalid key | 401 `unauthorized` |
| Not JSON (including `NaN`/`Infinity`) | 400 `invalid_json` |
| Unknown `format` | 400 `unknown_format` |
| Wrong envelope, empty list | 422 `invalid_envelope` |
| More than the maximum number of events | 422 `too_many_events` |
| Body over 1 MiB | 413 `payload_too_large` |
| An event's `project_id` differs from the key's project | 403 `project_mismatch` (nothing stored) |
| Rate limit exceeded | 429 `rate_limited` + `Retry-After` |
| At least one event accepted or a duplicate | 202 |
| Every event rejected | 422 |

202/422 bodies list every event: `{ "results": [ { "index": 0, "status": "accepted", "event_id": "…", "incident_id": "…" | null }, { "index": 1, "status": "duplicate", "event_id": "…" }, { "index": 2, "status": "rejected", "reason": "invalid_severity", "field": "severity" } ] }`.

Stable rejection reasons: `invalid_event`, `unknown_field`, `missing_field`, `invalid_<field>`, `<field>_too_long`, `timestamp_in_future`, `unknown_service`, `project_mismatch`, `attributes_too_large`, `attributes_too_deep`, `invalid_characters`.

Limits (defaults, configurable in `Settings`): 1 MiB body, 100 events, 8 KiB message, 32 KiB stack trace, 64 attributes, depth 6, 16 KiB serialized attributes, timestamps at most 10 minutes in the future; ingest rate burst 1000 events, 100 events/s per project (in-process).

## Adapters

Adapters convert a source format into `IngestEvent` dicts and go through the unchanged pipeline. The contract is documented in `backend/tracemind/ingestion/adapters.py` and enforced for every registered adapter by `tests/unit/test_adapter_contract.py`. Registered: `tracemind-json`, `otlp-logs`.

## M2 endpoints (implemented)

All require the project key. `{project_id}` must equal the key's project and the repository must belong to it, otherwise the response is `404 not_found` (never 403), so other projects' existence is not revealed. No endpoint returns a server filesystem path, an author name or an e-mail address, and none accepts a free-form git, filesystem or graph query.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/projects/{project_id}/repositories` | Register a **local** repository. Body `{display_name, path}`; the path must resolve (symlinks and junctions included) inside a configured allowed root and be a Git work tree with at least one commit. The path is never echoed back |
| GET | `/api/v1/projects/{project_id}/repositories` | List; pagination |
| POST | `.../repositories/{repository_id}/scans` | Queue a scan. Optional body `{revision}` (validated; default: the default branch tip). `202` for new work, `200` with `created: false` if that commit is already queued, running or indexed (idempotent) |
| GET | `.../repositories/{repository_id}/status` | `index_state` (`never_indexed`, `never_indexed_scan_pending`, `indexed`, `indexed_scan_pending`, `indexed_last_scan_failed`), `indexed_sha`, graph metadata (`derived_data: true`, tool, version, schema version, SHA-256, node/edge/inferred counts, **coverage**), the last scan and the last successful scan |
| GET | `.../repositories/{repository_id}/changes` | Append-only change records, newest first, paginated. Each has base and target SHA, files (path, status A/M/D/R, old path, line counts), commits (sha, parents, dates, redacted subject), graph delta, graph SHA-256 and a content hash. Optional `commit_sha` / `release` add a `release_mapping` and, if `verified`, filter to records containing that commit |
| GET | `.../repositories/{repository_id}/code` | Relationships for **exactly one** of `file=<repo-relative path>` or `symbol=<name>`. Every item carries `confidence` (`EXTRACTED`, `INFERRED`, `UNKNOWN`), file, line, evidence file/line; the response carries provenance (tool, version, graph hash, indexed SHA, `coverage_complete`) |

Error codes: `invalid_repository_path` (422, with `details[0].reason` such as `outside_allowed_roots`, `not_absolute`, `not_a_git_repository`, `git_dir_outside_allowed_roots`, `forbidden_location`), `invalid_revision` / `invalid_path` / `invalid_query` / `invalid_display_name` (422), `empty_repository` (422), `already_registered` (409), `not_found` (404), `not_in_graph` (404; absence from the graph does **not** mean the file is irrelevant), `not_indexed` (409), `repository_timeout` (504), `repository_too_large` (422), `repository_error` (502).

### `release_mapping` (approved 2026-10-28)

Optional query parameters of `GET .../changes`: `commit_sha` (up to 128 characters) and `release` (up to 128 characters). When **neither** is given the response has no `release_mapping` field and behaves as a plain listing. When **either** is given, the response always includes `release_mapping`:

```json
{ "items": [], "next_cursor": null,
  "release_mapping": { "commit_sha": null, "confidence": "unknown", "source": null, "reason": "no_commit_in_telemetry" } }
```

Semantics:

- `confidence` is `verified` **only** when `commit_sha` is a full 40-character hexadecimal id (case-insensitive, returned in lower case) that exists as a commit in the registered repository. Then `commit_sha` is that id, `source` is `"telemetry.commit_sha"`, `reason` is `null`, and `items` are the change records whose commit list contains it (possibly none: a commit can exist without having been part of any recorded scan range). `verified` means the commit exists and the source claimed it; it does not independently prove what is deployed.
- In every other case `confidence` is `unknown`, `commit_sha` and `source` are `null`, `items` is empty, and `reason` is one of: `no_commit_in_telemetry` (absent or empty `commit_sha`, including release-only queries), `invalid_commit_sha` (not hexadecimal, wrong length, branch names, tags, `HEAD`, expressions), `abbreviated_sha` (7 to 39 hexadecimal characters: not exact), `commit_not_in_repository`, `repository_unavailable`, `no_repository`.
- `release` is accepted so callers can pass what their telemetry carried, but it is **never** used to derive a commit. Release names, tags, branch names, timestamps and proximity play no role. There is no `estimated` value in M2.
- An `unknown` mapping is a normal `200` response, not an error. Clients must treat `unknown` as "no reliable release-to-commit link", never as "no change".

## Planned endpoints (not implemented)

| Milestone | Method | Path | Purpose |
|---|---|---|---|
| M3 | GET | `/api/v1/projects/{project_id}/deployments` | Deployment records |
| M3 | GET | `/api/v1/incidents/{incident_id}/history` | Similar historical incidents with evidence references |
| M4 | POST | `/api/v1/incidents/{incident_id}/investigations` | Start an investigation (202) |
| M4 | GET | `/api/v1/investigations/{investigation_id}` | Status and validated report |
| M4 | POST | `/api/v1/incidents/{incident_id}/feedback` | Developer feedback and verified outcome, stored separately |
| M5 | POST / GET | `/api/v1/projects`, `/api/v1/projects/{project_id}` | Project management; real authentication and project-level authorization on every route |

## Evidence references

Every claim in an investigation report cites `evidence_ids`. An id resolves to exactly one stored record: `event:<event_id>`, `commit:<sha>`, `diff:<sha>:<path>`, `history:<incident_id>`, `graph:<repo_id>:<graph_version>:<node>`. Only `event:` references exist today (M1). From M2, commits and graph facts are resolvable through change records and the code endpoint; they are wired into reports in M4. Unresolvable ids invalidate a report (M4).
