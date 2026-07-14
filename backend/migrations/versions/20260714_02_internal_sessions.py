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
    if "sessions" in sa.inspect(bind).get_table_names():
        op.add_column(
            "sessions",
            sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "sessions" in sa.inspect(bind).get_table_names():
        columns = {column["name"] for column in sa.inspect(bind).get_columns("sessions")}
        if "is_internal" in columns:
            op.drop_column("sessions", "is_internal")
