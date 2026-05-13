"""add excluded_mounts to containers

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-13

"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.add_column(sa.Column("excluded_mounts", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.drop_column("excluded_mounts")
