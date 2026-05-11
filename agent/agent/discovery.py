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


def _host_path_to_local(host_path: str) -> Path:
    """Map a host path to the mounted path inside the agent container.

    The agent mounts the host's docker parent directory at DOCKER_HOST_DIR
    (default /host/docker). HOST_BASE_DIR tells us the host-side base of
    that mount (e.g. /home/user/docker or /share/Container). The relative
    portion is preserved.
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


def discover_containers() -> list[ContainerInfo]:
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
        compose_dir_host = _get_compose_dir(primary) or ""

        all_volume_dirs: set[str] = set()
        for c in ctrs:
            all_volume_dirs.update(_get_volume_mount_dirs(c))

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
        )
        seen_projects[project] = info

    client.close()
    logger.info("Discovered %d compose projects", len(seen_projects))
    return list(seen_projects.values())
