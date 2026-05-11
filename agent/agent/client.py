from __future__ import annotations

import json
import logging
import socket

import httpx

from .config import settings
from .models import ContainerInfo, Job, JobType, LogEntry

logger = logging.getLogger(__name__)


class CentralClient:
    def __init__(self):
        self._token: str | None = None
        self._load_token()
        self._http = httpx.Client(base_url=settings.central_url, timeout=30)

    def _load_token(self):
        if settings.token_file.exists():
            self._token = settings.token_file.read_text().strip()
            logger.info("Loaded agent token from %s", settings.token_file)

    def _save_token(self, token: str):
        self._token = token
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.token_file.write_text(token)
        logger.info("Saved agent token to %s", settings.token_file)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @property
    def is_registered(self) -> bool:
        return self._token is not None

    def _apply_borg_config(self, data: dict):
        repo = data.get("borg_repo", "")
        passphrase = data.get("borg_passphrase", "")
        if repo:
            settings.borg_repo = repo
            logger.info("Borg repo set from central: %s", repo)
        if passphrase:
            settings.borg_passphrase = passphrase

    def register(self) -> bool:
        hostname = settings.agent_name or socket.gethostname()
        try:
            resp = self._http.post(
                "/api/v1/agents/register",
                json={
                    "hostname": hostname,
                    "agent_version": "0.1.0",
                    "token": settings.registration_token,
                },
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 201:
                data = resp.json()
                self._save_token(data["agent_token"])
                self._apply_borg_config(data)
                logger.info("Registered with central as '%s'", hostname)
                return True
            logger.error("Registration failed: %s %s", resp.status_code, resp.text)
            return False
        except httpx.RequestError as e:
            logger.warning("Cannot reach central for registration: %s", e)
            return False

    def heartbeat(self, containers: list[ContainerInfo]) -> bool:
        try:
            resp = self._http.post(
                "/api/v1/agents/heartbeat",
                json={
                    "hostname": settings.agent_name or socket.gethostname(),
                    "containers": [
                        {
                            "container_id": c.container_id,
                            "container_name": c.container_name,
                            "compose_project": c.compose_project,
                            "compose_dir": c.compose_dir,
                            "root_files": c.root_files,
                            "image": c.image,
                            "status": c.status,
                        }
                        for c in containers
                    ],
                },
                headers=self._headers(),
            )
            if resp.status_code == 200:
                self._apply_borg_config(resp.json())
                return True
            return False
        except httpx.RequestError as e:
            logger.warning("Heartbeat failed: %s", e)
            return False

    def poll_jobs(self) -> list[Job]:
        try:
            resp = self._http.get("/api/v1/jobs/pending", headers=self._headers())
            if resp.status_code != 200:
                return []
            data = resp.json()
            jobs = []
            for j in data.get("jobs", []):
                jobs.append(Job(
                    job_id=j["job_id"],
                    job_type=JobType(j["job_type"]),
                    containers=j.get("containers"),
                    params=j.get("params", {}),
                ))
            return jobs
        except httpx.RequestError as e:
            logger.warning("Job poll failed: %s", e)
            return []

    def report_job(self, job_id: int, status: str, result: dict | None = None, logs: list[LogEntry] | None = None):
        try:
            body: dict = {"status": status}
            if result:
                body["result"] = result
            if logs:
                body["logs"] = [
                    {"level": l.level, "message": l.message, "timestamp": l.timestamp}
                    for l in logs
                ]
            self._http.put(
                f"/api/v1/jobs/{job_id}/status",
                json=body,
                headers=self._headers(),
            )
        except httpx.RequestError as e:
            logger.warning("Job report failed: %s", e)

    def close(self):
        self._http.close()
