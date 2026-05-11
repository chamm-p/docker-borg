from __future__ import annotations

import socket
from datetime import datetime
from pathlib import Path

import httpx

from ..models import Agent


def check_connection(agent: Agent) -> tuple[bool, str]:
    backup_type = agent.backup_type or "scp"
    if backup_type == "scp":
        return _check_scp(agent)
    if backup_type == "local":
        return _check_local(agent)
    if backup_type == "webdav":
        return _check_webdav(agent)
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


def _check_webdav(agent: Agent) -> tuple[bool, str]:
    if not agent.webdav_url:
        return False, "WebDAV-URL nicht gesetzt"
    auth = None
    if agent.webdav_user:
        auth = (agent.webdav_user, agent.webdav_password or "")
    try:
        resp = httpx.request("PROPFIND", agent.webdav_url, auth=auth, timeout=10, headers={"Depth": "0"})
    except httpx.RequestError as e:
        return False, f"WebDAV nicht erreichbar: {e}"
    if resp.status_code in (200, 207):
        return True, "WebDAV erreichbar und Authentifizierung erfolgreich"
    if resp.status_code == 401:
        return False, "WebDAV: Authentifizierung fehlgeschlagen (401)"
    if resp.status_code == 404:
        return False, "WebDAV: Pfad nicht gefunden (404)"
    return False, f"WebDAV antwortet mit HTTP {resp.status_code}"


def record_result(agent: Agent, ok: bool, message: str) -> None:
    agent.last_connection_check = datetime.utcnow()
    agent.last_connection_ok = ok
    agent.last_connection_error = None if ok else message
