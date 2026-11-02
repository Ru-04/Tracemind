"""export markers, export state and the Snowflake call log (M3 S3/S4, PostgreSQL side only)

Revision ID: 0004
Revises: 0003

Nothing here talks to Snowflake. These tables let the (off by default) exporter know what it has
already delivered, remember its own health between runs, and let the query layer keep metadata
about every statement for the soft budget. PostgreSQL stays the source of truth (ADR-0007).
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "export_marks",
        sa.Column("stream", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("exported_at", TZ, nullable=False),
        sa.Column("batch_id", sa.Text, nullable=False),
        # incident summaries are versioned by updated_at; events and deployments are immutable
        sa.Column("version_at", TZ),
        sa.PrimaryKeyConstraint("stream", "project_id", "key", name="pk_export_marks"),
        sa.CheckConstraint(
            "stream IN ('events','incident_summaries','deployments')", name="ck_export_stream"
        ),
    )

    op.create_table(
        "export_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("last_started_at", TZ),
        sa.Column("last_success_at", TZ),
        sa.Column("next_attempt_at", TZ),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text),
        sa.Column("updated_at", TZ, nullable=False),
        sa.CheckConstraint("id = 1", name="ck_export_state_single_row"),
        sa.CheckConstraint("state IN ('idle','degraded','halted')", name="ck_export_state_value"),
    )

    # Metadata only: never SQL text, parameters or result content.
    op.create_table(
        "snowflake_calls",
        sa.Column("call_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("started_at", TZ, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("function", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text),
        sa.Column("rows", sa.Integer),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("query_id", sa.Text),
    )
    op.create_index("ix_snowflake_calls_started", "snowflake_calls", ["started_at"])
    op.create_index("ix_snowflake_calls_session", "snowflake_calls", ["session_id", "started_at"])

    # Ordered scans for the exporter's backlog query.
    op.create_index("ix_events_received", "events", ["received_at", "project_id", "event_id"])


def downgrade() -> None:
    op.drop_index("ix_events_received", table_name="events")
    op.drop_table("snowflake_calls")
    op.drop_table("export_state")
    op.drop_table("export_marks")
