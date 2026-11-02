-- =====================================================================================
-- TraceMind  -  versioned schema script V001  (M3 check M3-1)
-- =====================================================================================
-- STATUS : DRAFT. NEVER EXECUTED. Written offline; no Snowflake account has seen it.
--
-- WHO RUNS IT  : you, as TRACEMIND_MIGRATOR (the human-held role, no key pair), in Snowsight,
--                once per database: first TRACEMIND_DEV, later TRACEMIND_TEST (change the
--                USE DATABASE line). TraceMind never runs DDL and holds no admin credential.
-- WHEN         : only after infra/snowflake/01_admin_setup.sql Parts A-D and 02_verify_permissions
--                section 1 have been run and recorded PASS. The guard line below is a deliberate
--                syntax error; delete it only after that.
-- IDEMPOTENT   : every statement is CREATE ... IF NOT EXISTS, or an INSERT guarded by NOT EXISTS.
--                Running the script twice changes nothing the second time (M3-1). Nothing is
--                dropped, truncated or replaced. A later change is a NEW script (V002...), and it
--                must not rename or swap a table (future grants are not applied on rename/swap).
-- GRANTS       : none here. The writer, reader and cleaner privileges come from FUTURE grants in
--                01_admin_setup.sql Part D. Whether those future grants reach the objects created
--                below is UNVERIFIED; test FUT-1 in 02_verify_permissions.sql section 4 settles it.
-- CONSTRAINTS  : none declared. PRIMARY KEY and UNIQUE are NOT ENFORCED on standard tables
--                (documented), so the logical keys in the comments below are protected by the
--                exporter (single exporter, de-duplicated batches, duplicate audit), not by Snowflake.
-- TEST ROWS    : every table has a nullable test_run_id. Real data holds NULL. A test row holds a
--                'tmtest-YYYYMMDDTHHMMSSZ-xxxxxxxx' id, never in TRACEMIND_DEV. The reader views
--                do not expose the column.
-- =====================================================================================
SELECT <<V001_PREREQUISITES_VERIFIED__DELETE_THIS_LINE>>;
USE ROLE TRACEMIND_MIGRATOR;
USE SECONDARY ROLES NONE;                 -- the migrator must act as the migrator, nothing wider
USE DATABASE TRACEMIND_DEV;               -- run again with TRACEMIND_TEST for the test database
SELECT CURRENT_ROLE() AS role, CURRENT_SECONDARY_ROLES() AS secondary_roles, CURRENT_DATABASE() AS db;

-- ---------------------------------------------------------------------------------
-- META : bookkeeping (migrator only; the runtime roles have no access to this schema)
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS META.SCHEMA_VERSION (
    version     STRING        NOT NULL,
    description STRING,
    applied_at  TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    applied_by  STRING        NOT NULL DEFAULT CURRENT_USER()
);

-- ---------------------------------------------------------------------------------
-- TELEMETRY.EVENTS : normalized, already-redacted events.  Logical key (project_id, event_id).
-- Columns match tracemind.snowflake.sql.EVENTS (a unit test compares them).
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TELEMETRY.EVENTS (
    project_id          STRING        NOT NULL,
    event_id            STRING        NOT NULL,
    service             STRING        NOT NULL,
    environment         STRING        NOT NULL,
    occurred_at         TIMESTAMP_TZ  NOT NULL,
    severity            STRING        NOT NULL,
    error_type          STRING,
    message             STRING,
    fingerprint         STRING        NOT NULL,
    fingerprint_version NUMBER(38,0)  NOT NULL,
    trace_id            STRING,
    commit_sha          STRING,
    release             STRING,
    attributes          VARIANT,
    exported_at         TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    test_run_id         STRING
);

-- ---------------------------------------------------------------------------------
-- KNOWLEDGE.INCIDENT_SUMMARIES : one row per incident VERSION.
-- Logical key (project_id, incident_id, summary_version); summary_version is the incident's
-- updated_at at export time.
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS KNOWLEDGE.INCIDENT_SUMMARIES (
    project_id          STRING        NOT NULL,
    incident_id         STRING        NOT NULL,
    summary_version     STRING        NOT NULL,
    service             STRING        NOT NULL,
    environment         STRING        NOT NULL,
    fingerprint         STRING        NOT NULL,
    fingerprint_version NUMBER(38,0)  NOT NULL,
    error_type          STRING,
    severity            STRING        NOT NULL,
    status              STRING        NOT NULL,
    first_seen          TIMESTAMP_TZ  NOT NULL,
    last_seen           TIMESTAMP_TZ  NOT NULL,
    occurrence_count    NUMBER(38,0)  NOT NULL,
    reopen_count        NUMBER(38,0)  NOT NULL,
    summary             STRING,
    exported_at         TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    test_run_id         STRING
);

-- ---------------------------------------------------------------------------------
-- KNOWLEDGE.DEPLOYMENTS : deployment records.  Logical key (project_id, deployment_id).
-- commit_sha is non-null ONLY when TraceMind verified it (commit_confidence = 'verified').
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS KNOWLEDGE.DEPLOYMENTS (
    project_id          STRING        NOT NULL,
    deployment_id       STRING        NOT NULL,
    environment         STRING        NOT NULL,
    service             STRING,
    release             STRING        NOT NULL,
    deployed_at         TIMESTAMP_TZ  NOT NULL,
    commit_sha          STRING,
    commit_confidence   STRING        NOT NULL,
    exported_at         TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    test_run_id         STRING
);

-- ---------------------------------------------------------------------------------
-- KNOWLEDGE.INVESTIGATION_RESULTS : filled by M4. Created now so the schema is stable; it stays
-- EMPTY until an evidence-carrying resolution operation exists. Nothing exports to it in M3.
-- ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS KNOWLEDGE.INVESTIGATION_RESULTS (
    project_id          STRING        NOT NULL,
    investigation_id    STRING        NOT NULL,
    incident_id         STRING        NOT NULL,
    fingerprint         STRING        NOT NULL,
    resolution_state    STRING        NOT NULL,
    note                STRING,
    result              VARIANT,
    evidence_ids        VARIANT,
    recorded_at         TIMESTAMP_TZ  NOT NULL,
    exported_at         TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    test_run_id         STRING
);

-- ---------------------------------------------------------------------------------
-- Reader VIEWS. The reader role holds SELECT on views only (a role can query a view without
-- privileges on its tables - documented). IF NOT EXISTS, never OR REPLACE: a replaced view's
-- grants would need re-checking (see V002 for any change). test_run_id is deliberately absent.
-- ---------------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS TELEMETRY.V_EVENTS AS
    SELECT project_id, event_id, service, environment, occurred_at, severity, error_type, message,
           fingerprint, fingerprint_version, trace_id, commit_sha, release, attributes
    FROM TELEMETRY.EVENTS;

CREATE VIEW IF NOT EXISTS KNOWLEDGE.V_INCIDENT_SUMMARIES AS
    SELECT project_id, incident_id, summary_version, service, environment, fingerprint,
           fingerprint_version, error_type, severity, status, first_seen, last_seen,
           occurrence_count, reopen_count, summary
    FROM KNOWLEDGE.INCIDENT_SUMMARIES;

CREATE VIEW IF NOT EXISTS KNOWLEDGE.V_DEPLOYMENTS AS
    SELECT project_id, deployment_id, environment, service, release, deployed_at, commit_sha,
           commit_confidence
    FROM KNOWLEDGE.DEPLOYMENTS;

CREATE VIEW IF NOT EXISTS KNOWLEDGE.V_RESOLUTIONS AS
    SELECT project_id, incident_id, fingerprint, resolution_state, note, recorded_at
    FROM KNOWLEDGE.INVESTIGATION_RESULTS;

-- ---------------------------------------------------------------------------------
-- Record the version (once).
-- ---------------------------------------------------------------------------------
INSERT INTO META.SCHEMA_VERSION (version, description)
SELECT 'V001', 'telemetry events, incident summaries, deployments, investigation results, reader views'
WHERE NOT EXISTS (SELECT 1 FROM META.SCHEMA_VERSION WHERE version = 'V001');

-- Read-only confirmation (record the result; do not paste it anywhere with account details):
SELECT version, description, applied_at, applied_by FROM META.SCHEMA_VERSION ORDER BY applied_at;
SELECT table_schema, table_name, table_type
FROM INFORMATION_SCHEMA.TABLES
WHERE table_schema IN ('META', 'TELEMETRY', 'KNOWLEDGE')
ORDER BY table_schema, table_name;
