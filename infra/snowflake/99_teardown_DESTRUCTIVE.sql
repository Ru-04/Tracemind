-- =====================================================================================
-- TraceMind / Snowflake — 99 TEARDOWN AND CLEANUP  *** DESTRUCTIVE ***   (revision 4, 2026-11-02)
-- =====================================================================================
-- STATUS : DRAFT FOR REVIEW. NOT EXECUTED by TraceMind or by the assistant. The assistant never runs
--          this file and no automated process, test or CI job may.
-- EVERY STATEMENT BELOW IS COMMENTED OUT, so running this file as-is does nothing.
-- These statements permanently delete the named TraceMind objects and the data in them. Recovery is
-- limited: VERIFIED (drop-database, data-time-travel): a dropped database/table can be restored with
-- UNDROP only within its Time Travel retention (default 1 day on Standard Edition; up to 90 days on
-- Enterprise for permanent objects), after which Fail-safe recovery is possible only by Snowflake and is
-- not something to rely on. UNVERIFIED: the retention that applies to YOUR trial account. A DELETE of rows
-- is a different matter: do not assume a deleted row can be recovered. None of these statements touches a
-- non-TRACEMIND object.
--
-- CONFIRMATION PROTOCOL (decision 3) — all four steps, every time, for every destructive statement:
--   1. PREFER A LESS DESTRUCTIVE ACTION. Order of preference: do nothing (stale test rows carry a test run
--      id and are ignored) -> suspend/disable -> targeted delete of ONE test run id (section T)
--      -> permanent removal (section X). Use the first one that solves the problem.
--   2. RUN THE READ-ONLY INVENTORY for that statement first (given beside it) and read the result.
--   3. WRITE DOWN, outside Snowflake, what you expect to be deleted and why, with the number you expect,
--      then compare it with the inventory. If they differ in any way, STOP.
--   4. UNCOMMENT ONE STATEMENT ONLY, run it, re-comment it. Never run the whole file.
-- No runtime role (writer, reader) and no service user can or should run anything here.
-- =====================================================================================


-- =====================================================================================
-- SECTION R — REVERSIBLE (prefer these when in doubt)
-- =====================================================================================

-- R1. Stop spending: suspend the warehouse. VERIFIED (alter-warehouse): ALTER WAREHOUSE [IF EXISTS] <name>
--     SUSPEND needs the OPERATE privilege; SYSADMIN owns the warehouse. Auto-resume brings it back on the
--     next query by a role that holds USAGE, so also use R2/R3 to keep it off.
-- USE ROLE SYSADMIN;      ALTER WAREHOUSE IF EXISTS TRACEMIND_WH SUSPEND;
-- Inventory first:        SHOW WAREHOUSES LIKE 'TRACEMIND_WH';

-- R2. Disable a key pair without deleting it (VERIFIED, alter-user-modify-key-pair: SET DISABLED;
--     "a disabled key pair can't authenticate, but its metadata is kept and it can be re-enabled").
-- USE ROLE USERADMIN;     ALTER USER TRACEMIND_EXPORTER_SVC MODIFY KEY PAIR tm_exporter_v1 SET DISABLED = TRUE;
-- USE ROLE USERADMIN;     ALTER USER TRACEMIND_QUERY_SVC    MODIFY KEY PAIR tm_query_v1    SET DISABLED = TRUE;
-- Re-enable:              ... MODIFY KEY PAIR <name> SET DISABLED = FALSE;
-- Inventory first:        SHOW USER KEY PAIRS FOR USER TRACEMIND_EXPORTER_SVC;  (and QUERY_SVC)

-- R3. Disable a whole user (VERIFIED, alter-user: SET DISABLED = TRUE locks the user out and ABORTS running
--     statements). Reversible with = FALSE.
-- USE ROLE USERADMIN;     ALTER USER IF EXISTS TRACEMIND_EXPORTER_SVC SET DISABLED = TRUE;
-- USE ROLE USERADMIN;     ALTER USER IF EXISTS TRACEMIND_QUERY_SVC    SET DISABLED = TRUE;
-- Inventory first:        SHOW USERS LIKE 'TRACEMIND%';

-- R4. Take a human-held role away from your own user (it stays defined; re-grantable).
--     UNVERIFIED: the REVOKE ROLE ... FROM USER page was not read; the form below is the standard one.
-- USE ROLE USERADMIN;     REVOKE ROLE TRACEMIND_MIGRATOR     FROM USER <<OWNER_USER>>;
-- USE ROLE USERADMIN;     REVOKE ROLE TRACEMIND_TEST_CLEANER FROM USER <<OWNER_USER>>;
-- Inventory first:        SHOW GRANTS OF ROLE TRACEMIND_MIGRATOR;  SHOW GRANTS OF ROLE TRACEMIND_TEST_CLEANER;


-- =====================================================================================
-- SECTION T — TARGETED TEST CLEANUP (decision 3). TRACEMIND_TEST ONLY. Needs tables to exist.
--
-- WHY THIS IS AS SAFE AS IT CAN BE — six layers. They are NOT fully independent, and not all are enforced by
-- Snowflake. Said plainly:
--   * Protection of DEVELOPMENT data = L0 + L1 TOGETHER (L1 depends on L0). Everything else is secondary.
--   * Protection of OTHER TEST RUNS' rows (a well-formed but wrong run id) = L5, a human-read count; L3 only
--     rejects ill-formed ids.
--   * L2 blocks nothing by itself; L4 and L5 are checks a human must read.
--   L0 SECONDARY ROLES OFF. VERIFIED (use-secondary-roles): the privileges of active secondary roles count
--      towards authorization. Your user also holds TRACEMIND_MIGRATOR, which OWNS the development tables, and
--      VERIFIED (create-user) the default for a user created with CREATE USER is ALL (what your sign-up user has:
--      UNVERIFIED, see 00 preflight). Without T-1 below, a session whose primary role is the cleaner could
--      still delete development rows through the migrator. T-1 turns secondary roles off and checks it. This layer
--      is a PROCEDURE: it is what keeps L1 true for a human session. (Open decision O9: set your own user's
--      default to () so that this is no longer the default.)
--   L1 PRIVILEGES. The role used is TRACEMIND_TEST_CLEANER. With L0 satisfied it has NO privilege of any
--      kind on TRACEMIND_DEV (not even USAGE), so a DELETE against development data fails by privilege.
--      (TRACEMIND_MIGRATOR does own the development tables, which is why it is NOT used for deleting.)
--   L2 FULL QUALIFICATION. Every statement names TRACEMIND_TEST explicitly; it never relies on USE DATABASE.
--   L3 MARKER FORMAT. A test run id must match   tmtest-YYYYMMDDTHHMMSSZ-xxxxxxxx   (x = lower-case hex).
--      Real data carries NULL in the test-run-id column and can never match. The guard in T0 REFUSES any
--      other value, AND the DELETE statement itself repeats the format test (REGEXP_LIKE on the column), so
--      even a mistyped id cannot match a row that is not a test row.
--   L4 LEAK CHECK. T1b proves the marker appears nowhere in TRACEMIND_DEV; a non-zero count means a test
--      marker reached development data -> STOP, do not delete, report.
--   L5 COUNT + TRANSACTION. The delete is inside an explicit transaction and is COMMITTED only if the
--      number of rows deleted equals the number you wrote down; otherwise ROLLBACK.
-- NOT PROVIDED, on purpose: TRUNCATE; a DELETE without a test-run-id predicate; any statement against
-- TRACEMIND_DEV; deleting by time, by pattern or by "all test rows".
-- LIMIT: inside TRACEMIND_TEST the cleaner holds DELETE on whole tables. L3 and L5 are what narrow it.
--
-- ASSUMPTIONS (M3 design items, NOT yet implemented): every exported table has a nullable column named
-- test_run_id (NULL for real data); the test harness generates the run id in the format above and writes it
-- to its own log (that log is where <<EXPECTED_COUNT>> comes from); M3 code refuses to write a
-- tmtest- run id when it is connected to TRACEMIND_DEV. Change the column name here to match the migration.
-- VERIFIED (access-control-privileges): DELETE "enables executing a DELETE command on a table"; UNVERIFIED: whether
-- a DELETE with a WHERE clause also needs SELECT (the delete page names no privilege), so SELECT + DELETE is
-- the conservative grant. Syntax verified: BEGIN/COMMIT/ROLLBACK (begin page), DELETE ... WHERE (delete
-- page; the example output has a column "number of rows deleted"), REGEXP_LIKE anchors both ends
-- (regexp_like page), USE SECONDARY ROLES NONE and CURRENT_SECONDARY_ROLES() (their pages).
-- =====================================================================================

-- T-1 (SESSION SETUP; changes only your session) secondary roles OFF, then check. Do this FIRST, in every
--     cleanup session, and again if you reconnect or switch worksheets.
-- USE ROLE TRACEMIND_TEST_CLEANER;
-- USE SECONDARY ROLES NONE;
-- SELECT CURRENT_ROLE() AS primary_role, CURRENT_SECONDARY_ROLES() AS secondary_roles;
--   EXPECTED: primary_role = TRACEMIND_TEST_CLEANER and NO secondary role listed (the exact JSON when NONE is
--   active is not described on the function's page: record what you see). Any secondary role listed, or any
--   doubt -> STOP.
-- USE WAREHOUSE TRACEMIND_WH;

-- T0  (READ-ONLY) run-id guard. EXPECTED: RUN_ID_FORMAT_OK. Anything else: STOP.
-- SELECT CASE WHEN REGEXP_LIKE('<<RUN_ID>>', 'tmtest-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}')
--             THEN 'RUN_ID_FORMAT_OK' ELSE 'REFUSE: not a test run id' END AS guard;

-- T1a (READ-ONLY) how many rows would be deleted, and what share of the table that is. One table at a time.
-- COALESCE keeps rows_to_delete at 0 (not NULL) for an empty table.
-- SELECT COUNT(*)                                             AS total_rows,
--        COALESCE(SUM(IFF(test_run_id = '<<RUN_ID>>', 1, 0)), 0) AS rows_to_delete
-- FROM   TRACEMIND_TEST.TELEMETRY.<<TABLE>>;
--   rows_to_delete MUST equal <<EXPECTED_COUNT>> from the test log. If it does not, STOP.

-- T1b (READ-ONLY, role TRACEMIND_MIGRATOR because only it can read development data) LEAK CHECK.
--     Start this session the same way: USE ROLE TRACEMIND_MIGRATOR; USE SECONDARY ROLES NONE; check it.
-- USE ROLE TRACEMIND_MIGRATOR;
-- USE SECONDARY ROLES NONE;
-- SELECT CURRENT_ROLE() AS primary_role, CURRENT_SECONDARY_ROLES() AS secondary_roles;
-- SELECT COUNT(*) AS leaked_rows FROM TRACEMIND_DEV.TELEMETRY.<<TABLE>> WHERE test_run_id LIKE 'tmtest-%';
--   EXPECTED: 0. Non-zero -> STOP and report; this is a defect in the test harness, not a cleanup task.
--   (Then return to the cleaner for the delete: repeat T-1 first.)

-- T2  (DESTRUCTIVE) delete ONE run id from ONE test table, inside a transaction, as the cleaner.
--     T2a..T2e are SEPARATE statements. Uncomment and run them ONE AT A TIME, in order, reading each result.
--     NEVER uncomment T2d and T2e together: run exactly ONE of them. If everything were uncommented and run at
--     once, COMMIT would execute before anyone had read the count. (A Snowflake Scripting block that asserts the
--     count before committing was not used: its syntax was not verified.)
-- T2a  repeat T-1 first (cleaner, secondary roles NONE, checked), then:
-- BEGIN TRANSACTION;
-- T2b
-- DELETE FROM TRACEMIND_TEST.TELEMETRY.<<TABLE>>
--       WHERE test_run_id = '<<RUN_ID>>'
--         AND REGEXP_LIKE(test_run_id, 'tmtest-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}');
-- T2c  READ the output column "number of rows deleted" and compare it with <<EXPECTED_COUNT>>. No statement here.
-- T2d  ONLY IF the count equals <<EXPECTED_COUNT>>:
-- COMMIT;
-- T2e  INSTEAD of T2d if the count is different or you have any doubt (then report it):
-- ROLLBACK;
--      Never leave the transaction open: finish with exactly one of T2d / T2e.

-- T3  (READ-ONLY re-check) the T1a query must now show rows_to_delete = 0 and total_rows reduced by exactly
--     <<EXPECTED_COUNT>>.
-- Repeat T1a..T3 for each table that holds rows for that run id (TELEMETRY and KNOWLEDGE tables separately).


-- =====================================================================================
-- SECTION X — PERMANENT REMOVAL (destructive; strongest warnings in this file)
-- Start every session in this section with its role and   USE SECONDARY ROLES NONE;   so that only the
-- role named on the line is in effect.
-- =====================================================================================

-- X1. Remove credentials. VERIFIED grammar (alter-user-remove-key-pair): ALTER USER [IF EXISTS] <user>
--     REMOVE KEY PAIR <name>; "removal is permanent"; the name becomes reusable at once; the key can be
--     restored only by ADD KEY PAIR with the same public key.
-- USE ROLE USERADMIN;     ALTER USER IF EXISTS TRACEMIND_EXPORTER_SVC REMOVE KEY PAIR tm_exporter_v1;
-- USE ROLE USERADMIN;     ALTER USER IF EXISTS TRACEMIND_QUERY_SVC    REMOVE KEY PAIR tm_query_v1;
-- Inventory first:        SHOW USER KEY PAIRS FOR USER TRACEMIND_EXPORTER_SVC;  (and QUERY_SVC)

-- X2. Delete users and roles.
-- !! VERIFIED (drop-role): "Ownership of any objects owned by the dropped role is transferred to the role
-- !! that executes the DROP ROLE command", and all current and future grants naming the role are revoked.
-- !! So dropping TRACEMIND_MIGRATOR AFTER it has created tables does NOT delete them: the tables become
-- !! owned by USERADMIN, with their grants stripped. Decide what happens to those tables FIRST (X3 drops
-- !! them with the database), and drop the migrator only afterwards.
-- Inventory first:        SHOW USERS LIKE 'TRACEMIND%';   SHOW ROLES LIKE 'TRACEMIND%';
-- USE ROLE USERADMIN;     DROP USER IF EXISTS TRACEMIND_EXPORTER_SVC;
-- USE ROLE USERADMIN;     DROP USER IF EXISTS TRACEMIND_QUERY_SVC;
-- USE ROLE USERADMIN;     DROP ROLE IF EXISTS TRACEMIND_WRITER;
-- USE ROLE USERADMIN;     DROP ROLE IF EXISTS TRACEMIND_READER;
-- USE ROLE USERADMIN;     DROP ROLE IF EXISTS TRACEMIND_TEST_CLEANER;
-- USE ROLE USERADMIN;     DROP ROLE IF EXISTS TRACEMIND_MIGRATOR;     -- LAST, and only per the warning above

-- X3. Delete DATA — every table, view and row inside the database.
-- Inventory first (per database), READ-ONLY:
--     SHOW SCHEMAS IN DATABASE TRACEMIND_TEST;   SHOW TABLES IN DATABASE TRACEMIND_TEST;
--     (the same for TRACEMIND_DEV). Count rows in every table you are about to lose.
-- VERIFIED (drop-database): only the database owner (OWNERSHIP) can drop it; CASCADE, the default, drops
-- "the database and all objects in the database". UNVERIFIED: whether SYSADMIN (the database owner) may
-- drop a database that contains tables owned by TRACEMIND_MIGRATOR. Snowflake states that by default
-- "not even the ACCOUNTADMIN role can modify or drop objects created by a custom role" (access-control
-- best practices); read the error, and do not broaden privileges to get around it.
-- USE ROLE SYSADMIN;      DROP DATABASE IF EXISTS TRACEMIND_TEST;
-- USE ROLE SYSADMIN;      DROP DATABASE IF EXISTS TRACEMIND_DEV;       -- EXTRA CONFIRMATION: this is the development data

-- X4. Delete compute and budget.
-- Inventory first:        SHOW WAREHOUSES LIKE 'TRACEMIND_WH';   SHOW RESOURCE MONITORS LIKE 'TRACEMIND%';
-- USE ROLE SYSADMIN;      DROP WAREHOUSE IF EXISTS TRACEMIND_WH;
-- USE ROLE ACCOUNTADMIN;  DROP RESOURCE MONITOR IF EXISTS TRACEMIND_WH_MONITOR;
-- (Dropping the monitor removes the spending guard; do it only after the warehouse is gone or suspended
--  and you have decided you no longer need it. UNVERIFIED: the DROP RESOURCE MONITOR page was not read.)
