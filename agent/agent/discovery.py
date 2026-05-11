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


def _collect_root_files(compose_dir_local: Path, volume_dirs: set[str]) -> list[str]:
    if not compose_dir_local.is_dir():
        logger.warning("Compose dir not accessible: %s", compose_dir_local)
        return []

    volume_basenames = {Path(d).name for d in volume_dirs}
    files: list[str] = []

    for entry in compose_dir_local.iterdir():
        if entry.is_dir():
            continue
        name = entry.name
        for pattern in settings.root_file_globs:
            if fnmatch.fnmatch(name, pattern):
                files.append(name)
                break

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
        if compose_dir_host:
            compose_dir_local = _host_path_to_local(compose_dir_host)
            if compose_dir_local.is_dir():
                root_files = _collect_root_files(compose_dir_local, all_volume_dirs)
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
        )
        seen_projects[project] = info

    client.close()
    logger.info("Discovered %d compose projects", len(seen_projects))
    return list(seen_projects.values())
