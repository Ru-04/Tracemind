"""ingestion events, incidents, counters

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("service", sa.Text, nullable=False),
        sa.Column("environment", sa.Text, nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.Column("received_at", TZ, nullable=False),
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
        sa.CheckConstraint(
            "severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')", name="ck_events_severity"
        ),
    )
    op.create_index("ix_events_fingerprint", "events", ["project_id", "fingerprint"])
    op.create_index("ix_events_occurred", "events", ["project_id", "occurred_at"])

    op.create_table(
        "incidents",
        sa.Column("incident_id", sa.Text, primary_key=True),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("service", sa.Text, nullable=False),
        sa.Column("environment", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("fingerprint_version", sa.Integer, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("first_seen", TZ, nullable=False),
        sa.Column("last_seen", TZ, nullable=False),
        sa.Column("occurrence_count", sa.Integer, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "service",
            "environment",
            "fingerprint",
            "fingerprint_version",
            name="uq_incident_group",
        ),
        sa.CheckConstraint("severity IN ('ERROR','CRITICAL')", name="ck_incidents_severity"),
        sa.CheckConstraint(
            "status IN ('open','investigating','resolved','ignored')", name="ck_incidents_status"
        ),
    )
    op.create_index("ix_incidents_list", "incidents", ["project_id", "last_seen", "incident_id"])

    op.create_table(
        "incident_occurrences",
        sa.Column("incident_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("incident_id", "project_id", "event_id", name="pk_incident_occurrences"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.incident_id"], name="fk_occ_incident"),
        sa.ForeignKeyConstraint(
            ["project_id", "event_id"], ["events.project_id", "events.event_id"], name="fk_occ_event"
        ),
    )

    op.create_table(
        "ingestion_stats",
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("hour_bucket", TZ, nullable=False),
        sa.Column("accepted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duplicate", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duplicate_conflict", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rejected", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("project_id", "hour_bucket", name="pk_ingestion_stats"),
    )

    op.create_table(
        "ingestion_rejections",
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("hour_bucket", TZ, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("count", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("project_id", "hour_bucket", "reason", name="pk_ingestion_rejections"),
    )


def downgrade() -> None:
    for table in (
        "ingestion_rejections",
        "ingestion_stats",
        "incident_occurrences",
        "incidents",
        "events",
    ):
        op.drop_table(table)
