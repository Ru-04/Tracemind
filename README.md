# TraceMind

Evidence-driven application observability and AI-assisted incident investigation.

## TraceMind in plain English

**The problem.** When an app breaks, developers dig through thousands of log lines, guess which release caused it, and go hunting through code history. It is slow, and the same bug often gets "discovered" again and again.

**What TraceMind does.** It watches an app's logs and turns the noise into a short, organised list of problems, then helps you work out *why* each one happened.

Think of it as a detective's case file for your software:

1. **Collect.** The app sends its logs to TraceMind. Passwords, tokens and other secrets are scrubbed out before anything is stored.
2. **Clean up and label.** Every log entry is put into one standard shape. Each error gets a fingerprint, a stable ID based on the kind of error and where it happened. The same bug always gets the same fingerprint, even if the wording or timestamps differ.
3. **Group into incidents.** Hundreds of identical errors become one incident ("payment gateway URL is missing, seen 214 times") instead of 214 separate alerts.
4. **Connect to the code.** TraceMind reads your Git repository, read-only. It scans the code with a tool called Graphify to map how files and functions relate, and records which commits and releases exist. It links an incident to a release or commit only when it can prove the link. Otherwise it says `unknown`.
5. **Investigate** (planned). An AI assistant will read the collected evidence and write a report: what happened, which recent changes might be involved, how confident it is, and what to check next.

**Principles it sticks to**

- **Evidence first.** Facts are kept separate from guesses, and nothing is called a "root cause" without proof.
- **Read-only.** TraceMind never changes your app, your code or your servers. It cannot run shell commands or arbitrary database queries.
- **Private.** It does not collect Git author names or e-mails, and never exposes server file paths.

**Where it stands.** Working today: steps 1 to 4 (log ingestion, incidents, repository scanning and change tracking). Still to come: Snowflake storage (M3), the AI investigation (M4) and the dashboard with real login (M5). The `demo_app` is a small fake payments service that fails on purpose so you can see the whole flow in action. See [Try the M1 flow](#try-the-m1-flow-telemetry--incident) below.

**Technology.** Python and FastAPI, PostgreSQL, a separate background worker, and Docker for local setup.

## Documentation

- Scope and principles: [sow.md](sow.md)
- **Frozen milestone plan (followed strictly): [PLAN.md](PLAN.md)**
- Plans and reports: [M1 plan](docs/plans/M1-plan.md) / [report](docs/reports/M1-report.md), [M2 plan](docs/plans/M2-plan.md) / [report](docs/reports/M2-report.md)
- Architecture decisions: [docs/adr/](docs/adr/)
- Threat model: [docs/threat-model.md](docs/threat-model.md)
- Contracts (schemas, API, fingerprint): [docs/contracts/](docs/contracts/)
- Acceptance tests: [docs/acceptance-tests.md](docs/acceptance-tests.md)
- Dependency verification: [docs/dependency-verification.md](docs/dependency-verification.md)

## Current status

M0, M1 and M2 closed. **M3 (Snowflake and incident lifecycle) is blocked** until the Snowflake configuration is supplied through the secure configuration process; a prerequisites list and plan are in [docs/plans/M3-plan.md](docs/plans/M3-plan.md). Implemented: structured demo logs; ingestion API (native JSON and OTLP logs adapters); normalization, redaction, deterministic fingerprints, deduplication, incident grouping; registration of local Git repositories; a read-only Git adapter; scans of exact commits with the pinned Graphify CLI; append-only change records; a PostgreSQL-backed job queue with a separate worker; code-relationship queries; release-to-commit resolution that stays `unknown` unless proven. Not implemented yet: Snowflake (M3), AI investigation (M4), dashboard and real authentication (M5).

TraceMind is read-only with respect to monitored applications and repositories. It has no remediation, shell-execution or arbitrary-SQL capability, collects no Git author names or e-mails, and never returns server file paths.

Verified on **Windows only**, for **Python and JavaScript** repositories, on small fixtures. See the M2 report for limits.

## Local setup (Windows, PowerShell)

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
Copy-Item .env.example .env                      # fill in passwords/keys; .env is git-ignored
Copy-Item config\projects.example.json config\projects.json
docker compose --env-file .env -f infra/docker-compose.yml up -d
.venv\Scripts\alembic.exe upgrade head           # needs TRACEMIND_DATABASE_URL in the environment

# Graphify, isolated and hash-pinned (its own virtual environment, not TraceMind's):
.venv\Scripts\python.exe -m tools.install_graphify   # prints TRACEMIND_GRAPHIFY_BIN=...
```

Generate secrets with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. `TRACEMIND_KEY_PROJECT_DEMO` is the project's ingest/API key; `TRACEMIND_INGEST_KEY` must hold the same value for the shipper. For repositories set `TRACEMIND_REPO_ROOTS` (only directories under these can be registered), `TRACEMIND_DATA_DIR` (TraceMind's snapshots and graphs; never inside a repository root) and `TRACEMIND_GRAPHIFY_BIN`.

Quality gates and tests (integration tests need the Postgres container and `TRACEMIND_TEST_DATABASE_URL`; `graphify`-marked tests need the pinned CLI and skip locally without it):

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\ruff.exe check .
.venv\Scripts\mypy.exe
```

## Try the M1 flow (telemetry → incident)

```powershell
# terminal 1 - TraceMind API (load .env values into the environment first)
.venv\Scripts\python.exe -m uvicorn tracemind.api.app:create_app --factory --port 8000
# terminal 2 - demo app that fails on purpose, logging JSON to stdout
.venv\Scripts\python.exe -m uvicorn demo_app.main:app --port 8001 > demo.log
curl http://127.0.0.1:8001/demo/controlled-exception
.venv\Scripts\python.exe -m tools.ship_logs --file demo.log
curl -H "X-TraceMind-Key: <key>" http://127.0.0.1:8000/api/v1/incidents
```

Synthetic events without the demo app: `python -m tools.synthetic_events --kind valid --count 20 | python -m tools.ship_logs`.

## Try the M2 flow (code change → change record)

```powershell
# terminal 3 - the scan worker (a separate process; runs Graphify, never the API)
.venv\Scripts\python.exe -m tracemind.worker
# register a local repository under TRACEMIND_REPO_ROOTS, queue a scan, then read the results
#   POST /api/v1/projects/project-demo/repositories           {"display_name": "...", "path": "<absolute path>"}
#   POST /api/v1/projects/project-demo/repositories/{id}/scans
#   GET  .../repositories/{id}/status | /changes | /code?symbol=<name> | /code?file=<path>
```

Every request needs the header `X-TraceMind-Key`. The API contract is in [docs/contracts/api-v1.md](docs/contracts/api-v1.md).

The demo service has no authentication and is for local use only.
