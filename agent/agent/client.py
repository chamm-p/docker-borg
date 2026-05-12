from __future__ import annotations

import logging
import socket

import httpx

from .config import settings
from .models import ContainerInfo, Job, JobType, LogEntry
from .version import AGENT_VERSION

logger = logging.getLogger(__name__)


class CentralClient:
    def __init__(self):
        self._token: str | None = None
        self._load_token()
        self._http = httpx.Client(base_url=settings.central_url, timeout=30)
        self.manual_paths: dict[str, str] = {}
        self.cancelled_jobs: set[int] = set()

    def _load_token(self):
        if settings.token_file.exists():
            self._token = settings.token_file.read_text().strip()
            logger.info("Loaded agent token from %s", settings.token_file)

    def _save_token(self, token: str):
        self._token = token
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.token_file.write_text(token)
        logger.info("Saved agent token")

    def _clear_token(self):
        self._token = None
        try:
            settings.token_file.unlink()
        except FileNotFoundError:
            pass
        logger.info("Cleared agent token; will re-register")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @property
    def is_registered(self) -> bool:
        return self._token is not None

    def _apply_backup_config(self, data: dict):
        paths = data.get("manual_paths", {})
        if isinstance(paths, dict):
            self.manual_paths = {k: str(v) for k, v in paths.items() if v}
        cancelled = data.get("cancelled_jobs", [])
        if isinstance(cancelled, list):
            self.cancelled_jobs = {int(j) for j in cancelled if isinstance(j, int) or (isinstance(j, str) and j.isdigit())}
        backup = data.get("backup", {})
        if not isinstance(backup, dict):
            return
        settings.backup_type = backup.get("backup_type", "scp")
        settings.borg_repo = backup.get("borg_repo", "") or ""
        if backup.get("borg_passphrase"):
            settings.borg_passphrase = backup["borg_passphrase"]
        settings.scp_host = backup.get("scp_host", "")
        settings.scp_user = backup.get("scp_user", "")
        settings.scp_path = backup.get("scp_path", "")
        settings.scp_port = int(backup.get("scp_port") or 22)
        settings.local_path = backup.get("local_path", "")
        settings.webdav_url = backup.get("webdav_url", "")
        settings.webdav_user = backup.get("webdav_user", "")
        if backup.get("webdav_password"):
            settings.webdav_password = backup["webdav_password"]
        settings.webdav_verify_ssl = bool(backup.get("webdav_verify_ssl", True))

    def register(self) -> bool:
        hostname = settings.agent_name or socket.gethostname()
        try:
            resp = self._http.post(
                "/api/v1/agents/register",
                json={
                    "hostname": hostname,
                    "agent_version": AGENT_VERSION,
                    "token": settings.registration_token,
                },
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 201:
                data = resp.json()
                self._save_token(data["agent_token"])
                self._apply_backup_config(data)
                logger.info("Registered with central as '%s'", hostname)
                return True
            logger.error("Registration failed: %s %s", resp.status_code, resp.text)
            return False
        except httpx.RequestError as e:
            logger.warning("Cannot reach central for registration: %s", e)
            return False

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response | None:
        try:
            resp = self._http.request(method, path, headers=self._headers(), **kwargs)
        except httpx.RequestError as e:
            logger.warning("Request %s %s failed: %s", method, path, e)
            return None
        if resp.status_code == 401 and self._token:
            logger.warning("Central rejected token (401); re-registering")
            self._clear_token()
            if self.register():
                try:
                    return self._http.request(method, path, headers=self._headers(), **kwargs)
                except httpx.RequestError as e:
                    logger.warning("Retry %s %s failed: %s", method, path, e)
                    return None
        return resp

    def heartbeat(self, containers: list[ContainerInfo]) -> bool:
        ssh_pubkey = ""
        try:
            from .ssh import get_public_key
            ssh_pubkey = get_public_key()
        except Exception as e:
            logger.warning("Could not produce SSH pubkey: %s", e)
        body = {
            "hostname": settings.agent_name or socket.gethostname(),
            "agent_version": AGENT_VERSION,
            "ssh_public_key": ssh_pubkey,
            "containers": [
                {
                    "container_id": c.container_id,
                    "container_name": c.container_name,
                    "compose_project": c.compose_project,
                    "compose_dir": c.compose_dir,
                    "root_files": c.root_files,
                    "image": c.image,
                    "status": c.status,
                    "has_volumes": c.has_volumes,
                    "compose_dir_accessible": c.compose_dir_accessible,
                    "named_volumes": c.named_volumes,
                }
                for c in containers
            ],
        }
        resp = self._request("POST", "/api/v1/agents/heartbeat", json=body)
        if resp is not None and resp.status_code == 200:
            self._apply_backup_config(resp.json())
            return True
        return False

    def poll_jobs(self) -> list[Job]:
        resp = self._request("GET", "/api/v1/jobs/pending")
        if resp is None or resp.status_code != 200:
            return []
        data = resp.json()
        return [
            Job(
                job_id=j["job_id"],
                job_type=JobType(j["job_type"]),
                containers=j.get("containers"),
                params=j.get("params", {}),
            )
            for j in data.get("jobs", [])
        ]

    def report_job(self, job_id: int, status: str, result: dict | None = None, logs: list[LogEntry] | None = None):
        body: dict = {"status": status}
        if result:
            body["result"] = result
        if logs:
            body["logs"] = [
                {"level": l.level, "message": l.message, "timestamp": l.timestamp}
                for l in logs
            ]
        self._request("PUT", f"/api/v1/jobs/{job_id}/status", json=body)

    def close(self):
        self._http.close()
