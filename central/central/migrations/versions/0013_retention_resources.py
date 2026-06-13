"""agent-level retention + worker resource limits

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-14

"""
from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("retention_mode", sa.String(20), nullable=False, server_default="simple"))
        batch.add_column(sa.Column("keep_last", sa.Integer(), nullable=False, server_default="7"))
        batch.add_column(sa.Column("keep_daily", sa.Integer(), nullable=False, server_default="7"))
        batch.add_column(sa.Column("keep_weekly", sa.Integer(), nullable=False, server_default="4"))
        batch.add_column(sa.Column("keep_monthly", sa.Integer(), nullable=False, server_default="6"))
        batch.add_column(sa.Column("prune_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("worker_mem_mb", sa.Integer(), nullable=False, server_default="1024"))
        batch.add_column(sa.Column("worker_cpus", sa.String(10), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        for col in ("worker_cpus", "worker_mem_mb", "prune_enabled",
                    "keep_monthly", "keep_weekly", "keep_daily", "keep_last", "retention_mode"):
            batch.drop_column(col)
