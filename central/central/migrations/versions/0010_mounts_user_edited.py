"""add mounts_user_edited flag

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-13

"""
from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.add_column(sa.Column("mounts_user_edited", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.drop_column("mounts_user_edited")
