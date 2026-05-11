from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from croniter import croniter
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Agent, Container, Job, Schedule
from ..config import settings

logger = logging.getLogger(__name__)


def _should_run(cron_expr: str, last_check: datetime, now: datetime) -> bool:
    cron = croniter(cron_expr, last_check)
    next_run = cron.get_next(datetime)
    return next_run <= now


def _create_scheduled_jobs(db: Session):
    now = datetime.utcnow()
    schedules = db.query(Schedule).filter(Schedule.enabled == True).all()  # noqa: E712

    for schedule in schedules:
        last_job = (
            db.query(Job)
            .filter(Job.schedule_id == schedule.id)
            .order_by(Job.created_at.desc())
            .first()
        )
        last_check = last_job.created_at if last_job else schedule.created_at

        if not _should_run(schedule.cron_expr, last_check, now):
            continue

        if schedule.agent_id:
            agents = [db.query(Agent).filter(Agent.id == schedule.agent_id).first()]
        else:
            agents = db.query(Agent).filter(Agent.status == "online").all()

        for agent in agents:
            if not agent:
                continue

            pending = (
                db.query(Job)
                .filter(
                    Job.agent_id == agent.id,
                    Job.schedule_id == schedule.id,
                    Job.status == "pending",
                )
                .first()
            )
            if pending:
                continue

            enabled_containers = (
                db.query(Container)
                .filter(Container.agent_id == agent.id, Container.backup_enabled == True)  # noqa: E712
                .all()
            )
            projects = sorted({c.compose_project for c in enabled_containers if c.compose_project})
            overrides = {c.compose_project: c.manual_compose_dir for c in enabled_containers if c.manual_compose_dir}
            params = {"compose_dirs": overrides} if overrides else {}
            job = Job(
                agent_id=agent.id,
                schedule_id=schedule.id,
                job_type="backup",
                containers=json.dumps(projects) if projects else None,
                params=json.dumps(params) if params else "{}",
            )
            db.add(job)
            logger.info("Scheduled backup job for agent %s (schedule %d)", agent.hostname, schedule.id)

            if schedule.prune_after:
                prune_params = json.dumps({
                    "keep": {
                        "daily": schedule.keep_daily,
                        "weekly": schedule.keep_weekly,
                        "monthly": schedule.keep_monthly,
                    }
                })
                prune_job = Job(
                    agent_id=agent.id,
                    schedule_id=schedule.id,
                    job_type="prune",
                    params=prune_params,
                )
                db.add(prune_job)

    db.commit()


def _update_agent_status(db: Session):
    now = datetime.utcnow()
    agents = db.query(Agent).filter(Agent.status == "online").all()
    for agent in agents:
        if agent.last_heartbeat:
            delta = (now - agent.last_heartbeat).total_seconds()
            if delta > settings.agent_offline_seconds:
                agent.status = "offline"
                logger.warning("Agent %s marked offline (last heartbeat %ds ago)", agent.hostname, int(delta))
    db.commit()


async def scheduler_loop():
    logger.info("Scheduler started")
    while True:
        try:
            db = SessionLocal()
            try:
                _create_scheduled_jobs(db)
                _update_agent_status(db)
            finally:
                db.close()
        except Exception:
            logger.exception("Scheduler error")

        await asyncio.sleep(60)
