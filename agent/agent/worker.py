"""Orchestrates the docker-borg-worker container per backup operation.

The agent itself does NOT run borg; it only orchestrates ephemeral worker
containers. Each worker mounts the target compose containers' volumes via
--volumes-from and runs borgmatic.

Volume sharing between agent and worker:
- The agent's data dir (named volume DBORG_AGENT_DATA_VOLUME, mounted at /data
  in both) carries borgmatic configs, ssh keys, borg cache, etc.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import docker

from .config import settings
from .models import ContainerInfo, JobResult, LogEntry

logger = logging.getLogger(__name__)

WORKER_IMAGE = os.environ.get("DBORG_WORKER_IMAGE", "ghcr.io/chamm-p/docker-borg-worker:latest")
# Name of the docker volume that holds /data both inside agent and worker.
AGENT_DATA_VOLUME = os.environ.get("DBORG_AGENT_DATA_VOLUME", "agent-data")


@dataclass
class WorkerResult:
    success: bool
    job_result: JobResult
    logs: list[LogEntry] = field(default_factory=list)


_active_container_id: str | None = None
_active_lock = threading.Lock()


def cancel_active() -> bool:
    global _active_container_id
    with _active_lock:
        cid = _active_container_id
    if not cid:
        return False
    try:
        client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
        try:
            container = client.containers.get(cid)
            container.kill()
            logger.info("Killed active worker container %s", cid)
        finally:
            client.close()
        return True
    except Exception as e:
        logger.warning("Could not kill worker %s: %s", cid, e)
        return False


def _build_borgmatic_config(container: ContainerInfo, repo_path: str, archive_prefix: str) -> dict:
    """Produces a borgmatic config dict for one compose project."""
    sources: list[str] = []
    if container.compose_dir:
        sources.append("/mnt/compose")
    # Each mount inherited via --volumes-from appears at its original destination
    for m in (container.backup_mounts or []):
        dest = m.get("dest")
        if dest:
            sources.append(dest)
    sources = sorted(set(sources))

    config = {
        "source_directories": sources or ["/mnt/compose"],
        "repositories": [{"path": repo_path}],
        "archive_name_format": f"{archive_prefix}-{{now:%Y%m%dT%H%M%S}}",
        "compression": "lz4",
        "one_file_system": False,
        "exclude_patterns": [
            "**/.git", "**/node_modules", "**/__pycache__", "*.pyc",
            "**/.venv", "**/venv", "**/.cache", "**/.DS_Store",
        ],
    }
    return config


def _write_yaml(data: dict, path: Path) -> None:
    """Minimal YAML serializer (avoids adding PyYAML to the agent)."""
    def emit(value, indent: int = 0) -> str:
        pad = "  " * indent
        if isinstance(value, dict):
            out = []
            for k, v in value.items():
                if isinstance(v, (dict, list)):
                    out.append(f"{pad}{k}:")
                    out.append(emit(v, indent + 1))
                else:
                    out.append(f"{pad}{k}: {_yaml_scalar(v)}")
            return "\n".join(out)
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, dict):
                    first = True
                    for k, v in item.items():
                        prefix = f"{pad}- " if first else f"{pad}  "
                        first = False
                        if isinstance(v, (dict, list)):
                            out.append(f"{prefix}{k}:")
                            out.append(emit(v, indent + 2))
                        else:
                            out.append(f"{prefix}{k}: {_yaml_scalar(v)}")
                else:
                    out.append(f"{pad}- {_yaml_scalar(item)}")
            return "\n".join(out)
        return f"{pad}{_yaml_scalar(value)}"

    path.write_text(emit(data) + "\n")


_YAML_SPECIAL_FIRST = set("*&!?@,[]{}|>%`#-")


def _yaml_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if not s:
        return '""'
    if s[0] in _YAML_SPECIAL_FIRST:
        return json.dumps(s)
    if any(c in s for c in ":#'\"\n") or s.strip() != s:
        return json.dumps(s)
    # Tokens that would parse as booleans/null without quotes
    if s.lower() in {"true", "false", "yes", "no", "null", "~", "on", "off"}:
        return json.dumps(s)
    return s


def _resolve_target_container(docker_client, project: str):
    for c in docker_client.containers.list(all=False):
        if c.labels.get("com.docker.compose.project") == project:
            return c
    for c in docker_client.containers.list(all=True):
        if c.name == project or c.labels.get("com.docker.compose.project") == project:
            return c
    return None


def _container_networks(target) -> list[str]:
    nets = target.attrs.get("NetworkSettings", {}).get("Networks", {})
    return [name for name in nets.keys() if name not in ("bridge", "host", "none")]


def _resolve_repo_path() -> str:
    """Path that the WORKER sees for the borg repo."""
    if settings.backup_type == "scp":
        if settings.scp_host and settings.scp_user and settings.scp_path:
            return f"ssh://{settings.scp_user}@{settings.scp_host}:{settings.scp_port}/{settings.scp_path.lstrip('/')}"
    if settings.backup_type == "local":
        return "/mnt/repo"
    if settings.backup_type == "webdav":
        return "/mnt/webdav/borg"
    return "/mnt/repo"


def _spawn_worker(
    docker_client,
    *,
    config: dict,
    mode: str,
    extra_args: list[str] | None = None,
    volumes_from_target=None,
    extra_volumes: dict | None = None,
    on_log,
) -> tuple[int, dict]:
    """Common worker spawn + log streaming. Returns (exit_code, extra_state)."""
    global _active_container_id

    job_token = uuid.uuid4().hex[:12]
    config_dir_agent = Path("/data/shared") / job_token
    config_dir_agent.mkdir(parents=True, exist_ok=True)
    config_path_worker = f"/data/shared/{job_token}/config.yaml"
    _write_yaml(config, config_dir_agent / "config.yaml")

    env = {
        "BORG_PASSPHRASE": settings.borg_passphrase or "",
        "BORG_RELOCATED_REPO_ACCESS_IS_OK": "yes",
        "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK": "yes",
        "BORG_CACHE_DIR": "/data/borg-cache",
        "BORG_CONFIG_DIR": "/data/borg-config",
        "BORG_REPO": _resolve_repo_path(),
        "DBORG_CONFIG_PATH": config_path_worker,
        "TZ": os.environ.get("TZ", "UTC"),
    }
    if settings.backup_type == "scp":
        env["BORG_RSH"] = (
            "ssh -i /data/ssh/id_ed25519 "
            "-o UserKnownHostsFile=/data/ssh/known_hosts "
            "-o StrictHostKeyChecking=accept-new "
            "-o BatchMode=yes"
        )
    if settings.backup_type == "webdav":
        env["DBORG_WEBDAV_URL"] = settings.webdav_url or ""
        env["DBORG_WEBDAV_USER"] = settings.webdav_user or ""
        env["DBORG_WEBDAV_PASSWORD"] = settings.webdav_password or ""
        env["DBORG_WEBDAV_VERIFY_SSL"] = "true" if settings.webdav_verify_ssl else "false"

    volumes: dict = {AGENT_DATA_VOLUME: {"bind": "/data", "mode": "rw"}}
    if settings.backup_type == "local" and settings.local_path:
        volumes[settings.local_path] = {"bind": "/mnt/repo", "mode": "rw"}
    if extra_volumes:
        volumes.update(extra_volumes)

    cap_add: list[str] = []
    devices: list[str] = []
    security_opt: list[str] = []
    if settings.backup_type == "webdav":
        cap_add = ["SYS_ADMIN"]
        devices = ["/dev/fuse:/dev/fuse"]
        security_opt = ["apparmor=unconfined"]

    run_kwargs: dict = {
        "image": WORKER_IMAGE,
        "command": [mode] + (extra_args or []),
        "detach": True,
        "stdin_open": False,
        "tty": False,
        "environment": env,
        "volumes": volumes,
        "mem_limit": "512m",
        "memswap_limit": "512m",
        "cap_add": cap_add,
        "devices": devices,
        "security_opt": security_opt,
    }
    if volumes_from_target is not None:
        run_kwargs["volumes_from"] = [f"{volumes_from_target.name}:ro"]
        nets = _container_networks(volumes_from_target)
        if nets:
            run_kwargs["network"] = nets[0]
            on_log(f"Network: {nets[0]}", "info")

    on_log(f"Starte Worker ({mode}) — Image {WORKER_IMAGE}", "info")
    if volumes_from_target is not None:
        on_log(f"--volumes-from {volumes_from_target.name}:ro", "info")

    worker = docker_client.containers.run(**run_kwargs)
    with _active_lock:
        _active_container_id = worker.id

    exit_code = -1
    try:
        for raw in worker.logs(stream=True, follow=True, stdout=True, stderr=True):
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            for line in text.splitlines():
                line = line.rstrip()
                if not line:
                    continue
                level = "info"
                low = line.lower()
                if "error" in low or "critical" in low or "fatal" in low:
                    level = "error"
                elif "warn" in low:
                    level = "warning"
                on_log(line, level)

        worker.reload()
        exit_code = worker.attrs.get("State", {}).get("ExitCode", -1)
    finally:
        with _active_lock:
            _active_container_id = None
        try:
            worker.remove(force=True)
        except Exception:
            pass
        try:
            shutil.rmtree(config_dir_agent, ignore_errors=True)
        except Exception:
            pass

    return exit_code, {"config_token": job_token}


def run_backup(container: ContainerInfo, on_log) -> WorkerResult:
    """Run a backup for one compose project via an ephemeral borgmatic worker."""
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    try:
        target = _resolve_target_container(docker_client, container.compose_project)
        if not target:
            return WorkerResult(False, JobResult(),
                                [LogEntry("error", f"Kein laufender Container für '{container.compose_project}' gefunden")])

        archive_prefix = f"{settings.agent_name}-{container.compose_project}"
        repo_path = _resolve_repo_path()
        config = _build_borgmatic_config(container, repo_path, archive_prefix)

        extra_volumes: dict = {}
        if container.compose_dir:
            extra_volumes[container.compose_dir] = {"bind": "/mnt/compose", "mode": "ro"}

        exit_code, _ = _spawn_worker(
            docker_client,
            config=config,
            mode="create",
            volumes_from_target=target,
            extra_volumes=extra_volumes,
            on_log=on_log,
        )
        duration = round(time.time() - start, 2)
        if exit_code != 0:
            return WorkerResult(False, JobResult(duration_seconds=duration),
                                [LogEntry("error", f"Worker exit {exit_code}")])
        return WorkerResult(True, JobResult(archive_name=archive_prefix, duration_seconds=duration),
                            [LogEntry("info", f"Backup abgeschlossen in {duration}s")])
    finally:
        docker_client.close()


def _minimal_config(repo_path: str) -> dict:
    """borgmatic-Config für Operationen ohne Source-Dirs (check, prune, list, restore)."""
    return {
        "source_directories": ["/data/borg-cache"],  # dummy, nicht verwendet
        "repositories": [{"path": repo_path}],
    }


def run_check(on_log) -> WorkerResult:
    """borgmatic check über das Repository (alle Archive)."""
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    try:
        config = _minimal_config(_resolve_repo_path())
        exit_code, _ = _spawn_worker(docker_client, config=config, mode="check", on_log=on_log)
        duration = round(time.time() - start, 2)
        if exit_code != 0:
            return WorkerResult(False, JobResult(duration_seconds=duration),
                                [LogEntry("error", f"Worker exit {exit_code}")])
        return WorkerResult(True, JobResult(duration_seconds=duration),
                            [LogEntry("info", f"Prüfung abgeschlossen in {duration}s")])
    finally:
        docker_client.close()


def run_restore(archive: str, on_log) -> WorkerResult:
    """Stellt ein Archiv im Worker-Container nach /data/restore/<archive>/ wieder her."""
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    try:
        config = _minimal_config(_resolve_repo_path())
        exit_code, _ = _spawn_worker(
            docker_client,
            config=config,
            mode="restore",
            extra_args=["--archive", archive],
            on_log=on_log,
        )
        duration = round(time.time() - start, 2)
        if exit_code != 0:
            return WorkerResult(False, JobResult(duration_seconds=duration),
                                [LogEntry("error", f"Worker exit {exit_code}")])
        return WorkerResult(True, JobResult(archive_name=archive, duration_seconds=duration),
                            [LogEntry("info", f"Wiederherstellung abgeschlossen in {duration}s")])
    finally:
        docker_client.close()
