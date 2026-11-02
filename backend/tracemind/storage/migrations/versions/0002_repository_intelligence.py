"""repositories, scans (the job queue), change records, scan events

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("repository_id", sa.Text, primary_key=True),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("identity", sa.Text, nullable=False),
        sa.Column("default_branch", sa.Text),
        # Pointer to the last *successful* scan; only ever advanced together with its change record.
        sa.Column("indexed_sha", sa.Text),
        sa.Column("indexed_scan_id", sa.Text),
        sa.Column("graph_sha256", sa.Text),
        sa.Column("created_at", TZ, nullable=False),
        sa.UniqueConstraint("project_id", "path", name="uq_repository_path"),
    )
    op.create_index("ix_repositories_project", "repositories", ["project_id", "created_at", "repository_id"])

    op.create_table(
        "repository_scans",
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
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("available_at", TZ, nullable=False),
        sa.Column("locked_by", sa.Text),
        sa.Column("lease_expires_at", TZ),
        sa.Column("error_code", sa.Text),
        sa.Column("queued_at", TZ, nullable=False),
        sa.Column("started_at", TZ),
        sa.Column("finished_at", TZ),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.repository_id"], name="fk_scan_repo"),
        sa.CheckConstraint("kind IN ('full','incremental')", name="ck_scan_kind"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed')", name="ck_scan_status"
        ),
    )
    # Idempotence at the database level: one live scan and one successful scan per target commit.
    op.create_index(
        "uq_scan_live_target",
        "repository_scans",
        ["repository_id", "target_sha"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "uq_scan_success_target",
        "repository_scans",
        ["repository_id", "target_sha"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        "ix_scan_claim", "repository_scans", ["status", "available_at"]
    )

    op.create_table(
        "change_records",
        sa.Column("record_id", sa.Text, primary_key=True),
        sa.Column("repository_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("scan_id", sa.Text, nullable=False, unique=True),
        sa.Column("base_sha", sa.Text),
        sa.Column("target_sha", sa.Text, nullable=False),
        sa.Column("files", JSONB, nullable=False),
        sa.Column("commits", JSONB, nullable=False, server_default="[]"),
        sa.Column("graph_sha256", sa.Text),
        sa.Column("graph_delta", JSONB, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.repository_id"], name="fk_change_repo"),
        sa.ForeignKeyConstraint(["scan_id"], ["repository_scans.scan_id"], name="fk_change_scan"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_change_range ON change_records "
        "(repository_id, COALESCE(base_sha, ''), target_sha)"
    )
    op.create_index("ix_change_repo", "change_records", ["repository_id", "created_at", "record_id"])

    op.create_table(
        "repository_commits",
        sa.Column("repository_id", sa.Text, nullable=False),
        sa.Column("sha", sa.Text, nullable=False),
        sa.Column("parent_shas", JSONB, nullable=False),
        sa.Column("authored_at", TZ, nullable=False),
        sa.Column("committed_at", TZ, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("retrieved_at", TZ, nullable=False),
        sa.PrimaryKeyConstraint("repository_id", "sha", name="pk_repository_commits"),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.repository_id"], name="fk_commit_repo"),
    )

    op.create_table(
        "scan_events",
        sa.Column("event_id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("scan_id", sa.Text, nullable=False),
        sa.Column("repository_id", sa.Text, nullable=False),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("at", TZ, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("worker_id", sa.Text),
        sa.Column("error_code", sa.Text),
        sa.ForeignKeyConstraint(["scan_id"], ["repository_scans.scan_id"], name="fk_event_scan"),
    )
    op.create_index("ix_scan_events_scan", "scan_events", ["scan_id", "event_id"])

    # Append-only audit history: no row of these tables can be changed or removed.
    op.execute(
        """
        CREATE FUNCTION tracemind_forbid_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("change_records", "scan_events"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION tracemind_forbid_mutation()"
        )


def downgrade() -> None:
    for table in ("change_records", "scan_events"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS tracemind_forbid_mutation()")
    for table in (
        "scan_events",
        "repository_commits",
        "change_records",
        "repository_scans",
        "repositories",
    ):
        op.drop_table(table)
