# Backlog parking lot

Ideas and gaps found during a milestone that belong to a later milestone or outside the frozen plan. Nothing here is started until its milestone begins or the owner approves it.

## Resolved

| Date | Item | Outcome |
|---|---|---|
| 2026-10-28 | Frozen plan lists fingerprints (M1) and lifecycle (M3) but no "create/group incidents" bullet | Owner approved: M1 groups incidents by fingerprint; lifecycle transitions stay in M3 |
| 2026-10-28 | M1 needs a registered project before M5's project APIs | Owner approved config-seeded projects, isolated in `tracemind/projects` |

## Open

| Date | Item | Belongs to |
|---|---|---|
| 2026-10-28 | Starlette warns that `httpx` with `TestClient` is deprecated in favour of `httpx2`. Re-check on the next dependency pass | M2 |
| 2026-10-28 | Rate limiter is in-process and resets on restart; a multi-process deployment needs a shared limiter | M5 |
| 2026-10-28 | Static per-project key is coarse (no rotation, no per-user identity, no audit trail) | M5 |
| 2026-10-28 | Fingerprint v1 parses Python tracebacks only; other languages group by message | Later, new `fingerprint_version` |
| 2026-10-28 | Two distinct identical events without a client `event_id` in the same millisecond collapse into one | Documented limitation; clients should send `event_id` |
| 2026-10-28 | No retention/cleanup for `events`, counters or rejection reasons | M3 (retention is a Snowflake/Postgres design item) |
| 2026-10-28 | `ingestion_stats` hourly buckets are never pruned | M3 |
| 2026-10-28 | The dashboard (M5) must HTML-escape `summary`, `message` and `stack_trace`; they are untrusted text after redaction | M5 |
| 2026-10-28 | CI workflow (`.github/workflows/ci.yml`) has never run; action versions and the Postgres step are unverified until the first push | After push |
| 2026-10-28 | A bad `TRACEMIND_*` numeric env value raises at startup with a generic message; no per-setting diagnostics | Low |

### Added during M2

| Date | Item | Belongs to |
|---|---|---|
| 2026-10-28 | Remote repository cloning (network, credentials) is out of scope; registration is local-path only | Later, needs its own design and threat model |
| 2026-10-28 | Per-file diff text is available in the Git adapter but intentionally not exposed by any endpoint (secrets in source); expose only with redaction and an explicit decision | M4/M5 |
| 2026-10-28 | Author names/e-mails are deliberately not collected; revisit only if investigations need authorship | M4 (needs owner approval) |
| 2026-10-28 | Tag-based or timestamp-based release hints (`estimated`) are not implemented; the owner required `unknown` instead | M5 (only if the owner reverses this) |
| 2026-10-28 | Language support is verified for Python and JavaScript only; other languages are reported as `not_assessed`, not as covered | Later, per language with evidence |
| 2026-10-28 | No large-repository measurement; caps exist (20,000 files, 256 MiB, 2 MiB per file) but are unvalidated limits | M5 evaluation |
| 2026-10-28 | Unreferenced content-addressed graph files accumulate in `graphs/`; there is no retention or garbage collection yet | M3 (retention) |
| 2026-10-28 | Workers poll every 2 s; `LISTEN/NOTIFY` would cut latency | Optional |
| 2026-10-28 | CI workflow has not run; it now also installs the pinned Graphify (still unverified until the first push) | After push |
| 2026-10-28 | Abbreviated commit SHAs from telemetry map to `unknown` (not exact); an app that only emits short SHAs will never get `verified` | Owner decision if this proves too strict |

