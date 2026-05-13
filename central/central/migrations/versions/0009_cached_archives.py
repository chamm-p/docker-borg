"""add cached_archives to agents

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-13

"""
from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("cached_archives", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("cached_archives_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("cached_archives_at")
        batch.drop_column("cached_archives")
