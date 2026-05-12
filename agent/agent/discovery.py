from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

import docker

from .config import settings
from .models import ContainerInfo

logger = logging.getLogger(__name__)


def _get_compose_dir(container: docker.models.containers.Container) -> str | None:
    labels = container.labels or {}
    compose_dir = labels.get("com.docker.compose.project.working_dir")
    if compose_dir:
        return compose_dir
    return None


def _get_compose_project(container: docker.models.containers.Container) -> str:
    labels = container.labels or {}
    return labels.get("com.docker.compose.project", container.name or "unknown")


def _get_volume_mount_dirs(container: docker.models.containers.Container) -> set[str]:
    mounts = container.attrs.get("Mounts", [])
    dirs: set[str] = set()
    for m in mounts:
        source = m.get("Source", "")
        if source:
            dirs.add(source)
    return dirs


SYSTEM_MOUNTS = {"/var/run/docker.sock", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}


def _get_backup_mounts(container: docker.models.containers.Container, compose_dir_host: str) -> list[dict]:
    """Returns list of mounts to be backed up via Docker-API extraction.

    Excludes:
      - bind mounts inside the compose dir (already covered by compose-dir copy)
      - system mounts like /var/run/docker.sock

    Returned dicts: {type, name, dest, source, container}
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
                continue  # inside compose dir → already in compose copy
            except ValueError:
                pass
        result.append({
            "type": mtype,
            "name": m.get("Name", "") or "",
            "dest": dest,
            "source": source,
            "container": container.name or "",
        })
    return result


def _host_path_to_local(host_path: str) -> Path:
    """Map a host path to the mounted path inside the agent container.

    Only used for the compose dir (the one filesystem path we still need to
    read directly). HOST_BASE_DIR tells us the host-side base of the
    /host/docker mount; remaining path is preserved.
    """
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


SKIP_DIRS = {
    ".git", ".github", ".idea", ".vscode", ".venv", "venv", "node_modules",
    "__pycache__", "data", "logs", "dist", "build", ".pytest_cache",
}


def _collect_root_files(compose_dir_local: Path, volume_dirs: set[str], max_depth: int = 2) -> list[str]:
    volume_paths: set[Path] = set()
    for d in volume_dirs:
        try:
            volume_paths.add(Path(d).resolve())
        except OSError:
            pass

    files: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            resolved = directory.resolve()
        except OSError:
            return
        if resolved in volume_paths:
            return
        try:
            entries = list(directory.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file():
                    name = entry.name
                    for pattern in settings.root_file_globs:
                        if fnmatch.fnmatch(name, pattern):
                            rel = entry.relative_to(compose_dir_local)
                            files.append(str(rel))
                            break
                elif entry.is_dir() and depth < max_depth:
                    if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                        continue
                    walk(entry, depth + 1)
            except OSError:
                continue

    walk(compose_dir_local, 0)
    return sorted(set(files))


def discover_containers(manual_paths: dict[str, str] | None = None) -> list[ContainerInfo]:
    manual_paths = manual_paths or {}
    client = docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
    containers = client.containers.list(all=False)

    seen_projects: dict[str, ContainerInfo] = {}
    project_containers: dict[str, list[docker.models.containers.Container]] = {}

    for c in containers:
        project = _get_compose_project(c)
        if project not in project_containers:
            project_containers[project] = []
        project_containers[project].append(c)

    for project, ctrs in project_containers.items():
        primary = ctrs[0]
        compose_dir_host = manual_paths.get(project) or _get_compose_dir(primary) or ""

        all_volume_dirs: set[str] = set()
        backup_mounts: list[dict] = []
        seen_sources: set[str] = set()
        for c in ctrs:
            all_volume_dirs.update(_get_volume_mount_dirs(c))
            for m in _get_backup_mounts(c, compose_dir_host):
                if m["source"] in seen_sources:
                    continue
                seen_sources.add(m["source"])
                backup_mounts.append(m)

        has_volumes = len(all_volume_dirs) > 0

        if not compose_dir_host and not has_volumes:
            continue

        root_files: list[str] = []
        compose_dir_accessible = False
        if compose_dir_host:
            compose_dir_local = _host_path_to_local(compose_dir_host)
            if compose_dir_local.is_dir():
                compose_dir_accessible = True
                root_files = _collect_root_files(compose_dir_local, all_volume_dirs)
                if not root_files:
                    logger.info(
                        "Compose dir %s readable but no matching backup files within depth 2",
                        compose_dir_local,
                    )
            else:
                logger.warning(
                    "Compose dir %s mapped to %s but not accessible in agent. "
                    "Check DBORG_HOST_BASE_DIR and volume mount.",
                    compose_dir_host, compose_dir_local,
                )

        container_names = ", ".join(c.name for c in ctrs if c.name)
        images = ", ".join(
            {c.image.tags[0] if c.image.tags else c.image.short_id for c in ctrs}
        )

        info = ContainerInfo(
            container_id=primary.short_id,
            container_name=container_names,
            compose_project=project,
            compose_dir=compose_dir_host,
            root_files=root_files,
            image=images,
            status="running",
            has_volumes=has_volumes,
            compose_dir_accessible=compose_dir_accessible,
            backup_mounts=backup_mounts,
        )
        seen_projects[project] = info

    client.close()
    logger.info("Discovered %d compose projects", len(seen_projects))
    return list(seen_projects.values())
