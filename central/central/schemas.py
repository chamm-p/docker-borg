from __future__ import annotations

from pydantic import BaseModel


class AgentRegisterRequest(BaseModel):
    hostname: str
    agent_version: str = "0.2.0"
    token: str


class BackupConfig(BaseModel):
    backup_type: str = "scp"
    borg_repo: str = ""
    borg_passphrase: str = ""
    scp_host: str = ""
    scp_user: str = ""
    scp_path: str = ""
    scp_port: int = 22
    local_path: str = ""
    webdav_url: str = ""
    webdav_user: str = ""
    webdav_password: str = ""
    webdav_verify_ssl: bool = True


class AgentRegisterResponse(BaseModel):
    agent_id: int
    agent_token: str
    backup: BackupConfig = BackupConfig()
    poll_interval_seconds: int = 30


class HeartbeatResponse(BaseModel):
    ack: bool = True
    backup: BackupConfig = BackupConfig()
    manual_paths: dict[str, str] = {}
    cancelled_jobs: list[int] = []


class ContainerPayload(BaseModel):
    container_id: str
    container_name: str
    compose_project: str
    compose_dir: str
    root_files: list[str]
    image: str
    status: str
    has_volumes: bool = False
    compose_dir_accessible: bool = False
    named_volumes: list[dict] = []


class HeartbeatRequest(BaseModel):
    hostname: str
    agent_version: str = "0.2.0"
    ssh_public_key: str = ""
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
