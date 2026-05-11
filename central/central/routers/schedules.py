from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Schedule

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


@router.get("")
def list_schedules(db: Session = Depends(get_db)):
    schedules = db.query(Schedule).all()
    return [
        {
            "id": s.id,
            "agent_id": s.agent_id,
            "name": s.name,
            "schedule_kind": s.schedule_kind,
            "cron_expr": s.cron_expr,
            "enabled": s.enabled,
            "prune_after": s.prune_after,
            "keep_daily": s.keep_daily,
            "keep_weekly": s.keep_weekly,
            "keep_monthly": s.keep_monthly,
        }
        for s in schedules
    ]
