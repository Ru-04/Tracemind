# Snowflake account setup guide for TraceMind development

**Status:** **Snowflake preparation completed; M3 implementation pending** — revision 4, prepared 2026-11-02 (revision 3: 2026-10-29; revision 1: 2026-10-28). Nothing in this guide has been executed. TraceMind has not connected to Snowflake, no SQL has been run, no Snowflake resource exists, no key material was generated, inspected or configured, no application code was changed, and nothing was committed or pushed. Documentation files updated in this revision: this guide, the four SQL drafts, `.env.example`, `PLAN.md`, the M3 plan, `dependency-verification.md`, `threat-model.md` (plus a pre-existing broken-link fix) and an amendment note in ADR-0007. **Assumption to confirm:** the owner said the O1–O8 decisions "have been made" without restating them; the assistant applied its own recommendations (section 0.0). **O2–O7 and the 7,200 s floor are therefore "applied, owner confirmation pending"; only O1 (defer) is the owner's own instruction.**
**Milestone gate:** M3 stays **PROPOSED and BLOCKED** ([PLAN.md](../../PLAN.md), [M3 plan](../plans/M3-plan.md)). Completing this guide is a *prerequisite*, not authorization to start M3 or to run anything live.
**Secrets rule:** never paste a password, private key, passphrase, token or the contents of a key file into chat, SQL, source, logs, fixtures or Git. This guide uses only variable *names* and placeholders; values stay on your machine.
**Words used about evidence** — *VERIFIED*: a named docs.snowflake.com page (section 4) states it. *INFERRED*: follows from verified facts, no page states it. *UNVERIFIED*: no page read states it. **None of these means "tested".** Test outcomes are only ever **PASS**, **FAIL** or **NOT RUN** (section 11), and every test is currently **NOT RUN**.

| File | Purpose |
|---|---|
| [infra/snowflake/00_preflight_readonly.sql](../../infra/snowflake/00_preflight_readonly.sql) | Read-only facts and the **ACCOUNTADMIN gate** |
| [infra/snowflake/01_admin_setup.sql](../../infra/snowflake/01_admin_setup.sql) | Administrator setup in lettered parts, each naming its role |
| [infra/snowflake/02_verify_permissions.sql](../../infra/snowflake/02_verify_permissions.sql) | Gate query, read-only verification, expected-to-fail tests, cost review, canary and `MERGE` tests (the last two are commented out and post-authorization) |
| [infra/snowflake/99_teardown_DESTRUCTIVE.sql](../../infra/snowflake/99_teardown_DESTRUCTIVE.sql) | Reversible steps, guarded test cleanup, permanent removal — **all commented out** |

## 0.0 Decision record for O1–O8 (2026-11-02)

The owner wrote that the O1–O8 decisions "have been made" without restating them, and asked for them to be applied. **I applied my own recommendations of the previous report as the decisions**, and I record that assumption here so that any item can be reversed at once:

| # | Decision as applied | Where |
|---|---|---|
| O1 | **Deferred** until the read-only preflight lists existing warehouses and monitors (**the owner's own instruction**). Part E stays commented out; the decision rules are in `00` step 5 and `01` Part E | 01 Part E; 00 step 5 |
| O2 | **Yes:** record F20 (network-policy review, five triggers) | `PLAN.md` F20; section 10 |
| O3 | **Applied (owner confirmation pending):** `TRACEMIND_TEST_CLEANER`, with the secondary-role protocol that my self-review added and the independent review confirmed | 01 Parts C, D; 99 section T; section 7 |
| O4 | Default **14,400 s**; floor **raised from 3,600 s to 7,200 s** | section 8.3; `.env.example` |
| O5 | **90 days**; decided before Part C because it cannot be altered afterwards. Implications explained in section 10.1 | 01 Part C; section 10.1 |
| O6 | Variable names **applied** to `.env.example` (the variable set of the previous report) | `.env.example` |
| O7 | **Conditional** on MERGE-1 and MERGE-4 (no automatic grant, never `DELETE`) | section 6.5 |
| O8 | **Open information request** to the owner (credit price, edition, region, trial balance and the unit its tile shows, trial dates) | section 8.2; section 15 |
| O9 | **New, open:** secondary roles on the owner's own user (default `ALL` for users created with `CREATE USER`) | sections 7 and 12; 01 Part C |

**Status words used for O2–O7:** "applied, owner confirmation pending". O4's 7,200 s floor, O3's cleanup role and its protocol, and the recomputed cost figures in section 8.3 are the assistant's own proposals.

## 0. Approved decisions and where each is applied

| # | Owner decision (2026-10-29) | Applied as |
|---|---|---|
| 1 | 20 credits is a **provisional development allowance**, not a guaranteed total-cost cap | Monitor `TRACEMIND_WH_MONITOR`, quota 20, `FREQUENCY = NEVER`; the guide never calls it a cap (section 9) |
| 2 | Separate writer and reader service users, separate encrypted key pairs | `TRACEMIND_EXPORTER_SVC`→writer, `TRACEMIND_QUERY_SVC`→reader; named key pairs, role-restricted, 90-day expiry |
| 3 | Test isolation by a dedicated marker and unique run ids; cleanup narrow and protected against deleting production data | Marker `tmtest-…` run id in a `test_run_id` column, separate database, and a **cleanup role with no privilege at all on the development database** (section 7) |
| 4 | Verify privileges and ownership; do not assume `SYSADMIN` can administer custom roles | Section 4 rows 13–15: it cannot (VERIFIED); no TraceMind role is granted to it |
| 5 | Defer network-policy enforcement; document residual risk and a concrete review trigger | Part F stays commented out; section 10; follow-up F20 in `PLAN.md` || 6 | Manual migrations as `TRACEMIND_MIGRATOR`; no migration or admin credentials for runtime services | Migrator and cleaner are granted to **your user only**, have no key pair, never appear in `.env` |
| 7 | **Retain `MERGE`** for idempotent export unless an alternative is demonstrably safe; derive minimum privileges from the exact clauses; no `DELETE` unless implementation needs it | Writer stays `INSERT`+`SELECT`; `MERGE` clause analysis, test matrix and design requirements in section 6 |
| 8 | Resource monitors are one safeguard; document limitations, trial uncertainty, notifications, metering delay and uncovered costs | Section 9 |
| 9 | Exporter off by default; conservative configurable minimum export interval | Section 8.3: default 14,400 s, code floor 7,200 s (decision O4 as applied); `.env.example` |

## 1. Repository state (inspected again in this pass)

Read from disk in this pass: the four SQL drafts and this guide (as they stood before revision 4), `docs/plans/M3-plan.md`, `PLAN.md`, `docs/threat-model.md`, `docs/dependency-verification.md`, `.env.example`, ADR-0007, and the places where Snowflake is mentioned in `README.md`, `docs/acceptance-tests.md`, `docs/backlog-parking-lot.md` and ADR-0001. Earlier passes read the M2 report, `docs/contracts/api-v1.md`, ADRs 0001/0002/0003 and both migrations.

- M0–M2 closed; M3 blocked; there is **no Snowflake code, dependency or migration**; nothing is committed or pushed (`main` has no commits; the remote `origin` is configured; the repository's visibility was **not re-checked** in this pass and the push gate in `PLAN.md` still applies).
- Before revision 4, `.env.example` had one `SNOWFLAKE_USER`, one key file and one passphrase, which contradicted the two-service-user design. **Revision 4 replaced that block** (section 13); the only code that mentions `SNOWFLAKE` is three existing test files (`test_git_adapter.py`, `test_graphify_runner.py`, `test_isolation.py`) that check child-process environments are scrubbed of secrets, which are unaffected.
- `README.md` and `docs/acceptance-tests.md` (M3-1 to M3-7, status "defined") were **not changed**: the new checks M3-8 to M3-14 live in the M3 plan as additions awaiting approval, and acceptance-tests.md is updated only when they are approved.
- **Conflict found and now resolved in favour of `MERGE`:** revision 2 proposed replacing the M3 plan's `MERGE` (§§4.4, 4.5, M3-9, risks, S3) with `INSERT … WHERE NOT EXISTS`. That substitution is withdrawn: plain `INSERT` statements are documented as generally *not* serializing (section 6.4), so it cannot be shown safe under concurrency. The M3 plan's `MERGE` therefore stays, with the tightened design in section 6.
- **No blocker in the repository.** Blockers are on the Snowflake side (section 2) and the open owner decisions (section 12).

## 2. Who can do what — and where to STOP

Do **not** assume you hold `ACCOUNTADMIN`: the trial documentation does not say which role the sign-up user receives.

| Action | Role | Basis | If you lack it |
|---|---|---|---|
| Read account, region, roles, existing objects | any | script 00 | — |
| Create databases, schemas, warehouse (Part A) | `SYSADMIN` | `CREATE WAREHOUSE` is held by it by default (create-warehouse) | someone who holds it |
| **Create the monitor and attach it to the warehouse (Part B)** | **`ACCOUNTADMIN` only** | create-resource-monitor; alter-warehouse: "Only the ACCOUNTADMIN role can assign warehouses to resource monitors" | **STOP before Part A.** No substitute role, no skipping, no continuing |
| Roles, users, role grants, key pairs (Part C) | `USERADMIN` | create-user; ownership rule (row 13) | someone who holds it; fallback grant is in script 01 Part C |
| Privileges and future grants (Part D) | `SECURITYADMIN` (`MANAGE GRANTS`) | grant-privilege | someone who holds it |
| Optional account-level monitor (Part E) | `ACCOUNTADMIN` | alter-account; resource-monitors | owner decision O1 |
| Optional network policy (Part F, deferred) | `SECURITYADMIN`; user ownership + policy `USAGE` to assign | network-policies | deferred |
| Enable monitor notifications | the user's own Snowsight profile (verified e-mail) | resource-monitors | — |
| Generate keys, store them, edit `.env` | **you**, locally | — | — |

**STOP rules.** (1) Gate before anything is created: if `USE ROLE ACCOUNTADMIN` does not work, **nothing in script 01 is run**. (2) Gate between Part B and Part C: if the warehouse does not show `resource_monitor = TRACEMIND_WH_MONITOR` and state `SUSPENDED`, **do not run Part C or Part D**. (3) Any error in a part: stop, report the text, do not work around it with a broader role. **Stopping is free only before Part A.** If Part B fails after Part A ran (for example because monitors turn out not to be supported on a trial, which no page read excludes), the two databases, six schemas and the **suspended** warehouse already exist: ungranted and costing nothing while suspended. Leave them, report, run neither Part C nor Part D, and remove them only through the confirmed procedure in script 99 (X3, X4) if you wish. **Do not use "Run All":** each Part of script 01 begins with a guard line `SELECT <<..._DELETE_THIS_LINE>>;`, a deliberate syntax error you delete only after the verification it names (whether Snowsight stops at the first error is UNVERIFIED, hence one guard per Part).

## 3. Ordered execution procedure (every step names its role and its verification)

Nothing below is run by me. Record every verification as PASS / FAIL / NOT RUN (section 11); a step you did not perform, or whose output you could not read against its expectation, is **NOT RUN**.

| Step | Role | What | Verification (script 02 unless stated) |
|---|---|---|---|
| 0 | your normal role, then `ACCOUNTADMIN` (gate only), then `SECURITYADMIN` | Run `00_preflight_readonly.sql` (section 15 has the exact procedure). Note account, region (AWS), roles held, **that `USE ROLE ACCOUNTADMIN` works**, no pre-existing `TRACEMIND%` object, every existing warehouse and monitor (the SHOW statements run as `SECURITYADMIN` because they list only objects the current role has a privilege on, unless it holds `MANAGE GRANTS`). In Snowsight note edition, trial end date, the remaining-balance tile and **the unit it shows**, **credit price**. This step also settles **O1** | G0–G3. **If `ACCOUNTADMIN` fails: STOP.** |
| 1 | Snowsight, your profile | **Verify your e-mail**, then **enable resource-monitor notifications** (disabled by default; resource-monitors page) | N0. The real test is the 10 % canary e-mail later (N1) |
| 2 | local, Git Bash | Generate two **encrypted** key pairs **outside the repository**; type each passphrase only at OpenSSL's prompt (commands below). I do not run or inspect this | K0 (files exist, `.p8` header says ENCRYPTED, folder ACL restricted) |
| 3 | local | Copy `01_admin_setup.sql` out of the repo; replace `<<OWNER_USER>>`, `<<PUBLIC_KEY_BODY_EXPORTER>>`, `<<PUBLIC_KEY_BODY_QUERY>>`. Leave Parts E and F commented. Each of Parts A–D begins with a guard line (`SELECT <<…_DELETE_THIS_LINE>>;`): delete a guard **only** when you reach that Part after its predecessor's verification. Do not use "Run All"; run one Part at a time | — |
| 4 | `SYSADMIN` | Part A: databases, schemas, warehouse (created **suspended**) | A1: 1d, 1g — size X-Small, `auto_suspend` 60, state SUSPENDED, `resource_monitor` empty *for now*. No query on the warehouse yet |
| 5 | `ACCOUNTADMIN` | Part B: monitor, attached | **GATE-B** (`resource_monitor = TRACEMIND_WH_MONITOR`, SUSPENDED) and 1g: quota 20, `NEVER`, triggers 10/50/75 NOTIFY, 90 SUSPEND, 100 SUSPEND_IMMEDIATE. **FAIL → STOP.** Record what a *trial* shows (UNVERIFIED) |
| 6 | `USERADMIN` | Part C: four roles, two SERVICE users, role grants, key pairs | 1c, 1d (owners), 1e, 1f (one ACTIVE, role-restricted key per user, ~90-day expiry, fingerprint equals your local one — **if the text formats cannot be compared with confidence: NOT RUN, STOP**) |
| 7 | `SECURITYADMIN` | Part D: all privileges | 1a, 1b, 1h, 1i: only the intended privileges; the cleaner has **nothing** on `TRACEMIND_DEV` |
| 8 | local | Provisional `.env` entries using the names now in `.env.example` (section 13); `git check-ignore .env` prints `.env` | S0 |
| 9 | you | Report PASS / FAIL / NOT RUN per item and any error text (no secrets) | — |
| 10 | — | **Wait.** I do nothing further until you explicitly authorize it | — |

Key generation (step 2; copied from the key-pair-auth page's OpenSSL form — run by you, not by me):

```bash
mkdir -p ~/.tracemind/snowflake && cd ~/.tracemind/snowflake
openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes-256-cbc -inform PEM -out tm_exporter_rsa_key.p8
openssl rsa -in tm_exporter_rsa_key.p8 -pubout -out tm_exporter_rsa_key.pub
openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes-256-cbc -inform PEM -out tm_query_rsa_key.p8
openssl rsa -in tm_query_rsa_key.p8 -pubout -out tm_query_rsa_key.pub
```

```powershell
icacls "$env:USERPROFILE\.tracemind" /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"
```

Compute each **public** key's fingerprint and one-line body; only `*.pub` material is ever pasted anywhere, `*.p8` never leaves the folder, a different passphrase per key, kept in your password manager:

```bash
openssl rsa -pubin -in tm_exporter_rsa_key.pub -outform DER | openssl dgst -sha256 -binary | openssl enc -base64
grep -v -- '-----' tm_exporter_rsa_key.pub | tr -d '\n'; echo
```

## 4. Documentation consulted (2026-10-29) and what it establishes

**Naming note:** S1–S45 are *documentation source* ids used only in this guide; they are unrelated to the M3 slices S1–S5, and the ledger ids in section 11 (F1, T1, M3a …) are local to that table and unrelated to the follow-ups F1–F23 or the threat ids T1–T28 of other documents. All pages are on docs.snowflake.com, read 2026-10-29 (S1–S38) and 2026-11-02 (S39–S44 opened; S45 **not opened**, search-result summaries only). **Pages are read through a summarizing fetch tool; quoted fragments are as returned.** This is documentation review, not a test.

| ID | URL |
|---|---|
| S1 | https://docs.snowflake.com/en/sql-reference/sql/create-resource-monitor |
| S2 | https://docs.snowflake.com/en/user-guide/resource-monitors |
| S3 | https://docs.snowflake.com/en/sql-reference/sql/alter-resource-monitor |
| S4 | https://docs.snowflake.com/en/sql-reference/sql/alter-warehouse |
| S5 | https://docs.snowflake.com/en/sql-reference/sql/create-warehouse |
| S6 | https://docs.snowflake.com/en/user-guide/warehouses-overview |
| S7 | https://docs.snowflake.com/en/sql-reference/sql/alter-account |
| S8 | https://docs.snowflake.com/en/sql-reference/sql/create-user |
| S9 | https://docs.snowflake.com/en/user-guide/admin-user-management |
| S10 | https://docs.snowflake.com/en/sql-reference/sql/alter-user |
| S11 | https://docs.snowflake.com/en/sql-reference/sql/alter-user-add-key-pair |
| S12 | https://docs.snowflake.com/en/sql-reference/sql/alter-user-modify-key-pair |
| S13 | https://docs.snowflake.com/en/sql-reference/sql/alter-user-remove-key-pair |
| S14 | https://docs.snowflake.com/en/sql-reference/sql/show-user-key-pairs |
| S15 | https://docs.snowflake.com/en/user-guide/key-pair-auth |
| S16 | https://docs.snowflake.com/en/sql-reference/sql/grant-role |
| S17 | https://docs.snowflake.com/en/sql-reference/sql/grant-privilege |
| S18 | https://docs.snowflake.com/en/user-guide/security-access-control-privileges |
| S19 | https://docs.snowflake.com/en/user-guide/security-access-control-overview |
| S20 | https://docs.snowflake.com/en/user-guide/security-access-control-considerations |
| S21 | https://docs.snowflake.com/en/sql-reference/sql/merge |
| S22 | https://docs.snowflake.com/en/sql-reference/transactions |
| S23 | https://docs.snowflake.com/en/sql-reference/sql/begin |
| S24 | https://docs.snowflake.com/en/sql-reference/sql/delete |
| S25 | https://docs.snowflake.com/en/sql-reference/sql/create-table |
| S26 | https://docs.snowflake.com/en/sql-reference/sql/create-view |
| S27 | https://docs.snowflake.com/en/sql-reference/constraints-overview |
| S28 | https://docs.snowflake.com/en/sql-reference/parameters |
| S29 | https://docs.snowflake.com/en/sql-reference/sql/drop-database |
| S30 | https://docs.snowflake.com/en/sql-reference/sql/drop-role |
| S31 | https://docs.snowflake.com/en/user-guide/data-time-travel |
| S32 | https://docs.snowflake.com/en/user-guide/network-policies |
| S33 | https://docs.snowflake.com/en/user-guide/security-authentication-overview |
| S34 | https://docs.snowflake.com/en/user-guide/admin-trial-account |
| S35 | https://docs.snowflake.com/en/sql-reference/account-usage/warehouse_metering_history |
| S36 | https://docs.snowflake.com/en/sql-reference/functions/warehouse_metering_history |
| S37 | https://docs.snowflake.com/en/sql-reference/functions/regexp_like |
| S38 | https://docs.snowflake.com/en/migrations/aim-for-datawarehouses/data-migration-validation/required-privileges |
| S39 | https://docs.snowflake.com/en/sql-reference/sql/use-secondary-roles |
| S40 | https://docs.snowflake.com/en/sql-reference/functions/current_secondary_roles |
| S41 | https://docs.snowflake.com/en/sql-reference/sql/show-warehouses |
| S42 | https://docs.snowflake.com/en/sql-reference/sql/show-resource-monitors |
| S43 | https://docs.snowflake.com/en/user-guide/admin-account-identifier |
| S44 | https://docs.snowflake.com/en/user-guide/cost-understanding-overall (credit price is "see the Snowflake Pricing Guide"; no Snowsight path stated) |
| S45 | https://docs.snowflake.com/en/user-guide/cost-exploring-overall and https://docs.snowflake.com/en/user-guide/billing-usage-statement (**seen only as search-result titles and summaries, pages not opened**: `Admin » Cost management`, `Admin » Billing`, view `USAGE_IN_CURRENCY_DAILY`) |

| # | Item | Status | Source and detail |
|---|---|---|---|
| 1 | `CREATE RESOURCE MONITOR [IF NOT EXISTS]` with `CREDIT_QUOTA`, `FREQUENCY`, `START_TIMESTAMP`, `TRIGGERS ON n PERCENT DO NOTIFY / SUSPEND / SUSPEND_IMMEDIATE` | VERIFIED | S1: "can only be executed by account administrators" (the role is not named there); S2 names `ACCOUNTADMIN` ("Only users with the ACCOUNTADMIN role can create a resource monitor"); a frequency requires a start; usage resets 00:00 UTC; ≤5 `NOTIFY` per monitor; thresholds above 100 allowed |
| 2 | `FREQUENCY = NEVER` | value VERIFIED (S1); behaviour on a **trial** UNVERIFIED | no page read describes trials and monitors |
| 3 | Resource monitors on trial accounts at all | **UNVERIFIED** | S34 does not mention them, and no page I read links the two |
| 4 | `ALTER WAREHOUSE … SET RESOURCE_MONITOR`; assignment is `ACCOUNTADMIN`-only | VERIFIED | S4 |
| 5 | `ALTER RESOURCE MONITOR … SET CREDIT_QUOTA`; `TRIGGERS` not additive | syntax VERIFIED; the **privilege** is not stated (UNVERIFIED) | S3 |
| 6 | `ALTER ACCOUNT SET RESOURCE_MONITOR = <name>`; controls all virtual warehouses; "does not impact any of the Snowflake-provided warehouses" | syntax VERIFIED; privilege for that clause UNVERIFIED | S7; one account-level monitor per account (S2) |
| 7 | Monitor limits: includes cloud-services credits **without** the daily 10 % adjustment; "not intended for setting precise limits"; suspension "may take some time … even when the action is Suspend Immediate"; warehouses only (no serverless features or AI services); **"A warehouse-level resource monitor can monitor, but cannot suspend, credit usage by cloud services"**; notifications disabled by default | VERIFIED | S2 (found by the independent review) |
| 8 | `NOTIFY` goes "to all account administrators with notifications enabled"; `NOTIFY_USERS` need verified e-mail | VERIFIED | S1, S2 |
| 9 | `CREATE WAREHOUSE [IF NOT EXISTS]` parameters used; `AUTO_SUSPEND` under 30 or not a multiple of 30 "might not result in the expected behavior"; clusters >1 need Enterprise | VERIFIED | S5 |
| 10 | X-Small 1 credit/hour, per-second billing, **60-second minimum each start** | VERIFIED | S6 |
| 11 | Warehouse privileges: `USAGE` runs queries; `OPERATE` changes state; `MODIFY` alters properties; `MONITOR` views usage | VERIFIED | S18 |
| 12 | `CREATE USER … TYPE = SERVICE`; SERVICE users "cannot log in using a password", cannot enroll in MFA; `DEFAULT_ROLE` does not grant the role; role/warehouse/namespace not checked at creation | VERIFIED | S8, S9. **Snowsight access for SERVICE users: UNVERIFIED** |
| 13 | "Each securable object is owned by a single role, which by default is the role used to create the object" → `USERADMIN` owns the roles and users it creates | rule VERIFIED (S19); the consequence for `USERADMIN` is INFERRED | confirm with the `owner` column (1d) |
| 14 | `GRANT ROLE … TO USER` needs `OWNERSHIP` of the role or `MANAGE GRANTS` | VERIFIED | S16 |
| 15 | "if a custom role is not assigned to SYSADMIN through a role hierarchy, the system administrators cannot manage the objects owned by the role"; Snowflake recommends placing custom roles under `SYSADMIN` | VERIFIED | S19, S20 → **`SYSADMIN` has no custom-role administration unless the role is granted to it; here it is not** |
| 16 | `ALTER USER … ADD KEY PAIR <name> PUBLIC_KEY, ROLE_RESTRICTION, DAYS_TO_EXPIRY, COMMENT`; ≤10 per user; needs `MODIFY PROGRAMMATIC AUTHENTICATION METHODS` on the user (S15 also allows `OWNERSHIP`); `ROTATE` keeps the old key 24 h by default; `MODIFY KEY PAIR … SET DISABLED` reversible; `REMOVE KEY PAIR` permanent; `SHOW USER KEY PAIRS` columns | VERIFIED | S11–S15. **Fingerprint text format: UNVERIFIED. How the Python connector conveys the restricted role in the JWT: UNVERIFIED** (S15 does not say). Rotation (page alter-user-rotate-key-pair, opened 2026-11-02): "Comment, expiration duration, and role restriction are inherited from the rotated key pair"; the old key is kept as `<name>_ROTATED_<epoch_ms>`; `EXPIRE_ROTATED_KEY_PAIR_AFTER_HOURS` defaults to 24, `0` disables it immediately; whether the new key's expiry *date* restarts a full 90 days is **not stated** |
| 17 | `GRANT … ON FUTURE TABLES/VIEWS IN SCHEMA …`; `MANAGE GRANTS` required; schema-level overrides database-level; "Future grants are not applied when renaming or swapping a table" | VERIFIED | S17. **Applies to objects created by any role: UNVERIFIED** (page silent) |
| 18 | Querying a view needs no privilege on its underlying tables: "the role can use the view even if the role does not have privileges on the underlying table(s)"; the view's creator needs `SELECT` on them | VERIFIED (documentation only; **not tested**) | S26 |
| 19 | Creating a **temporary** table does not require `CREATE TABLE` | VERIFIED | S25 |
| 20 | `MERGE` syntax, `WHEN NOT MATCHED … THEN INSERT`; with duplicate source rows and only that clause "the target gets one copy of the row for each copy in the source"; inserts are deterministic | VERIFIED | S21 |
| 21 | **Privileges `MERGE` requires** | **UNVERIFIED** | S21 and S18 state no `MERGE` privilege rule. S38 (Snowflake) grants `SELECT, INSERT, UPDATE, DELETE, TRUNCATE` to a migration role that runs `MERGE` but does **not** claim that is the minimum. Third-party pages say `INSERT`+`UPDATE`+`DELETE`. Nothing official supports or refutes `INSERT`+`SELECT` |
| 22 | `UPDATE`, `DELETE`, `MERGE` "hold locks that generally prevent them from running in parallel with other UPDATE, DELETE, and MERGE statements"; INSERT/COPY "often can run in parallel with other INSERT and COPY operations"; isolation is READ COMMITTED; locks are held until `COMMIT`/`ROLLBACK`; a blocked statement waits or times out | VERIFIED | S22. **Whether a blocked `MERGE` re-reads the target after the lock is released: UNVERIFIED** |
| 23 | `LOCK_TIMEOUT`: session parameter settable at Account » User » Session, default 43200 s (12 h); `STATEMENT_TIMEOUT_IN_SECONDS`: warehouse object parameter and a session parameter, default two days | VERIFIED | S28 |
| 24 | `PRIMARY KEY` and `UNIQUE` are "Optional, not enforced" on standard tables | VERIFIED | S27 (table cells) |
| 25 | `BEGIN`/`START TRANSACTION`, explicit `COMMIT`/`ROLLBACK`; `DELETE FROM … WHERE`, output column "number of rows deleted"; `REGEXP_LIKE` anchors both ends | VERIFIED | S23, S24, S37. S18: the `DELETE` privilege "enables executing a DELETE command on a table" (VERIFIED). **Whether a `DELETE … WHERE` also needs `SELECT`: UNVERIFIED** (S24 names no privilege) |
| 26 | `DROP DATABASE`: only the owner; `CASCADE` default; `UNDROP` within Time Travel; `DROP ROLE`: **"Ownership of any objects owned by the dropped role is transferred to the role that executes the DROP ROLE command"** and its grants are revoked; Time Travel default 1 day (Standard), up to 90 (Enterprise, permanent objects) | VERIFIED | S29, S30, S31. **Whether objects owned by another role block `DROP DATABASE`: UNVERIFIED** |
| 27 | User-level `NETWORK_POLICY`; creation by `SECURITYADMIN`; assignment needs user ownership + policy `USAGE`; user-level overrides account-level | VERIFIED | S32. **Whether it is enforced on key-pair logins: UNVERIFIED.** S33: long-lived credentials "must be mitigated with other security measures like network policies and a robust storage and rotation strategy" |
| 28 | Trial: 30 days or until the free balance is used, then the account is suspended; a card does not end the trial; upgrading does | VERIFIED | S34: "Users with the ACCOUNTADMIN role can track the remaining balance" using "a tile in the left navigation bar". **Not stated:** the credit amount, the sign-up user's role, monitors on trials, and **whether the tile shows dollars or credits** (the owner reports "$400 in credits") |
| 29 | `ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` latency up to 180 min (cloud services up to 6 h); the `INFORMATION_SCHEMA` table function needs `ACCOUNTADMIN` or `MONITOR USAGE` | VERIFIED | S35, S36. Which roles can read the account-usage views by default: UNVERIFIED |
| 30 | `ALTER USER … SET DISABLED = TRUE` (locks out, aborts running statements) | VERIFIED | S10 |
| 31 | Not read at all (so UNVERIFIED): user-level `ALTER USER … SET LOCK_TIMEOUT`, `REVOKE ROLE … FROM USER`, `DROP RESOURCE MONITOR`, `QUALIFY`, `FROM VALUES … COLUMN1` naming, `SHOW WAREHOUSES … ->> SELECT … FROM $1` | UNVERIFIED | each is flagged where it appears in the SQL |
| 32 | `USE SECONDARY ROLES { ALL \| NONE \| <role>, … }`; active secondary roles count towards privilege checks; `ALL` = every role granted to the user besides the primary, re-evaluated per statement; `NONE` = "authorization for all SQL actions is provided via the primary role"; only `CREATE` authorization is limited to the primary role. `CREATE USER … DEFAULT_SECONDARY_ROLES = { ('ALL') \| () }`, "Default: `ALL`" | VERIFIED | S39, S8. **What your existing sign-up user has, and the `ALTER USER` form for changing it: UNVERIFIED** (S8 does not mention `ALTER USER` for this property). Found by the independent review |
| 33 | `CURRENT_SECONDARY_ROLES()` returns JSON with `roles` and `value` | VERIFIED | S40. **Its output when `NONE` is active: UNVERIFIED** (not described) |
| 34 | `SHOW WAREHOUSES`/`SHOW RESOURCE MONITORS` list only objects on which the current role holds ≥1 privilege; `MANAGE GRANTS` "implicitly allows its holder to see every object" (SECURITYADMIN, ACCOUNTADMIN by default) | VERIFIED | S41, S42. `SHOW WAREHOUSES` columns name, state, size, auto_suspend (null = never), auto_resume, resource_monitor, owner VERIFIED; **`SHOW RESOURCE MONITORS` column names other than `level` UNVERIFIED** |
| 35 | Account identifier: preferred form `orgname-account_name`; the locator form (`xy12345.us-east-2.aws`) is "not recommended"; found in Snowsight via the account selector → **View account details** | VERIFIED | S43. Whether an identifier is sensitive: not stated |
| 36 | Credit price: "see the Snowflake Pricing Guide"; varies with account type and region; no Snowsight path for it is given | VERIFIED (as an absence) | S44. `Admin » Cost management`, `Admin » Billing` and `USAGE_IN_CURRENCY_DAILY`: from search summaries only (S45); whether they work on a trial: UNVERIFIED. The trial page (S34) names the balance tile but not its unit |

## 5. Ownership and privilege results (decision 4)

- **Who owns what (VERIFIED rule, row 13):** `SYSADMIN` owns the two databases, six schemas and the warehouse; `USERADMIN` owns the four custom roles and two users; tables and views created later are owned by **`TRACEMIND_MIGRATOR`**.
- **`SYSADMIN` and custom roles (VERIFIED, row 15):** it cannot manage objects owned by a custom role that is not in its hierarchy. Here none is. Consequence: **`SYSADMIN` cannot alter, grant on or drop the migrator's tables**, which is intended (it should not touch exported data). It can still drop databases it owns, subject to the UNVERIFIED point in row 26.
- **Why the best-practice hierarchy is not applied:** placing the roles under `SYSADMIN` would give every `SYSADMIN` the writer, reader, migrator and cleaner privileges. The lines are in script 01 Part C, commented out, with the reason.
- **Grants need `MANAGE GRANTS` or ownership (rows 14, 17):** `SECURITYADMIN` provides it for every grant in Part D; `USERADMIN` grants roles because it owns them.
- **Dropping a role transfers its tables to the dropper (row 26).** Script 99 section X2 warns before `TRACEMIND_MIGRATOR` is dropped.

## 6. `MERGE` for idempotent export (decision 7)

### 6.1 The exact operation (as the M3 plan describes it, made concrete)

```sql
MERGE INTO <db>.TELEMETRY.EVENTS AS t
USING (   -- deduplicated bounded batch: inline bound VALUES, or a session TEMPORARY table
    SELECT ... FROM ( SELECT ..., ROW_NUMBER() OVER (PARTITION BY project_id, event_id ORDER BY ...) AS rn FROM <batch> ) WHERE rn = 1
) AS s
ON t.project_id = s.project_id AND t.event_id = s.event_id
WHEN NOT MATCHED THEN INSERT (...) VALUES (...);          -- no WHEN MATCHED clause at all
```

### 6.2 Minimum privileges, by clause

| Clause | Privilege on the target | Status |
|---|---|---|
| `MERGE INTO <target>` / `ON t.… = s.…` (reads target columns) | `SELECT` | INFERRED (no official `MERGE` privilege rule) |
| `WHEN NOT MATCHED THEN INSERT` | `INSERT` ("Enables executing an INSERT command on a table", S18) | INFERRED |
| (no `WHEN MATCHED … UPDATE`) | **no `UPDATE`** | follows from the clause being absent |
| (no `WHEN MATCHED … DELETE`) | **no `DELETE`** | follows from the clause being absent |
| source = inline bound values | none | INFERRED |
| source = session `TEMPORARY` table | no `CREATE TABLE` (VERIFIED, row 19); `USAGE` on the schema | VERIFIED / INFERRED |

**Minimum: `INSERT` + `SELECT` on the target tables. This is an inference and is UNVERIFIED by any official page** (row 21). The writer is therefore granted exactly that and no more; `DELETE` is not granted and is not needed by this implementation. **Test MERGE-1 (script 02 §4.1) must PASS before the exporter is trusted.** If it FAILS, M3 stops and the owner decides; no grant is added automatically.

### 6.3 What retries and concurrency need (design requirements for M3, not claims of safety)

1. **Single exporter, enforced in PostgreSQL** (the ADR-0002 lease/advisory-lock pattern): the primary guard. Snowflake cannot enforce uniqueness (row 24).
2. **Deduplicate every batch** before the `MERGE` and again in the `USING` subquery: with only `WHEN NOT MATCHED … INSERT`, duplicate source rows each become a target row (row 20). Test MERGE-3a demonstrates it, MERGE-3b the fix.
3. **A key makes a retry a no-op:** re-running a batch finds the key already in the target, so nothing is inserted (MERGE-2). *Not yet run.*
4. **Short `LOCK_TIMEOUT` on every session** (60–120 s): the default is 12 h (row 23), which would hang a blocked exporter. The application sets it; script 01 contains an optional user-level default, commented out.
5. **`MERGE` takes locks that stop it running in parallel with other `MERGE`/`UPDATE`/`DELETE` statements** (row 22): a second concurrent exporter would be made to wait. What happens *after* the wait (does it re-read the target?) is UNVERIFIED; **test MERGE-4a/4b is the demonstration**, using an explicit transaction held open in one worksheet while a second runs the same `MERGE`. If it FAILS, `MERGE` alone is not concurrency-safe and only guard 1 protects the data: **report, do not patch with grants.**
6. **Duplicate audit after every export** (the writer has `SELECT`): `GROUP BY project_id, event_id HAVING COUNT(*) > 1` must return nothing; a hit puts the exporter in a degraded state.
7. **No table rename or swap in migrations** (future grants would not apply, row 17) unless the migration re-grants.
8. **If `WHEN MATCHED THEN UPDATE` is ever wanted** (for example correcting an incident summary) it would need `UPDATE` (INFERRED) and is a new owner decision. Today every exported row and incident version is insert-only.

### 6.4 Why `INSERT … WHERE NOT EXISTS` is not substituted

Row 22: INSERT and COPY statements generally do not serialize against each other, so two concurrent `INSERT … WHERE NOT EXISTS` statements can both find the key absent and both insert it. No design or test showing otherwise exists. It stays withdrawn unless a later, tested design is approved.

### 6.5 Decision O7: the fallback is conditional on the tests (nothing is decided in advance)

| Test result | Consequence |
|---|---|
| MERGE-1 passes (insert-only `MERGE` runs with `INSERT`+`SELECT` only) | Proceed to MERGE-2 onward; the inference becomes a tested fact for this statement shape on this account |
| MERGE-1 fails | Stop. The error text goes to the owner. **No `UPDATE`, no `DELETE`, no other grant is added automatically.** A weaker alternative would need its own demonstrated design and an explicit decision |
| MERGE-4a does not wait (no serialization) or MERGE-4b adds duplicates | `MERGE` alone is not concurrency-safe. Only the PostgreSQL single-exporter guard (6.3 item 1) protects the data; the owner decides whether to add deduplicating reader views as defense in depth. No grant changes |
| MERGE-4a waits and times out, and MERGE-4b adds 0 rows | The documented lock behaviour is confirmed for this statement shape. Guards 1, 2, 4 and 6 of 6.3 still apply |

## 7. Test isolation and safe cleanup (decision 3)

**Marker.** Every test run uses a unique id `tmtest-YYYYMMDDTHHMMSSZ-xxxxxxxx` (UTC time, 8 lower-case hex characters), generated by the test harness and written to the harness log with the number of rows it inserted. The id is stored in a nullable `test_run_id` column on each exported table; **real data carries NULL**. (The column name and the refuse-to-write rules are M3 design items in the M3 plan, not yet implemented.)

**Isolation by default (non-destructive).** Test data lives only in `TRACEMIND_TEST`. Tests read only rows with their own run id. Stale rows are left alone. M3 code must refuse to write a `tmtest-` run id when connected to `TRACEMIND_DEV`, and refuse a non-test id when connected to `TRACEMIND_TEST`.

**Cleanup is optional and human-run**, in six layers (script 99 section T). **They are not fully independent and not all are enforced by Snowflake:** development data is protected by layers 0 and 1 *together* (layer 1 depends on layer 0); another test run's rows are protected only by layer 5 (a well-formed but wrong run id passes layer 3); layers 2, 4 and 5 are checks a human must read.
0. **Secondary roles off.** *Found in my own review of this revision and confirmed by the independent review.* VERIFIED (use-secondary-roles): the privileges of active secondary roles count towards authorization, and `ALL` means every role granted to the user besides the primary one. Your user holds `TRACEMIND_MIGRATOR`, which **owns the development tables**. VERIFIED (create-user): the default for a user created with `CREATE USER` is `ALL`; what your existing sign-up user has is UNVERIFIED (read it in the preflight). A session whose primary role is the cleaner but whose secondary roles are on could therefore delete development rows through the migrator, defeating layer 1. Every cleanup session starts with `USE SECONDARY ROLES NONE;` and a `CURRENT_SECONDARY_ROLES()` check. This layer is a **procedure**, not a privilege. **Open decision O9** (section 12) would make it the default instead.
1. **Privileges.** The role is `TRACEMIND_TEST_CLEANER`, which has **no privilege of any kind on `TRACEMIND_DEV`** (not even `USAGE`) and only `SELECT`+`DELETE` on tables in the two `TRACEMIND_TEST` data schemas. With layer 0 satisfied, a delete aimed at development data fails by privilege, not by care. (The migrator *can* read and delete development tables because it owns them; that is why it is not the cleanup role.)
2. **Full qualification:** every statement names `TRACEMIND_TEST`.
3. **Marker format guard:** a read-only `REGEXP_LIKE` check must return `RUN_ID_FORMAT_OK`, **and the `DELETE` statement itself repeats the format test on the column**, so even a mistyped id cannot match a row that is not a test row.
4. **Leak check:** the marker must appear **nowhere** in `TRACEMIND_DEV` (count 0, read as the migrator with secondary roles off) or cleanup does not proceed.
5. **Count and transaction:** `BEGIN`, `DELETE … WHERE test_run_id = '<id>'`, compare "number of rows deleted" with the number you wrote down, `COMMIT` only if equal, otherwise `ROLLBACK`.

**Exact model of `TRACEMIND_TEST_CLEANER`.**

| Aspect | Value |
|---|---|
| Created and owned by | `USERADMIN` (the creating role owns it, VERIFIED rule) |
| Held by | your user only; never a service user, never `SYSADMIN`, never another role; no key pair |
| Owns | nothing (the test tables are owned by `TRACEMIND_MIGRATOR`) |
| Warehouse | `USAGE` only |
| `TRACEMIND_TEST` | `USAGE` on the database and on the `TELEMETRY` and `KNOWLEDGE` schemas; `SELECT` and `DELETE` on future tables in those two schemas |
| `META` schema, `TRACEMIND_DEV`, any other database | **nothing** |
| Never granted | `TRUNCATE`, `UPDATE`, `INSERT`, any `CREATE`, `OWNERSHIP`, `MODIFY`, `OPERATE`, `MONITOR`, `MANAGE GRANTS` |
| Limit | inside `TRACEMIND_TEST` it can delete **any** row of a table; the run-id guards narrow it |
| Fail-safe direction | if future grants do not reach migrator-created tables (UNVERIFIED), the role has no privilege there: cleanup becomes impossible, never over-permissive |
| Unverified | whether a `DELETE … WHERE` also needs `SELECT`; the secondary-role setting of your existing user and the `ALTER USER` form for it; what `CURRENT_SECONDARY_ROLES()` shows when `NONE` is active |
| Also true | as table owner the **migrator holds every privilege on the tables it created, `UPDATE`, `DELETE` and `TRUNCATE` included, in both databases, and can grant them onward**; it is human-held and has no key pair, but it is the reason the cleanup role exists and the reason layer 0 matters |

Not provided: `TRUNCATE`, a delete without a run-id predicate, deletes by time or pattern, any statement against development data. **Decision O3 is applied: the role is approved.** If you later reject it, cleanup falls back to the migrator with layers 0 and 2–5, and layer 1 is lost.

## 8. Costs

### 8.1 Documented
Trial: 30 days or until the free balance is used, then the account is suspended; a card does not end the trial, upgrading does (S34). X-Small: 1 credit/hour, 60-second minimum per start (S6). The monitor counts cloud-services credits without the daily adjustment (S2). Usage views lag up to 3 h, cloud services up to 6 h (S35).

### 8.2 What you must read yourself (not stated in any page read)
Your **credit price**, edition, region, sign-up date, the remaining balance **and the unit its tile shows**, and the default Time Travel retention. The owner reports "$400 in credits"; the trial page (S34) says the balance is shown in a left-navigation tile but **does not say whether it shows dollars or credits**, so record what the tile actually shows. If it is dollars, convert the monitor's credits with `20 × price per credit`; if it is credits, compare directly. I have not verified any price; do not rely on a number from me. `Admin » Cost management` (credits consumed) and `Admin » Billing` / `USAGE_IN_CURRENCY_DAILY` (currency) come from search-result summaries only (S45) and are UNVERIFIED on a trial.

### 8.3 Export cadence (decision 9) — arithmetic and the decision applied
**Rate used.** X-Small = 1 credit per hour = **0.000278 credits/second** (1/3600). The warehouses page prints "0.0003 credits/second", a rounded figure; revision 3 of this guide used the rounded one and so overstated every figure by about 8 %. S6 says the 60-second minimum applies "each time the warehouse starts"; it does **not** say how idle time before auto-suspend is counted. Because the warehouse is running while it idles, the idle tail is assumed to be billed.

**Three scenarios per isolated use** (all **estimates from the documented rate, not measurements**):

| Scenario | Billed seconds per use | Credits per use |
|---|---|---|
| optimistic: ~5 s run + 60 s auto-suspend idle | ~65 | ≈ 0.018 |
| **base: short run + idle tail + up to 30 s suspend-check slack, rounded** | 120 | ≈ 0.033 |
| pessimistic: a ~4-minute run + 60 s idle tail | 300 | ≈ 0.083 |

Worst case in every row: a backlog exists at every tick and nothing else uses the warehouse (no reader queries, migrations or tests):

| Minimum interval | Uses/day | Base: credits/day | Base: per 30 days | Optimistic: per 30 days | Pessimistic: per 30 days |
|---|---|---|---|---|---|
| 5 minutes | 288 | 9.6 | 288 | 156 | 720 |
| 15 minutes | 96 | 3.2 | 96 | 52 | 240 |
| 1 hour (earlier floor, withdrawn) | 24 | 0.8 | 24 | 13 | 60 |
| **2 hours (floor, decision O4)** | 12 | 0.4 | **12** (60 % of 20) | 6.5 | **30** (150 %) |
| **4 hours (default, decision O4)** | 6 | 0.2 | **6** (30 %) | 3.3 | **15** (75 %) |
| 24 hours | 1 | 0.033 | 1 | 0.5 | 2.5 |

**What this says honestly.** At the default the exporter alone is expected to use roughly 3–15 credits per 30 days (base 6). At the 7,200 s floor it is roughly 6–30 (base 12): in the pessimistic case the floor **alone** exceeds the 20-credit allowance, so the floor reduces the risk but does **not** guarantee the allowance fits. These figures cover the exporter only, not reader queries, migrations, canaries or tests. Run lengths are unknown until M3 measures them.

**Decision O4 as applied (owner confirmation pending; the 7,200 s floor is the assistant's change):** `TRACEMIND_SNOWFLAKE_ENABLED=false` by default; `…_EXPORT_MIN_INTERVAL_SECONDS` default **14400**; a code-level floor of **7200** below which start-up is refused with a clear error (the earlier 3,600 s floor would use ≈24 credits per 30 days in the base case, more than the whole allowance, so it was raised); export only when a backlog exists; during development prefer a manual trigger over a schedule; reader calls and live tests count against the soft budget (`…_SOFT_BUDGET_CREDITS`, default 15 = 75 % of the allowance). A floor limits the rate; it is not a budget, so the monitor and your review still apply. **An alternative for you to consider:** a floor equal to the default (14,400 s) leaves no configurable room below it; the 7,200 s floor was chosen only to keep the interval configurable.

## 9. Controls — and why none is a guarantee (decision 8)

| # | Layer | Does | Does **not** |
|---|---|---|---|
| 1 | The trial itself | Snowflake suspends the account at 30 days or when the balance is used (S34) | Is not under TraceMind's control. Do not upgrade or add payment during M3 without an explicit decision: on-demand billing has no such ceiling |
| 2 | Warehouse settings | X-Small, 60 s auto-suspend, created suspended, one cluster, 300 s statement timeout, 60 s queue timeout | A loop of short queries still consumes credits; the statement timeout is also a session parameter and how the two values interact is UNVERIFIED (S28), so it is only a backstop |
| 3 | Monitor `TRACEMIND_WH_MONITOR` | Notifies at 10/50/75 %, suspends at 90 %, cancels at 100 % | See below |
| 4 | Optional account-level monitor (Part E) | Backstop over all warehouses (S7) | Same lag; suspends everything; one per account; owner decision O1 |
| 5 | Role design | No TraceMind role can resize, suspend, detach the monitor or view usage | A stolen key can still run permitted queries until something acts |
| 6 | Application limits (M3) | Export off by default, minimum interval, row/time caps, concurrency cap, soft budget from **TraceMind's own call log** | Fail if the application is wrong; the runtime roles cannot read Snowflake metering |
| 7 | **Your review** | Script 02 §3 and Snowsight Cost management after every working session and at least weekly | Views lag 3–6 h: after the fact |

**Monitor limitations (all VERIFIED unless marked):** not intended for precise limits; suspension may lag even for `SUSPEND_IMMEDIATE`; `SUSPEND` waits for running queries; **warehouses only** — serverless features and AI services are not covered; storage is not a warehouse credit; **"A warehouse-level resource monitor can monitor, but cannot suspend, credit usage by cloud services"** (S2), so cloud-services credits can keep accruing after a suspension and are not stopped by this monitor; they are counted without the daily adjustment and "subsequent queries … can still result in additional cloud services costs" after a suspend; **trial behaviour is UNVERIFIED** (no page mentions it, `FREQUENCY = NEVER` included); a frequency of `NEVER` means the 20 credits is a total for the monitor's life, so raising it is a deliberate `ALTER`.

**Notification configuration.** `NOTIFY` triggers reach "all account administrators with notifications enabled"; notifications are disabled by default; a verified e-mail is required (S1, S2). Steps: verify the e-mail; enable resource-monitor notifications in your profile; then wait for the **10 % canary** — until an e-mail arrives, treat notifications as **not working** (N1 stays NOT RUN). The trigger count stays within the five-`NOTIFY` limit.

**Metering delay.** Account-usage metering lags 3–6 h (S35); the monitor's own counter is read with `SHOW RESOURCE MONITORS` (its refresh interval is not documented). Neither is real-time.

**Costs no warehouse monitor covers:** storage; serverless features; AI services; any other warehouse unless it is attached to a monitor (the preflight lists them); cloud-services usage after a suspension (see above); an upgrade to paid billing; the trial balance, which the monitor does not measure (and whose unit is not stated).

## 10. Network policy deferred (decision 5): residual risk and review trigger

**Residual risk.** Until a policy exists, anyone holding a service user's **private key file and its passphrase** can attempt to connect from any IP address. Mitigations in place: encrypted keys outside the repository and any synced folder; a passphrase per key; a folder ACL limited to your account; **role-restricted** keys (a stolen reader key cannot act as the writer; UNVERIFIED how the connector conveys the role); **90-day expiry**; least-privilege roles; the monitor and trial ceiling bounding spend; synthetic, redacted data only. Not mitigated: reading whatever the stolen role may read, and spending credits until a control acts. Snowflake's authentication overview states key-pair "doesn't require" additional measures, which "can result in less secure authentication", and lists workload identity federation as the "preferred option" for service-to-service use (S33): decision 2 (key pairs) stands, and that alternative is noted only for the review below.

**Concrete review trigger (follow-up F20, recorded in `PLAN.md` under decision O2).** Re-open this decision **before the first of**: (a) any non-synthetic data enters Snowflake; (b) the account is upgraded from trial; (c) TraceMind runs from any machine other than the development machine, including CI; (d) M3 closes; (e) a key or passphrase may have been exposed. At that review, first **test** on a throwaway service user whether a user-level policy restricts key-pair logins (UNVERIFIED), keeping an administrator login path that the policy does not cover.

### 10.1 What a 90-day key expiry means (decision O5; the owner asked for this explanation)

| Aspect | Implication | Status |
|---|---|---|
| Security | A leaked key (and its passphrase) stops working within 90 days at most, and the owner practises rotation instead of keeping a key forever. It does not help against a leak that is used before it expires | design reasoning |
| The trial | The account is suspended at 30 days or when the balance is used (S34), so **a 90-day expiry cannot take effect during the trial** and the day-75 reminder only matters after an upgrade | VERIFIED trial rule; consequence INFERRED |
| What happens at expiry | `SHOW USER KEY PAIRS` has a `status` of `ACTIVE`, `EXPIRED` or `DISABLED` (S14). An expired key can no longer authenticate; the exporter and the query service would fail to connect and M3's degraded mode must report it while ingestion continues | status values VERIFIED; failure behaviour INFERRED |
| Who can see the date | `expires_at` is shown by `SHOW USER KEY PAIRS FOR USER <u>`, which needs `MODIFY PROGRAMMATIC AUTHENTICATION METHODS` on another user (S14) and which the service users themselves cannot run interactively. The application cannot read it. The owner/administrator checks it; M3's connectivity check can only warn if M3 is given the date out of band | VERIFIED privilege; "application cannot" INFERRED |
| Changing it | Not possible afterwards: `MODIFY KEY PAIR` cannot change the key, role restriction or expiry; the key pair must be removed and added again (S12). Decide the number **before** Part C | VERIFIED |
| Rotation | `ALTER USER <u> ROTATE KEY PAIR <name> PUBLIC_KEY = '<new>'` (needs `MODIFY PROGRAMMATIC AUTHENTICATION METHODS`). The new key inherits the comment, expiration duration and role restriction; the old key stays valid for 24 hours by default (`EXPIRE_ROTATED_KEY_PAIR_AFTER_HOURS`, `0` = immediately) and is kept as `<name>_ROTATED_<epoch_ms>` | VERIFIED (alter-user-rotate-key-pair) |
| After rotation | Whether the new key's expiry *date* restarts a full 90 days is **not stated**: read `expires_at` afterwards | UNVERIFIED |
| Procedure (draft, F22) | Generate a new encrypted key pair locally (step 2, new file names) → rotate → update the `.env` path and passphrase → run the connectivity check → confirm the old key is rejected after the grace period. After a suspected leak, rotate with `EXPIRE_ROTATED_KEY_PAIR_AFTER_HOURS = 0`, or disable/remove the key (script 99 R2/X1) | draft |
| Limits | A user can hold at most 10 key pairs; rotated-out pairs in their grace period count (S11) | VERIFIED |
| Cost of getting it wrong | A forgotten rotation is an outage, not a security breach: the safe failure direction | design reasoning |

## 11. Verification ledger — every item starts as **NOT RUN**

Fill the *Status* column yourself with PASS, FAIL or NOT RUN. PASS only if you ran it and the result matched. A test with a documented expectation (VERIFIED/INFERRED) is still NOT RUN until it is run.

| ID | Check | Where | Status |
|---|---|---|---|
| G0 | Preflight read: account, AWS region, roles held | script 00 | NOT RUN |
| G1 | `USE ROLE ACCOUNTADMIN` works (**otherwise STOP**) | script 00 gate | NOT RUN |
| G2 | No pre-existing `TRACEMIND%` object | script 00 | NOT RUN |
| G3 | Other warehouses / existing monitors noted | script 00, 02 §1j | NOT RUN |
| N0 | E-mail verified; notifications enabled | Snowsight | NOT RUN |
| K0 | Key files exist outside the repo; `.p8` header says ENCRYPTED; folder ACL restricted | local | NOT RUN |
| A1 | Warehouse X-Small, auto-suspend 60, suspended, no monitor yet | 02 1d, 1g | NOT RUN |
| B1 | **GATE-B:** monitor attached, warehouse suspended | 02 GATE-B | NOT RUN |
| B2 | Monitor: quota 20, `NEVER`, triggers as designed; trial behaviour recorded | 02 1g | NOT RUN |
| C1 | Role holders as designed; migrator/cleaner only your user; no `SYSADMIN` grant | 02 1c | NOT RUN |
| C2 | Owners as expected (USERADMIN / SYSADMIN) | 02 1d | NOT RUN |
| C3 | Users `SERVICE`, no password | 02 1e | NOT RUN |
| C4 | Key pairs ACTIVE, role-restricted, ~90 days, fingerprint equals local | 02 1f | NOT RUN |
| D1 | Role privileges exactly as designed | 02 1a, 1b | NOT RUN |
| D2 | Warehouse `USAGE` for exactly four roles; no `MODIFY`/`OPERATE`/`MONITOR` | 02 1h | NOT RUN |
| D3 | Cleaner has **nothing** on `TRACEMIND_DEV` | 02 1a, 1i | NOT RUN |
| D4 | Cleaner is granted to your user only and to no role | 02 1c | NOT RUN |
| N1 | The 10 % canary e-mail actually arrives | e-mail | NOT RUN |
| X1 | Expected-to-fail attempts (writer, reader, cleaner) all fail | 02 §2 | NOT RUN |
| S0 | `git check-ignore .env` prints `.env`; no key material in the repo | local | NOT RUN |
| F1 | Future grants reached migrator-created objects (FUT-1) | 02 §4.0 | NOT RUN |
| M1 | MERGE-1: insert-only MERGE succeeds with `INSERT`+`SELECT` only | 02 §4.1 | NOT RUN |
| M2 | MERGE-2: re-run inserts 0 rows | 02 §4.1 | NOT RUN |
| M3a/b | Duplicate source rows duplicate; dedupe fixes | 02 §4.1 | NOT RUN |
| M4a/b | Concurrent MERGE waits, times out, then adds 0 rows | 02 §4.2 | NOT RUN |
| M5 | Interrupted batch leaves nothing partial | M3 fault test | NOT RUN |
| M6 | Writer cannot UPDATE / DELETE / TRUNCATE | 02 §4.2 | NOT RUN |
| M7 | Duplicate audit clean | 02 §4.2 | NOT RUN |
| R1 | Reader selects the view without a table grant; cannot select the table | 02 §4.3 | NOT RUN |
| L1 | No `tmtest-` row in `TRACEMIND_DEV` | 02 §4.4 | NOT RUN |
| SR0 | Your user's `DEFAULT_SECONDARY_ROLES` value read from `DESC USER` (preflight step 2) and recorded | 00 step 2 | NOT RUN |
| SR1 | `USE SECONDARY ROLES NONE` leaves no secondary role in a cleaner session (`CURRENT_SECONDARY_ROLES()`) | 02 §2, script 99 T-1 | NOT RUN |
| SR2 | CLEAN-2: with secondary roles ALL the cleaner session can read development (prediction); with NONE it cannot | 02 §4.4 | NOT RUN |
| T1 | Cleanup dry-run: guards return OK, count equals the log | script 99 §T | NOT RUN |

The application-side check (connect with the real keys, request the restricted role, confirm key expiry, confirm no credential in logs) is **not built**; it is part of M3 S2 and only after authorization.

## 12. Items still open after applying O1–O8

| # | Item | State |
|---|---|---|
| O1 | Optional **account-level monitor** (Part E): yes/no, and the quota | **Deferred by the owner** until the preflight (00 step 5) shows what other compute exists. No number is offered. Part E remains commented out |
| O7 | What to do if MERGE-1 or MERGE-4 fails | **Conditional** (6.5). Never an automatic grant; never `DELETE` |
| O8 | Credit price, edition, region, trial balance and its unit, trial dates | **Information the owner must supply** (section 15, step 6) |
| O9 | **Secondary roles on your own user.** VERIFIED: the `CREATE USER` default is `ALL`; your sign-up user's value is unknown until the preflight `DESC USER` step. With `ALL`, the cleaner's boundary (and every runtime-role test) holds only if each session first runs `USE SECONDARY ROLES NONE`. Options: (a) rely on that procedure, as the files now do; (b) set your own user's `DEFAULT_SECONDARY_ROLES` to `()` (syntax verified for `CREATE USER`; the `ALTER USER` form is UNVERIFIED) — this changes your own user, so it is yours to do; (c) grant the cleaner to a separate human user that holds no migrator and no administrative role (a real privilege boundary, but one more login to secure) | **Open owner decision.** Not decided here; the files assume (a) |
| O4 | The 7,200 s floor and the recomputed cost figures (section 8.3) | **Assistant's proposal, confirmation pending.** A floor equal to the default (14,400 s) is the alternative |
| — | Assumption of this revision: the owner's "decisions have been made" was read as acceptance of the recommendations in the previous report | Say so if any of O2–O6 was decided differently; each is a one-line change recorded in section 0.0 |
| — | Everything marked UNVERIFIED in section 4, and every NOT RUN in section 11 | Only live testing after authorization can change these |

## 13. Changes applied to files outside the five preparation files (2026-11-02)

All are documentation or example configuration; **no application code changed**, and no variable listed is read by any code yet.

- **`.env.example`:** removed `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_FILE`, `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`; added `SNOWFLAKE_WRITER_*` and `SNOWFLAKE_READER_*` user/role/key-path/passphrase variables, default object names, `TRACEMIND_SNOWFLAKE_ENABLED=false`, and commented proposed defaults for the limits including `…_EXPORT_MIN_INTERVAL_SECONDS=14400` (code floor 7200). No migrator, cleaner or admin variable exists, by design. Only the two passphrases are secrets.
- **`docs/plans/M3-plan.md`:** §2.2–2.4, 3, 4.4, 4.5, 4.7, 5 (M3-9 reworded; M3-12 to M3-14 added as additions for approval), 6 (slice S3), 7, 8, 9 updated to match this guide.
- **`PLAN.md`:** M3 status wording ("Snowflake preparation completed; implementation pending", still **Blocked**), change-log rows, follow-ups F20–F23.
- **`docs/dependency-verification.md`:** the Snowflake section updated with the documentation facts of section 4 and its unverified list.
- **`docs/threat-model.md`:** T11 updated and T23–T28 added for the Snowflake design.
- **`docs/adr/0007-postgresql-transactional-store-and-snowflake-path.md`:** an amendment note correcting the statement that the export "cannot create duplicates because the key is unique there too" (VERIFIED: `PRIMARY KEY` and `UNIQUE` are not enforced on standard tables).

### 13.1 Variable reference (all names are in `.env.example`; nothing reads them until M3 is authorized)

| Variable | Purpose | Sensitivity | Default |
|---|---|---|---|
| `TRACEMIND_SNOWFLAKE_ENABLED` | Master switch: export and Snowflake queries do nothing unless `true` | no | `false` |
| `SNOWFLAKE_ACCOUNT` | Account identifier (`orgname-account_name`) | private, not secret | empty |
| `SNOWFLAKE_AUTHENTICATOR` | Key-pair login | no | `SNOWFLAKE_JWT` |
| `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_TEST_DATABASE` | Object names | no | `TRACEMIND_WH`, `TRACEMIND_DEV`, `TRACEMIND_TEST` |
| `SNOWFLAKE_WRITER_USER`, `SNOWFLAKE_WRITER_ROLE` | Exporter identity and its one role (the key pair is restricted to it) | no | `TRACEMIND_EXPORTER_SVC`, `TRACEMIND_WRITER` |
| `SNOWFLAKE_WRITER_PRIVATE_KEY_FILE` | **Path** to the exporter's encrypted key | path private; **file highly secret** | empty |
| `SNOWFLAKE_WRITER_PRIVATE_KEY_PASSPHRASE` | Passphrase of that key | **secret** | empty |
| `SNOWFLAKE_READER_USER`, `SNOWFLAKE_READER_ROLE` | Query-service identity and role | no | `TRACEMIND_QUERY_SVC`, `TRACEMIND_READER` |
| `SNOWFLAKE_READER_PRIVATE_KEY_FILE`, `SNOWFLAKE_READER_PRIVATE_KEY_PASSPHRASE` | The same pair of items for the reader | as above | empty |
| `TRACEMIND_SNOWFLAKE_EXPORT_MIN_INTERVAL_SECONDS` | Minimum time between exports; start-up refuses a value under the code floor of 7,200 | no | 14400 |
| `TRACEMIND_SNOWFLAKE_EXPORT_BATCH_MAX_ROWS` | Rows per `MERGE` batch (bounded batches; also bounds the duplicated-source risk) | no | 1000 |
| `TRACEMIND_SNOWFLAKE_EXPORT_MIN_SEVERITY` | Lowest severity exported (M3 plan decision 4: `ERROR` and above plus all incident versions) | no | `ERROR` |
| `TRACEMIND_SNOWFLAKE_QUERY_TIMEOUT_SECONDS` | Client-side per-statement timeout for approved query functions | no | 30 |
| `TRACEMIND_SNOWFLAKE_LOCK_TIMEOUT_SECONDS` | Session `LOCK_TIMEOUT` applied to every session (the account default is 12 hours) | no | 60 |
| `TRACEMIND_SNOWFLAKE_MAX_ROWS` | Hard row cap per query function call | no | 500 |
| `TRACEMIND_SNOWFLAKE_MAX_CONCURRENCY` | Concurrent Snowflake statements per process | no | 2 |
| `TRACEMIND_SNOWFLAKE_SOFT_BUDGET_CREDITS` | Credits (estimated from TraceMind's own call log) at which TraceMind reports a degraded state; 75 % of the provisional allowance | no | 15 |

The values in the last eight rows are **proposed defaults** to be settled in M3 slice S2; the file keeps them commented out. There is deliberately no variable for the migrator, the test-cleanup role or any administrator.

## 14. What happens next

1. The owner approves the read-only preflight (section 15) **explicitly**; nothing runs before that.
2. The owner runs it in Snowsight, decides O1 from its output, supplies the O8 facts, and reports PASS / FAIL / NOT RUN (no secrets).
3. Setup (section 3, steps 1–10) follows only on a separate explicit approval. M3 implementation starts only on a separate explicit authorization.

## 15. Read-only preflight: instructions for the owner (nothing is executed by me)

1. **Approve it first.** Tell me in writing that the preflight may be run; I will not run anything in any case, because I have no connection to your account. You run it.
2. Open a Snowsight SQL worksheet in the new account. Copy the contents of `infra/snowflake/00_preflight_readonly.sql` into it (the file contains no placeholders and no secrets).
3. Run it **statement by statement** in order, reading each result. The file changes role three times: your default role (steps 1–2), `ACCOUNTADMIN` (the gate: one `USE ROLE` and a `SELECT CURRENT_ROLE()`), then `SECURITYADMIN` (steps 3–4). All of it is `USE ROLE`, `SELECT` of context functions or `SHOW`; **none of it creates, alters, grants or drops anything or needs a running warehouse.**
4. **Gate:** if `USE ROLE ACCOUNTADMIN` fails, or `gate_role` is not `ACCOUNTADMIN`, stop and tell me; do not continue to any setup.
5. Record each item as PASS / FAIL / NOT RUN (G0–G3 in section 11). Send me only: cloud and region; which of the four administrative roles you hold; whether the gate passed; for each warehouse its name, size, `auto_suspend` and monitor; each monitor's name and `level`; whether any `TRACEMIND%` object exists; and any error text. **Do not send** the account identifier unless you choose to, and never anything in section 16's "never share" list.
6. In Snowsight also note, for O8: edition; trial sign-up and end dates; the remaining balance in the left-navigation tile (visible to `ACCOUNTADMIN`, S34) **and whether it is shown in dollars or credits** (the page does not say); and the price per credit for your edition, cloud and region from Snowflake's Pricing Guide. `Admin » Cost management` shows credits consumed; whether `Admin » Billing` or the view `USAGE_IN_CURRENCY_DAILY` work on a trial is UNVERIFIED.

## 16. Exact permissions required

### 16.1 By step (documentation-based; nothing here has been tested)

| Step | Role | Privilege that matters | Notes |
|---|---|---|---|
| Preflight 1–2 | your default | none expected | context functions; your own grants |
| Preflight gate | `ACCOUNTADMIN` | membership of the role | fails if not granted to your user |
| Preflight 3–4 | `SECURITYADMIN` | `MANAGE GRANTS` (sees every object) | other roles list only objects they hold a privilege on |
| Part A | `SYSADMIN` | `CREATE DATABASE`, `CREATE WAREHOUSE` (defaults) | creates containers; grants nothing |
| Part B | `ACCOUNTADMIN` | account administrator | monitor creation and assignment are ACCOUNTADMIN-only |
| Part C | `USERADMIN` | `CREATE ROLE`, `CREATE USER`; ownership of what it creates; `MODIFY PROGRAMMATIC AUTHENTICATION METHODS` on the users or their ownership (fallback grant in the script) | roles to users, key pairs |
| Part D | `SECURITYADMIN` | `MANAGE GRANTS` | all grants |
| Verification (02 §1) | `SECURITYADMIN` or higher | `MANAGE GRANTS` | read-only |
| Cost review (02 §3) | `ACCOUNTADMIN` | account usage access | which other roles can read the views: UNVERIFIED |

### 16.2 What each TraceMind role holds after Part D (and nothing else)

| Role | Held by | Privileges |
|---|---|---|
| `TRACEMIND_WRITER` | `TRACEMIND_EXPORTER_SVC` | `USAGE` warehouse; `USAGE` db `TRACEMIND_DEV`/`TRACEMIND_TEST`; `USAGE` schemas `TELEMETRY`, `KNOWLEDGE`; `INSERT`, `SELECT` on future tables in those schemas. **No `UPDATE`, `DELETE`, `TRUNCATE`, `CREATE`** |
| `TRACEMIND_READER` | `TRACEMIND_QUERY_SVC` | same `USAGE` set; `SELECT` on future **views** only |
| `TRACEMIND_MIGRATOR` | your user | `USAGE` warehouse and both databases; `USAGE`, `CREATE TABLE`, `CREATE VIEW` on all three schemas of each database; **owns what it creates** |
| `TRACEMIND_TEST_CLEANER` | your user | `USAGE` warehouse; `USAGE` `TRACEMIND_TEST` and its `TELEMETRY`/`KNOWLEDGE` schemas; `SELECT`, `DELETE` on future tables there. **Nothing** on `TRACEMIND_DEV` or `META` |
| (none of them) | — | `OWNERSHIP` of warehouse/databases, `MODIFY`, `OPERATE`, `MONITOR`, `MANAGE GRANTS`, any global privilege |

The only `DELETE` **grant** anywhere is the cleaner's, on `TRACEMIND_TEST` tables; there is no `UPDATE` or `TRUNCATE` grant. **But grants are not the whole picture:** as owner of the tables it creates, `TRACEMIND_MIGRATOR` holds every privilege on them (`UPDATE`, `DELETE`, `TRUNCATE` included) in both databases and can grant them onward; it is human-held and keyless. Also: `SELECT` on **future views** exposes every view the migrator ever creates in those schemas, not only "approved" ones (approval is a review step before the migration is run); and the writer and reader hold `USAGE` on both databases, so a configuration error could send a `tmtest-` row into `TRACEMIND_DEV` (M3 must refuse it in code; LEAK-1 only detects it afterwards).

### 16.3 Account information for the next stage

| Category | Items | May go in chat? |
|---|---|---|
| Non-sensitive facts | cloud (AWS) and region; edition; trial sign-up and end dates; the trial balance and its unit; the `default_secondary_roles` value your user shows (or that `DESC USER` failed); which administrative roles you hold; whether the gate passed; warehouse names, sizes, `auto_suspend`; monitor names and levels; PASS / FAIL / NOT RUN results; the identifier **format** (organization-account or locator) | Yes |
| Private but not secret | the account identifier `orgname-account_name` (goes only into your local `.env`); your Snowflake login name (goes only into your edited copy of script 01) | Not needed by me; keep to yourself |
| Public key material | the one-line public key bodies pasted into your edited copy of script 01 | Not secret, but I do not need them; keep the edited copy outside the repository |
| **Never share, never commit** | passwords; MFA or recovery codes; tokens; **private key files (`*.p8`) and their contents**; **passphrases**; connection/config files (for example the Snowsight "Config File" tab); screenshots showing any of these | **No** |
