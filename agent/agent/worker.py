"""Orchestrates the docker-borg-worker container per backup operation.

The agent itself does NOT run borg; it only orchestrates ephemeral worker
containers. Each worker mounts the target compose containers' volumes via
--volumes-from and runs borgmatic.

Volume sharing between agent and worker:
- The agent's data dir (auto-detected volume, mounted at /data
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


def ensure_worker_image_fresh(docker_client) -> None:
    """Pull the worker image so we never run stale code from a cached layer."""
    try:
        on_start = time.time()
        logger.info("Ziehe Worker-Image %s...", WORKER_IMAGE)
        docker_client.images.pull(WORKER_IMAGE)
        logger.info("Worker-Image aktualisiert in %.1fs", time.time() - on_start)
    except Exception as e:
        logger.warning("Konnte Worker-Image nicht pullen (%s) — nutze lokal gecachtes", e)


def _detect_agent_data_volume(docker_client) -> str:
    """Find the actual docker volume name backing this agent's /data dir.

    Priority:
      1. DBORG_AGENT_DATA_VOLUME env (manual override)
      2. Introspect our own container — read Mounts for Destination=/data
      3. Fallback to literal "agent-data"
    """
    override = os.environ.get("DBORG_AGENT_DATA_VOLUME", "").strip()
    if override:
        return override
    try:
        import socket
        our_id = socket.gethostname()  # Docker sets this to short container ID
        me = docker_client.containers.get(our_id)
        for m in me.attrs.get("Mounts", []):
            if m.get("Destination") == "/data" and m.get("Type") == "volume":
                name = m.get("Name")
                if name:
                    logger.info("Detected agent-data volume: %s", name)
                    return name
    except Exception as e:
        logger.warning("Could not introspect own container for /data volume: %s", e)
    return "agent-data"


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


_DB_DEFAULT_PORTS = {"postgresql": 5432, "mariadb": 3306, "mysql": 3306, "mongodb": 27017}
_DB_BORGMATIC_KEY = {
    "postgresql": "postgresql_databases",
    "mariadb": "mariadb_databases",
    "mysql": "mysql_databases",
    "mongodb": "mongodb_databases",
}


def _build_borgmatic_config(
    container: ContainerInfo,
    repo_path: str,
    archive_prefix: str,
    excluded_mounts: set | None = None,
    db_hooks: list[dict] | None = None,
) -> dict:
    """Produces a borgmatic config dict for one compose project."""
    excluded = set(excluded_mounts or [])
    sources: list[str] = []
    if container.compose_dir:
        sources.append("/mnt/compose")
    for m in (container.backup_mounts or []):
        dest = m.get("dest")
        if dest and dest not in excluded:
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

    # Datenbank-Hooks: borgmatic ruft pg_dump/mysqldump/mongodump und schiebt
    # den Dump direkt als Stream ins Archiv.
    grouped: dict[str, list[dict]] = {}
    for h in (db_hooks or []):
        t = h.get("type", "")
        key = _DB_BORGMATIC_KEY.get(t)
        if not key:
            continue
        entry: dict = {
            "name": h.get("name") or "",
            "hostname": h.get("hostname") or "",
        }
        port = int(h.get("port") or 0) or _DB_DEFAULT_PORTS.get(t, 0)
        if port:
            entry["port"] = port
        if h.get("username"):
            entry["username"] = h["username"]
        if h.get("password"):
            entry["password"] = h["password"]
        if t == "postgresql":
            entry["format"] = "custom"  # pg_dump custom format = restorable per table
        grouped.setdefault(key, []).append(entry)

    for key, items in grouped.items():
        config[key] = items

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


def _resolve_all_project_containers(docker_client, project: str) -> list:
    """All containers belonging to a compose project (running first, then stopped)."""
    out: list = []
    seen: set = set()
    for c in docker_client.containers.list(all=False):
        if c.labels.get("com.docker.compose.project") == project and c.id not in seen:
            out.append(c)
            seen.add(c.id)
    for c in docker_client.containers.list(all=True):
        if c.labels.get("com.docker.compose.project") == project and c.id not in seen:
            out.append(c)
            seen.add(c.id)
    if not out:
        # Fallback by name
        for c in docker_client.containers.list(all=True):
            if c.name == project and c.id not in seen:
                out.append(c)
                seen.add(c.id)
    return out


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
    extra_env: dict | None = None,
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
    if extra_env:
        env.update(extra_env)

    volumes: dict = {_detect_agent_data_volume(docker_client): {"bind": "/data", "mode": "rw"}}
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
        if isinstance(volumes_from_target, list):
            run_kwargs["volumes_from"] = [f"{c.name}:ro" for c in volumes_from_target]
            primary = volumes_from_target[0]
        else:
            run_kwargs["volumes_from"] = [f"{volumes_from_target.name}:ro"]
            primary = volumes_from_target
        nets = _container_networks(primary)
        if nets:
            run_kwargs["network"] = nets[0]
            on_log(f"Network: {nets[0]}", "info")

    on_log(f"Starte Worker ({mode}) — Image {WORKER_IMAGE}", "info")
    if volumes_from_target is not None:
        for vfrom in run_kwargs.get("volumes_from", []):
            on_log(f"--volumes-from {vfrom}", "info")

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


def run_backup(container: ContainerInfo, on_log,
               excluded_mounts: list[str] | None = None,
               db_hooks: list[dict] | None = None) -> WorkerResult:
    """Run a backup for one compose project via an ephemeral borgmatic worker."""
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    try:
        targets = _resolve_all_project_containers(docker_client, container.compose_project)
        if not targets:
            return WorkerResult(False, JobResult(),
                                [LogEntry("error", f"Kein Container für '{container.compose_project}' gefunden")])
        on_log(f"Projekt-Container: {', '.join(c.name for c in targets)}", "info")

        archive_prefix = f"{settings.agent_name}-{container.compose_project}"
        repo_path = _resolve_repo_path()
        config = _build_borgmatic_config(container, repo_path, archive_prefix,
                                          excluded_mounts=set(excluded_mounts or []),
                                          db_hooks=db_hooks)

        extra_volumes: dict = {}
        if container.compose_dir:
            extra_volumes[container.compose_dir] = {"bind": "/mnt/compose", "mode": "ro"}

        exit_code, _ = _spawn_worker(
            docker_client,
            config=config,
            mode="create",
            volumes_from_target=targets,
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


def run_list_archives(on_log) -> tuple[WorkerResult, list[dict]]:
    """Listet alle Archive im Repo. Gibt (Result, parsed_archives) zurück.
    parsed_archives = [{name, start, end, hostname, ...}, ...]
    """
    import json as _json
    import subprocess as _sp
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    archives: list[dict] = []
    output_lines: list[str] = []

    try:
        config = _minimal_config(_resolve_repo_path())
        # Wir nutzen mode=list — entrypoint ruft borgmatic list (text). Für JSON
        # umgehen wir borgmatic und rufen borg list direkt via "shell"-Pfad nicht;
        # einfacher: lasse borgmatic stdout an uns durch und parse JSON.
        # Eleganter: spezielle mode "list-json". Vorerst nutzen wir borg direkt:
        token = uuid.uuid4().hex[:12]
        cfg_dir = Path("/data/shared") / token
        cfg_dir.mkdir(parents=True, exist_ok=True)
        _write_yaml(config, cfg_dir / "config.yaml")

        env = {
            "BORG_PASSPHRASE": settings.borg_passphrase or "",
            "BORG_RELOCATED_REPO_ACCESS_IS_OK": "yes",
            "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK": "yes",
            "BORG_CACHE_DIR": "/data/borg-cache",
            "BORG_CONFIG_DIR": "/data/borg-config",
            "BORG_REPO": _resolve_repo_path(),
            "TZ": os.environ.get("TZ", "UTC"),
        }
        if settings.backup_type == "scp":
            env["BORG_RSH"] = (
                "ssh -i /data/ssh/id_ed25519 "
                "-o UserKnownHostsFile=/data/ssh/known_hosts "
                "-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
            )
        if settings.backup_type == "webdav":
            env["DBORG_WEBDAV_URL"] = settings.webdav_url or ""
            env["DBORG_WEBDAV_USER"] = settings.webdav_user or ""
            env["DBORG_WEBDAV_PASSWORD"] = settings.webdav_password or ""
            env["DBORG_WEBDAV_VERIFY_SSL"] = "true" if settings.webdav_verify_ssl else "false"

        volumes: dict = {_detect_agent_data_volume(docker_client): {"bind": "/data", "mode": "rw"}}
        if settings.backup_type == "local" and settings.local_path:
            volumes[settings.local_path] = {"bind": "/mnt/repo", "mode": "rw"}

        cap_add: list[str] = []
        devices: list[str] = []
        security_opt: list[str] = []
        if settings.backup_type == "webdav":
            cap_add = ["SYS_ADMIN"]
            devices = ["/dev/fuse:/dev/fuse"]
            security_opt = ["apparmor=unconfined"]

        # borg list --json (kein borgmatic — schneller, struktiert)
        on_log("Hole Archiv-Liste via borg list --json", "info")
        worker = docker_client.containers.run(
            image=WORKER_IMAGE,
            entrypoint="borg",
            command=["list", "--json"],
            detach=True,
            stdin_open=False,
            tty=False,
            environment=env,
            volumes=volumes,
            mem_limit="256m",
            memswap_limit="256m",
            cap_add=cap_add,
            devices=devices,
            security_opt=security_opt,
        )
        try:
            for raw in worker.logs(stream=True, follow=True, stdout=True, stderr=True):
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    continue
                output_lines.append(text)
            worker.reload()
            exit_code = worker.attrs.get("State", {}).get("ExitCode", -1)
        finally:
            try:
                worker.remove(force=True)
            except Exception:
                pass
            try:
                shutil.rmtree(cfg_dir, ignore_errors=True)
            except Exception:
                pass

        full_output = "".join(output_lines)
        duration = round(time.time() - start, 2)

        if exit_code != 0:
            on_log(f"borg list fehlgeschlagen (exit {exit_code}): {full_output[:300]}", "error")
            return (
                WorkerResult(False, JobResult(duration_seconds=duration),
                             [LogEntry("error", f"borg list exit {exit_code}")]),
                [],
            )

        try:
            data = _json.loads(full_output)
            archives = data.get("archives", [])
        except (_json.JSONDecodeError, KeyError) as e:
            on_log(f"Konnte JSON nicht parsen: {e}", "error")
            return (
                WorkerResult(False, JobResult(duration_seconds=duration),
                             [LogEntry("error", f"JSON parse failed: {e}")]),
                [],
            )

        on_log(f"{len(archives)} Archive gefunden", "info")
        return (
            WorkerResult(True, JobResult(nfiles=len(archives), duration_seconds=duration),
                         [LogEntry("info", f"{len(archives)} Archive gefunden")]),
            archives,
        )
    finally:
        docker_client.close()


def run_restore(archive: str, on_log, sub_path: str = "", host_target: str = "") -> WorkerResult:
    """Extrahiert ein Archiv (oder einen Sub-Pfad).

    Wenn host_target gesetzt: dieser HOST-Pfad wird in den Worker gebunden
    und die Dateien landen direkt dort. Andernfalls: Extract ins agent-data
    Volume unter /data/restore, abrufbar via `docker cp dborg-agent:/data/restore ...`.
    """
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")

    extra_volumes: dict = {}
    extra_env: dict = {}
    if host_target:
        try:
            from pathlib import Path as _P
            _P(host_target).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        extra_volumes[host_target] = {"bind": "/restore", "mode": "rw"}
        extra_env["DBORG_RESTORE_DIR"] = "/restore"
    else:
        extra_env["DBORG_RESTORE_DIR"] = "/data/restore"

    try:
        config = _minimal_config(_resolve_repo_path())
        args = [archive]
        if sub_path:
            args.append(sub_path)
        exit_code, _ = _spawn_worker(
            docker_client,
            config=config,
            mode="extract",
            extra_args=args,
            extra_volumes=extra_volumes or None,
            extra_env=extra_env,
            on_log=on_log,
        )
        duration = round(time.time() - start, 2)
        if exit_code != 0:
            return WorkerResult(False, JobResult(duration_seconds=duration),
                                [LogEntry("error", f"Worker exit {exit_code}")])
        location = host_target or "/data/restore (im Agent-Container — via `docker cp dborg-agent:/data/restore <ziel>` abrufbar)"
        return WorkerResult(True, JobResult(archive_name=archive, duration_seconds=duration),
                            [LogEntry("info", f"Wiederherstellung abgeschlossen in {duration}s — Dateien unter: {location}")])
    finally:
        docker_client.close()
