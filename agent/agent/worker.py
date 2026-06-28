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


def _looks_like_worker(container) -> bool:
    """Erkennt einen (auch alten, label-losen) Backup-Worker-Container."""
    labels = container.labels or {}
    if labels.get("com.docker-borg.role") == "worker":
        return True
    if (container.name or "").startswith("dborg-worker-"):
        return True
    try:
        img = container.image.tags[0] if container.image.tags else ""
    except Exception:  # noqa: BLE001
        img = ""
    return "docker-borg-worker" in img or img.startswith("dborg-worker")


def cleanup_stale_workers(docker_client=None) -> int:
    """Räumt verwaiste Worker-Container weg (Abstürze, alte Versionen). Nur
    NICHT laufende werden entfernt — ein gerade aktiver Worker bleibt unangetastet.
    Beim Agent-Start aufgerufen. Gibt die Anzahl entfernter Container zurück.
    """
    own = docker_client is None
    if own:
        docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    removed = 0
    try:
        for c in docker_client.containers.list(all=True):
            if c.status == "running":
                continue
            if not _looks_like_worker(c):
                continue
            try:
                c.remove(force=True)
                removed += 1
            except Exception:  # noqa: BLE001
                pass
        if removed:
            logger.info("Verwaiste Backup-Worker entfernt: %d", removed)
    finally:
        if own:
            docker_client.close()
    return removed


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


# Worker-Output-Klassifizierung: borgmatic mit -v 2 loggt subprocess-Invocations
# in Form "<repo>: ENV=*** ENV=*** borg <subcmd> --critical --log-json ..." —
# das ist kein Fehler, nur Debug. Naive "error in line" Heuristik triggert da
# fälschlich (--critical, --log-json enthalten "critical" und "json").
import re as _re
_ERROR_WORD = _re.compile(r"\b(error|critical|fatal)\b", _re.IGNORECASE)
_WARN_WORD = _re.compile(r"\b(warning|warn)\b", _re.IGNORECASE)
_COMMAND_DUMP = _re.compile(r":\s+(\w+=\S+\s+)+borg\s+\w+", _re.IGNORECASE)


def _classify_log_line(line: str) -> str:
    if _COMMAND_DUMP.search(line):
        return "info"
    if _ERROR_WORD.search(line):
        return "error"
    if _WARN_WORD.search(line):
        return "warning"
    return "info"


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


def _retention_to_config(retention: dict | None) -> dict:
    """Übersetzt Retention-Settings in borgmatic keep_* Keys.

    mode 'simple': keep_secondly=N entspricht borg --keep-last N (die N
        neuesten Archive behalten — exakt das 'wie viele Versionen' Modell).
    mode 'advanced': keep_daily/weekly/monthly.
    Gibt {} zurück wenn nichts Sinnvolles gesetzt → dann KEIN prune.
    """
    if not retention:
        return {}
    mode = retention.get("mode", "simple")
    out: dict = {}
    if mode == "advanced":
        for unit in ("daily", "weekly", "monthly"):
            v = int(retention.get(f"keep_{unit}") or 0)
            if v > 0:
                out[f"keep_{unit}"] = v
    else:
        n = int(retention.get("keep_last") or 0)
        if n > 0:
            out["keep_secondly"] = n  # == borg --keep-last N
    return out


def _build_borgmatic_config(
    container: ContainerInfo,
    repo_path: str,
    archive_prefix: str,
    excluded_mounts: set | None = None,
    db_hooks: list[dict] | None = None,
    excluded_entries: list[str] | None = None,
    retention: dict | None = None,
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

    exclude_patterns = [
        "**/.git", "**/node_modules", "**/__pycache__", "*.pyc",
        "**/.venv", "**/venv", "**/.cache", "**/.DS_Store",
    ]
    # Vom User abgewählte Top-Level-Einträge unter dem Compose-Dir
    for name in (excluded_entries or []):
        if not name or "/" in name or name.startswith("."):
            # Sicherheitsnetz: keine Pfad-Traversal-Patterns, keine versteckten Dirs hier
            if name and name not in (".git", ".github", ".idea", ".vscode", "__pycache__", ".pytest_cache"):
                continue
        exclude_patterns.append(f"/mnt/compose/{name}")
        exclude_patterns.append(f"/mnt/compose/{name}/**")

    config = {
        "source_directories": sources or ["/mnt/compose"],
        "repositories": [{"path": repo_path}],
        "archive_name_format": f"{archive_prefix}-{{now:%Y%m%dT%H%M%S}}",
        "compression": "lz4",
        "one_file_system": False,
        "exclude_patterns": exclude_patterns,
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
            # Server-Version automatisch erkennen und passenden Client wählen
            # (pg_restore 17 crashed gegen PG <17 Server, transaction_timeout)
            entry["pg_dump_command"] = "dborg-pg-shim pg_dump"
            entry["pg_restore_command"] = "dborg-pg-shim pg_restore"
        if t in ("mariadb", "mysql"):
            # mariadb-client 10.19+/11.x verlangt TLS by default. Compose-interne
            # mariadb-/mysql-Container haben oft kein TLS → Verbindung schlägt fehl
            # mit "TLS/SSL error: SSL is required, but the server does not support it".
            # Default tls=false; User kann das im UI überschreiben, wenn er TLS-Server hat.
            entry.setdefault("tls", False)
            entry.setdefault("restore_tls", False)
        grouped.setdefault(key, []).append(entry)

    for key, items in grouped.items():
        config[key] = items

    # Retention (keep_*) — matched archives auf dieses Projekt einschränken,
    # damit prune nur die Archive DIESES Compose-Projekts ausdünnt und nicht
    # die anderer Projekte im selben Repo wegräumt.
    keep = _retention_to_config(retention)
    if keep:
        config.update(keep)
        config["match_archives"] = f"sh:{archive_prefix}-*"

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


def _safe_host(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or "agent"))


def _resolve_repo_path() -> str:
    """Path that the WORKER sees for the borg repo.

    SCP: das von Central gebaute borg_repo (enthält schon den Agent-Unterordner
    ssh://…/basis/<hostname>) — eine Quelle, keine doppelte URL-Konstruktion.
    local: der lokale Pfad ist als /mnt/repo gemountet; das Repo liegt im
    Unterordner /mnt/repo/<hostname> — so zeigt borg nie auf einen Share-Root
    (der z.B. auf QNAP @Recycle/Metadaten enthält → 'already something').
    """
    if settings.backup_type == "scp":
        return settings.borg_repo or ""
    if settings.backup_type == "local":
        return f"/mnt/repo/{_safe_host(settings.agent_name)}"
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
    resources: dict | None = None,
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
        # borg/borgmatic immer mit niedrigster CPU/IO-Priorität fahren, damit
        # das Backup laufende Workloads (z.B. ML-Inferenz) nicht ausbremst.
        "DBORG_NICE": "1",
    }
    if settings.backup_type == "scp":
        env["BORG_RSH"] = (
            "ssh -i /data/ssh/id_ed25519 "
            "-o UserKnownHostsFile=/data/ssh/known_hosts "
            "-o StrictHostKeyChecking=accept-new "
            "-o BatchMode=yes"
        )
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

    res = resources or {}
    mem_mb = int(res.get("mem_mb") or 0)
    if mem_mb <= 0:
        mem_mb = 1024  # vernünftiger Default; 512m war zu knapp für große Repos
    mem_str = f"{mem_mb}m"

    run_kwargs: dict = {
        "image": WORKER_IMAGE,
        "command": [mode] + (extra_args or []),
        "name": f"dborg-worker-{mode}-{job_token}",
        "labels": {
            "com.docker-borg.role": "worker",
            "com.docker-borg.agent": settings.agent_name or "",
            "com.docker-borg.mode": mode,
        },
        "detach": True,
        "stdin_open": False,
        "tty": False,
        "environment": env,
        "volumes": volumes,
        "mem_limit": mem_str,
        "memswap_limit": mem_str,
        "cap_add": cap_add,
        "devices": devices,
        "security_opt": security_opt,
    }
    # Optionales CPU-Limit (cores). 0/leer = kein hartes Limit (nice/ionice
    # sorgt ohnehin für niedrige Priorität).
    try:
        cpus = float(res.get("cpus") or 0)
    except (TypeError, ValueError):
        cpus = 0.0
    if cpus > 0:
        run_kwargs["nano_cpus"] = int(cpus * 1_000_000_000)
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
                on_log(line, _classify_log_line(line))

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
               db_hooks: list[dict] | None = None,
               excluded_entries: list[str] | None = None,
               retention: dict | None = None,
               resources: dict | None = None) -> WorkerResult:
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
                                          db_hooks=db_hooks,
                                          excluded_entries=excluded_entries,
                                          retention=retention)

        keep = _retention_to_config(retention)
        prune = bool(keep)
        if prune:
            on_log(f"Retention aktiv ({', '.join(f'{k}={v}' for k, v in keep.items())}) — prune+compact nach Backup", "info")

        extra_volumes: dict = {}
        if container.compose_dir:
            extra_volumes[container.compose_dir] = {"bind": "/mnt/compose", "mode": "ro"}

        exit_code, _ = _spawn_worker(
            docker_client,
            config=config,
            mode="create",
            volumes_from_target=targets,
            extra_volumes=extra_volumes,
            extra_env={"DBORG_PRUNE": "1"} if prune else None,
            resources=resources,
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

        volumes: dict = {_detect_agent_data_volume(docker_client): {"bind": "/data", "mode": "rw"}}
        if settings.backup_type == "local" and settings.local_path:
            volumes[settings.local_path] = {"bind": "/mnt/repo", "mode": "rw"}

        cap_add: list[str] = []
        devices: list[str] = []
        security_opt: list[str] = []

        # borg list --json (kein borgmatic — schneller, struktiert)
        on_log("Hole Archiv-Liste via borg list --json", "info")
        worker = docker_client.containers.run(
            image=WORKER_IMAGE,
            entrypoint="borg",
            command=["list", "--json"],
            name=f"dborg-worker-list-{token}",
            labels={
                "com.docker-borg.role": "worker",
                "com.docker-borg.agent": settings.agent_name or "",
                "com.docker-borg.mode": "list",
            },
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


def run_restore(archive: str, on_log, sub_path: str = "", host_target: str = "",
                structured: bool = False) -> WorkerResult:
    """Extrahiert ein Archiv (oder einen Sub-Pfad).

    structured=True: legt den Inhalt aufgeräumt ab (compose/ volumes/ databases/)
    statt der rohen borg-internen Struktur — für Restore an einen Zielort, mit
    dem man arbeiten will.

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
        # Strukturierter Restore ignoriert sub_path (legt immer alles aufgeräumt ab)
        mode = "extract-structured" if structured else "extract"
        args = [archive]
        if sub_path and not structured:
            args.append(sub_path)
        exit_code, _ = _spawn_worker(
            docker_client,
            config=config,
            mode=mode,
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
        hint = " — aufgeräumt in compose/ volumes/ databases/" if structured else ""
        return WorkerResult(True, JobResult(archive_name=archive, duration_seconds=duration),
                            [LogEntry("info", f"Wiederherstellung abgeschlossen in {duration}s — Dateien unter: {location}{hint}")])
    finally:
        docker_client.close()


def _extract_to(docker_client, archive: str, sub_path: str, strip: int, on_log,
                host_target: str | None = None, volume_name: str | None = None) -> int:
    """Ein Worker-Spawn: extrahiert sub_path aus archive, schneidet 'strip'
    Pfadkomponenten ab und schreibt den Inhalt direkt ins Ziel — entweder einen
    Host-Pfad (bind-mount) oder ein Docker-Named-Volume, jeweils als /restore
    in den Worker gebunden. Gibt den Exit-Code zurück.
    """
    config = _minimal_config(_resolve_repo_path())
    extra_volumes: dict = {}
    extra_env = {"DBORG_RESTORE_DIR": "/restore", "DBORG_STRIP": str(strip)}
    if host_target:
        try:
            from pathlib import Path as _P
            _P(host_target).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        extra_volumes[host_target] = {"bind": "/restore", "mode": "rw"}
    elif volume_name:
        extra_volumes[volume_name] = {"bind": "/restore", "mode": "rw"}
    else:
        return 2
    exit_code, _ = _spawn_worker(
        docker_client, config=config, mode="extract",
        extra_args=[archive, sub_path], extra_volumes=extra_volumes,
        extra_env=extra_env, on_log=on_log,
    )
    return exit_code


def run_restore_inplace(container: ContainerInfo, archive: str, mounts: list[dict],
                        compose_dir: str, db_hooks: list[dict], on_log) -> WorkerResult:
    """Modus B — Komplett-Restore an den Originalort ('läuft wieder'):
      1. Projekt-Container stoppen
      2. Compose-Verzeichnis + Volumes (bind UND named) zurückschreiben
      3. Container wieder starten
      4. DB-Dumps einspielen (mit Retry, weil die DB nach dem Start erst
         hochfahren muss)
    """
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    logs: list[LogEntry] = []
    try:
        targets = _resolve_all_project_containers(docker_client, container.compose_project)
        names = [c.name for c in targets]

        # 1. Stop
        on_log(f"Stoppe {len(targets)} Container: {', '.join(names) or '(keine)'}", "info")
        for c in targets:
            try:
                c.stop(timeout=30)
            except Exception as e:  # noqa: BLE001
                on_log(f"  Stop {c.name} fehlgeschlagen: {e}", "warning")

        # 2. Compose-Verzeichnis
        if compose_dir:
            on_log(f"Compose-Verzeichnis → {compose_dir}", "info")
            rc = _extract_to(docker_client, archive, "mnt/compose", 2, on_log, host_target=compose_dir)
            if rc != 0:
                on_log(f"  Compose-Restore exit {rc}", "warning")

        # 2b. Volumes (bind + named)
        for m in (mounts or []):
            ap = (m.get("dest") or "").lstrip("/")
            if not ap:
                continue
            strip = len(ap.split("/"))
            if m.get("type") == "bind" and m.get("source"):
                on_log(f"Volume (bind) {ap} → {m['source']}", "info")
                _extract_to(docker_client, archive, ap, strip, on_log, host_target=m["source"])
            elif m.get("type") == "volume" and m.get("name"):
                on_log(f"Volume (named) {ap} → {m['name']}", "info")
                _extract_to(docker_client, archive, ap, strip, on_log, volume_name=m["name"])

        # 3. Start
        on_log("Starte Container wieder…", "info")
        for c in targets:
            try:
                c.start()
            except Exception as e:  # noqa: BLE001
                on_log(f"  Start {c.name} fehlgeschlagen: {e}", "warning")

        # 4. DB-Replay mit Retry (DB braucht nach dem Start einen Moment)
        if db_hooks:
            import time as _t
            ok = False
            for attempt in range(1, 5):
                on_log(f"DB-Dumps einspielen (Versuch {attempt}/4)…", "info")
                _t.sleep(10)
                dbres = run_db_restore(container, archive, db_hooks, on_log)
                if dbres.success:
                    ok = True
                    break
                on_log("  DB noch nicht bereit, neuer Versuch…", "warning")
            if not ok:
                on_log("DB-Replay nicht erfolgreich — bitte später über den "
                        "'DB-Dumps zurückspielen'-Button nachholen.", "error")
                logs.append(LogEntry("warning", "Files + Container wiederhergestellt, DB-Replay offen"))

        duration = round(time.time() - start, 2)
        on_log(f"Restore an Originalort abgeschlossen in {duration}s", "info")
        return WorkerResult(True, JobResult(archive_name=archive, duration_seconds=duration), logs)
    finally:
        docker_client.close()


def run_db_restore(container: ContainerInfo, archive: str, db_hooks: list[dict], on_log) -> WorkerResult:
    """Spielt DB-Dumps eines Archivs zurück in die laufenden DB-Container.

    Der Worker wird ans Compose-Netz des Projekts gehängt, damit er den
    DB-Container per Hostname (Container-Name) erreichen kann. borgmatic
    extrahiert die Dumps aus dem Archiv und feuert pg_restore/mysql/mongorestore
    automatisch.
    """
    start = time.time()
    docker_client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    try:
        targets = _resolve_all_project_containers(docker_client, container.compose_project)
        if not targets:
            return WorkerResult(False, JobResult(),
                                [LogEntry("error", f"Kein Container für '{container.compose_project}' gefunden")])

        archive_prefix = f"{settings.agent_name}-{container.compose_project}"
        repo_path = _resolve_repo_path()
        config = _build_borgmatic_config(container, repo_path, archive_prefix,
                                          db_hooks=db_hooks)
        # Restore braucht keine source_directories — leeren, sonst sieht borgmatic
        # einen Backup-Lauf
        config["source_directories"] = ["/tmp"]

        on_log(f"DB-Replay aus Archiv {archive} → {len(db_hooks)} DB(s)", "info")
        exit_code, _ = _spawn_worker(
            docker_client,
            config=config,
            mode="restore",
            extra_args=["--archive", archive],
            volumes_from_target=targets,  # für Netz-Anschluss ans Compose-Netz
            on_log=on_log,
        )
        duration = round(time.time() - start, 2)
        if exit_code != 0:
            return WorkerResult(False, JobResult(duration_seconds=duration),
                                [LogEntry("error", f"DB-Replay fehlgeschlagen (exit {exit_code})")])
        return WorkerResult(True, JobResult(archive_name=archive, duration_seconds=duration),
                            [LogEntry("info", f"DB-Replay abgeschlossen in {duration}s")])
    finally:
        docker_client.close()
