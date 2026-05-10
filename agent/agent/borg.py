from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import settings
from .models import ContainerInfo, JobResult, LogEntry

logger = logging.getLogger(__name__)


@dataclass
class BorgResult:
    success: bool
    job_result: JobResult
    logs: list[LogEntry] = field(default_factory=list)


def _borg_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env["BORG_REPO"] = settings.borg_repo
    env["BORG_PASSPHRASE"] = settings.borg_passphrase
    env["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = "yes"
    env["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"
    return env


def _run_borg(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["borg"] + args
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_borg_env(),
        cwd=cwd,
        timeout=3600,
    )


def _host_path_to_local(host_path: str) -> Path:
    host_dir = settings.docker_host_dir
    return Path(host_dir) / Path(host_path).name


def init_repo() -> bool:
    result = _run_borg(["init", "--encryption=repokey-blake2"])
    if result.returncode == 0:
        logger.info("Borg repo initialized at %s", settings.borg_repo)
        return True
    if "already exists" in result.stderr.lower() or "repository already exists" in result.stderr.lower():
        logger.info("Borg repo already exists at %s", settings.borg_repo)
        return True
    logger.error("Failed to init borg repo: %s", result.stderr)
    return False


def create_backup(container: ContainerInfo) -> BorgResult:
    logs: list[LogEntry] = []
    start = time.time()

    compose_dir_local = _host_path_to_local(container.compose_dir)
    if not compose_dir_local.is_dir():
        msg = f"Compose dir not accessible: {compose_dir_local}"
        logger.error(msg)
        return BorgResult(success=False, job_result=JobResult(), logs=[LogEntry("error", msg)])

    if not container.root_files:
        msg = f"No root files found for {container.compose_project}"
        logger.warning(msg)
        return BorgResult(success=False, job_result=JobResult(), logs=[LogEntry("warning", msg)])

    archive_name = f"{settings.agent_name}-{container.compose_project}-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    logs.append(LogEntry("info", f"Starting backup: {archive_name}"))

    result = _run_borg(
        ["create", "--json", f"::{archive_name}"] + container.root_files,
        cwd=str(compose_dir_local),
    )

    duration = time.time() - start

    if result.returncode != 0:
        if "repository" in result.stderr.lower() and "does not exist" in result.stderr.lower():
            logs.append(LogEntry("info", "Repository not found, initializing..."))
            if not init_repo():
                logs.append(LogEntry("error", "Failed to initialize repository"))
                return BorgResult(success=False, job_result=JobResult(), logs=logs)
            result = _run_borg(
                ["create", "--json", f"::{archive_name}"] + container.root_files,
                cwd=str(compose_dir_local),
            )
            duration = time.time() - start

    if result.returncode != 0:
        msg = f"Borg create failed: {result.stderr}"
        logger.error(msg)
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    job_result = JobResult(archive_name=archive_name, duration_seconds=round(duration, 2))
    try:
        data = json.loads(result.stdout)
        archive_stats = data.get("archive", {}).get("stats", {})
        job_result.size_bytes = archive_stats.get("original_size", 0)
        job_result.nfiles = archive_stats.get("nfiles", 0)
    except (json.JSONDecodeError, KeyError):
        pass

    logs.append(LogEntry("info", f"Backup complete: {job_result.nfiles} files, {job_result.size_bytes} bytes"))
    logger.info("Backup created: %s", archive_name)
    return BorgResult(success=True, job_result=job_result, logs=logs)


def backup_all(containers: list[ContainerInfo]) -> list[BorgResult]:
    if not init_repo():
        return [BorgResult(success=False, job_result=JobResult(), logs=[LogEntry("error", "Failed to init repo")])]

    results = []
    for c in containers:
        if not c.compose_dir or not c.root_files:
            logger.info("Skipping %s (no compose dir or root files)", c.compose_project)
            continue
        r = create_backup(c)
        results.append(r)
    return results


def prune(keep_daily: int = 7, keep_weekly: int = 4, keep_monthly: int = 6) -> BorgResult:
    logs: list[LogEntry] = []
    logs.append(LogEntry("info", f"Pruning: keep daily={keep_daily}, weekly={keep_weekly}, monthly={keep_monthly}"))

    prefix = f"{settings.agent_name}-"
    result = _run_borg([
        "prune",
        f"--prefix={prefix}",
        f"--keep-daily={keep_daily}",
        f"--keep-weekly={keep_weekly}",
        f"--keep-monthly={keep_monthly}",
    ])

    if result.returncode != 0:
        msg = f"Borg prune failed: {result.stderr}"
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    logs.append(LogEntry("info", "Prune completed"))
    return BorgResult(success=True, job_result=JobResult(), logs=logs)


def list_archives() -> BorgResult:
    logs: list[LogEntry] = []
    result = _run_borg(["list", "--json"])

    if result.returncode != 0:
        msg = f"Borg list failed: {result.stderr}"
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    try:
        data = json.loads(result.stdout)
        archives = data.get("archives", [])
        logs.append(LogEntry("info", f"Found {len(archives)} archives"))
        job_result = JobResult()
        job_result.nfiles = len(archives)
        return BorgResult(success=True, job_result=job_result, logs=logs)
    except json.JSONDecodeError:
        logs.append(LogEntry("info", result.stdout))
        return BorgResult(success=True, job_result=JobResult(), logs=logs)


def extract_archive(archive_name: str, target_dir: str) -> BorgResult:
    logs: list[LogEntry] = []
    logs.append(LogEntry("info", f"Restoring {archive_name} to {target_dir}"))

    Path(target_dir).mkdir(parents=True, exist_ok=True)

    result = _run_borg(["extract", f"::{archive_name}"], cwd=target_dir)

    if result.returncode != 0:
        msg = f"Borg extract failed: {result.stderr}"
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    logs.append(LogEntry("info", f"Restore complete to {target_dir}"))
    return BorgResult(success=True, job_result=JobResult(archive_name=archive_name), logs=logs)
