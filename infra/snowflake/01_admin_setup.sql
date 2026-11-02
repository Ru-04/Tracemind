-- =====================================================================================
-- TraceMind / Snowflake — 01 ADMINISTRATOR SETUP            (revision 4, 2026-11-02)
-- =====================================================================================
-- STATUS : DRAFT FOR REVIEW. NOT EXECUTED by TraceMind or by the assistant. It contains
--          <<PLACEHOLDERS>> that are syntax errors until replaced. DO NOT USE "RUN ALL": each Part
--          starts with a guard line  SELECT <<..._DELETE_THIS_LINE>>;  that is a deliberate syntax
--          error. You delete a Part's guard ONLY after the verification named in it has been done, so
--          a worksheet cannot silently run on past a failed gate. (Whether Snowsight stops at the
--          first error is UNVERIFIED, which is why every Part has its own guard.)
-- WHO    : A human administrator, in Snowsight, running ONE PART AT A TIME with the role
--          named at the top of each part. TraceMind never runs this script and its runtime
--          users never hold any of these administrative roles.
-- SECRETS: none belong in this file. The only values to substitute are your own user name
--          and the two PUBLIC keys (public keys are not secret, but keep your edited copy
--          OUTSIDE the repository). Never put a private key, passphrase or password here.
--
-- DOCUMENTATION STATUS WORDS (used in every comment below)
--   VERIFIED   a named docs.snowflake.com page, read 2026-10-29, states it. URLs: the guide, section 4.
--   INFERRED   follows logically from VERIFIED facts but no page states it.
--   UNVERIFIED not stated by any page read. None of these words means "tested": nothing here has
--              been run against any Snowflake account. Snowflake will reject a wrong statement.
--
-- DECISION RECORD (owner review of 2026-11-02; assumed = the assistant's recommendations, see the guide §0)
--   O1 account-level monitor : DEFERRED until the read-only preflight lists existing warehouses (Part E stays commented)
--   O2 F20 network-policy review : recorded in PLAN.md            O3 TRACEMIND_TEST_CLEANER : approved (Part C, Part D)
--   O4 export interval 14400 s default / 7200 s floor (application setting, not SQL)
--   O5 key expiry 90 days (Part C; cannot be changed later, decide BEFORE running Part C)
--   O6 variable names: applied to .env.example                    O7 MERGE fallback : conditional on tests MERGE-1 / MERGE-4
--   O8 credit price / trial balance (and the unit the balance tile shows) : information the owner supplies
--   O9 (new, OPEN) secondary roles on the owner's user : see Part C; not decided here
--   STATUS OF THESE DECISIONS: applied by the assistant as its own recommendations because the owner said they
--   "have been made" without restating them; owner confirmation of O2-O7 and of the 7200 s floor is pending.
--
-- EXECUTION ORDER (deliberate; it is NOT alphabetical)
--   GATE  : run 00_preflight_readonly.sql. If you cannot USE ROLE ACCOUNTADMIN -> STOP here.
--           Nothing below has been run yet, so stopping here leaves the account untouched. Stopping
--           LATER is not free of residue: if Part B fails after Part A ran, the two databases, six
--           schemas and the SUSPENDED warehouse already exist. They are ungranted and cost nothing
--           while suspended; leave them, report the error, do NOT run Parts C or D, and remove them
--           only with the confirmed procedure in 99_teardown_DESTRUCTIVE.sql (X3, X4) if you wish.
--   A  SYSADMIN      databases, schemas, warehouse (created suspended; no TraceMind role can use it)
--   B  ACCOUNTADMIN  resource monitor, attached to the warehouse
--   --  GATE B->C   : run the GATE-B query in 02_verify_permissions.sql. If the monitor is not
--                     attached to TRACEMIND_WH, STOP: do not run Part C or Part D.
--   C  USERADMIN     roles, service users, role grants, key pairs
--   D  SECURITYADMIN privileges (the first moment any TraceMind role can use the warehouse)
--   E  (optional, ACCOUNTADMIN) account-level monitor      — DEFERRED (O1) until the preflight, commented out
--   F  (optional, SECURITYADMIN) network policy            — deferred (decision 5), commented out
--   Verify after each part with the statements named in docs/setup/snowflake-account-setup.md.
--
-- WHAT IT CREATES (all names are TraceMind-specific; nothing outside them is touched)
--   databases : TRACEMIND_DEV, TRACEMIND_TEST         (schemas TELEMETRY, KNOWLEDGE, META)
--   warehouse : TRACEMIND_WH            X-Small, auto-suspend 60 s, statement timeout 300 s
--   monitor   : TRACEMIND_WH_MONITOR    20 credits PROVISIONAL allowance (frequency NEVER)
--   roles     : TRACEMIND_MIGRATOR     creates tables/views; held by YOUR user; never by the app
--               TRACEMIND_WRITER       export job: INSERT + SELECT on tables (what MERGE needs: see Part D)
--               TRACEMIND_READER       query functions: SELECT on VIEWS only
--               TRACEMIND_TEST_CLEANER human-run test cleanup; privileges exist ONLY in TRACEMIND_TEST
--   users     : TRACEMIND_EXPORTER_SVC  -> TRACEMIND_WRITER only, key pair restricted to it
--               TRACEMIND_QUERY_SVC     -> TRACEMIND_READER only, key pair restricted to it
--
-- DOES NOT CREATE: tables or views. Those are versioned migrations written in M3 (S2) and
--                  run later, by hand, as TRACEMIND_MIGRATOR. Future grants below cover them.
--
-- THE MONITOR IS ONE SAFEGUARD, NOT A GUARANTEE
--   Documentation: monitors "are not intended for setting precise limits on credit usage";
--   suspension "may take some time ... even when the action is Suspend Immediate";
--   they cover WAREHOUSES ONLY (not serverless features or AI services); the trial's free balance
--   is a separate limit (the unit its tile shows is not stated); and trial-account behaviour is not documented.
--   The layered controls and every uncovered cost are listed in the guide, section 9.
--
-- SAFETY NOTES
--   * Idempotent (safe to re-run): CREATE ... IF NOT EXISTS, GRANT, ALTER WAREHOUSE SET.
--     IF NOT EXISTS skips creation but does NOT reconcile an object that already exists
--     with different settings. Run 00_preflight_readonly.sql first.
--   * NOT idempotent / changes something: ALTER USER ... ADD KEY PAIR (fails if the key
--     name exists; marked [CHANGES AUTH]), ALTER WAREHOUSE ... SET RESOURCE_MONITOR.
--   * DESTRUCTIVE statements are NOT in this file. See 99_teardown_DESTRUCTIVE.sql
--     (every statement there is commented out).
--   * No CREATE OR REPLACE is used anywhere, so no existing object is silently replaced.
--   * The runtime roles receive NO privilege on any database other than the two
--     TRACEMIND databases, and NO global privilege.
-- =====================================================================================


-- =====================================================================================
-- PART A  —  role: SYSADMIN   (or any role holding CREATE DATABASE and CREATE WAREHOUSE)
-- Creates containers and compute. Grants nothing to anyone, so no TraceMind role can use
-- the warehouse yet. (Until Part B is done, do not run any query on TRACEMIND_WH yourself.)
-- VERIFIED (create-warehouse): CREATE WAREHOUSE needs the account privilege CREATE WAREHOUSE,
-- held by SYSADMIN by default.
-- =====================================================================================
SELECT <<GATE_G1_ACCOUNTADMIN_VERIFIED__DELETE_THIS_LINE>>;     -- guard: delete only after G1 passed (00 preflight)
USE ROLE SYSADMIN;

CREATE DATABASE IF NOT EXISTS TRACEMIND_DEV COMMENT = 'TraceMind development data. Redacted telemetry copy only.';
CREATE DATABASE IF NOT EXISTS TRACEMIND_TEST COMMENT = 'TraceMind automated test data. Disposable; every row carries a test run id.';

-- META holds the migration bookkeeping table (migrator only). TELEMETRY and KNOWLEDGE hold
-- the exported data; the runtime roles see only what is granted to them in PART D.
CREATE SCHEMA IF NOT EXISTS TRACEMIND_DEV.META;
CREATE SCHEMA IF NOT EXISTS TRACEMIND_DEV.TELEMETRY;
CREATE SCHEMA IF NOT EXISTS TRACEMIND_DEV.KNOWLEDGE;
CREATE SCHEMA IF NOT EXISTS TRACEMIND_TEST.META;
CREATE SCHEMA IF NOT EXISTS TRACEMIND_TEST.TELEMETRY;
CREATE SCHEMA IF NOT EXISTS TRACEMIND_TEST.KNOWLEDGE;

-- VERIFIED (warehouses-overview): X-Small = 1 credit/hour, per-second billing, 60-second minimum
-- each time the warehouse starts. VERIFIED (create-warehouse): XSMALL accepted; INITIALLY_SUSPENDED,
-- AUTO_RESUME, cluster counts, STATEMENT_TIMEOUT_IN_SECONDS, STATEMENT_QUEUED_TIMEOUT_IN_SECONDS exist;
-- AUTO_SUSPEND "less than 30, or not a multiple of 30 ... might not result in the expected behavior"
-- (60 is fine); MAX_CLUSTER_COUNT above 1 needs Enterprise. STATEMENT_TIMEOUT is a server-side backstop
-- to the application's own shorter timeouts. It is also a session parameter (VERIFIED, parameters page);
-- how the warehouse value and a session value interact is UNVERIFIED, so the application must set its own
-- per-statement timeout and this value is only a backstop, not a guarantee. Created SUSPENDED.
CREATE WAREHOUSE IF NOT EXISTS TRACEMIND_WH WITH
    WAREHOUSE_SIZE                      = 'XSMALL'
    AUTO_SUSPEND                        = 60
    AUTO_RESUME                         = TRUE
    INITIALLY_SUSPENDED                 = TRUE
    MIN_CLUSTER_COUNT                   = 1
    MAX_CLUSTER_COUNT                   = 1
    STATEMENT_TIMEOUT_IN_SECONDS        = 300
    STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = 60
    COMMENT = 'TraceMind only. Do not share with other workloads.';


-- =====================================================================================
-- PART B  —  role: ACCOUNTADMIN ONLY   (the monitor, attached BEFORE any role can use the warehouse)
-- VERIFIED (create-resource-monitor): "This command can only be executed by account administrators."
-- VERIFIED (alter-warehouse): "Only the ACCOUNTADMIN role can assign warehouses to resource monitors."
-- IF YOU DO NOT HOLD ACCOUNTADMIN: STOP. Do not improvise, do not use another role, do not
-- continue to Part C/D. The warehouse stays unusable by every TraceMind role until this part is done.
-- =====================================================================================
SELECT <<PART_A_VERIFIED__DELETE_THIS_LINE>>;                   -- guard: delete only after Part A was verified
USE ROLE ACCOUNTADMIN;

-- Decision 1: 20 credits is a PROVISIONAL DEVELOPMENT ALLOWANCE, not a guaranteed total-cost cap.
--   FREQUENCY = NEVER : the quota is not renewed automatically (VERIFIED value). A frequency requires
--                        a START_TIMESTAMP (VERIFIED); IMMEDIATELY uses the current time.
--   UNVERIFIED          : how FREQUENCY = NEVER and the used-credit counter behave on a TRIAL account;
--                        no page read mentions monitors on trials. After creation, read
--                        SHOW RESOURCE MONITORS and record what it shows (frequency, start, quota, used).
--   Raising the quota later is a deliberate human act: ALTER RESOURCE MONITOR <name> SET CREDIT_QUOTA = <n>
--   (VERIFIED syntax; the privilege needed is NOT stated). Its TRIGGERS clause is NOT additive:
--   restating triggers replaces ALL of them (VERIFIED), so restate every one.
-- Triggers (VERIFIED actions NOTIFY, SUSPEND, SUSPEND_IMMEDIATE; <=5 NOTIFY, 1 of each other):
--   10 % NOTIFY  -> CANARY: the first e-mail proves notifications reach you. If usage passes 10 %
--                   (2 credits) and no e-mail has arrived, fix notifications BEFORE continuing.
--   50 %, 75 % NOTIFY
--   90 % SUSPEND           : running queries finish; new ones are refused. 90 rather than 100 because
--                            suspension is not instantaneous (docs recommend a buffer below 100 %).
--   100 % SUSPEND_IMMEDIATE: running queries are cancelled; may still take time to take effect.
-- NOTIFY goes "to all account administrators with notifications enabled" (VERIFIED). Notifications are
-- DISABLED BY DEFAULT: verify your e-mail address, then enable resource-monitor notifications in your
-- Snowsight profile (guide, step 1). The monitor counts cloud-services credits too, WITHOUT the daily
-- 10 % adjustment (VERIFIED). The monitor does NOT cover serverless features, AI services or storage.
-- VERIFIED (resource-monitors): "A warehouse-level resource monitor can monitor, but cannot suspend, credit
-- usage by cloud services", and queries run after a suspension "can still result in additional cloud services
-- costs": cloud-services credits can keep accruing and are not stopped by this monitor.
CREATE RESOURCE MONITOR IF NOT EXISTS TRACEMIND_WH_MONITOR WITH
    CREDIT_QUOTA    = 20
    FREQUENCY       = NEVER
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS ON 10  PERCENT DO NOTIFY
             ON 50  PERCENT DO NOTIFY
             ON 75  PERCENT DO NOTIFY
             ON 90  PERCENT DO SUSPEND
             ON 100 PERCENT DO SUSPEND_IMMEDIATE;

-- Attach it to the TraceMind warehouse ONLY (one warehouse per monitor = tightest control).
-- [CHANGES THE WAREHOUSE] Re-running is harmless. Does not affect any other warehouse.
ALTER WAREHOUSE TRACEMIND_WH SET RESOURCE_MONITOR = TRACEMIND_WH_MONITOR;

-- >>> GATE B->C: run GATE-B in 02_verify_permissions.sql NOW. The warehouse row must show
-- >>> resource_monitor = TRACEMIND_WH_MONITOR and state SUSPENDED. If not, STOP.


-- =====================================================================================
-- PART C  —  role: USERADMIN   (holds CREATE ROLE and CREATE USER by default — VERIFIED, create-user)
-- Creates identities and attaches each to exactly one role. Gives nobody warehouse access yet.
-- VERIFIED (security-access-control-overview): "Each securable object is owned by a single role, which
-- by default is the role used to create the object" -> USERADMIN owns the roles and users created here.
-- =====================================================================================
SELECT <<GATE_B_PASSED__DELETE_THIS_LINE>>;                     -- guard: delete only after GATE-B passed (02)
USE ROLE USERADMIN;

CREATE ROLE IF NOT EXISTS TRACEMIND_MIGRATOR    COMMENT = 'Creates/changes TraceMind tables and views. Held by a human administrator user, never by the application.';
CREATE ROLE IF NOT EXISTS TRACEMIND_WRITER       COMMENT = 'Runtime export job: INSERT and SELECT on TraceMind tables only.';
CREATE ROLE IF NOT EXISTS TRACEMIND_READER       COMMENT = 'Runtime query functions: SELECT on approved TraceMind views only.';
CREATE ROLE IF NOT EXISTS TRACEMIND_TEST_CLEANER COMMENT = 'Human-run test cleanup. Privileges exist ONLY in TRACEMIND_TEST; none in TRACEMIND_DEV.';

-- TYPE = SERVICE: a non-human identity (VERIFIED, admin-user-management: "They cannot log in using a
-- password"; PASSWORD cannot be set; they cannot enroll in MFA). UNVERIFIED: whether a SERVICE user can
-- open Snowsight (ALLOWED_INTERFACES is not described on the pages read) -> check DESC USER.
-- VERIFIED (create-user): DEFAULT_ROLE does not grant the role; CREATE USER does not verify that the
-- default role, warehouse or namespace exist, so a spelling error surfaces only at the first login.
CREATE USER IF NOT EXISTS TRACEMIND_EXPORTER_SVC
    TYPE              = SERVICE
    DEFAULT_ROLE      = TRACEMIND_WRITER
    DEFAULT_WAREHOUSE = TRACEMIND_WH
    DEFAULT_NAMESPACE = TRACEMIND_DEV
    COMMENT           = 'TraceMind export job (writer). Key-pair authentication only.';

CREATE USER IF NOT EXISTS TRACEMIND_QUERY_SVC
    TYPE              = SERVICE
    DEFAULT_ROLE      = TRACEMIND_READER
    DEFAULT_WAREHOUSE = TRACEMIND_WH
    DEFAULT_NAMESPACE = TRACEMIND_DEV
    COMMENT           = 'TraceMind query functions (reader). Key-pair authentication only.';

-- One role per service user. VERIFIED syntax (grant-role): GRANT ROLE <name> TO USER <user>; the
-- granting role needs OWNERSHIP of the role being granted, or MANAGE GRANTS. USERADMIN created the
-- roles (VERIFIED ownership rule above). Confirm with the owner column of SHOW ROLES LIKE 'TRACEMIND%'.
-- If a grant fails, STOP and report the error.
GRANT ROLE TRACEMIND_WRITER TO USER TRACEMIND_EXPORTER_SVC;
GRANT ROLE TRACEMIND_READER TO USER TRACEMIND_QUERY_SVC;

-- Decision 6: migrations are run BY HAND as TRACEMIND_MIGRATOR, so a human user must hold that role.
-- Decision 3: test cleanup is human-run as TRACEMIND_TEST_CLEANER (see Part D and script 99).
-- Replace <<OWNER_USER>> with YOUR Snowflake user name (the one you log in to Snowsight with).
-- Neither role is ever given to a service user, and neither has a key pair, so no migration or
-- cleanup credential can exist in the application's configuration.
--
-- !! SECONDARY ROLES CAN DEFEAT THE CLEANER'S BOUNDARY. VERIFIED (use-secondary-roles): "The currently-active
-- !! secondary roles set the context that determines whether the current user has the necessary privileges to
-- !! perform SQL actions"; ALL means every role granted to the user in addition to the primary role,
-- !! re-evaluated on each statement; only CREATE authorization is limited to the primary role. Your user will
-- !! hold TRACEMIND_MIGRATOR (which OWNS the development tables) and probably other roles. If secondary roles
-- !! are enabled, a session whose PRIMARY role is TRACEMIND_TEST_CLEANER can still delete development data
-- !! through the migrator's privileges. VERIFIED (create-user): DEFAULT_SECONDARY_ROLES = { ('ALL') | () } with
-- !! "Default: ALL" for users created with CREATE USER, so a user created today has ALL unless set otherwise.
-- !! UNVERIFIED: what YOUR existing sign-up user has (read it: 00_preflight_readonly.sql, DESC USER step), and the
-- !! ALTER USER form for changing it (the page does not mention ALTER USER for this property). Open owner decision
-- !! O9 (guide section 12): leave it and rely on the procedure, or set your own user's default to () yourself.
-- !! Therefore every cleanup or migration session starts with
-- !!     USE SECONDARY ROLES NONE;      and is checked with      SELECT CURRENT_SECONDARY_ROLES();
-- !! (script 99, section T). That is a procedure, not a privilege boundary: it is the reason the cleaner is a
-- !! separate role AND the reason the procedure exists. I did not alter your user's own default secondary roles.
GRANT ROLE TRACEMIND_MIGRATOR     TO USER <<OWNER_USER>>;
GRANT ROLE TRACEMIND_TEST_CLEANER TO USER <<OWNER_USER>>;

-- Decision 4: SYSADMIN is deliberately NOT given any TraceMind role.
-- VERIFIED (security-access-control-overview): "if a custom role is not assigned to SYSADMIN through a
-- role hierarchy, the system administrators cannot manage the objects owned by the role"; only roles with
-- MANAGE GRANTS (SECURITYADMIN by default) can then see those objects and change their grants. Snowflake
-- recommends placing custom roles under SYSADMIN, which would let every SYSADMIN inherit the writer,
-- reader, migrator and cleaner privileges. For THIS plan SYSADMIN needs none of that: it owns the containers
-- it created (Part A); SECURITYADMIN (MANAGE GRANTS) grants privileges (Part D); USERADMIN owns and grants
-- the roles and owns the users (Part C). Tables created later are owned by TRACEMIND_MIGRATOR, so table
-- maintenance and any cleanup is done as that role or the cleaner, never as SYSADMIN. Enable the lines
-- below only if you decide a SYSADMIN must administer these roles, and record why.
-- (None of the four TraceMind roles, TRACEMIND_TEST_CLEANER included, is to be granted to SYSADMIN: that
-- would give every SYSADMIN delete rights on test data and, through the migrator, ownership of everything.)
-- GRANT ROLE TRACEMIND_MIGRATOR TO ROLE SYSADMIN;
-- GRANT ROLE TRACEMIND_WRITER   TO ROLE SYSADMIN;
-- GRANT ROLE TRACEMIND_READER   TO ROLE SYSADMIN;

-- [CHANGES AUTH] Register the PUBLIC keys as NAMED key pairs (guide, step 2 generates them locally).
-- VERIFIED grammar (alter-user-add-key-pair):
--   ALTER USER <user> ADD KEY PAIR <name> PUBLIC_KEY = '<key>' [ROLE_RESTRICTION = '<role>']
--                                          [DAYS_TO_EXPIRY = <n>] [COMMENT = '<text>']
--   ROLE_RESTRICTION : the key pair authenticates only if the role requested in the JWT equals this role;
--                      the role must already be granted (it is, above); a later REVOKE makes the key fail.
--                      UNVERIFIED: how the Python connector places the role in the JWT (the key-pair-auth
--                      page does not say). The M3 connectivity check must test it, and the connector must
--                      always request the role explicitly.
--   DAYS_TO_EXPIRY   : 90 (decision O5) = the key stops working after 90 days unless rotated. VERIFIED
--                      (alter-user-modify-key-pair): MODIFY KEY PAIR cannot change the key, the role restriction
--                      or the expiry; to change any of them the key pair must be REMOVED and added again. Decide
--                      the number BEFORE running this part. Put a reminder in your calendar at about day 75.
--                      VERIFIED (alter-user-rotate-key-pair): ROTATE KEY PAIR <name> PUBLIC_KEY = '<new>' inherits
--                      the comment, "expiration duration" and role restriction of the rotated key pair; the old
--                      key is kept as <name>_ROTATED_<epoch_ms> for 24 h by default. Whether the new key's expiry
--                      DATE restarts a full 90 days is not stated: read it from SHOW USER KEY PAIRS afterwards.
--                      Reading expiry: SHOW USER KEY PAIRS FOR USER <u> needs MODIFY PROGRAMMATIC AUTHENTICATION
--                      METHODS on another user (VERIFIED); a service user cannot sign in to look at its own.
-- Paste the public key body only: no -----BEGIN/END----- lines, no line breaks.
-- VERIFIED (alter-user-add-key-pair): the running role needs MODIFY PROGRAMMATIC AUTHENTICATION METHODS on
-- the user (the key-pair-auth page also allows OWNERSHIP). USERADMIN owns the users it created (see above).
-- If this fails with "insufficient privileges", STOP; an administrator who may grant it can run the
-- commented statements below (example form VERIFIED on the key-pair-auth page), then retry:
--   USE ROLE SECURITYADMIN;
--   GRANT MODIFY PROGRAMMATIC AUTHENTICATION METHODS ON USER TRACEMIND_EXPORTER_SVC TO ROLE USERADMIN;
--   GRANT MODIFY PROGRAMMATIC AUTHENTICATION METHODS ON USER TRACEMIND_QUERY_SVC    TO ROLE USERADMIN;
--   (and REVOKE them afterwards if you want USERADMIN back to its default reach)
ALTER USER TRACEMIND_EXPORTER_SVC ADD KEY PAIR tm_exporter_v1
    PUBLIC_KEY       = '<<PUBLIC_KEY_BODY_EXPORTER>>'
    ROLE_RESTRICTION = 'TRACEMIND_WRITER'
    DAYS_TO_EXPIRY   = 90
    COMMENT          = 'TraceMind export job key, v1';
ALTER USER TRACEMIND_QUERY_SVC    ADD KEY PAIR tm_query_v1
    PUBLIC_KEY       = '<<PUBLIC_KEY_BODY_QUERY>>'
    ROLE_RESTRICTION = 'TRACEMIND_READER'
    DAYS_TO_EXPIRY   = 90
    COMMENT          = 'TraceMind query key, v1';

-- OPTIONAL hardening, commented out: LOCK_TIMEOUT is a session parameter that "can be set for
-- Account » User » Session" with a DEFAULT OF 43200 s (12 hours) (VERIFIED, parameters page). A writer
-- blocked behind another MERGE would otherwise wait up to 12 h. The application MUST set a short value on
-- every session regardless (M3); this line would only add a user-level default. UNVERIFIED: the exact
-- ALTER USER ... SET <session parameter> form was not read on the alter-user page.
-- ALTER USER TRACEMIND_EXPORTER_SVC SET LOCK_TIMEOUT = 120;


-- =====================================================================================
-- PART D  —  role: SECURITYADMIN   (holds MANAGE GRANTS, required for FUTURE grants — VERIFIED, grant-privilege)
-- All privileges, spelled out. Nothing is granted on any other database or globally.
-- This is the first point at which any TraceMind role can use the warehouse, and by now the
-- monitor is attached (Part B) and has been checked (GATE B->C).
-- VERIFIED (grant-privilege): schema-level future grants take precedence over database-level ones;
-- "Future grants are not applied when renaming or swapping a table" -> M3 migrations must not rename or swap
-- tables (or must re-grant afterwards). UNVERIFIED: whether a future grant applies to objects created by
-- ANY role (the page is silent). The canary test in 02_verify_permissions.sql section 4 proves it for
-- TRACEMIND_MIGRATOR before anything is trusted.
-- =====================================================================================
SELECT <<PART_C_VERIFIED__DELETE_THIS_LINE>>;                   -- guard: delete only after Part C was verified
USE ROLE SECURITYADMIN;

-- ---- compute: roles may USE the warehouse, nobody but its owner may change it -----------
-- VERIFIED (access-control-privileges): USAGE "enables using a virtual warehouse and, as a result,
-- executing queries". MODIFY / OPERATE / MONITOR are NOT granted, so no TraceMind role can resize it,
-- change auto-suspend, detach the monitor, suspend it, or view its usage.
GRANT USAGE ON WAREHOUSE TRACEMIND_WH TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE ON WAREHOUSE TRACEMIND_WH TO ROLE TRACEMIND_WRITER;
GRANT USAGE ON WAREHOUSE TRACEMIND_WH TO ROLE TRACEMIND_READER;
GRANT USAGE ON WAREHOUSE TRACEMIND_WH TO ROLE TRACEMIND_TEST_CLEANER;

-- ---- development database ---------------------------------------------------------------
GRANT USAGE ON DATABASE TRACEMIND_DEV TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE ON DATABASE TRACEMIND_DEV TO ROLE TRACEMIND_WRITER;
GRANT USAGE ON DATABASE TRACEMIND_DEV TO ROLE TRACEMIND_READER;
-- TRACEMIND_TEST_CLEANER receives NOTHING on TRACEMIND_DEV: not even USAGE. It cannot see or delete
-- development data. That is the privilege boundary for decision 3 — it holds ONLY while secondary roles are
-- off in the session (see Part C): with secondary roles on, the boundary is bypassed through the migrator.

-- Migrator: may create tables and views in the three schemas. It will OWN what it creates (VERIFIED
-- ownership rule). Because it owns the development tables it could also delete from them, so it is held
-- only by you, has no key pair, and is NOT the cleanup role.
-- VERIFIED (create-view): a view's creator needs SELECT on the tables the view reads (it owns them).
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA TRACEMIND_DEV.META      TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA TRACEMIND_DEV.TELEMETRY TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA TRACEMIND_DEV.KNOWLEDGE TO ROLE TRACEMIND_MIGRATOR;

-- Writer: sees only TELEMETRY and KNOWLEDGE (not META).
--
-- DECISION 7 — MERGE IS RETAINED FOR IDEMPOTENT EXPORT. What the exact operation needs:
--   MERGE INTO <target> AS t
--   USING ( <deduplicated batch: inline bound VALUES, or a session TEMPORARY table> ) AS s
--   ON t.project_id = s.project_id AND t.event_id = s.event_id
--   WHEN NOT MATCHED THEN INSERT ( ... ) VALUES ( ... );            -- NO "WHEN MATCHED" clause
--   * No WHEN MATCHED clause -> no UPDATE and no DELETE clause is ever executed.
--   * ON reads the target's columns -> SELECT on the target (INFERRED).
--   * WHEN NOT MATCHED ... INSERT -> INSERT on the target (INFERRED from "INSERT enables executing an
--     INSERT command on a table").
--   * The source needs no object privilege if it is inline values; a TEMPORARY table needs no CREATE TABLE
--     privilege (VERIFIED, create-table: "creating a temporary table does not require the CREATE TABLE
--     privilege"), only USAGE on the schema (INFERRED), which the writer has.
--   MINIMUM = INSERT + SELECT on the target tables. THIS IS AN INFERENCE. UNVERIFIED: the official MERGE
--   page and the privileges page state NO privilege rule for MERGE. Snowflake's own data-migration page
--   grants SELECT, INSERT, UPDATE, DELETE, TRUNCATE to a migration role that runs MERGE, but does not
--   claim that is the minimum. Third-party pages say MERGE needs INSERT, UPDATE and DELETE.
--   THEREFORE: the writer is granted INSERT + SELECT ONLY, and test MERGE-1 in script 02 section 4 must
--   PASS before the exporter is trusted. If it FAILS, STOP: the error text goes to the owner and NO grant
--   (above all NOT DELETE, and not UPDATE) is added without a new, explicit, reviewed decision.
--   Safety of retries and concurrency is NOT claimed here; see the guide, section 6.
GRANT USAGE ON SCHEMA TRACEMIND_DEV.TELEMETRY TO ROLE TRACEMIND_WRITER;
GRANT USAGE ON SCHEMA TRACEMIND_DEV.KNOWLEDGE TO ROLE TRACEMIND_WRITER;
GRANT INSERT, SELECT ON FUTURE TABLES IN SCHEMA TRACEMIND_DEV.TELEMETRY TO ROLE TRACEMIND_WRITER;
GRANT INSERT, SELECT ON FUTURE TABLES IN SCHEMA TRACEMIND_DEV.KNOWLEDGE TO ROLE TRACEMIND_WRITER;

-- Reader: sees only TELEMETRY and KNOWLEDGE and may SELECT from VIEWS only. It receives no privilege on
-- tables, so it can read only what a reviewed view exposes. VERIFIED (create-view): "the role can use the
-- view even if the role does not have privileges on the underlying table(s)". Not yet TESTED here: the
-- canary test in script 02 section 4 (READ-1) is the live confirmation.
GRANT USAGE ON SCHEMA TRACEMIND_DEV.TELEMETRY TO ROLE TRACEMIND_READER;
GRANT USAGE ON SCHEMA TRACEMIND_DEV.KNOWLEDGE TO ROLE TRACEMIND_READER;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA TRACEMIND_DEV.TELEMETRY TO ROLE TRACEMIND_READER;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA TRACEMIND_DEV.KNOWLEDGE TO ROLE TRACEMIND_READER;

-- ---- test database: identical shape, so tests exercise the same permissions ------------
GRANT USAGE ON DATABASE TRACEMIND_TEST TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE ON DATABASE TRACEMIND_TEST TO ROLE TRACEMIND_WRITER;
GRANT USAGE ON DATABASE TRACEMIND_TEST TO ROLE TRACEMIND_READER;
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA TRACEMIND_TEST.META      TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA TRACEMIND_TEST.TELEMETRY TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA TRACEMIND_TEST.KNOWLEDGE TO ROLE TRACEMIND_MIGRATOR;
GRANT USAGE ON SCHEMA TRACEMIND_TEST.TELEMETRY TO ROLE TRACEMIND_WRITER;
GRANT USAGE ON SCHEMA TRACEMIND_TEST.KNOWLEDGE TO ROLE TRACEMIND_WRITER;
GRANT INSERT, SELECT ON FUTURE TABLES IN SCHEMA TRACEMIND_TEST.TELEMETRY TO ROLE TRACEMIND_WRITER;
GRANT INSERT, SELECT ON FUTURE TABLES IN SCHEMA TRACEMIND_TEST.KNOWLEDGE TO ROLE TRACEMIND_WRITER;
GRANT USAGE ON SCHEMA TRACEMIND_TEST.TELEMETRY TO ROLE TRACEMIND_READER;
GRANT USAGE ON SCHEMA TRACEMIND_TEST.KNOWLEDGE TO ROLE TRACEMIND_READER;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA TRACEMIND_TEST.TELEMETRY TO ROLE TRACEMIND_READER;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA TRACEMIND_TEST.KNOWLEDGE TO ROLE TRACEMIND_READER;

-- Decision 3 — test cleanup role: SELECT and DELETE on test TABLES ONLY, in the two data schemas of
-- TRACEMIND_TEST only. VERIFIED (access-control-privileges): DELETE "enables executing a DELETE command on a
-- table". Whether a DELETE with a WHERE clause ALSO needs SELECT is not stated (UNVERIFIED), and SELECT is needed
-- to count before deleting anyway, so SELECT + DELETE is the conservative pairing. No TRUNCATE, no UPDATE, no INSERT,
-- no CREATE, and NOTHING in TRACEMIND_DEV or in META. Decision O3: approved. It is a human-held role,
-- granted to your user only (Part C), owns nothing, has no key pair, and is never granted to SYSADMIN or to
-- any other role. Ownership: USERADMIN owns the role; TRACEMIND_MIGRATOR owns the tables it will delete from.
-- Limits of this boundary: (1) inside TRACEMIND_TEST the role can delete ANY row, so the run-id guards in
-- script 99 section T are what narrow it; (2) secondary roles on your user can add privileges to a session
-- (see the warning in Part C), hence "USE SECONDARY ROLES NONE" at the start of every cleanup.
-- If the future-grant behaviour turns out not to reach migrator-created tables (UNVERIFIED), the role simply
-- has no privilege on them: cleanup becomes impossible, which fails SAFE (it never over-grants).
GRANT USAGE ON DATABASE TRACEMIND_TEST TO ROLE TRACEMIND_TEST_CLEANER;
GRANT USAGE ON SCHEMA TRACEMIND_TEST.TELEMETRY TO ROLE TRACEMIND_TEST_CLEANER;
GRANT USAGE ON SCHEMA TRACEMIND_TEST.KNOWLEDGE TO ROLE TRACEMIND_TEST_CLEANER;
GRANT SELECT, DELETE ON FUTURE TABLES IN SCHEMA TRACEMIND_TEST.TELEMETRY TO ROLE TRACEMIND_TEST_CLEANER;
GRANT SELECT, DELETE ON FUTURE TABLES IN SCHEMA TRACEMIND_TEST.KNOWLEDGE TO ROLE TRACEMIND_TEST_CLEANER;

-- Test isolation (decision 3) is NON-destructive by default: separate database, a unique test run id
-- in every test row (format and rules: guide, section 7), tests never connect to TRACEMIND_DEV, stale
-- rows are simply ignored. Application roles cannot delete. Deleting anything is an optional, human-run,
-- explicitly confirmed act (script 99, section T).


-- =====================================================================================
-- PART E  —  OPTIONAL, ACCOUNTADMIN, COMMENTED OUT ON PURPOSE.   DEFERRED (decision O1)
-- Decide only AFTER the read-only preflight (00) has listed every warehouse and monitor in the account:
--   * an account-level monitor already exists  -> add none (only one is allowed);
--   * other warehouses exist with no/long auto-suspend or a large size -> first consider fixing THOSE
--     (owner action), or add this backstop;
--   * only TRACEMIND_WH exists                  -> this part may be skipped (your decision).
-- An ACCOUNT-level monitor is a broader backstop. VERIFIED (alter-account, resource-monitors): it controls
-- credit usage of all virtual warehouses created in the account (including any default one created with the
-- trial), only one can exist, it suspends them ALL when it triggers, and it does not control serverless
-- features. It still lags and does not replace the controls in the guide, section 9.
-- Run 00_preflight_readonly.sql first: if an account-level monitor already exists, do NOT add another.
-- VERIFIED syntax (alter-account): ALTER ACCOUNT SET RESOURCE_MONITOR = <monitor_name>; the privilege is not
-- named for this clause (ACCOUNTADMIN is the role the page names for ALTER ACCOUNT).
-- The quota is the owner's number; no placeholder default is offered.
-- =====================================================================================
-- USE ROLE ACCOUNTADMIN;
-- CREATE RESOURCE MONITOR IF NOT EXISTS TRACEMIND_ACCOUNT_BACKSTOP WITH
--     CREDIT_QUOTA    = <<ACCOUNT_CREDIT_QUOTA>>
--     FREQUENCY       = NEVER
--     START_TIMESTAMP = IMMEDIATELY
--     TRIGGERS ON 50 PERCENT DO NOTIFY
--              ON 90 PERCENT DO SUSPEND
--              ON 100 PERCENT DO SUSPEND_IMMEDIATE;
-- ALTER ACCOUNT SET RESOURCE_MONITOR = TRACEMIND_ACCOUNT_BACKSTOP;


-- =====================================================================================
-- PART F  —  OPTIONAL: network policy. DEFERRED for initial local development (decision 5).
-- COMMENTED OUT ON PURPOSE. Residual risk and the concrete review triggers: guide, section 10.
-- VERIFIED (network-policies): policies can be set per user (ALTER USER <u> SET NETWORK_POLICY = <policy>);
-- creating one needs SECURITYADMIN or CREATE NETWORK POLICY; assigning one to a user needs OWNERSHIP of the
-- user and USAGE on the policy; user-level overrides account-level; the activating session's own IP must be
-- on the allowed list or activation fails.
-- VERIFIED (security-authentication-overview): long-lived credentials "must be mitigated with other security
-- measures like network policies and a robust storage and rotation strategy"; key-pair "doesn't require"
-- such measures, which "can result in less secure authentication".
-- UNVERIFIED: that a network policy is enforced on key-pair (JWT) logins — no page read says so.
-- =====================================================================================
-- USE ROLE SECURITYADMIN;
-- CREATE NETWORK POLICY IF NOT EXISTS TRACEMIND_SVC_POLICY
--     ALLOWED_IP_LIST = ('<<YOUR_STATIC_EGRESS_IP>>/32')
--     COMMENT = 'Only the machine that runs TraceMind';
-- ALTER USER TRACEMIND_EXPORTER_SVC SET NETWORK_POLICY = TRACEMIND_SVC_POLICY;
-- ALTER USER TRACEMIND_QUERY_SVC    SET NETWORK_POLICY = TRACEMIND_SVC_POLICY;

-- =====================================================================================
-- END. Next: 02_verify_permissions.sql (read-only checks), then the application-side
-- connectivity check that M3 adds. No data is created by this script.
-- =====================================================================================
