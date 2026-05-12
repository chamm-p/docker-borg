from __future__ import annotations

import json
import secrets
from datetime import datetime
from hashlib import sha256

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Agent, Container
from ..schemas import AgentRegisterRequest, AgentRegisterResponse, BackupConfig, HeartbeatRequest, HeartbeatResponse


router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


def _backup_config(agent: Agent) -> BackupConfig:
    return BackupConfig(
        backup_type=agent.backup_type or "scp",
        borg_repo=agent.borg_repo or "",
        borg_passphrase=agent.borg_passphrase or "",
        scp_host=agent.scp_host or "",
        scp_user=agent.scp_user or "",
        scp_path=agent.scp_path or "",
        scp_port=agent.scp_port or 22,
        local_path=agent.local_path or "",
        webdav_url=agent.webdav_url or "",
        webdav_user=agent.webdav_user or "",
        webdav_password=agent.webdav_password or "",
        webdav_verify_ssl=bool(agent.webdav_verify_ssl),
    )


def _hash_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def get_current_agent(
    authorization: str = Header(None),
    db: Session = Depends(get_db),
) -> Agent:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid authorization header")
    token = authorization[7:]
    token_hash = _hash_token(token)
    agent = db.query(Agent).filter(Agent.token_hash == token_hash).first()
    if not agent:
        raise HTTPException(401, "Invalid agent token")
    agent.last_heartbeat = datetime.utcnow()
    agent.status = "online"
    db.commit()
    return agent


@router.post("/register", status_code=201, response_model=AgentRegisterResponse)
def register(req: AgentRegisterRequest, db: Session = Depends(get_db)):
    if req.token != settings.registration_token:
        raise HTTPException(403, "Invalid registration token")

    existing = db.query(Agent).filter(Agent.hostname == req.hostname).first()
    if existing:
        agent_token = secrets.token_urlsafe(32)
        existing.token_hash = _hash_token(agent_token)
        existing.agent_version = req.agent_version
        existing.status = "online"
        existing.last_heartbeat = datetime.utcnow()
        db.commit()
        return AgentRegisterResponse(
            agent_id=existing.id,
            agent_token=agent_token,
            backup=_backup_config(existing),
        )

    agent_token = secrets.token_urlsafe(32)
    agent = Agent(
        hostname=req.hostname,
        token_hash=_hash_token(agent_token),
        agent_version=req.agent_version,
        status="online",
        last_heartbeat=datetime.utcnow(),
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return AgentRegisterResponse(
        agent_id=agent.id,
        agent_token=agent_token,
        backup=_backup_config(agent),
    )


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    req: HeartbeatRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    agent.last_heartbeat = datetime.utcnow()
    agent.status = "online"
    if req.agent_version:
        agent.agent_version = req.agent_version
    if req.ssh_public_key:
        agent.ssh_public_key = req.ssh_public_key

    existing = {c.compose_project: c for c in db.query(Container).filter(Container.agent_id == agent.id).all()}
    seen: set[str] = set()

    for c in req.containers:
        seen.add(c.compose_project)
        row = existing.get(c.compose_project)
        if row:
            row.container_id = c.container_id
            row.container_name = c.container_name
            if c.compose_dir or not row.manual_compose_dir:
                row.compose_dir = c.compose_dir
            row.root_files = json.dumps(c.root_files)
            row.image = c.image
            row.status = c.status
            row.has_volumes = c.has_volumes
            row.compose_dir_accessible = c.compose_dir_accessible
            row.backup_mounts = json.dumps(c.backup_mounts or [])
        else:
            db.add(Container(
                agent_id=agent.id,
                container_id=c.container_id,
                container_name=c.container_name,
                compose_project=c.compose_project,
                compose_dir=c.compose_dir,
                root_files=json.dumps(c.root_files),
                image=c.image,
                status=c.status,
                has_volumes=c.has_volumes,
                compose_dir_accessible=c.compose_dir_accessible,
                backup_mounts=json.dumps(c.backup_mounts or []),
                backup_enabled=True,
            ))

    for project, row in existing.items():
        if project not in seen:
            db.delete(row)

    db.commit()

    manual_paths = {
        row.compose_project: row.manual_compose_dir
        for row in db.query(Container).filter(Container.agent_id == agent.id).all()
        if row.manual_compose_dir
    }
    from ..models import Job
    cancelled_jobs = [
        j.id for j in db.query(Job).filter(
            Job.agent_id == agent.id, Job.status == "cancelled"
        ).all()
    ]
    return HeartbeatResponse(
        backup=_backup_config(agent),
        manual_paths=manual_paths,
        cancelled_jobs=cancelled_jobs,
    )
