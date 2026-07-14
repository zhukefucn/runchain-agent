"""Mark AgentScope worker sessions as internal.

Revision ID: 20260714_02
Revises: 20260714_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_02"
down_revision = "20260714_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "sessions" in tables:
        op.add_column(
            "sessions",
            sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        session_columns = {column["name"] for column in inspector.get_columns("sessions")}
        storage_columns = (
            {column["name"] for column in inspector.get_columns("agentscope_storage_records")}
            if "agentscope_storage_records" in tables
            else set()
        )
        if {"owner_user_id", "agent_id"} <= session_columns and {
            "owner_user_id", "namespace", "record_id", "payload"
        } <= storage_columns:
            # Legacy AgentCreate worker sessions may have SessionSource.USER,
            # so title/source/team_id alone cannot distinguish them from a
            # legitimate user session.  The persisted AgentRecord is durable
            # and explicitly carries source="team".
            op.execute(
                sa.text(
                    """
                    UPDATE sessions AS session
                    SET is_internal = 1
                    WHERE EXISTS (
                        SELECT 1
                        FROM agentscope_storage_records AS agent
                        WHERE agent.owner_user_id = session.owner_user_id
                          AND agent.namespace = 'agent'
                          AND agent.record_id = session.agent_id
                          AND json_extract(CAST(agent.payload AS TEXT), '$.source') = 'team'
                    )
                    """
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "sessions" in sa.inspect(bind).get_table_names():
        columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
        if "is_internal" in columns:
            op.drop_column("sessions", "is_internal")
