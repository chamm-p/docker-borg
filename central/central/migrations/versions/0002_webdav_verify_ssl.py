"""add webdav_verify_ssl

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-11

"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("webdav_verify_ssl", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("webdav_verify_ssl")
