from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Schedule
from ..schemas import ScheduleCreate, ScheduleUpdate

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


@router.get("")
def list_schedules(db: Session = Depends(get_db)):
    schedules = db.query(Schedule).all()
    return [
        {
            "id": s.id,
            "agent_id": s.agent_id,
            "cron_expr": s.cron_expr,
            "enabled": s.enabled,
            "prune_after": s.prune_after,
            "keep_daily": s.keep_daily,
            "keep_weekly": s.keep_weekly,
            "keep_monthly": s.keep_monthly,
        }
        for s in schedules
    ]


@router.post("", status_code=201)
def create_schedule(req: ScheduleCreate, db: Session = Depends(get_db)):
    schedule = Schedule(
        agent_id=req.agent_id,
        cron_expr=req.cron_expr,
        enabled=req.enabled,
        prune_after=req.prune_after,
        keep_daily=req.keep_daily,
        keep_weekly=req.keep_weekly,
        keep_monthly=req.keep_monthly,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return {"id": schedule.id}


@router.put("/{schedule_id}")
def update_schedule(schedule_id: int, req: ScheduleUpdate, db: Session = Depends(get_db)):
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(404, "Schedule not found")

    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)

    db.commit()
    return {"ack": True}


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.query(Schedule).filter(Schedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(404, "Schedule not found")
    db.delete(schedule)
    db.commit()
    return {"ack": True}
