from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, Job, JobLog
from ..routers.agents import get_current_agent
from ..schemas import CreateJobRequest, JobStatusUpdate, PendingJobsResponse, JobPayload

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("/pending", response_model=PendingJobsResponse)
def get_pending_jobs(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    jobs = (
        db.query(Job)
        .filter(Job.agent_id == agent.id, Job.status == "pending")
        .order_by(Job.created_at)
        .all()
    )

    result = []
    for j in jobs:
        containers = json.loads(j.containers) if j.containers else None
        params = json.loads(j.params) if j.params else {}
        result.append(JobPayload(
            job_id=j.id,
            job_type=j.job_type,
            containers=containers,
            params=params,
        ))

    return PendingJobsResponse(jobs=result)


@router.put("/{job_id}/status")
def update_job_status(
    job_id: int,
    update: JobStatusUpdate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.agent_id == agent.id).first()
    if not job:
        raise HTTPException(404, "Job not found")

    if job.status == "cancelled" and update.status != "cancelled":
        for log in update.logs:
            ts = datetime.fromisoformat(log.timestamp) if log.timestamp else datetime.utcnow()
            db.add(JobLog(job_id=job.id, level=log.level, message=log.message, timestamp=ts))
        db.commit()
        return {"ack": True, "note": "job was cancelled — status not overwritten"}

    job.status = update.status
    if update.status == "running":
        job.started_at = datetime.utcnow()
    elif update.status in ("success", "failed", "cancelled"):
        job.completed_at = datetime.utcnow()

    if update.result:
        job.result = json.dumps(update.result)

    for log in update.logs:
        ts = datetime.fromisoformat(log.timestamp) if log.timestamp else datetime.utcnow()
        db.add(JobLog(job_id=job.id, level=log.level, message=log.message, timestamp=ts))

    db.commit()
    return {"ack": True}


@router.post("", status_code=201)
def create_job(
    req: CreateJobRequest,
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == req.agent_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")

    job = Job(
        agent_id=req.agent_id,
        job_type=req.job_type,
        containers=json.dumps(req.containers) if req.containers else None,
        params=json.dumps(req.params) if req.params else "{}",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"job_id": job.id}


@router.get("")
def list_jobs(
    agent_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(Job).order_by(Job.created_at.desc())
    if agent_id:
        q = q.filter(Job.agent_id == agent_id)
    if status:
        q = q.filter(Job.status == status)
    jobs = q.limit(limit).all()

    return [
        {
            "id": j.id,
            "agent_id": j.agent_id,
            "job_type": j.job_type,
            "status": j.status,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            "result": json.loads(j.result) if j.result else None,
        }
        for j in jobs
    ]


@router.get("/{job_id}/logs")
def get_job_logs(job_id: int, db: Session = Depends(get_db)):
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.timestamp).all()
    return [
        {"level": l.level, "message": l.message, "timestamp": l.timestamp.isoformat()}
        for l in logs
    ]
