from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

SSH_DIR = Path("/data/ssh")
KEY_PATH = SSH_DIR / "id_ed25519"
PUB_PATH = SSH_DIR / "id_ed25519.pub"
KNOWN_HOSTS = SSH_DIR / "known_hosts"


def ensure_keypair() -> None:
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    SSH_DIR.chmod(0o700)
    if KEY_PATH.exists():
        return
    hostname = settings.agent_name or "agent"
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEY_PATH),
         "-C", f"docker-borg-agent@{hostname}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {result.stderr.strip()}")
    KEY_PATH.chmod(0o600)
    logger.info("Generated SSH keypair at %s", KEY_PATH)


def get_public_key() -> str:
    ensure_keypair()
    return PUB_PATH.read_text().strip()


def ssh_base_options() -> list[str]:
    return [
        "-i", str(KEY_PATH),
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
    ]


def borg_rsh() -> str:
    ensure_keypair()
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    return "ssh " + " ".join(ssh_base_options())


def test_connection(host: str, user: str, port: int) -> tuple[bool, str]:
    if not host or not user:
        return False, "Host und Benutzer müssen gesetzt sein"
    ensure_keypair()
    cmd = ["ssh"] + ssh_base_options() + ["-p", str(port), f"{user}@{host}", "echo dborg-connection-ok"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "SSH-Verbindung: Timeout nach 30s"
    if r.returncode == 0 and "dborg-connection-ok" in (r.stdout or ""):
        return True, f"SSH-Verbindung zu {user}@{host}:{port} OK (Public Key wird akzeptiert)"
    err = ((r.stderr or "") + (r.stdout or "")).strip()
    if "Permission denied" in err:
        return False, (
            f"SSH zu {user}@{host}:{port} verweigert. "
            "Public Key ist auf dem Server nicht autorisiert — "
            "kopiere ihn in ~/.ssh/authorized_keys oder nutze 'Auf Server installieren'."
        )
    return False, err or f"ssh exit code {r.returncode}"


def install_pubkey(host: str, user: str, port: int, password: str) -> tuple[bool, str]:
    if not (host and user and password):
        return False, "Host, Benutzer und Passwort müssen gesetzt sein"
    ensure_keypair()
    pub = get_public_key()
    # Use sshpass via env var (not command line) so password isn't visible in ps
    remote_script = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"echo '{pub}' >> ~/.ssh/authorized_keys && "
        "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys"
    )
    cmd = [
        "sshpass", "-e", "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "PubkeyAuthentication=no",
        "-o", "ConnectTimeout=15",
        "-p", str(port),
        f"{user}@{host}",
        remote_script,
    ]
    env = os.environ.copy()
    env["SSHPASS"] = password
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env)
    except subprocess.TimeoutExpired:
        return False, "Installation: Timeout nach 45s"
    if r.returncode == 0:
        return True, f"Public Key auf {user}@{host}:{port} installiert. Künftige Verbindungen laufen passwortlos."
    err = ((r.stderr or "") + (r.stdout or "")).strip()
    return False, err or f"sshpass exit code {r.returncode}"
