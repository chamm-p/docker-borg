from __future__ import annotations

from ..config import settings
from ..database import SessionLocal
from ..models import AppSetting

ADMIN_PASSWORD_KEY = "admin_password"


def get_admin_password() -> str:
    with SessionLocal() as db:
        row = db.get(AppSetting, ADMIN_PASSWORD_KEY)
        if row and row.value:
            return row.value
    return settings.admin_password


def set_admin_password(new_password: str) -> None:
    with SessionLocal() as db:
        row = db.get(AppSetting, ADMIN_PASSWORD_KEY)
        if row:
            row.value = new_password
        else:
            db.add(AppSetting(key=ADMIN_PASSWORD_KEY, value=new_password))
        db.commit()
