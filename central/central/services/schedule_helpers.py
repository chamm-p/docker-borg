from __future__ import annotations

WEEKDAYS = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag",
]


def cron_for(kind: str, hour: int, minute: int, weekday: int = 0, day_of_month: int = 1) -> str:
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    if kind == "hourly":
        return f"{minute} * * * *"
    if kind == "daily":
        return f"{minute} {hour} * * *"
    if kind == "weekly":
        wd = max(0, min(6, weekday))
        cron_wd = (wd + 1) % 7
        return f"{minute} {hour} * * {cron_wd}"
    if kind == "monthly":
        dom = max(1, min(28, day_of_month))
        return f"{minute} {hour} {dom} * *"
    return f"{minute} {hour} * * *"


def human_for(schedule) -> str:
    if schedule is None:
        return "—"
    kind = schedule.schedule_kind
    h, m = schedule.hour, schedule.minute
    time = f"{h:02d}:{m:02d}"
    if kind == "hourly":
        return f"Stündlich um Minute {m:02d}"
    if kind == "daily":
        return f"Täglich um {time}"
    if kind == "weekly":
        day = WEEKDAYS[schedule.weekday or 0]
        return f"Wöchentlich am {day} um {time}"
    if kind == "monthly":
        return f"Monatlich am {schedule.day_of_month}. um {time}"
    return schedule.cron_expr
