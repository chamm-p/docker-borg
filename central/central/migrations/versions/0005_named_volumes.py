"""add named_volumes to containers

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-12

"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.add_column(sa.Column("named_volumes", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.drop_column("named_volumes")
