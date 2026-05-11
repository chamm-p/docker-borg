from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AgentRegisterRequest(BaseModel):
    hostname: str
    agent_version: str = "0.1.0"
    token: str


class BackupConfig(BaseModel):
    backup_type: str = "ssh"
    borg_repo: str = ""
    borg_passphrase: str = ""
    webdav_url: str = ""
    webdav_user: str = ""
    webdav_password: str = ""


class AgentRegisterResponse(BaseModel):
    agent_id: int
    agent_token: str
    backup: BackupConfig = BackupConfig()
    poll_interval_seconds: int = 30


class HeartbeatResponse(BaseModel):
    ack: bool = True
    backup: BackupConfig = BackupConfig()


class ContainerPayload(BaseModel):
    container_id: str
    container_name: str
    compose_project: str
    compose_dir: str
    root_files: list[str]
    image: str
    status: str
    has_volumes: bool = False


class HeartbeatRequest(BaseModel):
    hostname: str
    containers: list[ContainerPayload]


class JobPayload(BaseModel):
    job_id: int
    job_type: str
    containers: list[str] | None = None
    params: dict = {}


class PendingJobsResponse(BaseModel):
    jobs: list[JobPayload]


class LogPayload(BaseModel):
    level: str
    message: str
    timestamp: str = ""


class JobStatusUpdate(BaseModel):
    status: str
    result: dict | None = None
    logs: list[LogPayload] = []


class CreateJobRequest(BaseModel):
    agent_id: int
    job_type: str
    containers: list[str] | None = None
    params: dict = {}


class ScheduleCreate(BaseModel):
    agent_id: int | None = None
    cron_expr: str = "0 3 * * *"
    enabled: bool = True
    prune_after: bool = True
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6


class ScheduleUpdate(BaseModel):
    cron_expr: str | None = None
    enabled: bool | None = None
    prune_after: bool | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
