"""add database_hooks table

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-13

"""
from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "database_hooks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("container_id", sa.Integer(), sa.ForeignKey("containers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("db_type", sa.String(20), nullable=False),
        sa.Column("db_name", sa.String(255), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("username", sa.String(255), nullable=False, server_default=""),
        sa.Column("password", sa.String(500), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_database_hooks_container_id", "database_hooks", ["container_id"])


def downgrade() -> None:
    op.drop_index("ix_database_hooks_container_id", table_name="database_hooks")
    op.drop_table("database_hooks")
