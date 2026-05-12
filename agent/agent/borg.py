from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import settings
from .models import ContainerInfo, JobResult, LogEntry

logger = logging.getLogger(__name__)


@dataclass
class BorgResult:
    success: bool
    job_result: JobResult
    logs: list[LogEntry] = field(default_factory=list)


def _borg_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env["BORG_REPO"] = settings.borg_repo
    env["BORG_PASSPHRASE"] = settings.borg_passphrase
    env["BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK"] = "yes"
    env["BORG_RELOCATED_REPO_ACCESS_IS_OK"] = "yes"
    if settings.backup_type == "scp":
        from .ssh import borg_rsh
        env["BORG_RSH"] = borg_rsh()
    return env


_active_proc: subprocess.Popen | None = None


def cancel_active() -> bool:
    """Kill the currently running borg subprocess, if any. Returns True if a process was killed."""
    global _active_proc
    proc = _active_proc
    if proc is None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError:
        pass
    return True


def _run_borg(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    global _active_proc
    cmd = ["borg"] + args
    logger.debug("Running: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_borg_env(),
        cwd=cwd,
    )
    _active_proc = proc
    try:
        stdout, stderr = proc.communicate(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, returncode=124, stdout=stdout, stderr=stderr + "\nTimeout after 3600s")
    finally:
        _active_proc = None
    return subprocess.CompletedProcess(cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr)


def _host_path_to_local(host_path: str) -> Path:
    host_dir = settings.docker_host_dir
    return Path(host_dir) / Path(host_path).name


def init_repo() -> bool:
    result = _run_borg(["init", "--encryption=repokey-blake2"])
    if result.returncode == 0:
        logger.info("Borg repo initialized at %s", settings.borg_repo)
        return True
    if "already exists" in result.stderr.lower() or "repository already exists" in result.stderr.lower():
        logger.info("Borg repo already exists at %s", settings.borg_repo)
        return True
    logger.error("Failed to init borg repo: %s", result.stderr)
    return False


_EXCLUDE_NAMES = [
    ".git", ".svn", ".hg",
    "node_modules",
    "__pycache__",
    ".venv", "venv",
    ".cache",
]


def _build_excludes() -> list[str]:
    patterns: list[str] = []
    for name in _EXCLUDE_NAMES:
        patterns.append(name)
        patterns.append(f"{name}/**")
        patterns.append(f"**/{name}")
        patterns.append(f"**/{name}/**")
    patterns.extend(["*.pyc", ".DS_Store", "**/.DS_Store"])
    return patterns


DEFAULT_EXCLUDES = _build_excludes()


def create_backup(container: ContainerInfo) -> BorgResult:
    logs: list[LogEntry] = []
    start = time.time()

    compose_dir_local = _host_path_to_local(container.compose_dir)
    if not compose_dir_local.is_dir():
        msg = f"Compose dir not accessible: {compose_dir_local}"
        logger.error(msg)
        return BorgResult(success=False, job_result=JobResult(), logs=[LogEntry("error", msg)])

    archive_name = f"{settings.agent_name}-{container.compose_project}-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    logs.append(LogEntry("info", f"Starte Backup: {archive_name}"))
    logs.append(LogEntry("info", f"Compose-Verzeichnis: {compose_dir_local}"))

    extra_sources: list[str] = []
    skipped_volumes: list[str] = []
    for v in (container.named_volumes or []):
        src = v.get("source") or ""
        name = v.get("name") or ""
        if not src:
            continue
        p = Path(src)
        if p.is_dir():
            extra_sources.append(str(p))
            logs.append(LogEntry("info", f"Named Volume: {name} → {src}"))
        else:
            skipped_volumes.append(f"{name} ({src})")

    if skipped_volumes:
        logs.append(LogEntry("warning", f"Volume-Daten nicht zugreifbar (Mount fehlt im Agent?): {', '.join(skipped_volumes)}"))

    exclude_args: list[str] = []
    for pattern in DEFAULT_EXCLUDES:
        exclude_args.extend(["--exclude", pattern])

    create_args = ["create", "--json", "--stats"] + exclude_args + [f"::{archive_name}", "."] + extra_sources

    def _attempt() -> subprocess.CompletedProcess:
        return _run_borg(create_args, cwd=str(compose_dir_local))

    result = _attempt()

    if result.returncode != 0 and "repository" in (result.stderr or "").lower() and "does not exist" in (result.stderr or "").lower():
        logs.append(LogEntry("info", "Repository nicht vorhanden, wird initialisiert..."))
        if not init_repo():
            logs.append(LogEntry("error", "Failed to initialize repository"))
            return BorgResult(success=False, job_result=JobResult(), logs=logs)
        result = _attempt()

    retries = 0
    max_retries = 2
    while result.returncode != 0 and retries < max_retries:
        combined = (result.stderr or "") + (result.stdout or "")
        if "Input/output error" in combined or "Transport endpoint" in combined or "Connection reset" in combined:
            retries += 1
            logs.append(LogEntry("warning", f"I/O-Fehler beim Backup, Versuch {retries+1}/{max_retries+1} in 10s..."))
            time.sleep(10)
            result = _attempt()
        else:
            break

    duration = time.time() - start

    if result.returncode != 0:
        msg = f"Borg create failed: {(result.stderr or '').strip() or (result.stdout or '').strip() or '(keine Ausgabe)'}"
        logger.error(msg)
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    job_result = JobResult(archive_name=archive_name, duration_seconds=round(duration, 2))
    try:
        data = json.loads(result.stdout)
        archive_stats = data.get("archive", {}).get("stats", {})
        job_result.size_bytes = archive_stats.get("original_size", 0)
        job_result.nfiles = archive_stats.get("nfiles", 0)
    except (json.JSONDecodeError, KeyError):
        pass

    logs.append(LogEntry("info", f"Backup complete: {job_result.nfiles} files, {job_result.size_bytes} bytes"))
    logger.info("Backup created: %s", archive_name)
    return BorgResult(success=True, job_result=job_result, logs=logs)


def backup_all(containers: list[ContainerInfo]) -> list[BorgResult]:
    if not init_repo():
        return [BorgResult(success=False, job_result=JobResult(), logs=[LogEntry("error", "Failed to init repo")])]

    results = []
    for c in containers:
        if not c.compose_dir:
            logger.info("Skipping %s (no compose dir)", c.compose_project)
            continue
        r = create_backup(c)
        results.append(r)
    return results


def prune(keep_daily: int = 7, keep_weekly: int = 4, keep_monthly: int = 6) -> BorgResult:
    logs: list[LogEntry] = []
    logs.append(LogEntry("info", f"Pruning: keep daily={keep_daily}, weekly={keep_weekly}, monthly={keep_monthly}"))

    prefix = f"{settings.agent_name}-"
    result = _run_borg([
        "prune",
        f"--prefix={prefix}",
        f"--keep-daily={keep_daily}",
        f"--keep-weekly={keep_weekly}",
        f"--keep-monthly={keep_monthly}",
    ])

    if result.returncode != 0:
        msg = f"Borg prune failed: {result.stderr}"
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    logs.append(LogEntry("info", "Prune completed"))
    return BorgResult(success=True, job_result=JobResult(), logs=logs)


def list_archives() -> BorgResult:
    logs: list[LogEntry] = []
    result = _run_borg(["list", "--json"])

    if result.returncode != 0:
        msg = f"Borg list failed: {result.stderr}"
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    try:
        data = json.loads(result.stdout)
        archives = data.get("archives", [])
        logs.append(LogEntry("info", f"Found {len(archives)} archives"))
        job_result = JobResult()
        job_result.nfiles = len(archives)
        return BorgResult(success=True, job_result=job_result, logs=logs)
    except json.JSONDecodeError:
        logs.append(LogEntry("info", result.stdout))
        return BorgResult(success=True, job_result=JobResult(), logs=logs)


def verify_repo(verify_data: bool = False) -> BorgResult:
    """Run borg check + dry-run restore of the latest archive.

    verify_data: if True, also reads all data chunks (slow, hours on large repos).
                  If False (default), only checks repository + archive structure.
    """
    logs: list[LogEntry] = []
    start = time.time()

    check_cmd = ["check"]
    if verify_data:
        check_cmd.append("--verify-data")
        logs.append(LogEntry("info", "Starte borg check --verify-data (liest alle Daten, kann lange dauern)"))
    else:
        logs.append(LogEntry("info", "Starte borg check (Repository- und Archiv-Struktur)"))

    result = _run_borg(check_cmd)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip() or "(keine Ausgabe)"
        logs.append(LogEntry("error", f"borg check fehlgeschlagen: {msg}"))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)
    logs.append(LogEntry("info", "borg check OK"))

    result = _run_borg(["list", "--json"])
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip() or "(keine Ausgabe)"
        logs.append(LogEntry("warning", f"borg list fehlgeschlagen: {msg}"))
        return BorgResult(success=True, job_result=JobResult(duration_seconds=round(time.time() - start, 2)), logs=logs)

    try:
        data = json.loads(result.stdout)
        archives = sorted(data.get("archives", []), key=lambda a: a.get("start", ""), reverse=True)
    except (json.JSONDecodeError, KeyError):
        archives = []

    if not archives:
        logs.append(LogEntry("warning", "Keine Archive im Repository vorhanden — nichts zum Testen der Wiederherstellung"))
        return BorgResult(success=True, job_result=JobResult(duration_seconds=round(time.time() - start, 2)), logs=logs)

    latest = archives[0]["name"]
    logs.append(LogEntry("info", f"Teste Wiederherstellung des neuesten Archivs: {latest}"))

    result = _run_borg(["extract", "--dry-run", f"::{latest}"])
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip() or "(keine Ausgabe)"
        logs.append(LogEntry("error", f"Wiederherstellungs-Test fehlgeschlagen: {msg}"))
        return BorgResult(
            success=False,
            job_result=JobResult(archive_name=latest, duration_seconds=round(time.time() - start, 2)),
            logs=logs,
        )

    logs.append(LogEntry("info", f"Wiederherstellungs-Test erfolgreich: {latest} ist lesbar und entpackbar"))
    logs.append(LogEntry("info", f"Backup ist recovery-fähig ({len(archives)} Archiv(e) im Repository)"))
    return BorgResult(
        success=True,
        job_result=JobResult(archive_name=latest, nfiles=len(archives), duration_seconds=round(time.time() - start, 2)),
        logs=logs,
    )


def extract_archive(archive_name: str, target_dir: str) -> BorgResult:
    logs: list[LogEntry] = []
    logs.append(LogEntry("info", f"Restoring {archive_name} to {target_dir}"))

    Path(target_dir).mkdir(parents=True, exist_ok=True)

    result = _run_borg(["extract", f"::{archive_name}"], cwd=target_dir)

    if result.returncode != 0:
        msg = f"Borg extract failed: {result.stderr}"
        logs.append(LogEntry("error", msg))
        return BorgResult(success=False, job_result=JobResult(), logs=logs)

    logs.append(LogEntry("info", f"Restore complete to {target_dir}"))
    return BorgResult(success=True, job_result=JobResult(archive_name=archive_name), logs=logs)
