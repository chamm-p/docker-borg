from __future__ import annotations

import socket
from datetime import datetime
from pathlib import Path

from ..models import Agent


def check_connection(agent: Agent) -> tuple[bool, str]:
    backup_type = agent.backup_type or "scp"
    if backup_type == "scp":
        return _check_scp(agent)
    if backup_type == "local":
        return _check_local(agent)
    return False, f"Unbekannter Backup-Typ: {backup_type}"


def _check_scp(agent: Agent) -> tuple[bool, str]:
    if not agent.scp_host:
        return False, "SCP-Host nicht gesetzt"
    port = agent.scp_port or 22
    try:
        with socket.create_connection((agent.scp_host, port), timeout=5):
            pass
    except OSError as e:
        return False, f"Verbindung zu {agent.scp_host}:{port} fehlgeschlagen: {e}"
    return True, f"SSH-Port {port} auf {agent.scp_host} erreichbar (Authentifizierung wird beim Backup auf dem Agent geprüft)"


def _check_local(agent: Agent) -> tuple[bool, str]:
    if not agent.local_path:
        return False, "Lokaler Pfad nicht gesetzt"
    return True, f"Pfad „{agent.local_path}“ wird auf dem Agent geprüft, sobald ein Backup läuft"


def record_result(agent: Agent, ok: bool, message: str) -> None:
    agent.last_connection_check = datetime.utcnow()
    agent.last_connection_ok = ok
    agent.last_connection_error = None if ok else message
