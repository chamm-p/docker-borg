from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, Container, DatabaseHook, Job, JobLog, Schedule
from ..services.admin import get_admin_password, set_admin_password
from ..services.backup_params import build_backup_params, build_borg_repo
from ..services.connection_check import check_connection, record_result
from ..services.format import localtime, localtime_short, relative
from ..services.schedule_helpers import cron_for, human_for
from ..version import APP_VERSION

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="central/templates")
templates.env.globals["app_version"] = APP_VERSION
templates.env.filters["localtime"] = localtime
templates.env.filters["localtime_short"] = localtime_short
templates.env.filters["relative"] = relative
templates.env.filters["human_schedule"] = human_for


def _bytesize(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
    return f"{n:.1f} PB"


templates.env.filters["bytesize"] = _bytesize


_RELEVANT_FOR_HEALTH = ("backup", "verify", "archive_list")


def _agent_traffic_light(agent: Agent, last_relevant_job: Job | None) -> str:
    if agent.status != "online":
        return "red"
    # Nur "Backup-relevante" Jobs zählen — alte SCP_INSTALL_KEY-Failures sollen
    # die Ampel nicht rot/gelb halten.
    if last_relevant_job and last_relevant_job.status == "failed":
        # Und nur recent (< 24h) — ein alter Failure von gestern, der durch
        # erfolgreiches Backup heute überholt wäre, soll auch nicht stören.
        age = (datetime.utcnow() - (last_relevant_job.completed_at or last_relevant_job.created_at)).total_seconds()
        if age < 86400:
            return "yellow"
    if not agent.borg_repo and agent.backup_type not in ("local", "scp"):
        return "yellow"
    return "green"


def _last_relevant_job(db: Session, agent_id: int) -> "Job | None":
    return (
        db.query(Job)
        .filter(Job.agent_id == agent_id, Job.job_type.in_(_RELEVANT_FOR_HEALTH))
        .order_by(Job.created_at.desc())
        .first()
    )


def _last_successful_relevant_job(db: Session, agent_id: int) -> "Job | None":
    return (
        db.query(Job)
        .filter(Job.agent_id == agent_id,
                Job.job_type.in_(_RELEVANT_FOR_HEALTH),
                Job.status == "success")
        .order_by(Job.created_at.desc())
        .first()
    )


def _connection_error_active(agent: Agent, last_success: "Job | None") -> bool:
    """Connection-Fehler nur anzeigen wenn KEIN neueres Erfolg-Backup existiert.

    Sonst bleibt eine alte SCP-Timeout-Meldung ewig stehen, obwohl der
    Zustand längst wieder ok ist (Backup läuft sauber gegen das Ziel).
    """
    if agent.last_connection_ok or not agent.last_connection_error:
        return False
    if not agent.last_connection_check:
        return True
    if last_success and last_success.completed_at and last_success.completed_at > agent.last_connection_check:
        return False
    return True


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    agents = db.query(Agent).order_by(Agent.hostname).all()
    cards: list[dict[str, Any]] = []
    for a in agents:
        containers = db.query(Container).filter(Container.agent_id == a.id).all()
        backupable = [c for c in containers if c.compose_dir]
        enabled = [c for c in backupable if c.backup_enabled]
        last_job = db.query(Job).filter(Job.agent_id == a.id).order_by(Job.created_at.desc()).first()
        last_backup = (
            db.query(Job)
            .filter(Job.agent_id == a.id, Job.job_type == "backup",
                    Job.status.in_(("success", "failed", "cancelled")))
            .order_by(Job.completed_at.desc())
            .first()
        )
        last_success = (
            db.query(Job)
            .filter(Job.agent_id == a.id, Job.job_type == "backup", Job.status == "success")
            .order_by(Job.completed_at.desc())
            .first()
        )
        running_job = (
            db.query(Job)
            .filter(Job.agent_id == a.id, Job.status.in_(("pending", "running")))
            .order_by(Job.created_at.desc())
            .first()
        )
        cards.append({
            "agent": a,
            "container_count": len(containers),
            "backupable_count": len(backupable),
            "enabled_count": len(enabled),
            "last_job": last_job,
            "last_backup": last_backup,
            "last_success": last_success,
            "running_job": running_job,
            "traffic_light": _agent_traffic_light(a, _last_relevant_job(db, a.id)),
            "connection_error_active": _connection_error_active(a, _last_successful_relevant_job(db, a.id)),
        })
    return templates.TemplateResponse(request, "dashboard.html", {"cards": cards})


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
def agent_detail(
    agent_id: int,
    request: Request,
    tab: str = "overview",
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)

    containers = db.query(Container).filter(Container.agent_id == agent_id).order_by(Container.compose_project).all()
    for c in containers:
        c._files = json.loads(c.root_files) if c.root_files else []
        try:
            c._entries = json.loads(c.top_level_entries) if c.top_level_entries else []
        except (json.JSONDecodeError, TypeError):
            c._entries = []
        try:
            c._excluded_entries = set(json.loads(c.excluded_entries)) if c.excluded_entries else set()
        except (json.JSONDecodeError, TypeError):
            c._excluded_entries = set()
        try:
            c._backup_mounts = json.loads(c.backup_mounts) if c.backup_mounts else []
        except (json.JSONDecodeError, TypeError):
            c._backup_mounts = []
        try:
            c._excluded_mounts = set(json.loads(c.excluded_mounts)) if c.excluded_mounts else set()
        except (json.JSONDecodeError, TypeError):
            c._excluded_mounts = set()
        try:
            c._db_candidates = json.loads(c.db_candidates) if c.db_candidates else []
        except (json.JSONDecodeError, TypeError):
            c._db_candidates = []
        try:
            c._db_dismissed = set(json.loads(c.db_candidates_dismissed) if c.db_candidates_dismissed else [])
        except (json.JSONDecodeError, TypeError):
            c._db_dismissed = set()
        c._sicherbar = bool(c.compose_dir and (c._entries or c._backup_mounts))
        c._db_hooks = db.query(DatabaseHook).filter(DatabaseHook.container_id == c.id).all()
        # Größenschätzung (unkomprimiert): enthaltene Compose-Inhalte + enthaltene
        # externe Mounts, minus Excludes. DB-Raw-Dirs die per aktivem Hook
        # auto-exkludiert werden, hier auch abziehen (dump-only).
        _auto_excl_entries: set = set()
        _auto_excl_mounts: set = set()
        if c._db_hooks:
            _active = {f"{h.db_type}:{h.hostname}:{h.db_name}" for h in c._db_hooks if h.enabled}
            for cand in c._db_candidates:
                key = f"{cand.get('db_type','')}:{cand.get('hostname','')}:{cand.get('db_name','')}"
                raw = cand.get("raw_exclude")
                if key in _active and raw:
                    if raw.get("kind") == "entry":
                        _auto_excl_entries.add(raw.get("value"))
                    elif raw.get("kind") == "mount":
                        _auto_excl_mounts.add(raw.get("value"))
        est = 0
        for e in c._entries:
            if e.get("name") in c._excluded_entries or e.get("name") in _auto_excl_entries:
                continue
            est += int(e.get("size_bytes") or 0)
        for m in c._backup_mounts:
            if m.get("dest") in c._excluded_mounts or m.get("dest") in _auto_excl_mounts:
                continue
            est += int(m.get("size_bytes") or 0)
        c._est_bytes = est
        c._has_db_dump = bool(c._db_hooks)

    jobs = db.query(Job).filter(Job.agent_id == agent_id).order_by(Job.created_at.desc()).limit(50).all()
    schedules = db.query(Schedule).filter(Schedule.agent_id == agent_id).all()
    last_job = jobs[0] if jobs else None
    last_verify = (
        db.query(Job)
        .filter(Job.agent_id == agent_id, Job.job_type == "verify", Job.completed_at.isnot(None))
        .order_by(Job.completed_at.desc())
        .first()
    )

    try:
        archives = json.loads(agent.cached_archives) if agent.cached_archives else []
    except (json.JSONDecodeError, TypeError):
        archives = []
    archives.sort(key=lambda a: a.get("start", ""), reverse=True)

    projects_with_hooks = {
        c.compose_project for c in containers
        if any(h.enabled for h in (c._db_hooks or []))
    }

    est_total = sum(c._est_bytes for c in containers if c.backup_enabled)
    est_has_db = any(c._has_db_dump for c in containers if c.backup_enabled)

    last_relevant = _last_relevant_job(db, agent_id)
    last_success_relevant = _last_successful_relevant_job(db, agent_id)
    # last_verify nur wenn neuer als 30 Tage — sonst nicht "current state"
    if last_verify and last_verify.completed_at:
        age_days = (datetime.utcnow() - last_verify.completed_at).days
        if age_days > 30:
            last_verify = None

    return templates.TemplateResponse(request, "agent_detail.html", {
        "agent": agent,
        "containers": containers,
        "jobs": jobs,
        "schedules": schedules,
        "tab": tab,
        "traffic_light": _agent_traffic_light(agent, last_relevant),
        "last_verify": last_verify,
        "archives": archives,
        "archives_at": agent.cached_archives_at,
        "projects_with_hooks": projects_with_hooks,
        "connection_error_active": _connection_error_active(agent, last_success_relevant),
        "est_total": est_total,
        "est_has_db": est_has_db,
        "effective_repo": build_borg_repo(agent),
    })


def _apply_target_form(agent, backup_type, scp_host, scp_user, scp_path, scp_port,
                       local_path):
    prev_repo = agent.borg_repo or ""
    agent.backup_type = backup_type
    if backup_type == "scp":
        agent.scp_host = scp_host
        agent.scp_user = scp_user
        agent.scp_path = scp_path
        agent.scp_port = scp_port
        agent.borg_repo = build_borg_repo(agent)
    elif backup_type == "local":
        agent.local_path = local_path
        agent.borg_repo = build_borg_repo(agent)
    # Ziel-Konfig geändert → alter Connection-Fehler ist nicht mehr current state
    if (agent.borg_repo or "") != prev_repo:
        agent.last_connection_ok = None
        agent.last_connection_error = None
        agent.last_connection_check = None


@router.post("/agents/{agent_id}/target")
def update_target(
    agent_id: int,
    backup_type: str = Form("scp"),
    scp_host: str = Form(""),
    scp_user: str = Form(""),
    scp_path: str = Form(""),
    scp_port: int = Form(22),
    local_path: str = Form(""),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    _apply_target_form(agent, backup_type, scp_host, scp_user, scp_path, scp_port,
                       local_path)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=target", status_code=303)


@router.post("/agents/{agent_id}/retention")
def update_retention(
    agent_id: int,
    retention_mode: str = Form("simple"),
    keep_last: int = Form(7),
    keep_daily: int = Form(7),
    keep_weekly: int = Form(4),
    keep_monthly: int = Form(6),
    prune_enabled: bool = Form(False),
    worker_mem_mb: int = Form(1024),
    worker_cpus: str = Form(""),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    agent.retention_mode = retention_mode if retention_mode in ("simple", "advanced") else "simple"
    agent.keep_last = max(0, keep_last)
    agent.keep_daily = max(0, keep_daily)
    agent.keep_weekly = max(0, keep_weekly)
    agent.keep_monthly = max(0, keep_monthly)
    agent.prune_enabled = prune_enabled
    agent.worker_mem_mb = max(256, worker_mem_mb)
    # worker_cpus: leer oder positive Zahl als String
    wc = (worker_cpus or "").strip()
    try:
        agent.worker_cpus = wc if (wc and float(wc) > 0) else ""
    except ValueError:
        agent.worker_cpus = ""
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=schedule", status_code=303)


@router.post("/agents/{agent_id}/scp/test")
def scp_test(agent_id: int, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    job = Job(
        agent_id=agent_id,
        job_type="scp_test",
        params=json.dumps({
            "host": agent.scp_host or "",
            "user": agent.scp_user or "",
            "port": agent.scp_port or 22,
        }),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/agents/{agent_id}/scp/install-key")
def scp_install_key(
    agent_id: int,
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    if not password:
        return RedirectResponse(f"/agents/{agent_id}?tab=target&error=password_required", status_code=303)
    job = Job(
        agent_id=agent_id,
        job_type="scp_install_key",
        params=json.dumps({
            "host": agent.scp_host or "",
            "user": agent.scp_user or "",
            "port": agent.scp_port or 22,
            "password": password,
        }),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/agents/{agent_id}/target/check")
def check_target(
    agent_id: int,
    backup_type: str = Form("scp"),
    scp_host: str = Form(""),
    scp_user: str = Form(""),
    scp_path: str = Form(""),
    scp_port: int = Form(22),
    local_path: str = Form(""),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    _apply_target_form(agent, backup_type, scp_host, scp_user, scp_path, scp_port,
                       local_path)
    db.commit()

    # Für SCP delegieren: der Test muss vom Agent aus laufen (nur er sieht das
    # Netz wo der SSH-Server steht; Central kann auf einem völlig anderen
    # Netz hängen). Wir erzeugen denselben scp_test-Job wie der
    # "SSH-Verbindung testen"-Button und springen direkt aufs Live-Log.
    if backup_type == "scp":
        job = Job(
            agent_id=agent_id,
            job_type="scp_test",
            params=json.dumps({
                "host": agent.scp_host or "",
                "user": agent.scp_user or "",
                "port": agent.scp_port or 22,
            }),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    ok, msg = check_connection(agent)
    record_result(agent, ok, msg)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=target", status_code=303)


@router.post("/agents/{agent_id}/encryption")
def update_encryption(
    agent_id: int,
    borg_passphrase: str = Form(""),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    if borg_passphrase and borg_passphrase != "********":
        agent.borg_passphrase = borg_passphrase
    elif not agent.borg_passphrase:
        agent.borg_passphrase = secrets.token_urlsafe(32)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=encryption", status_code=303)


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


@router.post("/agents/{agent_id}/containers/{container_id}/path")
def set_manual_path(
    agent_id: int,
    container_id: int,
    manual_compose_dir: str = Form(""),
    db: Session = Depends(get_db),
):
    row = db.query(Container).filter(Container.id == container_id, Container.agent_id == agent_id).first()
    if not row:
        return HTMLResponse("Container nicht gefunden", status_code=404)
    row.manual_compose_dir = manual_compose_dir.strip() or None
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=containers", status_code=303)


@router.post("/agents/{agent_id}/containers/{container_id}/mounts")
async def update_container_mounts(
    agent_id: int,
    container_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    row = db.query(Container).filter(Container.id == container_id, Container.agent_id == agent_id).first()
    if not row:
        return HTMLResponse("Container nicht gefunden", status_code=404)
    form = await request.form()
    # "include" enthält dest-Paths externer Mounts; "include_entry" Namen der
    # Top-Level-Einträge des Compose-Dirs. Was NICHT angehakt ist → excluded.
    included_mounts = set(form.getlist("include"))
    included_entries = set(form.getlist("include_entry"))
    try:
        all_mounts = json.loads(row.backup_mounts) if row.backup_mounts else []
    except (json.JSONDecodeError, TypeError):
        all_mounts = []
    try:
        all_entries = json.loads(row.top_level_entries) if row.top_level_entries else []
    except (json.JSONDecodeError, TypeError):
        all_entries = []
    excluded = [m.get("dest") for m in all_mounts if m.get("dest") and m["dest"] not in included_mounts]
    excluded_e = [e.get("name") for e in all_entries if e.get("name") and e["name"] not in included_entries]
    row.excluded_mounts = json.dumps(excluded)
    row.excluded_entries = json.dumps(excluded_e)
    row.mounts_user_edited = True
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=containers", status_code=303)


@router.post("/agents/{agent_id}/containers/{container_id}/dismiss-db")
def dismiss_db_candidate(
    agent_id: int,
    container_id: int,
    cand_key: str = Form(...),
    db: Session = Depends(get_db),
):
    row = db.query(Container).filter(Container.id == container_id, Container.agent_id == agent_id).first()
    if not row:
        return HTMLResponse("Container nicht gefunden", status_code=404)
    try:
        dismissed = set(json.loads(row.db_candidates_dismissed or "[]"))
    except (json.JSONDecodeError, TypeError):
        dismissed = set()
    dismissed.add(cand_key)
    row.db_candidates_dismissed = json.dumps(sorted(dismissed))
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=containers", status_code=303)


@router.post("/agents/{agent_id}/containers/{container_id}/db-hook")
def add_db_hook(
    agent_id: int,
    container_id: int,
    db_type: str = Form(...),
    db_name: str = Form(...),
    hostname: str = Form(...),
    port: int = Form(0),
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    row = db.query(Container).filter(Container.id == container_id, Container.agent_id == agent_id).first()
    if not row:
        return HTMLResponse("Container nicht gefunden", status_code=404)
    if db_type not in ("postgresql", "mariadb", "mysql", "mongodb"):
        return RedirectResponse(f"/agents/{agent_id}?tab=containers&error=bad_db_type", status_code=303)
    hook = DatabaseHook(
        container_id=container_id,
        db_type=db_type,
        db_name=db_name.strip(),
        hostname=hostname.strip(),
        port=port or 0,
        username=username.strip(),
        password=password,
        enabled=True,
    )
    db.add(hook)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=containers", status_code=303)


@router.post("/agents/{agent_id}/db-hook/{hook_id}/delete")
def delete_db_hook(agent_id: int, hook_id: int, db: Session = Depends(get_db)):
    hook = db.query(DatabaseHook).filter(DatabaseHook.id == hook_id).first()
    if hook:
        # Validate ownership via container → agent chain
        ctr = db.query(Container).filter(Container.id == hook.container_id, Container.agent_id == agent_id).first()
        if ctr:
            db.delete(hook)
            db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=containers", status_code=303)


@router.post("/agents/{agent_id}/containers")
async def update_container_selection(
    agent_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    form = await request.form()
    enabled_ids = set(form.getlist("enabled"))
    containers = db.query(Container).filter(Container.agent_id == agent_id).all()
    for c in containers:
        c.backup_enabled = str(c.id) in enabled_ids
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=containers", status_code=303)


@router.post("/agents/{agent_id}/schedule")
def update_schedule(
    agent_id: int,
    schedule_id: int = Form(0),
    name: str = Form("Backup"),
    schedule_kind: str = Form("daily"),
    hour: int = Form(3),
    minute: int = Form(0),
    weekday: int = Form(0),
    day_of_month: int = Form(1),
    enabled: bool = Form(False),
    prune_after: bool = Form(False),
    keep_daily: int = Form(7),
    keep_weekly: int = Form(4),
    keep_monthly: int = Form(6),
    db: Session = Depends(get_db),
):
    if schedule_id:
        sched = db.query(Schedule).filter(Schedule.id == schedule_id, Schedule.agent_id == agent_id).first()
    else:
        sched = Schedule(agent_id=agent_id)
        db.add(sched)
    if not sched:
        return HTMLResponse("Schedule nicht gefunden", status_code=404)
    sched.name = name
    sched.schedule_kind = schedule_kind
    sched.hour = hour
    sched.minute = minute
    sched.weekday = weekday if schedule_kind == "weekly" else None
    sched.day_of_month = day_of_month if schedule_kind == "monthly" else None
    sched.cron_expr = cron_for(schedule_kind, hour, minute, weekday, day_of_month)
    sched.enabled = enabled
    sched.prune_after = prune_after
    sched.keep_daily = keep_daily
    sched.keep_weekly = keep_weekly
    sched.keep_monthly = keep_monthly
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=schedule", status_code=303)


@router.post("/agents/{agent_id}/schedule/{schedule_id}/delete")
def delete_schedule(agent_id: int, schedule_id: int, db: Session = Depends(get_db)):
    sched = db.query(Schedule).filter(Schedule.id == schedule_id, Schedule.agent_id == agent_id).first()
    if sched:
        db.delete(sched)
        db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=schedule", status_code=303)


def ensure_agent_passphrase(agent: Agent, db: Session) -> bool:
    """Returns True if a fresh passphrase was just generated for this agent."""
    if agent.borg_passphrase:
        return False
    agent.borg_passphrase = secrets.token_urlsafe(32)
    db.commit()
    return True


def _backup_params_for(agent_id: int, db: Session) -> tuple[list[str], dict]:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return [], {}
    return build_backup_params(agent, db)


@router.post("/agents/{agent_id}/backup")
def trigger_backup(agent_id: int, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    fresh_pp = ensure_agent_passphrase(agent, db)
    projects, params = _backup_params_for(agent_id, db)
    job = Job(
        agent_id=agent_id,
        job_type="backup",
        containers=json.dumps(projects) if projects else None,
        params=json.dumps(params) if params else "{}",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    if fresh_pp:
        db.add(JobLog(job_id=job.id, level="warning",
                      message="Passphrase wurde automatisch erzeugt. Bitte unter Verschlüsselung → Anzeigen / Download sichern, sonst ist im Disaster-Fall ein Restore unmöglich."))
        db.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/agents/{agent_id}/archives/refresh")
def refresh_archives(agent_id: int, request: Request, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    ensure_agent_passphrase(agent, db)
    job = Job(agent_id=agent_id, job_type="archive_list")
    db.add(job)
    db.commit()
    db.refresh(job)
    accepts = request.headers.get("accept", "")
    if "application/json" in accepts:
        return JSONResponse({"job_id": job.id})
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/agents/{agent_id}/verify")
def trigger_verify(
    agent_id: int,
    verify_data: bool = Form(False),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    ensure_agent_passphrase(agent, db)
    job = Job(
        agent_id=agent_id,
        job_type="verify",
        params=json.dumps({"verify_data": verify_data}),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/agents/{agent_id}/restore")
def trigger_restore(
    agent_id: int,
    archive: str = Form(...),
    sub_path: str = Form(""),
    sub_path_custom: str = Form(""),
    host_target: str = Form(""),
    structured: bool = Form(False),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    ensure_agent_passphrase(agent, db)
    # Sentinel: "__custom__" radio selected → take the freitext field
    effective_sub_path = sub_path_custom.strip() if sub_path == "__custom__" else sub_path.strip()
    job = Job(
        agent_id=agent_id,
        job_type="restore",
        params=json.dumps({
            "archive": archive,
            "sub_path": effective_sub_path,
            "host_target": host_target.strip(),
            "structured": bool(structured),
        }),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/agents/{agent_id}/db-restore")
def trigger_db_restore(
    agent_id: int,
    archive: str = Form(...),
    project: str = Form(...),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return HTMLResponse("Agent nicht gefunden", status_code=404)
    ensure_agent_passphrase(agent, db)
    container_row = (
        db.query(Container)
        .filter(Container.agent_id == agent_id, Container.compose_project == project)
        .first()
    )
    if not container_row:
        return HTMLResponse(f"Projekt {project} nicht gefunden", status_code=404)
    hooks = db.query(DatabaseHook).filter(
        DatabaseHook.container_id == container_row.id,
        DatabaseHook.enabled == True,  # noqa: E712
    ).all()
    if not hooks:
        return HTMLResponse(f"Keine aktiven DB-Hooks für Projekt {project}", status_code=400)
    hook_payload = [
        {"type": h.db_type, "name": h.db_name, "hostname": h.hostname,
         "port": h.port, "username": h.username, "password": h.password}
        for h in hooks
    ]
    job = Job(
        agent_id=agent_id,
        job_type="db_restore",
        params=json.dumps({"archive": archive, "project": project, "db_hooks": hook_payload}),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/agents/{agent_id}/delete")
def delete_agent(agent_id: int, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent:
        db.delete(agent)
        db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return HTMLResponse("Job nicht gefunden", status_code=404)
    if job.status in ("pending", "running"):
        job.status = "cancelled"
        if not job.completed_at:
            job.completed_at = datetime.utcnow()
        db.add(JobLog(
            job_id=job.id,
            level="warning",
            message="Job abgebrochen via Web-UI",
        ))
        db.commit()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/delete")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return HTMLResponse("Job nicht gefunden", status_code=404)
    agent_id = job.agent_id
    db.delete(job)
    db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=jobs", status_code=303)


@router.post("/jobs/cleanup")
def cleanup_jobs(
    status: str = Form("done"),
    db: Session = Depends(get_db),
):
    if status == "done":
        targets = ("success", "failed", "cancelled")
    elif status == "failed":
        targets = ("failed",)
    elif status == "cancelled":
        targets = ("cancelled",)
    else:
        targets = ()
    if targets:
        db.query(Job).filter(Job.status.in_(targets)).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse("/jobs", status_code=303)


@router.post("/agents/{agent_id}/jobs/cleanup")
def cleanup_agent_jobs(
    agent_id: int,
    status: str = Form("done"),
    db: Session = Depends(get_db),
):
    """Pro-Agent Jobs-Cleanup. status: 'done' (erfolg+fehler+cancelled),
    'failed', 'cancelled', oder 'all' (Live-Jobs ausgenommen).
    """
    if status == "done":
        targets = ("success", "failed", "cancelled")
    elif status == "failed":
        targets = ("failed",)
    elif status == "cancelled":
        targets = ("cancelled",)
    elif status == "all":
        targets = ("success", "failed", "cancelled")
    else:
        targets = ()
    if targets:
        db.query(Job).filter(
            Job.agent_id == agent_id,
            Job.status.in_(targets),
        ).delete(synchronize_session=False)
        db.commit()
    return RedirectResponse(f"/agents/{agent_id}?tab=jobs", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    status: str = "",
    db: Session = Depends(get_db),
):
    q = db.query(Job).order_by(Job.created_at.desc())
    if status:
        q = q.filter(Job.status == status)
    jobs = q.limit(200).all()
    agents = {a.id: a for a in db.query(Agent).all()}
    counts = {
        "all": db.query(Job).count(),
        "running": db.query(Job).filter(Job.status == "running").count(),
        "pending": db.query(Job).filter(Job.status == "pending").count(),
        "success": db.query(Job).filter(Job.status == "success").count(),
        "failed": db.query(Job).filter(Job.status == "failed").count(),
        "cancelled": db.query(Job).filter(Job.status == "cancelled").count(),
    }
    return templates.TemplateResponse(request, "jobs.html", {
        "jobs": jobs,
        "agents": agents,
        "status_filter": status,
        "counts": counts,
    })


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return HTMLResponse("Job nicht gefunden", status_code=404)
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.timestamp).all()
    agent = db.query(Agent).filter(Agent.id == job.agent_id).first()
    return templates.TemplateResponse(request, "job_detail.html", {
        "job": job,
        "logs": logs,
        "agent": agent,
    })


@router.get("/api/v1/ui/jobs/{job_id}/logs")
def job_logs_json(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return {"status": "not_found"}
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.timestamp).all()
    return {
        "status": job.status,
        "logs": [
            {"timestamp": localtime(l.timestamp), "level": l.level, "message": l.message}
            for l in logs
        ],
    }


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request, db: Session = Depends(get_db)):
    schedules = db.query(Schedule).order_by(Schedule.agent_id).all()
    agents = {a.id: a for a in db.query(Agent).all()}
    return templates.TemplateResponse(request, "schedules.html", {
        "schedules": schedules,
        "agents": agents,
    })


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {})


@router.post("/settings/password")
def change_password(
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if not secrets.compare_digest(current_password, get_admin_password()):
        return RedirectResponse("/settings?error=current_wrong", status_code=303)
    if not new_password or new_password != confirm_password:
        return RedirectResponse("/settings?error=mismatch", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse("/settings?error=too_short", status_code=303)
    set_admin_password(new_password)
    return RedirectResponse("/settings?ok=1", status_code=303)
