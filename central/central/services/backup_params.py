"""Gemeinsame Backup-Parameter-Erzeugung für manuelle (UI) und geplante
(Scheduler) Backups. Vorher dupliziert/divergiert — der Scheduler hat z.B.
weder Excludes noch DB-Hooks mitgeschickt.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..models import Agent, Container, DatabaseHook


def _load_json(raw, default):
    try:
        v = json.loads(raw) if raw else default
        return v if v is not None else default
    except (json.JSONDecodeError, TypeError):
        return default


def _hook_key(db_type: str, hostname: str, db_name: str) -> str:
    return f"{db_type}:{hostname}:{db_name}"


def _safe_host(hostname: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (hostname or "agent"))


def build_borg_repo(agent: Agent) -> str:
    """Effektiver borg-Repo-Pfad für diesen Agent — IMMER frisch berechnet,
    nicht aus dem (evtl. veralteten) gespeicherten Feld gelesen.

    SCP: scp_path ist ein Basis-Verzeichnis; pro Agent wird automatisch ein
    eigener Unterordner <hostname> angehängt (eigenes, leeres borg-Repo, keine
    Kollision zwischen Agents). 'local': der gemountete Pfad.
    """
    if agent.backup_type == "scp":
        if agent.scp_host and agent.scp_user and agent.scp_path:
            base = (agent.scp_path or "").strip().strip("/")
            host = _safe_host(agent.hostname)
            if not base.split("/")[-1] == host:   # nicht doppelt anhängen
                base = f"{base}/{host}"
            return f"ssh://{agent.scp_user}@{agent.scp_host}:{agent.scp_port or 22}/{base}"
        return ""
    if agent.backup_type == "local":
        return agent.local_path or ""
    return ""


def retention_for(agent: Agent) -> dict | None:
    """Retention-Dict fürs Job-Param, oder None wenn Prune deaktiviert."""
    if not agent.prune_enabled:
        return None
    mode = agent.retention_mode or "simple"
    if mode == "advanced":
        return {
            "mode": "advanced",
            "keep_daily": agent.keep_daily or 0,
            "keep_weekly": agent.keep_weekly or 0,
            "keep_monthly": agent.keep_monthly or 0,
        }
    return {"mode": "simple", "keep_last": agent.keep_last or 0}


def resources_for(agent: Agent) -> dict:
    return {
        "mem_mb": agent.worker_mem_mb or 1024,
        "cpus": agent.worker_cpus or "",
    }


def restore_plan_for(container_row, db: Session) -> dict:
    """Plan für einen Komplett-Restore an den Originalort eines Projekts:
    compose_dir, die TATSÄCHLICH gesicherten Mounts (excludete + DB-Raw raus)
    und die aktiven DB-Hooks (für den Dump-Replay).
    """
    mounts = _load_json(container_row.backup_mounts, [])
    ex_mounts = set(_load_json(container_row.excluded_mounts, []))

    hooks = (
        db.query(DatabaseHook)
        .filter(DatabaseHook.container_id == container_row.id, DatabaseHook.enabled == True)  # noqa: E712
        .all()
    )
    active_keys = {_hook_key(h.db_type, h.hostname, h.db_name) for h in hooks}
    # DB-Raw-Mounts (dump-only) ebenfalls ausschließen — die sind nicht im Archiv
    for cand in _load_json(container_row.db_candidates, []):
        key = _hook_key(cand.get("db_type", ""), cand.get("hostname", ""), cand.get("db_name", ""))
        if key in active_keys:
            raw = cand.get("raw_exclude")
            if raw and raw.get("kind") == "mount" and raw.get("value"):
                ex_mounts.add(raw["value"])

    active_mounts = [m for m in mounts if m.get("dest") not in ex_mounts]
    compose_dir = container_row.manual_compose_dir or container_row.compose_dir or ""
    db_hooks = [
        {"type": h.db_type, "name": h.db_name, "hostname": h.hostname,
         "port": h.port, "username": h.username, "password": h.password}
        for h in hooks
    ]
    return {
        "compose_dir": compose_dir,
        "mounts": active_mounts,
        "db_hooks": db_hooks,
    }


def build_backup_params(agent: Agent, db: Session) -> tuple[list[str], dict]:
    """(projects, params) für einen Backup-Job dieses Agents.

    Enthält: compose_dirs, exclude_mounts, exclude_entries (inkl. automatischer
    DB-Raw-Verzeichnis-Excludes bei aktivem DB-Hook = dump-only), db_hooks,
    retention, resources.
    """
    enabled = (
        db.query(Container)
        .filter(Container.agent_id == agent.id, Container.backup_enabled == True)  # noqa: E712
        .all()
    )
    projects = sorted({c.compose_project for c in enabled if c.compose_project})
    overrides = {c.compose_project: c.manual_compose_dir for c in enabled if c.manual_compose_dir}

    excludes: dict[str, list[str]] = {}
    exclude_entries: dict[str, list[str]] = {}
    db_hooks_by_project: dict[str, list[dict]] = {}

    for c in enabled:
        proj = c.compose_project
        ex_mounts = list(_load_json(c.excluded_mounts, []))
        ex_entries = list(_load_json(c.excluded_entries, []))

        hooks = (
            db.query(DatabaseHook)
            .filter(DatabaseHook.container_id == c.id, DatabaseHook.enabled == True)  # noqa: E712
            .all()
        )
        active_keys = {_hook_key(h.db_type, h.hostname, h.db_name) for h in hooks}

        # DB-Raw-Verzeichnisse automatisch ausschließen (dump-only), wenn für
        # diese DB ein Hook aktiv ist. Quelle: db_candidates[].raw_exclude
        for cand in _load_json(c.db_candidates, []):
            key = _hook_key(cand.get("db_type", ""), cand.get("hostname", ""), cand.get("db_name", ""))
            if key not in active_keys:
                continue
            raw = cand.get("raw_exclude")
            if not raw:
                continue
            if raw.get("kind") == "entry" and raw.get("value") and raw["value"] not in ex_entries:
                ex_entries.append(raw["value"])
            elif raw.get("kind") == "mount" and raw.get("value") and raw["value"] not in ex_mounts:
                ex_mounts.append(raw["value"])

        if ex_mounts:
            excludes[proj] = ex_mounts
        if ex_entries:
            exclude_entries[proj] = ex_entries

        if hooks:
            db_hooks_by_project.setdefault(proj, [])
            for h in hooks:
                db_hooks_by_project[proj].append({
                    "type": h.db_type,
                    "name": h.db_name,
                    "hostname": h.hostname,
                    "port": h.port,
                    "username": h.username,
                    "password": h.password,
                })

    params: dict = {}
    if overrides:
        params["compose_dirs"] = overrides
    if excludes:
        params["exclude_mounts"] = excludes
    if exclude_entries:
        params["exclude_entries"] = exclude_entries
    if db_hooks_by_project:
        params["db_hooks"] = db_hooks_by_project
    retention = retention_for(agent)
    if retention:
        params["retention"] = retention
    params["resources"] = resources_for(agent)
    return projects, params
