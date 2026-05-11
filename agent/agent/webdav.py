from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

_mounted = False


def ensure_mounted() -> bool:
    global _mounted

    if settings.backup_type != "webdav":
        return True

    if not settings.webdav_url:
        logger.error("WebDAV URL not configured")
        return False

    mount_point = Path(settings.webdav_mount)
    mount_point.mkdir(parents=True, exist_ok=True)

    if _is_mounted(mount_point):
        _mounted = True
        return True

    secrets_file = Path("/tmp/davfs2-secrets")
    secrets_file.write_text(
        f"{settings.webdav_url} {settings.webdav_user} {settings.webdav_password}\n"
    )
    secrets_file.chmod(0o600)

    result = subprocess.run(
        [
            "mount", "-t", "davfs",
            "-o", f"conf=/etc/davfs2/davfs2.conf,secrets={secrets_file}",
            settings.webdav_url,
            str(mount_point),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error("WebDAV mount failed: %s", result.stderr.strip())
        return False

    logger.info("WebDAV mounted: %s -> %s", settings.webdav_url, mount_point)
    _mounted = True

    if not settings.borg_repo:
        settings.borg_repo = str(mount_point / "borg")

    return True


def unmount():
    global _mounted
    if not _mounted:
        return

    mount_point = Path(settings.webdav_mount)
    subprocess.run(["umount", str(mount_point)], capture_output=True)
    _mounted = False
    logger.info("WebDAV unmounted: %s", mount_point)


def _is_mounted(path: Path) -> bool:
    try:
        with open("/proc/mounts") as f:
            return str(path) in f.read()
    except FileNotFoundError:
        result = subprocess.run(["mount"], capture_output=True, text=True)
        return str(path) in result.stdout
