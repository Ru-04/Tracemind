-- =====================================================================================
-- TraceMind / Snowflake — 00 PREFLIGHT (READ-ONLY)                       (revision 4, 2026-11-02)
-- =====================================================================================
-- STATUS : DRAFT FOR REVIEW. NOT EXECUTED by TraceMind or by the assistant. It is run by YOU, in a
--          Snowsight SQL worksheet, only after you explicitly approve the preflight.
-- NATURE : Every statement below is USE ROLE, SET of a session variable, SELECT of context functions, SHOW or
--          DESC USER. Nothing creates, alters, grants, drops or resumes a warehouse. VERIFIED: SHOW WAREHOUSES
--          "doesn't require a running warehouse"; that the other statements need none, and whether they consume
--          any cloud-services credit, is not stated on the pages read (UNVERIFIED; expected to be negligible).
--          If any statement asks for a warehouse, STOP and tell me: do not start one to make it run.
-- PURPOSE: Establish facts before running anything: which account and region you are in, which roles you
--          actually hold, whether you can assume ACCOUNTADMIN, what compute and monitors already exist, and
--          whether any TRACEMIND objects already exist.
-- SECRETS: none. Do not paste query results containing credentials anywhere. Account identifiers, region and
--          role names are not secrets and may be shared (the account identifier is better kept to yourself).
-- RECORDING RESULTS: for every numbered item write PASS, FAIL or NOT RUN.
--          PASS only if you ran the statement and its output matched what the comment says to expect.
--          A statement you skipped, or one that returned nothing you checked, is NOT RUN — never PASS.
--
-- EXACT PERMISSIONS, STATEMENT BY STATEMENT (what the documentation says; nothing here is tested)
--   Step 1  SELECT CURRENT_*()            : no privilege needed.
--   Step 2  SHOW GRANTS (= TO USER you), SET me, DESC USER IDENTIFIER($me) : your own user; no special
--           privilege expected (UNVERIFIED for DESC USER).
--   GATE    USE ROLE ACCOUNTADMIN         : only works if ACCOUNTADMIN is granted to your user.
--   Step 3  SHOW DATABASES/WAREHOUSES/ROLES/USERS/RESOURCE MONITORS, SHOW NETWORK POLICIES
--           VERIFIED (show-warehouses, show-resource-monitors): each SHOW lists only objects on which the
--           CURRENT role holds at least one privilege, except that "The MANAGE GRANTS access privilege
--           implicitly allows its holder to see every object in the account" (held by SECURITYADMIN and
--           ACCOUNTADMIN by default). So Steps 3-4 run as SECURITYADMIN; run as any other role they may
--           silently omit warehouses and monitors, which would make the preflight misleading.
--   Step 4  SHOW PARAMETERS ... IN ACCOUNT: the privilege needed is NOT stated (UNVERIFIED). If it errors
--           as SECURITYADMIN, repeat it as ACCOUNTADMIN and record that.
--   No step needs a running warehouse, and none needs any privilege on TRACEMIND objects (none exist yet).
-- =====================================================================================

-- STEP 1. Where am I, and who am I? (standard context functions; if one errors, record it and continue)
SELECT CURRENT_ACCOUNT()  AS account_locator,
       CURRENT_REGION()   AS region,
       CURRENT_USER()     AS me,
       CURRENT_ROLE()     AS current_role,
       CURRENT_VERSION()  AS snowflake_version;

-- The ACCOUNT EDITION is not read from SQL here. Look at it in the sign-up e-mail / Snowsight and note it.
-- It decides which features exist (Time Travel length, multi-cluster) and, with the region, the credit price.

-- STEP 2. Which roles do I hold? Do NOT assume ACCOUNTADMIN. Look for ACCOUNTADMIN, SECURITYADMIN,
--         USERADMIN, SYSADMIN in the output. The setup script says which role each part needs.
-- VERIFIED (show-grants): the no-argument form "SHOW GRANTS" is "syntactically equivalent to SHOW GRANTS TO USER
-- <current_user>" and lists the roles granted to you. (An earlier draft used IDENTIFIER(CURRENT_USER()); the
-- IDENTIFIER page lists only a string literal, session variable, bind variable or scripting variable, so a
-- function call is not a documented argument and that form was removed.)
SHOW GRANTS;
-- Your user's properties, including its DEFAULT_SECONDARY_ROLES (needed for decision O9: VERIFIED that the
-- CREATE USER default is ALL; what YOUR sign-up user has is UNVERIFIED until you read it here). SET only creates
-- a session variable; IDENTIFIER($me) with a session variable is a documented form. The privilege to DESC your
-- own user and the exact property name shown are UNVERIFIED: record what you see, or NOT RUN if it errors.
SET me = CURRENT_USER();
DESC USER IDENTIFIER($me);

-- GATE. Script 01 Part B (the resource monitor) can ONLY be run by ACCOUNTADMIN (VERIFIED). If the next
-- statement errors, or ACCOUNTADMIN is not in the grants above: STOP. Do not run any part of script 01,
-- because the warehouse must not exist unmonitored. Nothing has been created yet, so stopping is free.
USE ROLE ACCOUNTADMIN;
SELECT CURRENT_ROLE() AS gate_role;      -- EXPECTED: ACCOUNTADMIN

-- STEPS 3 and 4 run as SECURITYADMIN so that SHOW lists EVERYTHING (see the permissions note above).
USE ROLE SECURITYADMIN;

-- STEP 3. Do any TraceMind objects already exist? (Expect none on a fresh account.)
SHOW DATABASES  LIKE 'TRACEMIND%';
SHOW WAREHOUSES LIKE 'TRACEMIND%';
SHOW ROLES      LIKE 'TRACEMIND%';
SHOW USERS      LIKE 'TRACEMIND%';
SHOW RESOURCE MONITORS LIKE 'TRACEMIND%';

-- STEP 4. What guardrails and OTHER compute does the account already have? (decision O1 depends on this)
--    Any warehouse (for example a default one created with the trial) can also spend your free balance.
--    For EACH warehouse record: name, size, auto_suspend (null = never suspends), auto_resume,
--    resource_monitor, state and owner. These are the documented SHOW WAREHOUSES columns.
SHOW WAREHOUSES;
--    For EACH monitor record: its name and its "level" column (WAREHOUSE / ACCOUNT / NULL). The page read
--    documents only the "level" column; the quota and used-credit column names are NOT documented there.
SHOW RESOURCE MONITORS;
SHOW NETWORK POLICIES;
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN ACCOUNT;
SHOW PARAMETERS LIKE 'LOCK_TIMEOUT' IN ACCOUNT;    -- documented default 43200 s (12 h)
-- TRIAL NOTE: no documentation page read says whether resource monitors can be created, or how they interact
-- with the free balance, on a TRIAL account. Record what SHOW RESOURCE MONITORS lists (it may already contain
-- a monitor you did not create) and treat the behaviour as UNVERIFIED until script 01 Part B has run and
-- script 02 section 1 (1g) has been checked.

-- STEP 5 (decision O1). Fill in, in your own notes (do not send credentials):
--   * only TRACEMIND-owned compute will exist           -> skip the account-level monitor (Part E)
--   * another warehouse is large or never suspends      -> fix it yourself, or choose Part E
--   * an ACCOUNT-level monitor already exists           -> add none
--   Record the facts; the decision is yours.

-- If any TRACEMIND object already exists: STOP and review it before running script 01.
-- The setup script uses IF NOT EXISTS, which skips creation but does NOT reconcile an
-- existing object with what the script describes.
