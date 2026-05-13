from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(255))
    agent_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="offline")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    backup_type: Mapped[str] = mapped_column(String(20), default="scp")
    borg_repo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    borg_passphrase: Mapped[str | None] = mapped_column(String(500), nullable=True)

    scp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scp_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scp_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    scp_port: Mapped[int] = mapped_column(Integer, default=22)

    local_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    webdav_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webdav_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    webdav_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    webdav_verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)

    last_connection_check: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_connection_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_connection_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    ssh_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    cached_archives: Mapped[str] = mapped_column(Text, default="[]")
    cached_archives_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    containers: Mapped[list["Container"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="agent", cascade="all, delete-orphan")


class Container(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    container_id: Mapped[str] = mapped_column(String(100))
    container_name: Mapped[str] = mapped_column(String(255))
    compose_project: Mapped[str] = mapped_column(String(255))
    compose_dir: Mapped[str] = mapped_column(String(500), default="")
    manual_compose_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    root_files: Mapped[str] = mapped_column(Text, default="[]")
    image: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="running")
    has_volumes: Mapped[bool] = mapped_column(Boolean, default=False)
    compose_dir_accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    mounts_user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    named_volumes: Mapped[str] = mapped_column(Text, default="[]")
    backup_mounts: Mapped[str] = mapped_column(Text, default="[]")
    excluded_mounts: Mapped[str] = mapped_column(Text, default="[]")
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agent: Mapped["Agent"] = relationship(back_populates="containers")


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(100), default="Backup")
    schedule_kind: Mapped[str] = mapped_column(String(20), default="daily")
    hour: Mapped[int] = mapped_column(Integer, default=3)
    minute: Mapped[int] = mapped_column(Integer, default=0)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cron_expr: Mapped[str] = mapped_column(String(100), default="0 3 * * *")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    prune_after: Mapped[bool] = mapped_column(Boolean, default=True)
    keep_daily: Mapped[int] = mapped_column(Integer, default=7)
    keep_weekly: Mapped[int] = mapped_column(Integer, default=4)
    keep_monthly: Mapped[int] = mapped_column(Integer, default=6)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    agent: Mapped["Agent | None"] = relationship(back_populates="schedules")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    schedule_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True)
    job_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    containers: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)

    agent: Mapped["Agent"] = relationship(back_populates="jobs")
    logs: Mapped[list["JobLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    level: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)

    job: Mapped["Job"] = relationship(back_populates="logs")


class AppSetting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
