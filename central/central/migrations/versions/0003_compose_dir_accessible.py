"""add compose_dir_accessible

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-12

"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.add_column(sa.Column("compose_dir_accessible", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.drop_column("compose_dir_accessible")
