from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import docker

from .config import settings
from .models import ContainerInfo

logger = logging.getLogger(__name__)


SYSTEM_MOUNTS = {"/var/run/docker.sock", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}

DEFAULT_HIDDEN_ENTRIES = {".git", ".github", ".idea", ".vscode", "__pycache__", ".pytest_cache"}

DIR_SIZE_TIMEOUT_S = 4.0

DB_IMAGE_PATTERNS = [
    ("postgresql", ("postgres", "pgvector", "timescale", "bitnami/postgresql")),
    ("mariadb", ("mariadb",)),
    ("mysql", ("mysql", "percona", "bitnami/mysql")),
    ("mongodb", ("mongo", "bitnami/mongodb")),
]

DB_DEFAULT_PORT = {"postgresql": 5432, "mariadb": 3306, "mysql": 3306, "mongodb": 27017}


def _env_dict(container) -> dict[str, str]:
    env_list = container.attrs.get("Config", {}).get("Env", []) or []
    result: dict[str, str] = {}
    for e in env_list:
        if "=" in e:
            k, v = e.split("=", 1)
            result[k] = v
    return result


# Default-Daten-Verzeichnis (Container-intern) je DB-Typ — wird auto-exkludiert
# wenn ein DB-Hook aktiv ist (dump-only Strategie). Liste von möglichen dests.
_DB_DATADIR_DESTS = {
    "postgresql": ("/var/lib/postgresql/data", "/var/lib/postgresql", "/bitnami/postgresql"),
    "mariadb": ("/var/lib/mysql", "/bitnami/mariadb"),
    "mysql": ("/var/lib/mysql", "/bitnami/mysql"),
    "mongodb": ("/data/db", "/bitnami/mongodb"),
}


def _db_data_exclude(container, db_type: str, compose_dir_host: str) -> dict | None:
    """Ermittelt, wo das rohe DB-Daten-Verzeichnis im Backup landet, damit es
    bei aktivem DB-Hook (dump-only) ausgeschlossen werden kann.

    Rückgabe:
      {"kind": "entry", "value": "<top-level-name>"} wenn die Daten in einem
          Unterordner des Compose-Verzeichnisses liegen, ODER
      {"kind": "mount", "value": "<container-dest>"} wenn sie über ein externes
          Volume / einen Bind-Mount außerhalb des Compose-Verzeichnisses kommen.
      None wenn kein Daten-Mount gefunden wurde.
    """
    candidates = _DB_DATADIR_DESTS.get(db_type, ())
    # PGDATA-Override berücksichtigen
    if db_type == "postgresql":
        env = _env_dict(container)
        pgdata = env.get("PGDATA")
        if pgdata:
            candidates = (pgdata,) + candidates

    for m in container.attrs.get("Mounts", []):
        dest = m.get("Destination", "")
        source = m.get("Source", "")
        if not dest or not source:
            continue
        matched = any(dest == c or dest.startswith(c + "/") or c.startswith(dest + "/") or c == dest
                      for c in candidates)
        if not matched:
            continue
        if compose_dir_host:
            try:
                rel = Path(source).relative_to(compose_dir_host)
                parts = rel.parts
                if parts:
                    return {"kind": "entry", "value": parts[0]}
            except ValueError:
                pass
        return {"kind": "mount", "value": dest}
    return None


def _detect_db(container, compose_dir_host: str = "") -> dict | None:
    image = ""
    if container.image and container.image.tags:
        image = container.image.tags[0].lower()
    else:
        image = (container.attrs.get("Config", {}).get("Image") or "").lower()
    if not image:
        return None

    matched: str | None = None
    for db_type, needles in DB_IMAGE_PATTERNS:
        for n in needles:
            if n in image:
                matched = db_type
                break
        if matched:
            break
    if not matched:
        return None

    env = _env_dict(container)
    result: dict | None = None

    if matched == "postgresql":
        result = {
            "db_type": "postgresql",
            "db_name": env.get("POSTGRES_DB") or env.get("POSTGRESQL_DATABASE") or "postgres",
            "hostname": container.name or "",
            "port": DB_DEFAULT_PORT["postgresql"],
            "username": env.get("POSTGRES_USER") or env.get("POSTGRESQL_USERNAME") or "postgres",
            "password": env.get("POSTGRES_PASSWORD") or env.get("POSTGRESQL_PASSWORD") or "",
            "container": container.name or "",
            "image": image,
        }
    elif matched in ("mariadb", "mysql"):
        result = {
            "db_type": matched,
            "db_name": env.get("MARIADB_DATABASE") or env.get("MYSQL_DATABASE") or "",
            "hostname": container.name or "",
            "port": DB_DEFAULT_PORT[matched],
            "username": (
                env.get("MARIADB_USER")
                or env.get("MYSQL_USER")
                or ("root" if (env.get("MARIADB_ROOT_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD")) else "")
            ),
            "password": (
                env.get("MARIADB_PASSWORD")
                or env.get("MYSQL_PASSWORD")
                or env.get("MARIADB_ROOT_PASSWORD")
                or env.get("MYSQL_ROOT_PASSWORD")
                or ""
            ),
            "container": container.name or "",
            "image": image,
        }
    elif matched == "mongodb":
        result = {
            "db_type": "mongodb",
            "db_name": env.get("MONGO_INITDB_DATABASE") or "admin",
            "hostname": container.name or "",
            "port": DB_DEFAULT_PORT["mongodb"],
            "username": env.get("MONGO_INITDB_ROOT_USERNAME") or "",
            "password": env.get("MONGO_INITDB_ROOT_PASSWORD") or "",
            "container": container.name or "",
            "image": image,
        }

    if result is not None:
        result["raw_exclude"] = _db_data_exclude(container, matched, compose_dir_host)
    return result


def _get_compose_dir(container) -> str | None:
    return (container.labels or {}).get("com.docker.compose.project.working_dir")


def _get_compose_project(container) -> str:
    labels = container.labels or {}
    return labels.get("com.docker.compose.project", container.name or "unknown")


def _host_path_to_local(host_path: str) -> Path:
    docker_host_dir = settings.docker_host_dir
    if host_path.startswith("/host/"):
        return Path(host_path)
    if settings.host_base_dir:
        try:
            rel = Path(host_path).relative_to(settings.host_base_dir)
            return Path(docker_host_dir) / rel
        except ValueError:
            pass
    return Path(docker_host_dir) / Path(host_path).name


def _dir_size_capped(path: Path, deadline: float) -> tuple[int, bool]:
    """Walk dir summing st_size; abort when time.monotonic() > deadline.

    Returns (bytes, complete). complete=False signals timeout.
    """
    total = 0
    try:
        stack = [str(path)]
    except OSError:
        return 0, True
    while stack:
        if time.monotonic() > deadline:
            return total, False
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
    return total, True


def _entry_size(path: Path, timeout_s: float = DIR_SIZE_TIMEOUT_S) -> tuple[int, bool]:
    try:
        if path.is_symlink():
            return 0, True
        if path.is_file():
            return path.stat().st_size, True
        if path.is_dir():
            return _dir_size_capped(path, time.monotonic() + timeout_s)
    except OSError:
        pass
    return 0, True


def _list_compose_dir_entries(compose_dir_local: Path) -> list[dict]:
    """Top-level Listing des Compose-Dirs: alle Files und Verzeichnisse,
    jeweils mit Größe. Versteckte „technische" Dirs (.git, __pycache__) sind
    als default_excluded markiert.
    """
    entries: list[dict] = []
    try:
        children = sorted(compose_dir_local.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (OSError, PermissionError):
        return entries
    for child in children:
        try:
            if child.is_symlink():
                continue
        except OSError:
            continue
        name = child.name
        kind = "dir" if child.is_dir() else "file"
        size, complete = _entry_size(child)
        entries.append({
            "name": name,
            "type": kind,
            "size_bytes": size,
            "size_complete": complete,
            "default_excluded": name in DEFAULT_HIDDEN_ENTRIES,
        })
    return entries


def _docker_exec_size(client: docker.DockerClient, container, dest: str) -> tuple[int, bool]:
    """Größe eines Mount-Ziels INNERHALB des laufenden Containers ermitteln.
    Verwendet `du -sb` — funktioniert in den meisten Images (busybox, coreutils).
    Bei Misserfolg: (0, False).
    """
    if not dest:
        return 0, False
    try:
        result = container.exec_run(
            cmd=["du", "-sb", "--", dest],
            stdout=True, stderr=False, demux=False,
        )
        out = (result.output or b"").decode("utf-8", errors="ignore").strip()
        if result.exit_code != 0 or not out:
            return 0, False
        first = out.split()[0]
        return int(first), True
    except (docker.errors.APIError, ValueError, AttributeError, OSError):
        return 0, False


def _get_backup_mounts(
    docker_client: docker.DockerClient,
    container,
    compose_dir_host: str,
    seen_sources: set[str],
) -> list[dict]:
    """Externe Mounts (Volumes + Bind-Mounts außerhalb compose_dir) listen.
    Größe per docker exec im Container — kapselt auch externe Bind-Sources
    wie Plex-Mediathek, die agent-seitig nicht erreichbar wären.
    """
    mounts = container.attrs.get("Mounts", [])
    result: list[dict] = []
    for m in mounts:
        mtype = m.get("Type", "")
        if mtype not in ("volume", "bind"):
            continue
        source = m.get("Source", "")
        dest = m.get("Destination", "")
        if not source or not dest:
            continue
        if source in SYSTEM_MOUNTS:
            continue
        if mtype == "bind" and compose_dir_host:
            try:
                Path(source).relative_to(compose_dir_host)
                continue
            except ValueError:
                pass
        if source in seen_sources:
            continue
        seen_sources.add(source)
        size, ok = _docker_exec_size(docker_client, container, dest)
        result.append({
            "type": mtype,
            "name": m.get("Name", "") or "",
            "dest": dest,
            "source": source,
            "container": container.name or "",
            "size_bytes": size,
            "size_complete": ok,
        })
    return result


def _own_compose_project(client: docker.DockerClient) -> str | None:
    """Compose-Projekt des Agent-Containers selbst — wird vom Backup ausgeschlossen.

    Backup von sich selbst bringt nichts: der Agent-Container hat keine Daten
    außer der Konfig, die zentral liegt. Self-Backup würde nur ein nutzloses
    Mini-Archive pro Lauf erzeugen.
    """
    try:
        import socket as _socket
        me = client.containers.get(_socket.gethostname())
        return (me.labels or {}).get("com.docker.compose.project")
    except Exception:  # noqa: BLE001
        return None


def discover_containers(manual_paths: dict[str, str] | None = None) -> list[ContainerInfo]:
    manual_paths = manual_paths or {}
    client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")

    own_project = _own_compose_project(client)

    project_containers: dict[str, list] = {}
    for c in client.containers.list(all=False):
        project = _get_compose_project(c)
        if project == own_project:
            continue  # Self-Backup vermeiden
        project_containers.setdefault(project, []).append(c)

    discovered: list[ContainerInfo] = []

    for project, ctrs in project_containers.items():
        primary = ctrs[0]
        compose_dir_host = manual_paths.get(project) or _get_compose_dir(primary) or ""

        backup_mounts: list[dict] = []
        seen_sources: set[str] = set()
        db_candidates: list[dict] = []

        for c in ctrs:
            backup_mounts.extend(_get_backup_mounts(client, c, compose_dir_host, seen_sources))
            db = _detect_db(c, compose_dir_host)
            if db:
                db_candidates.append(db)

        compose_dir_accessible = False
        top_level_entries: list[dict] = []
        if compose_dir_host:
            compose_dir_local = _host_path_to_local(compose_dir_host)
            if compose_dir_local.is_dir():
                compose_dir_accessible = True
                top_level_entries = _list_compose_dir_entries(compose_dir_local)
            else:
                logger.warning(
                    "Compose dir %s mapped to %s not accessible — check DBORG_HOST_BASE_DIR",
                    compose_dir_host, compose_dir_local,
                )

        has_volumes = len(backup_mounts) > 0
        if not compose_dir_host and not has_volumes:
            continue

        container_names = ", ".join(c.name for c in ctrs if c.name)
        images = ", ".join({c.image.tags[0] if c.image.tags else c.image.short_id for c in ctrs})

        info = ContainerInfo(
            container_id=primary.short_id,
            container_name=container_names,
            compose_project=project,
            compose_dir=compose_dir_host,
            root_files=[],
            image=images,
            status="running",
            has_volumes=has_volumes,
            compose_dir_accessible=compose_dir_accessible,
            backup_mounts=backup_mounts,
            top_level_entries=top_level_entries,
            db_candidates=db_candidates,
        )
        discovered.append(info)

    client.close()
    logger.info("Discovered %d compose projects", len(discovered))
    return discovered
