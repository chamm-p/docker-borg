"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-11

"""
from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hostname", sa.String(255), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("agent_version", sa.String(50)),
        sa.Column("status", sa.String(20), nullable=False, server_default="offline"),
        sa.Column("last_heartbeat", sa.DateTime()),
        sa.Column("backup_type", sa.String(20), nullable=False, server_default="scp"),
        sa.Column("borg_repo", sa.String(500)),
        sa.Column("borg_passphrase", sa.String(500)),
        sa.Column("scp_host", sa.String(255)),
        sa.Column("scp_user", sa.String(255)),
        sa.Column("scp_path", sa.String(500)),
        sa.Column("scp_port", sa.Integer(), server_default="22"),
        sa.Column("local_path", sa.String(500)),
        sa.Column("webdav_url", sa.String(500)),
        sa.Column("webdav_user", sa.String(255)),
        sa.Column("webdav_password", sa.String(255)),
        sa.Column("last_connection_check", sa.DateTime()),
        sa.Column("last_connection_ok", sa.Boolean()),
        sa.Column("last_connection_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_agents_hostname", "agents", ["hostname"])

    op.create_table(
        "containers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("container_id", sa.String(100), nullable=False),
        sa.Column("container_name", sa.String(255), nullable=False),
        sa.Column("compose_project", sa.String(255), nullable=False),
        sa.Column("compose_dir", sa.String(500), nullable=False, server_default=""),
        sa.Column("root_files", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("image", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("has_volumes", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("backup_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_containers_agent_id", "containers", ["agent_id"])

    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(100), nullable=False, server_default="Backup"),
        sa.Column("schedule_kind", sa.String(20), nullable=False, server_default="daily"),
        sa.Column("hour", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("minute", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("weekday", sa.Integer()),
        sa.Column("day_of_month", sa.Integer()),
        sa.Column("cron_expr", sa.String(100), nullable=False, server_default="0 3 * * *"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("prune_after", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("keep_daily", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("keep_weekly", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("keep_monthly", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("schedules.id", ondelete="SET NULL")),
        sa.Column("job_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("containers", sa.Text()),
        sa.Column("params", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("result", sa.Text()),
    )
    op.create_index("ix_jobs_agent_id", "jobs", ["agent_id"])

    op.create_table(
        "job_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
    )
    op.create_index("ix_job_logs_job_id", "job_logs", ["job_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("job_logs")
    op.drop_table("jobs")
    op.drop_table("schedules")
    op.drop_table("containers")
    op.drop_table("agents")
