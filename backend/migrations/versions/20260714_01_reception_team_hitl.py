"""Add reception team node lifecycle and durable HITL decisions."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260714_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "team_runs" not in tables:
        return

    team_columns = _columns(inspector, "team_runs")
    additions = (
        ("request_id", sa.Column("request_id", sa.String(100), nullable=True)),
        ("started_at", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)),
        ("completed_at", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)),
        ("result_data", sa.Column("result_data", sa.JSON(), nullable=True)),
    )
    for name, column in additions:
        if name not in team_columns:
            op.add_column("team_runs", column)
    indexes = {index["name"] for index in inspector.get_indexes("team_runs")}
    if "ix_team_runs_request_id" not in indexes:
        op.create_index("ix_team_runs_request_id", "team_runs", ["request_id"])

    if "team_node_runs" not in tables:
        op.create_table(
            "team_node_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("team_run_id", sa.String(36), nullable=False),
            sa.Column("owner_user_id", sa.String(36), nullable=False),
            sa.Column("agent_type", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("result_data", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["team_run_id"], ["team_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("team_run_id", "agent_type"),
        )
        op.create_index("ix_team_node_runs_team_run_id", "team_node_runs", ["team_run_id"])
        op.create_index("ix_team_node_runs_owner_user_id", "team_node_runs", ["owner_user_id"])

    if "hitl_requests" in tables:
        hitl_columns = _columns(inspector, "hitl_requests")
        hitl_additions = (
            ("decision", sa.Column("decision", sa.String(32), nullable=True)),
            ("modifications", sa.Column("modifications", sa.JSON(), nullable=True)),
            ("result_data", sa.Column("result_data", sa.JSON(), nullable=True)),
            ("decided_at", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True)),
        )
        for name, column in hitl_additions:
            if name not in hitl_columns:
                op.add_column("hitl_requests", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "team_node_runs" in tables:
        op.drop_table("team_node_runs")

    if "hitl_requests" in tables:
        hitl_columns = _columns(inspector, "hitl_requests")
        for name in ("decided_at", "result_data", "modifications", "decision"):
            if name in hitl_columns:
                op.drop_column("hitl_requests", name)

    if "team_runs" in tables:
        team_columns = _columns(inspector, "team_runs")
        indexes = {index["name"] for index in inspector.get_indexes("team_runs")}
        if "ix_team_runs_request_id" in indexes:
            op.drop_index("ix_team_runs_request_id", table_name="team_runs")
        for name in ("result_data", "completed_at", "started_at", "request_id"):
            if name in team_columns:
                op.drop_column("team_runs", name)
