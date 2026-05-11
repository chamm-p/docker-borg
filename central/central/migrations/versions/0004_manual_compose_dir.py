"""add manual_compose_dir

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-12

"""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.add_column(sa.Column("manual_compose_dir", sa.String(500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.drop_column("manual_compose_dir")
