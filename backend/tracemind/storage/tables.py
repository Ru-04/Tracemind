"""Table definitions. The Alembic migration in migrations/versions is the schema of record."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

events = sa.Table(
    "events",
    metadata,
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.Column("service", sa.Text, nullable=False),
    sa.Column("environment", sa.Text, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("severity", sa.Text, nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("error_type", sa.Text),
    sa.Column("stack_trace", sa.Text),
    sa.Column("trace_id", sa.Text),
    sa.Column("span_id", sa.Text),
    sa.Column("release", sa.Text),
    sa.Column("commit_sha", sa.Text),
    sa.Column("attributes", JSONB, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("fingerprint_version", sa.Integer, nullable=False),
    sa.Column("redactions_applied", sa.Integer, nullable=False),
    sa.Column("source", sa.Text),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("project_id", "event_id", name="pk_events"),
)

incidents = sa.Table(
    "incidents",
    metadata,
    sa.Column("incident_id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("service", sa.Text, nullable=False),
    sa.Column("environment", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("fingerprint_version", sa.Integer, nullable=False),
    sa.Column("severity", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="open"),
    sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    sa.Column("occurrence_count", sa.Integer, nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("reopen_count", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint(
        "project_id",
        "service",
        "environment",
        "fingerprint",
        "fingerprint_version",
        name="uq_incident_group",
    ),
)

incident_occurrences = sa.Table(
    "incident_occurrences",
    metadata,
    sa.Column("incident_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint(
        "incident_id", "project_id", "event_id", name="pk_incident_occurrences"
    ),
)

ingestion_stats = sa.Table(
    "ingestion_stats",
    metadata,
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False),
    sa.Column("accepted", sa.Integer, nullable=False, server_default="0"),
    sa.Column("duplicate", sa.Integer, nullable=False, server_default="0"),
    sa.Column("duplicate_conflict", sa.Integer, nullable=False, server_default="0"),
    sa.Column("rejected", sa.Integer, nullable=False, server_default="0"),
    sa.PrimaryKeyConstraint("project_id", "hour_bucket", name="pk_ingestion_stats"),
)

ingestion_rejections = sa.Table(
    "ingestion_rejections",
    metadata,
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("count", sa.Integer, nullable=False, server_default="0"),
    sa.PrimaryKeyConstraint("project_id", "hour_bucket", "reason", name="pk_ingestion_rejections"),
)

# ---- M2: repository intelligence ----------------------------------------------------------

_TZ = sa.DateTime(timezone=True)

repositories = sa.Table(
    "repositories",
    metadata,
    sa.Column("repository_id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("identity", sa.Text, nullable=False),
    sa.Column("default_branch", sa.Text),
    sa.Column("indexed_sha", sa.Text),
    sa.Column("indexed_scan_id", sa.Text),
    sa.Column("graph_sha256", sa.Text),
    sa.Column("created_at", _TZ, nullable=False),
)

repository_scans = sa.Table(
    "repository_scans",
    metadata,
    sa.Column("scan_id", sa.Text, primary_key=True),
    sa.Column("repository_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("target_sha", sa.Text, nullable=False),
    sa.Column("base_sha", sa.Text),
    sa.Column("graph_tool", sa.Text),
    sa.Column("graph_tool_version", sa.Text),
    sa.Column("graph_schema_version", sa.Integer),
    sa.Column("graph_sha256", sa.Text),
    sa.Column("node_count", sa.Integer),
    sa.Column("edge_count", sa.Integer),
    sa.Column("inferred_edge_count", sa.Integer),
    sa.Column("files_in_snapshot", sa.Integer),
    sa.Column("files_in_graph", sa.Integer),
    sa.Column("coverage", JSONB),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("available_at", _TZ, nullable=False),
    sa.Column("locked_by", sa.Text),
    sa.Column("lease_expires_at", _TZ),
    sa.Column("error_code", sa.Text),
    sa.Column("queued_at", _TZ, nullable=False),
    sa.Column("started_at", _TZ),
    sa.Column("finished_at", _TZ),
)

change_records = sa.Table(
    "change_records",
    metadata,
    sa.Column("record_id", sa.Text, primary_key=True),
    sa.Column("repository_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("scan_id", sa.Text, nullable=False),
    sa.Column("base_sha", sa.Text),
    sa.Column("target_sha", sa.Text, nullable=False),
    sa.Column("files", JSONB, nullable=False),
    sa.Column("commits", JSONB, nullable=False),
    sa.Column("graph_sha256", sa.Text),
    sa.Column("graph_delta", JSONB, nullable=False),
    sa.Column("created_at", _TZ, nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
)

repository_commits = sa.Table(
    "repository_commits",
    metadata,
    sa.Column("repository_id", sa.Text, nullable=False),
    sa.Column("sha", sa.Text, nullable=False),
    sa.Column("parent_shas", JSONB, nullable=False),
    sa.Column("authored_at", _TZ, nullable=False),
    sa.Column("committed_at", _TZ, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("retrieved_at", _TZ, nullable=False),
    sa.PrimaryKeyConstraint("repository_id", "sha", name="pk_repository_commits"),
)

scan_events = sa.Table(
    "scan_events",
    metadata,
    sa.Column("event_id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("scan_id", sa.Text, nullable=False),
    sa.Column("repository_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("at", _TZ, nullable=False),
    sa.Column("event", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("worker_id", sa.Text),
    sa.Column("error_code", sa.Text),
)

# ---- M3: incident lifecycle and deployments ------------------------------------------------

incident_transitions = sa.Table(
    "incident_transitions",
    metadata,
    sa.Column("transition_id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("incident_id", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("at", _TZ, nullable=False),
    sa.Column("from_status", sa.Text, nullable=False),
    sa.Column("to_status", sa.Text, nullable=False),
    sa.Column("actor_kind", sa.Text, nullable=False),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("note", sa.Text),
    sa.Column("trigger_event_id", sa.Text),
)

deployments = sa.Table(
    "deployments",
    metadata,
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("deployment_id", sa.Text, nullable=False),
    sa.Column("environment", sa.Text, nullable=False),
    sa.Column("service", sa.Text),
    sa.Column("release", sa.Text, nullable=False),
    sa.Column("deployed_at", _TZ, nullable=False),
    sa.Column("repository_id", sa.Text),
    sa.Column("commit_sha_reported", sa.Text),
    sa.Column("commit_sha", sa.Text),
    sa.Column("commit_confidence", sa.Text, nullable=False),
    sa.Column("commit_source", sa.Text),
    sa.Column("commit_reason", sa.Text),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("created_at", _TZ, nullable=False),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("project_id", "deployment_id", name="pk_deployments"),
)

# ---- M3: Snowflake export state (PostgreSQL side) -------------------------------------------

export_marks = sa.Table(
    "export_marks",
    metadata,
    sa.Column("stream", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("exported_at", _TZ, nullable=False),
    sa.Column("batch_id", sa.Text, nullable=False),
    sa.Column("version_at", _TZ),
    sa.PrimaryKeyConstraint("stream", "project_id", "key", name="pk_export_marks"),
)

export_state = sa.Table(
    "export_state",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("last_started_at", _TZ),
    sa.Column("last_success_at", _TZ),
    sa.Column("next_attempt_at", _TZ),
    sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error_code", sa.Text),
    sa.Column("updated_at", _TZ, nullable=False),
)

snowflake_calls = sa.Table(
    "snowflake_calls",
    metadata,
    sa.Column("call_id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("started_at", _TZ, nullable=False),
    sa.Column("duration_ms", sa.Integer, nullable=False),
    sa.Column("function", sa.Text, nullable=False),
    sa.Column("project_id", sa.Text),
    sa.Column("rows", sa.Integer),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("query_id", sa.Text),
)
