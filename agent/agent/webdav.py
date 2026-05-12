from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from .config import settings

logger = logging.getLogger(__name__)

_mounted = False

DAVFS_CERTS_DIR = Path("/etc/davfs2/certs")
DAVFS_CONF = Path("/etc/davfs2/davfs2.conf")


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

    cert_info = ""
    if not settings.webdav_verify_ssl:
        cert_path = _fetch_server_cert(settings.webdav_url)
        if cert_path:
            _set_servercert_in_conf(cert_path)
            cert_info = " (selbst-signiertes Zertifikat akzeptiert)"
        else:
            return False, "SSL-Zertifikat konnte nicht via openssl s_client geholt werden"

    secrets_file = Path("/tmp/davfs2-secrets")
    secrets_file.write_text(
        f"{settings.webdav_url} {settings.webdav_user} {settings.webdav_password}\n"
    )
    secrets_file.chmod(0o600)

    result = subprocess.run(
        [
            "mount", "-t", "davfs",
            "-o", f"conf={DAVFS_CONF},secrets={secrets_file}",
            settings.webdav_url,
            str(mount_point),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "(keine Ausgabe)").strip()
        logger.error("WebDAV mount failed: %s", err)
        return False, f"davfs2-Mount fehlgeschlagen: {err}"

    logger.info("WebDAV mounted: %s -> %s", settings.webdav_url, mount_point)
    _mounted = True

    if not settings.borg_repo:
        settings.borg_repo = str(mount_point / "borg")

    return True, f"WebDAV gemountet: {settings.webdav_url} → {mount_point}{cert_info}"


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


def _fetch_server_cert(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        return None

    DAVFS_CERTS_DIR.mkdir(parents=True, exist_ok=True)
    cert_file = DAVFS_CERTS_DIR / f"{host}.pem"

    try:
        result = subprocess.run(
            ["openssl", "s_client", "-connect", f"{host}:{port}", "-servername", host, "-showcerts"],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error("Cannot fetch server cert via openssl: %s", e)
        return None

    output = result.stdout
    pem_blocks: list[str] = []
    in_block = False
    current: list[str] = []
    for line in output.splitlines():
        if "BEGIN CERTIFICATE" in line:
            in_block = True
            current = [line]
        elif "END CERTIFICATE" in line and in_block:
            current.append(line)
            pem_blocks.append("\n".join(current))
            in_block = False
        elif in_block:
            current.append(line)

    if not pem_blocks:
        logger.error("No certificate found for %s:%d", host, port)
        return None

    cert_file.write_text("\n".join(pem_blocks) + "\n")
    cert_file.chmod(0o644)
    return cert_file


def _set_servercert_in_conf(cert_path: Path) -> None:
    text = DAVFS_CONF.read_text() if DAVFS_CONF.exists() else ""
    lines = [l for l in text.splitlines() if not l.strip().startswith("servercert ")]
    lines.append(f"servercert {cert_path}")
    DAVFS_CONF.write_text("\n".join(lines) + "\n")
