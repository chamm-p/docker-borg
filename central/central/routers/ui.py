from __future__ import annotations

import json
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, Container, Job, JobLog, Schedule

from ..version import APP_VERSION

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="central/templates")
templates.env.globals["app_version"] = APP_VERSION


def _agent_status_class(agent: Agent, last_job: Job | None = None) -> str:
    if agent.status != "online":
        return "danger"
    if last_job and last_job.status == "failed":
        return "danger"
    return "success"


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    agents = db.query(Agent).all()
    recent_jobs = db.query(Job).order_by(Job.created_at.desc()).limit(10).all()

    agent_data = []
    for a in agents:
        containers = db.query(Container).filter(Container.agent_id == a.id).all()
        last_job = db.query(Job).filter(Job.agent_id == a.id).order_by(Job.created_at.desc()).first()
        last_success = (
            db.query(Job)
            .filter(Job.agent_id == a.id, Job.job_type == "backup", Job.status == "success")
            .order_by(Job.completed_at.desc())
            .first()
        )
        backupable_count = sum(1 for c in containers if c.compose_dir and c.root_files and c.root_files != "[]")
        agent_data.append({
            "agent": a,
            "containers": containers,
            "container_count": len(containers),
            "backupable_count": backupable_count,
            "last_job": last_job,
            "last_success": last_success,
            "status_class": _agent_status_class(a, last_job),
        })

    return templates.TemplateResponse(request, "dashboard.html", {
        "agents": agent_data,
        "recent_jobs": recent_jobs,
        "now": datetime.utcnow(),
    })


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
def agent_detail(agent_id: int, request: Request, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent not found", status_code=404)

    containers = db.query(Container).filter(Container.agent_id == agent_id).all()
    for c in containers:
        c._root_files_list = json.loads(c.root_files) if c.root_files else []

    jobs = db.query(Job).filter(Job.agent_id == agent_id).order_by(Job.created_at.desc()).limit(20).all()
    schedules = db.query(Schedule).filter(
        (Schedule.agent_id == agent_id) | (Schedule.agent_id == None)  # noqa: E711
    ).all()

    return templates.TemplateResponse(request, "agent_detail.html", {
        "agent": agent,
        "containers": containers,
        "jobs": jobs,
        "schedules": schedules,
        "status_class": _agent_status_class(agent),
    })


@router.post("/agents/{agent_id}/settings")
def update_agent_settings(
    agent_id: int,
    backup_type: str = Form("ssh"),
    borg_repo: str = Form(""),
    borg_passphrase: str = Form(""),
    webdav_url: str = Form(""),
    webdav_user: str = Form(""),
    webdav_password: str = Form(""),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent not found", status_code=404)
    agent.backup_type = backup_type
    if backup_type == "webdav":
        agent.webdav_url = webdav_url
        agent.webdav_user = webdav_user
        if webdav_password and webdav_password != "********":
            agent.webdav_password = webdav_password
        agent.borg_repo = "/mnt/webdav/borg"
    else:
        agent.borg_repo = borg_repo
    if borg_passphrase and borg_passphrase != "********":
        agent.borg_passphrase = borg_passphrase
    elif not agent.borg_passphrase:
        agent.borg_passphrase = secrets.token_urlsafe(32)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}", status_code=303)


@router.get("/agents/{agent_id}/passphrase")
def reveal_passphrase(agent_id: int, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent or not agent.borg_passphrase:
        return PlainTextResponse("(keine Passphrase gesetzt)", status_code=404)
    return PlainTextResponse(agent.borg_passphrase)


@router.get("/agents/{agent_id}/passphrase/download")
def download_passphrase(agent_id: int, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent or not agent.borg_passphrase:
        return PlainTextResponse("(keine Passphrase gesetzt)", status_code=404)
    filename = f"borg-passphrase-{agent.hostname}.txt"
    return PlainTextResponse(
        agent.borg_passphrase,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/agents/{agent_id}/backup")
def trigger_backup(agent_id: int, db: Session = Depends(get_db)):
    job = Job(agent_id=agent_id, job_type="backup")
    db.add(job)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}", status_code=303)


@router.post("/agents/{agent_id}/restore")
def trigger_restore(
    agent_id: int,
    archive: str = Form(...),
    db: Session = Depends(get_db),
):
    job = Job(
        agent_id=agent_id,
        job_type="restore",
        params=json.dumps({"archive": archive, "target_dir": "/tmp/restore"}),
    )
    db.add(job)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, db: Session = Depends(get_db)):
    jobs = db.query(Job).order_by(Job.created_at.desc()).limit(100).all()
    agents = {a.id: a for a in db.query(Agent).all()}

    job_data = []
    for j in jobs:
        job_data.append({
            "job": j,
            "agent": agents.get(j.agent_id),
            "result": json.loads(j.result) if j.result else None,
        })

    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": job_data,
    })


@router.get("/jobs/{job_id}/logs", response_class=HTMLResponse)
def job_logs_page(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.timestamp).all()
    return templates.TemplateResponse(request, "job_logs.html", {
        "job": job,
        "logs": logs,
    })


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request, db: Session = Depends(get_db)):
    schedules = db.query(Schedule).all()
    agents = {a.id: a for a in db.query(Agent).all()}
    return templates.TemplateResponse(request, "schedules.html", {
        "schedules": schedules,
        "agents": agents,
    })


@router.post("/schedules")
def create_schedule_ui(
    request: Request,
    agent_id: str = Form(""),
    cron_expr: str = Form("0 3 * * *"),
    prune_after: bool = Form(False),
    keep_daily: int = Form(7),
    keep_weekly: int = Form(4),
    keep_monthly: int = Form(6),
    db: Session = Depends(get_db),
):
    schedule = Schedule(
        agent_id=int(agent_id) if agent_id else None,
        cron_expr=cron_expr,
        enabled=True,
        prune_after=prune_after,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
    )
    db.add(schedule)
    db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule_ui(schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if schedule:
        db.delete(schedule)
        db.commit()
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule_ui(schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if schedule:
        schedule.enabled = not schedule.enabled
        db.commit()
    return RedirectResponse("/schedules", status_code=303)
