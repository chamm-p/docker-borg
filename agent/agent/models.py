from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class JobType(str, Enum):
    BACKUP = "backup"
    PRUNE = "prune"
    RESTORE = "restore"
    LIST = "list"
    VERIFY = "verify"
    SCP_TEST = "scp_test"
    SCP_INSTALL_KEY = "scp_install_key"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class ContainerInfo:
    container_id: str
    container_name: str
    compose_project: str
    compose_dir: str
    root_files: list[str]
    image: str
    status: str
    has_volumes: bool = False
    compose_dir_accessible: bool = False
    named_volumes: list[dict] = field(default_factory=list)  # [{name, source}, ...]


@dataclass
class JobResult:
    archive_name: str = ""
    size_bytes: int = 0
    nfiles: int = 0
    duration_seconds: float = 0.0


@dataclass
class LogEntry:
    level: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class Job:
    job_id: int
    job_type: JobType
    containers: list[str] | None = None
    params: dict = field(default_factory=dict)
