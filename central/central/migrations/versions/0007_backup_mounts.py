"""add backup_mounts to containers

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-12

"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.add_column(sa.Column("backup_mounts", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.drop_column("backup_mounts")
