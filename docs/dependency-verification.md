# Dependency and integration verification (M0)

Verified on 2026-10-28 from this machine. "Verified" means exactly what the Evidence column says; everything else is **unresolved** and has an owner milestone.

## Toolchain

| Item | Result | Evidence |
|---|---|---|
| Python | 3.12.10 available | `python --version` |
| Node | v24.21.0 available | `node --version` |
| Git | 2.55.0 available | `git --version` |
| Docker | 29.8.2 CLI present (daemon not checked) | `docker --version` |
| uv | available | `Get-Command uv` |
| GitHub CLI (`gh`) | **missing** | `Get-Command gh` |

## Python packages (latest on PyPI at check time)

| Package | Version | Used in | Status |
|---|---|---|---|
| fastapi | 0.162.0 | M0 | Installed, demo tests pass |
| pydantic | 2.14.1 | M0 | Installed |
| uvicorn | 0.54.0 | M0 | Installed |
| httpx | 0.28.1 | M0 (tests) | Installed. Starlette warns it is deprecated for `TestClient` in favour of `httpx2` — see parking lot |
| pytest / ruff / mypy | 9.2.0 / 0.17.3 / 2.4.0 | M0 | Installed, checks run |
| jsonschema | 4.x | M0 (tests) | Installed |
| sqlalchemy / psycopg | 2.1.6 / 3.3.6 | M1 | Exist on PyPI. Not installed; choice confirmed in M1 |
| snowflake-connector-python | 5.1.0 | M3 | Exists on PyPI. **Not tested against a real account** |
| groq | 1.7.0 | M4 | Exists on PyPI. **No API key tested; model list and structured-output support not checked** |
| graphifyy | 0.9.97 | M2 | Exists on PyPI. See below |

## Graphify — spike results (2026-10-28)

Decision record: [ADR-0003](adr/0003-graphify-adoption.md) (proposed: adopt as an isolated, pinned CLI). This section replaces the earlier, secondary-source notes, which were partly wrong (the package is Apache-2.0, not MIT-only).

**Method.** `uv pip install graphifyy` into a throwaway virtual environment outside the project (31 packages: numpy, networkx, tree-sitter plus ~25 grammars; no compiler needed). Ran the installed CLI against (a) a 5-file Python/JavaScript Git fixture and (b) a copy of this project's 47 Python files. LLM API keys were unset and `HTTP(S)_PROXY` pointed at a dead port. Scratch work was outside the repository and is not committed.

**Observed facts** (each seen in a command's actual output):

| Topic | Result |
|---|---|
| Identity | `graphifyy` 0.9.97; metadata repository `github.com/Graphify-Labs/graphify`; Apache-2.0 (+ MIT file, NOTICE); wheel sha256 `eb24dd7f93293753c001ca36e7304e0b52e86fe4124c2f531dcdb3d64865e01a` |
| Help text | Lists `extract <path> --code-only` ("local AST, no API key"), `update <path>` ("no LLM needed"), `query`, `path`, `explain`, `affected`, plus many LLM-, export- and assistant-install commands that TraceMind will not use |
| Headless run | `extract --code-only` and `update` ran with no LLM key and a dead proxy; exit 0. Fixture: 13 nodes / 19 edges in ~7 s. This repo: 47 files → 597 nodes / 1506 edges in ~7.3 s |
| `graph.json` | Keys `directed, multigraph, graph{schema_version:1, graphify_version}, nodes, links, hyperedges, built_at_commit`. Node: `id, label, source_file, source_location, file_type, community, …`. Edge: `source, target, relation, confidence, confidence_score, source_file, source_location, …`. Relations seen: calls, contains, imports, imports_from, method, references, uses, inherits, indirect_call, re_exports, rationale_for. Confidence seen: `EXTRACTED` (1367), `INFERRED` (139) |
| Files written | In the scanned directory: `graphify-out/{graph.json, graph.html, GRAPH_REPORT.md, manifest.json, cache/, .graphify_root}`. `.graphify_root` holds an absolute local path |
| Add / edit / delete / rename | Each produced exactly the expected node and edge additions/removals via `update`; rename = remove old + add new. Mass deletion (10 of 14 nodes) also succeeded without `--force` |
| Failure handling | Corrupt `graph.json`: `update` exits 1 ("Delete the file and run a full rebuild") and does not overwrite; full rebuild afterwards succeeded. Nonexistent path: exit 1. **Syntax-error file: indexed silently, exit 0** |
| Determinism | Two clean builds of the same tree gave identical node/edge/community sets |
| `built_at_commit` | Git `HEAD` at scan time, regardless of uncommitted edits; `null` outside a Git repository. Not usable as provenance |
| Query helpers | `affected "prepare_event" --depth 2` returned the real callers (including `IngestionPipeline.process`) headless |
| Known imprecision | A call through an instance (`OrderService().avg(...)`) produced an edge to the class, not the method |

**Further findings while implementing M2** (each seen in command output or a passing test):

| Topic | Result |
|---|---|
| Path base | `extract` records paths relative to the scanned directory, but `update <dir>` records them relative to the **current working directory**. TraceMind always runs Graphify with the snapshot as working directory and the argument `.`, and the loader additionally maps every path onto a real snapshot file (a node that maps to no snapshot file invalidates the graph) |
| `.gitignore` | `extract` skips files matched by the repository's `.gitignore` (a tracked-but-ignored file silently disappeared) unless `--no-gitignore` is given; `update` did not skip them. TraceMind passes `--no-gitignore` and compares coverage against the snapshot |
| `--no-cluster` | Produces a `graph.json` **without** the `graph` metadata block (no schema version). TraceMind therefore uses the default clustering |
| Document nodes | `update` adds `document` nodes (e.g. README.md) that `extract --code-only` omits, so the same commit differs by path. The loader keeps only `file_type == "code"` nodes and counts what it dropped |
| Determinism (stronger) | Two builds of the same snapshot produced byte-identical `graph.json` (identical SHA-256), so graph hashes are usable for idempotence checks |
| Hash-pinned install | `uv pip compile --generate-hashes --universal` produced a 558-line lock (494 hashes) for the whole tree; `pip install --require-hashes` into a fresh venv succeeded; 244 installed files verified against their recorded hashes by the runner |
| Process tree | A timeout kills Graphify and its children (`taskkill /T` on Windows); a grandchild planted by a fake CLI did not survive |
| Speed | 0.7 s for a 5-file snapshot; about 1 s per scan including Git export and validation in the test repositories. No large-repository measurement was made |

**Vulnerability status of Graphify's environment** is separate from hash pinning. A point-in-time `pip-audit` of `infra/graphify/requirements.lock` on 2026-10-28 reported no known vulnerabilities; that is not an ongoing guarantee and recurring audits are follow-up F1 in PLAN.md.

**Still not verified:** languages other than Python and JavaScript; large-repository performance; symlink handling inside Graphify itself (TraceMind removes symlinks before scanning); the `--force` fewer-nodes guard trigger; Linux and macOS; absence of network use beyond the dead-proxy test (no traffic capture was done); project governance.

## Snowflake — UNRESOLVED (M3 blocked on configuration)

No account, user, role, warehouse or key material exists in this repository, and none may be pasted into chat or committed; see the secure-configuration requirements in [docs/plans/M3-plan.md](plans/M3-plan.md).

**Documentation read on 2026-10-28** (official docs, not yet exercised against an account):

| Topic | What the docs say |
|---|---|
| Package | `snowflake-connector-python` 5.1.0 exists on PyPI. The release notes describe 5.0.0 release candidates in 2026, so the exact version and its behaviour must be re-verified when M3 starts |
| Key-pair auth | `authenticator='SNOWFLAKE_JWT'` with `private_key_file` and `private_key_password` (the older `private_key_file_pwd` still works but emits a DeprecationWarning); `private_key` accepts key bytes/object; encrypted and unencrypted keys and key rotation are supported |
| Binding | Default `paramstyle` is `pyformat` (client-side binding). `qmark` or `numeric` give **server-side** binding. M3 will use `qmark` |
| Timeouts | `cursor.execute(..., timeout=seconds)` aborts a query that has not completed; `login_timeout` default 120 s; `query_timeout` (connect level, default 0 = none); `network_timeout` and `socket_timeout` are deprecated |
| Query tag | `QUERY_TAG` appears as an example of a per-statement parameter on `execute()` (exact mechanism to be verified) |
| Resource monitors | Warehouse-only (not serverless features); creation is limited to account administrators (the create-monitor page says "account administrators"; the resource-monitors page names `ACCOUNTADMIN`; an earlier version of this row said "or granted privileges", which applies only to viewing/modifying an *existing* monitor with `MONITOR`/`MODIFY`, not to creating one); triggers can `NOTIFY`, `SUSPEND`, `SUSPEND_IMMEDIATE` at a percentage of quota; assigning one warehouse per monitor gives the tightest control; warehouses auto-suspend by default |

**Additional documentation read on 2026-10-29 and 2026-11-02** (docs.snowflake.com; 44 pages opened, a few of them read only in part, plus two seen only as search-result summaries; **still nothing exercised against any account**; page URLs and per-item status are in [docs/setup/snowflake-account-setup.md](setup/snowflake-account-setup.md) §4). Documentation states:

| Topic | What the docs say |
|---|---|
| Resource monitor | Creation and assignment to a warehouse are `ACCOUNTADMIN`-only; `FREQUENCY = NEVER`, `TRIGGERS` (`NOTIFY` ≤5, `SUSPEND`, `SUSPEND_IMMEDIATE`); not intended for precise limits; suspension may lag; warehouses only (no serverless features or AI services); counts cloud-services credits without the daily 10 % adjustment; notifications disabled by default and need a verified e-mail; **a warehouse-level monitor "can monitor, but cannot suspend, credit usage by cloud services"**; `ALTER RESOURCE MONITOR … TRIGGERS` is not additive |
| Warehouse billing | X-Small 1 credit/hour, per-second billing with a 60-second minimum on every start |
| Users and keys | `TYPE = SERVICE` users cannot log in with a password; named key pairs support `ROLE_RESTRICTION` and `DAYS_TO_EXPIRY`; expiry and role restriction cannot be changed afterwards (remove and re-add); `ROTATE` keeps the old key 24 h by default; `SHOW USER KEY PAIRS` shows fingerprint, role scope, status, expiry, last use |
| Roles and ownership | The creating role owns an object; `SYSADMIN` cannot manage objects owned by a custom role outside its hierarchy; `GRANT ROLE` needs ownership of the role or `MANAGE GRANTS`; `DROP ROLE` transfers the dropped role's objects to the role that drops it; active secondary roles count towards privilege checks (`USE SECONDARY ROLES NONE` disables them) and the `CREATE USER` default for `DEFAULT_SECONDARY_ROLES` is `ALL` |
| Grants | Future grants need `MANAGE GRANTS`, schema-level overrides database-level, and they are **not applied when a table is renamed or swapped**; a role can query a view without privileges on its tables; a temporary table needs no `CREATE TABLE` |
| `MERGE` and DML | `MERGE` with only `WHEN NOT MATCHED … INSERT` inserts one target row **per source copy** of a duplicate; `UPDATE`/`DELETE`/`MERGE` hold locks that stop them running in parallel with each other while `INSERT` generally does not serialize; `LOCK_TIMEOUT` defaults to 12 hours and can be set at account, user or session level; `PRIMARY KEY`/`UNIQUE` are **not enforced** on standard tables |
| `SHOW` visibility | `SHOW WAREHOUSES` and `SHOW RESOURCE MONITORS` list only objects on which the current role holds at least one privilege, except for holders of `MANAGE GRANTS` |
| Trial | 30 days or until the free balance is used, then the account is suspended; a card does not end the trial, upgrading does; credit amount, sign-up role and monitors on trials are not stated |
| Metering | `ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` lags up to 180 minutes (cloud services up to 6 hours) |
| Pricing | The price per credit is not on the pages read; docs refer to Snowflake's Pricing Guide (varies by account type and region) |

**Not verified:** anything against a real account (connectivity, roles, privileges, costs); `session_parameters` and `STATEMENT_TIMEOUT_IN_SECONDS` handling and how a session value interacts with the warehouse value; service-user Snowsight access; **whether a network policy applies to key-pair logins**; **what privileges `MERGE` needs** (no official page states it); whether a blocked `MERGE` re-reads the target after the lock is released; whether future grants apply to tables created by any role; how the Python connector conveys the restricted role in the key-pair token; the fingerprint text format; the secondary-role setting of the owner's existing user, the `ALTER USER` form for changing it, and `CURRENT_SECONDARY_ROLES()` output when none is active; whether a `DELETE … WHERE` also needs `SELECT`; whether rotating a key restarts a full expiry period; whether resource monitors, `FREQUENCY = NEVER` and the used-credit counter behave as documented on a **trial** account; the names of the `SHOW RESOURCE MONITORS` columns other than `level`; whether `Admin » Billing` and `USAGE_IN_CURRENCY_DAILY` work on a trial; whether the connector works as expected on this platform; how consumption can be observed per query; account edition, region and credit price.

## Groq — UNRESOLVED

No API key present. Needed before M4: key, chosen model, confirmed JSON/structured-output support and rate limits (ADR-0004).

## PostgreSQL — UNRESOLVED

Docker CLI exists; Postgres image/compose not yet created (M1).
