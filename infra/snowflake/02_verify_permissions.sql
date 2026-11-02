-- =====================================================================================
-- TraceMind / Snowflake — 02 VERIFY PERMISSIONS, KEYS, MERGE AND COST   (revision 4, 2026-11-02)
-- =====================================================================================
-- STATUS : DRAFT FOR REVIEW. NOT EXECUTED by TraceMind or by the assistant. Nothing in this file has
--          ever been run: every "EXPECTED" below is a prediction, not a result.
-- WHO    : You, after script 01 has been run, in Snowsight.
-- NATURE : GATE-B and Section 1 are READ-ONLY. Section 2 is expected-to-fail attempts (commented out).
--          Section 3 is a READ-ONLY cost review (commented out). Section 4 creates a CANARY table and
--          runs the MERGE tests; it is only for TRACEMIND_TEST, only AFTER the owner authorizes M3, and
--          every statement in it is commented out.
-- SECRETS: none. Never paste credentials. Results are not secrets.
--
-- RECORDING RESULTS — one of exactly three words per item, written down by you:
--   PASS    you RAN the statement(s) and the output matched EXPECTED.
--   FAIL    you ran it and the output did NOT match EXPECTED (or it errored where success was expected,
--           or SUCCEEDED where failure was expected). A forbidden operation that succeeds is a FINDING.
--   NOT RUN you did not run it, or could not interpret the output. An item you skipped is NOT RUN, never
--           PASS; "no error appeared" is not a pass for a step you did not execute.
--   DOCUMENTATION words VERIFIED / INFERRED / UNVERIFIED describe what a docs page states; they are not
--   test results and never turn a NOT RUN into a PASS.
-- =====================================================================================


-- =====================================================================================
-- GATE-B  (between Part B and Part C of script 01) — READ-ONLY
-- The monitor must be attached to the warehouse BEFORE any TraceMind role is created or granted
-- anything. EXPECTED: one row; state SUSPENDED; size X-Small; auto_suspend 60;
-- resource_monitor = TRACEMIND_WH_MONITOR.  Anything else -> STOP; do not run Part C or Part D.
-- Read the plain SHOW output (VERIFIED: it needs no running warehouse; columns name, state, size,
-- auto_suspend, auto_resume, resource_monitor, owner). A "->> SELECT ... FROM $1" pipe form exists (VERIFIED
-- for DESC USER on the key-pair-auth page) but it runs a query and may need a warehouse; do NOT start a
-- warehouse for it, so it is left commented out.
-- =====================================================================================
USE ROLE ACCOUNTADMIN;
SHOW WAREHOUSES LIKE 'TRACEMIND_WH';
-- SHOW WAREHOUSES LIKE 'TRACEMIND_WH' ->> SELECT "name", "state", "size", "auto_suspend", "resource_monitor" FROM $1;
SHOW RESOURCE MONITORS LIKE 'TRACEMIND_WH_MONITOR';   -- EXPECTED: credit_quota 20, frequency NEVER, triggers as in script 01 Part B


-- =====================================================================================
-- SECTION 1 — READ-ONLY inspection (SECURITYADMIN or higher can see all of these)
-- =====================================================================================
USE ROLE SECURITYADMIN;

-- 1a. [after Part D] What can each role do? EXPECTED: only the privileges named in script 01.
--     Writer  : USAGE (warehouse, db, 2 schemas), INSERT+SELECT on FUTURE tables.
--     Reader  : USAGE (warehouse, db, 2 schemas), SELECT on FUTURE views.
--     Migrator: USAGE (warehouse, db, 3 schemas), CREATE TABLE and CREATE VIEW on 3 schemas.
--     Cleaner : USAGE (warehouse, TRACEMIND_TEST, 2 TEST schemas), SELECT+DELETE on FUTURE tables in
--               the TEST data schemas ONLY. NO privilege of any kind on TRACEMIND_DEV or on META.
--     NONE of the runtime roles (writer, reader): OWNERSHIP, MODIFY, OPERATE, MONITOR, MANAGE GRANTS,
--     DELETE, UPDATE, TRUNCATE, any CREATE, anything on another database, any global privilege.
SHOW GRANTS TO ROLE TRACEMIND_WRITER;
SHOW GRANTS TO ROLE TRACEMIND_READER;
SHOW GRANTS TO ROLE TRACEMIND_MIGRATOR;
SHOW GRANTS TO ROLE TRACEMIND_TEST_CLEANER;

-- 1b. [after Part D] Future grants. EXPECTED: writer on TABLES, reader on VIEWS, cleaner on TABLES in
--     TEST only; nothing else; nothing for any role in the DEV schemas except writer/reader as above.
SHOW FUTURE GRANTS IN SCHEMA TRACEMIND_DEV.TELEMETRY;
SHOW FUTURE GRANTS IN SCHEMA TRACEMIND_DEV.KNOWLEDGE;
SHOW FUTURE GRANTS IN SCHEMA TRACEMIND_TEST.TELEMETRY;
SHOW FUTURE GRANTS IN SCHEMA TRACEMIND_TEST.KNOWLEDGE;

-- 1c. [after Part C] Who holds the roles? EXPECTED:
--     writer   -> TRACEMIND_EXPORTER_SVC only
--     reader   -> TRACEMIND_QUERY_SVC only
--     migrator -> YOUR user only (decision 6), and no service user
--     cleaner  -> YOUR user only, and no service user
--     No grant of any TRACEMIND role to SYSADMIN (decision 4), unless you enabled it on purpose.
SHOW GRANTS OF ROLE TRACEMIND_WRITER;
SHOW GRANTS OF ROLE TRACEMIND_READER;
SHOW GRANTS OF ROLE TRACEMIND_MIGRATOR;
SHOW GRANTS OF ROLE TRACEMIND_TEST_CLEANER;
SHOW GRANTS TO USER TRACEMIND_EXPORTER_SVC;    -- EXPECTED: exactly TRACEMIND_WRITER
SHOW GRANTS TO USER TRACEMIND_QUERY_SVC;       -- EXPECTED: exactly TRACEMIND_READER

-- 1d. [after Part C] Ownership (decision 4). VERIFIED rule: the creating role owns an object. EXPECTED:
--     the "owner" column is USERADMIN for the four roles and both users; SYSADMIN for the databases,
--     schemas and warehouse. If any owner differs, a later grant or key registration may fail: record it.
SHOW ROLES LIKE 'TRACEMIND%';
SHOW USERS LIKE 'TRACEMIND%';                  -- EXPECTED: type = SERVICE
SHOW DATABASES LIKE 'TRACEMIND%';
SHOW WAREHOUSES LIKE 'TRACEMIND_WH';

-- 1e. [after Part C] Service users. EXPECTED: TYPE = SERVICE, default roles as designed, no password
--     (VERIFIED: SERVICE users "cannot log in using a password"). UNVERIFIED: whether they can open
--     Snowsight (note ALLOWED_INTERFACES if DESC USER shows it).
DESC USER TRACEMIND_EXPORTER_SVC;
DESC USER TRACEMIND_QUERY_SVC;

-- 1f. [after Part C] The key pairs. They are NAMED key pairs, so DESC USER's RSA_PUBLIC_KEY_FP (legacy
--     keys) is NOT the place to look. VERIFIED columns (show-user-key-pairs): name, user_name,
--     fingerprint, role_scope, status, comment, created_on, created_by, last_used_on, expires_at, rotated_to.
--     EXPECTED per user: ONE row; status ACTIVE; role_scope = the user's own role; expires_at about
--     90 days away; last_used_on NULL until the first real login; fingerprint equal to the one you computed
--     locally from the PUBLIC key. UNVERIFIED: the fingerprint's text format; if the two cannot be compared
--     with confidence, record NOT RUN and STOP — do not guess.
SHOW USER KEY PAIRS FOR USER TRACEMIND_EXPORTER_SVC;
SHOW USER KEY PAIRS FOR USER TRACEMIND_QUERY_SVC;

-- 1g. [after Parts A, B] Warehouse and monitor (same expectations as GATE-B, plus timeouts).
--     Monitor: a quota of 20, frequency NEVER, a start time, used credits 0 (or near), triggers at
--     10/50/75 NOTIFY, 90 SUSPEND, 100 SUSPEND_IMMEDIATE, level WAREHOUSE. The show-resource-monitors page
--     read documents only the "level" column, so the NAMES of the quota/used-credit/trigger columns are
--     UNVERIFIED: read whatever the output calls them and record it. Record what a TRIAL account actually
--     shows (UNVERIFIED behaviour). Timeouts: statement 300, queued 60.
SHOW WAREHOUSES LIKE 'TRACEMIND_WH';
SHOW RESOURCE MONITORS LIKE 'TRACEMIND_WH_MONITOR';
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE TRACEMIND_WH;
SHOW PARAMETERS LIKE 'STATEMENT_QUEUED_TIMEOUT_IN_SECONDS' IN WAREHOUSE TRACEMIND_WH;

-- 1h. [after Part D] Who can use the warehouse? EXPECTED: USAGE for exactly the four TraceMind roles,
--     plus ownership by SYSADMIN. MODIFY/OPERATE/MONITOR for nobody but the owner.
SHOW GRANTS ON WAREHOUSE TRACEMIND_WH;

-- 1i. Nothing else is reachable. EXPECTED: the only TRACEMIND objects are the ones above, and no TraceMind
--     role has a grant on any non-TRACEMIND database (inspect 1a). Cleaner: no grant naming TRACEMIND_DEV.
SHOW DATABASES LIKE 'TRACEMIND%';

-- 1j. Other compute that could spend the trial balance (not TraceMind's, but real). EXPECTED: note each
--     warehouse's size, auto_suspend and resource_monitor; any large or never-suspending one is a cost
--     risk the TraceMind monitor does NOT cover.
SHOW WAREHOUSES;
SHOW RESOURCE MONITORS;


-- =====================================================================================
-- SECTION 2 — EXPECTED-TO-FAIL attempts (optional; needs a role you may assume)
-- To run these as a runtime role WITHOUT using its key, an administrator would have to grant
-- that role to your own user temporarily:
--     GRANT ROLE TRACEMIND_WRITER TO USER <your_user>;      -- adds a TEMPORARY grant
--     ... run the tests ...
--     REVOKE ROLE TRACEMIND_WRITER FROM USER <your_user>;   -- REMOVE IT AFTERWARDS
-- That temporary grant gives YOU the runtime role; do it only if you accept that, and revoke it in the
-- same sitting. If you prefer not to, record every item below as NOT RUN: the same checks are repeated,
-- with the real service users and their keys, by the application-side permission check that M3 adds.
--
-- !! SECONDARY ROLES INVALIDATE THESE TESTS UNLESS THEY ARE OFF. VERIFIED (use-secondary-roles): the privileges
-- !! of active secondary roles count towards authorization, and CREATE-type authorization is the only thing
-- !! limited to the primary role. VERIFIED (create-user): the default for a user created with CREATE USER is
-- !! ALL. Your user also holds TRACEMIND_MIGRATOR and probably SYSADMIN/ACCOUNTADMIN, so with secondary roles on
-- !! "expected to fail" lines such as DROP DATABASE or ALTER WAREHOUSE can SUCCEED through another role, and
-- !! "expected to succeed" lines can pass through the migrator, which owns the tables. THEREFORE, immediately after
-- !! every USE ROLE below run   USE SECONDARY ROLES NONE;   and   SELECT CURRENT_ROLE(), CURRENT_SECONDARY_ROLES();
-- !! and proceed ONLY if no secondary role is listed. A result obtained any other way is NOT RUN, never PASS.
-- !! Safest alternative: run these checks as the real service users (M3 S2 check), or as a throwaway human user
-- !! that holds only the one role under test and no administrative role.
-- =====================================================================================

-- ---- as TRACEMIND_WRITER ------------------------------------------------------------
-- USE ROLE TRACEMIND_WRITER;  USE SECONDARY ROLES NONE;  USE WAREHOUSE TRACEMIND_WH;
-- SELECT CURRENT_ROLE(), CURRENT_SECONDARY_ROLES();           -- EXPECTED: TRACEMIND_WRITER, no secondary role
-- CREATE TABLE TRACEMIND_DEV.TELEMETRY.should_fail (x INT);   -- EXPECTED: insufficient privileges
-- CREATE SCHEMA TRACEMIND_DEV.ROGUE;                          -- EXPECTED: insufficient privileges
-- DROP SCHEMA TRACEMIND_DEV.TELEMETRY;                        -- EXPECTED: insufficient privileges
-- DROP DATABASE TRACEMIND_DEV;                                -- EXPECTED: insufficient privileges
-- ALTER WAREHOUSE TRACEMIND_WH SET WAREHOUSE_SIZE = 'LARGE';  -- EXPECTED: insufficient privileges
-- ALTER WAREHOUSE TRACEMIND_WH SET RESOURCE_MONITOR = NULL;   -- EXPECTED: insufficient privileges
-- ALTER WAREHOUSE TRACEMIND_WH SUSPEND;                       -- EXPECTED: insufficient privileges
-- SELECT * FROM TRACEMIND_DEV.META.anything;                  -- EXPECTED: schema not accessible
-- SHOW DATABASES;                                             -- EXPECTED: only TRACEMIND_* visible
-- NOTE: VERIFIED (create-table) a TEMPORARY table needs no CREATE TABLE privilege, so
--       CREATE TEMPORARY TABLE TRACEMIND_DEV.TELEMETRY.t (x INT); is EXPECTED TO SUCCEED for the writer
--       (it only needs USAGE on the schema, INFERRED). Record the result; it is a documented, accepted
--       capability, used for staging batches, and it vanishes with the session.

-- ---- as TRACEMIND_READER ------------------------------------------------------------
-- USE ROLE TRACEMIND_READER;  USE SECONDARY ROLES NONE;  USE WAREHOUSE TRACEMIND_WH;
-- SELECT CURRENT_ROLE(), CURRENT_SECONDARY_ROLES();           -- EXPECTED: TRACEMIND_READER, no secondary role
-- CREATE TABLE TRACEMIND_DEV.TELEMETRY.should_fail (x INT);   -- EXPECTED: insufficient privileges
-- CREATE VIEW TRACEMIND_DEV.TELEMETRY.should_fail AS SELECT 1; -- EXPECTED: insufficient privileges
-- DROP SCHEMA TRACEMIND_DEV.KNOWLEDGE;                        -- EXPECTED: insufficient privileges
-- ALTER WAREHOUSE TRACEMIND_WH SUSPEND;                       -- EXPECTED: insufficient privileges
-- SELECT * FROM TRACEMIND_DEV.META.anything;                  -- EXPECTED: schema not accessible
-- SHOW DATABASES;                                             -- EXPECTED: only TRACEMIND_* visible

-- ---- as TRACEMIND_TEST_CLEANER ------------------------------------------------------
-- SECONDARY ROLES FIRST. Privileges of active secondary roles count (VERIFIED, use-secondary-roles), so a
-- cleaner session on a user that also holds TRACEMIND_MIGRATOR would otherwise pass the checks below for the
-- wrong reason, or fail to show the boundary at all. Always start with:
-- USE ROLE TRACEMIND_TEST_CLEANER;
-- USE SECONDARY ROLES NONE;                                   -- VERIFIED syntax: "Disables secondary roles"
-- SELECT CURRENT_ROLE(), CURRENT_SECONDARY_ROLES();           -- EXPECTED: primary TRACEMIND_TEST_CLEANER and NO
--                                                             --   secondary role. VERIFIED function (JSON with
--                                                             --   "roles" and "value"); its output when NONE is
--                                                             --   active is NOT described: record what you see.
--                                                             --   If any secondary role is listed: STOP, NOT RUN.
-- USE WAREHOUSE TRACEMIND_WH;
-- USE DATABASE TRACEMIND_DEV;                                 -- EXPECTED: insufficient privileges / does not exist
-- SELECT 1 FROM TRACEMIND_DEV.TELEMETRY.anything;             -- EXPECTED: object does not exist or not authorized
-- DROP TABLE TRACEMIND_TEST.TELEMETRY.anything;               -- EXPECTED: insufficient privileges (it does not own tables)
-- TRUNCATE TABLE TRACEMIND_TEST.TELEMETRY.anything;           -- EXPECTED: insufficient privileges
-- SHOW DATABASES;                                             -- EXPECTED: TRACEMIND_DEV NOT listed; TRACEMIND_TEST listed

-- NOTE: each of these resumes the warehouse (60 s minimum billing, then the 60 s idle tail).
-- Run them in one sitting; each resume costs on the order of 0.03 credits (guide, section 8).


-- =====================================================================================
-- SECTION 3 — READ-ONLY COST REVIEW (run after every working session; guide, section 9)
-- Needs ACCOUNTADMIN or a role granted access to the SNOWFLAKE database usage views (which roles can
-- read them by default is NOT stated on the page read -> use ACCOUNTADMIN).
-- LATENCY (VERIFIED, account-usage page): the view lags by up to 180 minutes; cloud-services credits by
-- up to 6 hours. A review right after a session will NOT show that session yet. This is an after-the-fact
-- check, never a real-time guard.
-- =====================================================================================
-- USE ROLE ACCOUNTADMIN;

-- 3a. Credits per warehouse over the last 7 days (every warehouse, not just TraceMind's).
-- SELECT WAREHOUSE_NAME,
--        SUM(CREDITS_USED)                 AS credits_total,
--        SUM(CREDITS_USED_COMPUTE)         AS credits_compute,
--        SUM(CREDITS_USED_CLOUD_SERVICES)  AS credits_cloud_services
-- FROM   SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
-- WHERE  START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
-- GROUP  BY WAREHOUSE_NAME
-- ORDER  BY credits_total DESC;

-- 3b. Hour-by-hour for TraceMind's warehouse (shows the resume pattern).
-- SELECT START_TIME, CREDITS_USED_COMPUTE, CREDITS_USED_CLOUD_SERVICES
-- FROM   SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
-- WHERE  WAREHOUSE_NAME = 'TRACEMIND_WH'
--   AND  START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
-- ORDER  BY START_TIME DESC;

-- 3c. The monitor's own counter (what the suspend triggers act on).
-- SHOW RESOURCE MONITORS LIKE 'TRACEMIND_WH_MONITOR';     -- compare used_credits with 3a/3b

-- 3d. NOT covered by any query above (look in Snowsight, Admin > Cost management / Billing):
--     storage, serverless features, AI services, and the remaining trial balance (the unit the left-navigation
--     tile shows is not stated on the trial page: record it). The monitor does not cover these either, and
--     it counts cloud-services credits but CANNOT suspend them (VERIFIED, resource-monitors).


-- =====================================================================================
-- SECTION 4 — CANARY AND MERGE TESTS. ALL COMMENTED OUT. NOT PART OF THIS PREPARATION.
-- Runs only AFTER (a) the owner authorizes M3 and (b) scripts 01 and 02 section 1 have PASSED.
-- Runs only against TRACEMIND_TEST. Every row carries a TEST RUN ID of the form
--     tmtest-YYYYMMDDTHHMMSSZ-xxxxxxxx      (lower-case hex x; e.g. generated by the test harness)
-- and the statements below show it as <<RUN_ID>>. Use a NEW run id for every test group.
-- The SQL templates were written from general Snowflake knowledge and the pages cited in the guide; the
-- exact form of a template (for example FROM VALUES with COLUMN1.. names) is UNVERIFIED until it is run.
-- Each test records PASS / FAIL / NOT RUN. Until it is run, it is NOT RUN.
-- RULE S4-0 (applies to EVERY session below): right after each USE ROLE run USE SECONDARY ROLES NONE; and
--   SELECT CURRENT_ROLE(), CURRENT_SECONDARY_ROLES();  A test whose session shows any secondary role is NOT RUN:
--   privileges of secondary roles count (VERIFIED), so the migrator/ACCOUNTADMIN could be doing the work and the
--   result would say nothing about the role under test. Prefer the real service users or a throwaway human
--   user holding only that role (the M3 S2 check does this); the temporary-grant route has the weakness above.
-- =====================================================================================

-- ---- S4.0  Canary objects (role TRACEMIND_MIGRATOR, TEST only) ------------------------------------
-- USE ROLE TRACEMIND_MIGRATOR;  USE SECONDARY ROLES NONE;  USE WAREHOUSE TRACEMIND_WH;
-- CREATE TABLE IF NOT EXISTS TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS (
--     project_id  STRING NOT NULL,
--     event_id    STRING NOT NULL,
--     payload     STRING,
--     test_run_id STRING NOT NULL);
-- CREATE VIEW IF NOT EXISTS TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS_V AS
--     SELECT project_id, event_id, payload, test_run_id FROM TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS;
-- FUT-1  (role SECURITYADMIN) the future grants reached MIGRATOR-created objects (UNVERIFIED in the docs):
--        SHOW GRANTS ON TABLE TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS;
--        EXPECTED: INSERT+SELECT to TRACEMIND_WRITER; SELECT+DELETE to TRACEMIND_TEST_CLEANER; owner MIGRATOR.
--        SHOW GRANTS ON VIEW  TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS_V;
--        EXPECTED: SELECT to TRACEMIND_READER. If a grant is missing: FAIL, STOP, report; do not grant by hand.

-- ---- S4.1  MERGE privileges (role TRACEMIND_WRITER) — settles the minimum-privilege question -----
-- USE ROLE TRACEMIND_WRITER;  USE SECONDARY ROLES NONE;  USE WAREHOUSE TRACEMIND_WH;   -- rule S4-0: check it
-- ALTER SESSION SET LOCK_TIMEOUT = 120;      -- VERIFIED session parameter; never leave the 12 h default
-- The exact operation under test (insert-only MERGE, source deduplicated, NO "WHEN MATCHED" clause):
-- MERGE INTO TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS AS t
-- USING (
--     SELECT project_id, event_id, payload, test_run_id
--     FROM (
--         SELECT column1 AS project_id, column2 AS event_id, column3 AS payload, column4 AS test_run_id,
--                ROW_NUMBER() OVER (PARTITION BY column1, column2 ORDER BY column3) AS rn
--         FROM VALUES ('p1', 'e1', 'a', '<<RUN_ID>>'),
--                     ('p1', 'e2', 'b', '<<RUN_ID>>')
--     ) WHERE rn = 1
-- ) AS s
-- ON t.project_id = s.project_id AND t.event_id = s.event_id
-- WHEN NOT MATCHED THEN INSERT (project_id, event_id, payload, test_run_id)
--                       VALUES (s.project_id, s.event_id, s.payload, s.test_run_id);
-- MERGE-1  EXPECTED: succeeds; 2 rows inserted, with only INSERT + SELECT granted.
--          PASS  = it ran and inserted 2 rows.
--          FAIL  = "insufficient privileges" (or anything else). Then: STOP. Record the exact error text.
--                  Do NOT grant DELETE. Do NOT grant UPDATE. The owner decides what, if anything, to do:
--                  that is a new decision, not an automatic fix.
-- MERGE-2  (idempotent retry) run the IDENTICAL statement again.
--          EXPECTED: 0 rows inserted; SELECT COUNT(*) ... WHERE test_run_id = '<<RUN_ID>>' still 2.
-- MERGE-3a (documented duplicate behaviour) a NEW run id; put the same key TWICE in the VALUES list WITHOUT
--          the ROW_NUMBER() subquery. EXPECTED (VERIFIED in the MERGE page): the target receives one copy for
--          EACH source copy -> duplicate rows. This proves why the batch MUST be deduplicated. The duplicates
--          stay in the TEST table under that run id; it is a test row set, never in DEV.
-- MERGE-3b the same data WITH the ROW_NUMBER() deduplication. EXPECTED: exactly one row for the key.

-- ---- S4.2  Concurrency (two Snowsight worksheets, both TRACEMIND_WRITER) --------------------------
-- Purpose: does a second identical MERGE, started while the first still holds its lock, add duplicates?
-- Known (VERIFIED, transactions page): UPDATE, DELETE and MERGE "hold locks that generally prevent them
-- from running in parallel with other UPDATE, DELETE, and MERGE statements"; locks are held until COMMIT or
-- ROLLBACK; the isolation level is READ COMMITTED; INSERT statements generally do NOT serialize ("often can
-- run in parallel with other INSERT and COPY operations"). NOT stated anywhere: whether a MERGE that was
-- blocked re-reads the target after the lock is released. That is exactly what this test finds out.
-- Both worksheets: rule S4-0 first (USE ROLE TRACEMIND_WRITER; USE SECONDARY ROLES NONE; check).
-- Worksheet 1:  BEGIN TRANSACTION;   then run the S4.1 MERGE with a NEW run id R. DO NOT COMMIT yet.
--               (BEGIN TRANSACTION rather than a bare BEGIN: a bare BEGIN can be mistaken for the start of a
--               Snowflake Scripting block. VERIFIED syntax: "BEGIN [ { WORK | TRANSACTION } ] [ NAME <name> ]".)
-- Worksheet 2:  ALTER SESSION SET LOCK_TIMEOUT = 5;
--               run the IDENTICAL MERGE (same keys, same run id R).
-- MERGE-4a EXPECTED: worksheet 2 WAITS, then errors after about 5 s with a lock-timeout error (the exact
--          message is unknown). PASS = it waited and timed out. FAIL = it ran immediately (no serialization).
-- Worksheet 2:  ALTER SESSION SET LOCK_TIMEOUT = 120;   and run the identical MERGE again (it will wait).
-- Worksheet 1:  COMMIT;
-- MERGE-4b EXPECTED: worksheet 2 then completes with 0 rows inserted, and COUNT(*) for R stays 2.
--          PASS = 0 inserted.
--          FAIL = it inserted rows (duplicates). That means MERGE alone is NOT safe against concurrent
--          writers. Record it and STOP: the exporter would then rely ONLY on the single-exporter guard in
--          PostgreSQL, and the owner must decide whether that is acceptable. Do not "fix" it with grants.
-- MERGE-5  (interrupted batch leaves nothing partial) NOT testable in Snowsight with confidence; it is an
--          M3 fault-injection test (kill the connection mid-batch, then re-run). NOT RUN until then.
-- MERGE-6  (immutability) as WRITER: UPDATE <canary> SET payload = 'x';  DELETE FROM <canary>;
--          TRUNCATE TABLE <canary>;                       EXPECTED: all three insufficient privileges.
-- MERGE-7  (duplicate audit, writer has SELECT) after every group:
--          SELECT project_id, event_id, COUNT(*) AS n FROM TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS
--          WHERE test_run_id = '<<RUN_ID>>' GROUP BY project_id, event_id HAVING COUNT(*) > 1;
--          EXPECTED: no rows for every run id EXCEPT the intentional MERGE-3a run id.
--          VERIFIED (constraints-overview): UNIQUE and PRIMARY KEY are "not enforced" on standard tables, so
--          this audit, plus the single-exporter guard, is the ONLY duplicate protection.

-- ---- S4.3  Reader (role TRACEMIND_READER) ------------------------------------------------------
--      (rule S4-0 first: USE ROLE TRACEMIND_READER; USE SECONDARY ROLES NONE; check it)
-- READ-1  SELECT * FROM TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS_V;   EXPECTED: succeeds WITHOUT any grant on
--         the base table (VERIFIED in create-view; this is the live confirmation). FAIL -> report; never add
--         a table grant to make it pass.
-- READ-2  SELECT * FROM TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS;     EXPECTED: insufficient privileges.
-- READ-3  INSERT / UPDATE / DELETE anything;                         EXPECTED: insufficient privileges.
-- READ-4  as WRITER: SELECT * FROM TRACEMIND_TEST.TELEMETRY.CANARY_EVENTS_V; EXPECTED: insufficient privileges.

-- ---- S4.4  Test-data safety (decision 3) -------------------------------------------------------
-- LEAK-1  as TRACEMIND_MIGRATOR (it can read DEV), for EVERY table in TRACEMIND_DEV that has the column:
--         SELECT COUNT(*) FROM TRACEMIND_DEV.TELEMETRY.<table> WHERE test_run_id LIKE 'tmtest-%';
--         EXPECTED: 0. Anything else means test-marked rows reached development data: STOP and report.
--         (The column name test_run_id is an M3 design proposal, not yet implemented.)
-- CLEAN-0 as TRACEMIND_TEST_CLEANER, secondary roles NONE (see Section 2): the attempts against
--         TRACEMIND_DEV must all fail. With the cleaner's grants only, a SELECT on a development TABLE fails.
-- CLEAN-2 (READ-ONLY demonstration of why "USE SECONDARY ROLES NONE" is mandatory). Primary role
--         TRACEMIND_TEST_CLEANER, then:   USE SECONDARY ROLES ALL;
--         SELECT COUNT(*) FROM TRACEMIND_DEV.TELEMETRY.<table>;
--         PREDICTION (INFERRED from the VERIFIED secondary-role rule, not from a test): it SUCCEEDS through
--         the migrator role your user also holds, which proves a session without NONE can reach development
--         data. Then   USE SECONDARY ROLES NONE;   and repeat: it must FAIL. Never run DELETE for this
--         demonstration, only SELECT. FAIL/NOT RUN if the prediction is wrong; record what you saw.
-- CLEAN-3 the delete-count guard: the cleanup procedure (script 99, section T) reports the same count as the
--         harness log for the run id, on the canary table, before anything real is cleaned.
-- CLEAN-1 the cleanup procedure itself is script 99, section T. It is NOT run here.
-- Cleanup of canary rows follows the same procedure; the canary table and view are removed, if at all,
-- only by the owner under script 99 section X.

-- Return to a safe default
USE ROLE SECURITYADMIN;
