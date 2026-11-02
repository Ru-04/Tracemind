"""incident lifecycle (append-only transitions, reopen counter) and deployments

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
STATUSES = "('open','investigating','resolved','ignored')"


def upgrade() -> None:
    op.add_column(
        "incidents", sa.Column("reopen_count", sa.Integer, nullable=False, server_default="0")
    )
    op.create_check_constraint("ck_incident_status", "incidents", f"status IN {STATUSES}")
    op.create_check_constraint("ck_incident_reopen_count", "incidents", "reopen_count >= 0")

    op.create_table(
        "incident_transitions",
        sa.Column("transition_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("incident_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("at", TZ, nullable=False),
        sa.Column("from_status", sa.Text, nullable=False),
        sa.Column("to_status", sa.Text, nullable=False),
        # The temporary M1-M4 authentication identifies a project key, not a person; 'system' is
        # the automatic reopen. No client-supplied identity is trusted or stored.
        sa.Column("actor_kind", sa.Text, nullable=False),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("trigger_event_id", sa.Text),  # the occurrence that caused an automatic reopen
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.incident_id"], name="fk_transition_incident"
        ),
        sa.CheckConstraint(f"from_status IN {STATUSES}", name="ck_transition_from"),
        sa.CheckConstraint(f"to_status IN {STATUSES}", name="ck_transition_to"),
        sa.CheckConstraint("from_status <> to_status", name="ck_transition_changes_status"),
        sa.CheckConstraint("actor_kind IN ('project_key','system')", name="ck_transition_actor"),
    )
    op.create_index(
        "ix_transitions_incident", "incident_transitions", ["incident_id", "transition_id"]
    )

    op.create_table(
        "deployments",
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("deployment_id", sa.Text, nullable=False),
        sa.Column("environment", sa.Text, nullable=False),
        sa.Column("service", sa.Text),
        sa.Column("release", sa.Text, nullable=False),
        sa.Column("deployed_at", TZ, nullable=False),
        sa.Column("repository_id", sa.Text),
        sa.Column("commit_sha_reported", sa.Text),  # exactly what the caller supplied
        # Resolved once, when the record is written, by the M2 resolver: 'verified' only for a
        # full SHA that exists in the named, registered repository. Never inferred from time.
        sa.Column("commit_sha", sa.Text),
        sa.Column("commit_confidence", sa.Text, nullable=False),
        sa.Column("commit_source", sa.Text),
        sa.Column("commit_reason", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("project_id", "deployment_id", name="pk_deployments"),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["repositories.repository_id"], name="fk_deployment_repo"
        ),
        sa.CheckConstraint("commit_confidence IN ('verified','unknown')", name="ck_deploy_conf"),
        sa.CheckConstraint(
            "(commit_confidence = 'verified') = (commit_sha IS NOT NULL)", name="ck_deploy_sha"
        ),
    )
    op.create_index(
        "ix_deployments_project_time", "deployments", ["project_id", "deployed_at", "deployment_id"]
    )

    # Both tables are append-only, like the M2 audit tables (function created in 0002).
    for table in ("incident_transitions", "deployments"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION tracemind_forbid_mutation()"
        )


def downgrade() -> None:
    for table in ("incident_transitions", "deployments"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("deployments")
    op.drop_table("incident_transitions")
    op.drop_constraint("ck_incident_reopen_count", "incidents", type_="check")
    op.drop_constraint("ck_incident_status", "incidents", type_="check")
    op.drop_column("incidents", "reopen_count")
