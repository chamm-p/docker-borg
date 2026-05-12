from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

_mounted = False

RCLONE_CONFIG_DIR = Path("/tmp/rclone")


def _obscure(password: str) -> str:
    result = subprocess.run(
        ["rclone", "obscure", password],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


def _write_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    obscured = _obscure(settings.webdav_password or "")
    path.write_text(
        "[webdav]\n"
        "type = webdav\n"
        f"url = {settings.webdav_url}\n"
        "vendor = other\n"
        f"user = {settings.webdav_user}\n"
        f"pass = {obscured}\n"
    )
    path.chmod(0o600)


def ensure_mounted() -> tuple[bool, str]:
    """Returns (ok, detail). detail is a human message describing what happened."""
    global _mounted

    if settings.backup_type != "webdav":
        return True, ""

    if not settings.webdav_url:
        return False, "WebDAV-URL ist nicht gesetzt"

    mount_point = Path(settings.webdav_mount)
    mount_point.mkdir(parents=True, exist_ok=True)

    if _is_mounted(mount_point):
        _mounted = True
        return True, f"WebDAV bereits gemountet unter {mount_point}"

    config_path = RCLONE_CONFIG_DIR / "rclone.conf"
    _write_config(config_path)

    probe_cmd = ["rclone", "--config", str(config_path), "lsd", "webdav:"]
    if not settings.webdav_verify_ssl:
        probe_cmd.append("--no-check-certificate")
    try:
        probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "rclone-Verbindungsprüfung: Timeout nach 30s (Server antwortet nicht)"
    if probe.returncode != 0:
        err = (probe.stderr or probe.stdout or "").strip() or f"exit {probe.returncode}"
        return False, f"WebDAV-Verbindung fehlgeschlagen (vor Mount):\n{err}"

    if not Path("/dev/fuse").exists():
        return False, ("/dev/fuse fehlt im Agent-Container. Ergänze in der Agent-Compose:\n"
                       "  cap_add:\n    - SYS_ADMIN\n  devices:\n    - /dev/fuse")

    log_path = Path("/tmp/rclone-mount.log")
    log_path.write_text("")

    cmd = [
        "rclone",
        "--config", str(config_path),
        "--log-file", str(log_path),
        "--log-level", "INFO",
        "mount", "webdav:", str(mount_point),
        "--vfs-cache-mode", "writes",
        "--dir-cache-time", "5s",
    ]
    if not settings.webdav_verify_ssl:
        cmd.append("--no-check-certificate")

    logger.info("Running (background): %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        if _is_mounted(mount_point):
            break
        if proc.poll() is not None:
            log_tail = ""
            try:
                log_tail = "\n".join(log_path.read_text().splitlines()[-30:])
            except OSError:
                pass
            return False, f"rclone-Mount-Prozess hat sich beendet (exit {proc.returncode}). Log:\n{log_tail or '(leer)'}"
        time.sleep(0.5)
    else:
        try:
            proc.terminate()
        except OSError:
            pass
        log_tail = ""
        try:
            log_tail = "\n".join(log_path.read_text().splitlines()[-30:])
        except OSError:
            pass
        return False, f"rclone-Mount: 30s Timeout — Mount erscheint nicht. Log:\n{log_tail or '(leer)'}"

    insecure_info = " (SSL-Verifikation deaktiviert)" if not settings.webdav_verify_ssl else ""
    logger.info("WebDAV mounted: %s -> %s", settings.webdav_url, mount_point)
    _mounted = True

    if not settings.borg_repo:
        settings.borg_repo = str(mount_point / "borg")

    return True, f"WebDAV gemountet via rclone: {settings.webdav_url} → {mount_point}{insecure_info}"


def unmount():
    global _mounted
    if not _mounted:
        return
    mount_point = Path(settings.webdav_mount)
    for cmd in (["fusermount3", "-u", str(mount_point)],
                ["fusermount", "-u", str(mount_point)],
                ["umount", str(mount_point)]):
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            break
    _mounted = False
    logger.info("WebDAV unmounted: %s", mount_point)


def _is_mounted(path: Path) -> bool:
    try:
        with open("/proc/mounts") as f:
            return str(path) in f.read()
    except FileNotFoundError:
        result = subprocess.run(["mount"], capture_output=True, text=True)
        return str(path) in result.stdout
