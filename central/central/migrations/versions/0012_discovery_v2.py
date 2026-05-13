"""discovery v2: top_level_entries, db_candidates, excluded_entries

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-13

"""
from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.add_column(sa.Column("top_level_entries", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("excluded_entries", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("db_candidates", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("db_candidates_dismissed", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("containers") as batch:
        batch.drop_column("db_candidates_dismissed")
        batch.drop_column("db_candidates")
        batch.drop_column("excluded_entries")
        batch.drop_column("top_level_entries")
