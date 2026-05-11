from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _tz() -> ZoneInfo:
    tz_name = os.environ.get("TZ", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def localtime(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_tz()).strftime(fmt)


def localdate(dt: datetime | None) -> str:
    return localtime(dt, "%d.%m.%Y")


def localtime_short(dt: datetime | None) -> str:
    return localtime(dt, "%d.%m. %H:%M")


def relative(dt: datetime | None) -> str:
    if dt is None:
        return "nie"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"vor {secs}s"
    if secs < 3600:
        return f"vor {secs // 60} min"
    if secs < 86400:
        return f"vor {secs // 3600} h"
    return f"vor {secs // 86400} Tagen"
