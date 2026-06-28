from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .config import settings
from .discovery import discover_containers
from .client import CentralClient
from .models import JobType
from . import worker

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("docker-borg-agent")

_shutdown = False


def _signal_handler(sig, frame):
    global _shutdown
    logger.info("Shutdown signal received")
    _shutdown = True


def cmd_discover(args):
    containers = discover_containers()
    for c in containers:
        files = ", ".join(c.root_files) if c.root_files else "(none)"
        print(f"  [{c.compose_project}] {c.container_name} -> {c.compose_dir}")
        print(f"    Files: {files}")
    print(f"\nTotal: {len(containers)} compose projects")


def cmd_backup(args):
    """One-shot backup from the CLI. Spawns the same worker container that the daemon would."""
    containers = discover_containers()
    if args.project:
        containers = [c for c in containers if c.compose_project == args.project]
        if not containers:
            print(f"Project '{args.project}' not found")
            sys.exit(1)
    for c in containers:
        if not c.compose_dir:
            continue
        print(f"Backing up {c.compose_project}...")
        r = worker.run_backup(c, lambda m, lvl="info": print(f"  [{lvl}] {m}"))
        status = "OK" if r.success else "FAILED"
        print(f"  [{status}] {r.job_result.archive_name}")


def cmd_list(args):
    print("Use the web UI for archive listing in v0.5.0+ (CLI list not implemented).")
    sys.exit(1)


def cmd_daemon(args):
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Aufräumen: alte Staging-Reste aus früheren Läufen (besonders v0.4.1 Disaster)
    staging_root = Path("/data/staging")
    if staging_root.exists():
        for entry in staging_root.iterdir():
            if entry.is_dir():
                # Falls Bind-Mount noch aktiv: erst umount
                try:
                    bind_target = entry / "compose"
                    if bind_target.is_mount():
                        import subprocess as _sp
                        _sp.run(["umount", str(bind_target)], capture_output=True, timeout=10)
                except Exception:
                    pass
                try:
                    import shutil as _sh
                    _sh.rmtree(entry, ignore_errors=True)
                except Exception:
                    pass

    # Worker-Image beim Start frisch pullen (Cache umgehen) + verwaiste
    # Worker-Container vom letzten Lauf / von Abstürzen aufräumen.
    try:
        import docker as _docker
        _wc = _docker.DockerClient(base_url=f"unix://{settings.docker_socket}")
        try:
            worker.ensure_worker_image_fresh(_wc)
            n = worker.cleanup_stale_workers(_wc)
            if n:
                logger.info("Beim Start %d verwaiste Worker-Container entfernt", n)
        finally:
            _wc.close()
    except Exception as e:
        logger.warning("Worker-Image-Refresh / Cleanup fehlgeschlagen: %s", e)

    client = CentralClient()

    if not client.is_registered:
        logger.info("Not registered, attempting registration...")
        while not client.is_registered and not _shutdown:
            if client.register():
                break
            logger.info("Retrying registration in %ds...", settings.poll_interval)
            time.sleep(settings.poll_interval)

    logger.info("Agent daemon started (poll interval: %ds)", settings.poll_interval)

    last_containers: list = []
    last_containers_lock = threading.Lock()

    def worker_loop():
        from .models import LogEntry
        while not _shutdown:
            try:
                jobs = client.poll_jobs()
                for job in jobs:
                    if job.job_id in client.cancelled_jobs:
                        client.report_job(job.job_id, "cancelled", logs=[LogEntry("warning", "Job vor Start abgebrochen")])
                        continue
                    client.report_job(job.job_id, "running")

                    cancel_watchdog_stop = threading.Event()

                    def watchdog():
                        while not cancel_watchdog_stop.wait(2):
                            if job.job_id in client.cancelled_jobs:
                                logger.warning("Cancellation signal for job %d — killing worker container", job.job_id)
                                worker.cancel_active()
                                return

                    wd = threading.Thread(target=watchdog, daemon=True, name=f"cancel-watchdog-{job.job_id}")
                    wd.start()

                    try:
                        with last_containers_lock:
                            containers_for_job = list(last_containers)
                        # Vor jedem Job einen synchronen Heartbeat — sonst läuft
                        # der erste Worker möglicherweise mit stale Settings
                        # (z.B. leerer Passphrase, weil Central die gerade erst
                        # generiert hat). Heartbeat-Response aktualisiert
                        # settings.borg_passphrase + backup_type etc.
                        old_pp = settings.borg_passphrase
                        try:
                            client.heartbeat(containers_for_job)
                        except Exception as e:
                            logger.warning("Pre-job heartbeat sync failed: %s", e)
                        if old_pp != settings.borg_passphrase:
                            client.report_job(
                                job.job_id, "running",
                                logs=[LogEntry("info", "Backup-Settings (inkl. Passphrase) wurden vor Job-Start aktualisiert")],
                            )
                        _execute_job(job, containers_for_job, client)
                    finally:
                        cancel_watchdog_stop.set()
                        wd.join(timeout=3)
            except Exception:
                logger.exception("Worker loop error")
            for _ in range(settings.poll_interval):
                if _shutdown:
                    break
                time.sleep(1)

    job_worker = threading.Thread(target=worker_loop, daemon=True, name="job-worker")
    job_worker.start()

    DISCOVERY_INTERVAL = 30  # seconds between Docker introspections (heavy)

    def discovery_loop():
        while not _shutdown:
            try:
                containers = discover_containers(client.manual_paths)
                with last_containers_lock:
                    last_containers.clear()
                    last_containers.extend(containers)
            except Exception:
                logger.exception("Discovery loop error")
            for _ in range(DISCOVERY_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)

    discovery = threading.Thread(target=discovery_loop, daemon=True, name="discovery")
    discovery.start()

    # Main thread = heartbeat. Never touches Docker SDK directly so a stalled
    # docker daemon cannot block the heartbeat anymore.
    while not _shutdown:
        try:
            with last_containers_lock:
                containers_snapshot = list(last_containers)
            client.heartbeat(containers_snapshot)
        except Exception:
            logger.exception("Heartbeat loop error")
        for _ in range(settings.poll_interval):
            if _shutdown:
                break
            time.sleep(1)

    job_worker.join(timeout=10)
    discovery.join(timeout=10)
    client.close()
    logger.info("Agent daemon stopped")


def _execute_job(job, containers, client: CentralClient):
    from .models import LogEntry
    logger.info("Executing job %d: %s", job.job_id, job.job_type)

    def stream(level: str, message: str):
        client.report_job(job.job_id, "running", logs=[LogEntry(level, message)])

    try:
        if job.job_type == JobType.BACKUP:
            targets = containers
            if job.containers:
                targets = [c for c in containers if c.compose_project in job.containers]

            overrides = (job.params or {}).get("compose_dirs", {}) if job.params else {}
            exclude_mounts_by_project = (job.params or {}).get("exclude_mounts", {}) if job.params else {}
            exclude_entries_by_project = (job.params or {}).get("exclude_entries", {}) if job.params else {}
            db_hooks_by_project = (job.params or {}).get("db_hooks", {}) if job.params else {}
            retention = (job.params or {}).get("retention") if job.params else None
            resources = (job.params or {}).get("resources") if job.params else None
            existing_projects = {c.compose_project for c in targets}
            for project, manual_dir in overrides.items():
                if project not in existing_projects and project in (job.containers or []):
                    from .models import ContainerInfo
                    targets.append(ContainerInfo(
                        container_id="manual",
                        container_name="(manual)",
                        compose_project=project,
                        compose_dir=manual_dir,
                        root_files=[],
                        image="",
                        status="manual",
                    ))

            stream("info", f"Backup geplant für {len(targets)} Projekt(e): {', '.join(c.compose_project for c in targets) or '(keine)'}")

            all_success = True
            backed_up = 0
            for c in targets:
                if job.job_id in client.cancelled_jobs:
                    stream("warning", "Job-Abbruch erkannt — restliche Projekte werden übersprungen")
                    all_success = False
                    break
                manual = overrides.get(c.compose_project)
                if manual:
                    c.compose_dir = manual
                if not c.compose_dir:
                    stream("warning", f"{c.compose_project}: kein Pfad gesetzt, übersprungen")
                    continue
                stream("info", f"→ {c.compose_project}")
                excludes = exclude_mounts_by_project.get(c.compose_project, [])
                exclude_entries = exclude_entries_by_project.get(c.compose_project, [])
                if excludes:
                    stream("info", f"  exkludierte Mounts: {', '.join(excludes)}")
                if exclude_entries:
                    stream("info", f"  exkludierte Inhalte: {', '.join(exclude_entries)}")
                db_hooks = db_hooks_by_project.get(c.compose_project, [])
                if db_hooks:
                    stream("info", f"  Datenbanken: {', '.join(h['type'] + '/' + h['name'] for h in db_hooks)}")
                r = worker.run_backup(c, lambda m, lvl="info": stream(lvl, m),
                                       excluded_mounts=excludes, db_hooks=db_hooks,
                                       excluded_entries=exclude_entries,
                                       retention=retention, resources=resources)
                for log in r.logs:
                    client.report_job(job.job_id, "running", logs=[log])
                if r.success:
                    backed_up += 1
                else:
                    all_success = False

            if job.job_id in client.cancelled_jobs:
                stream("warning", "Backup wurde abgebrochen")
                client.report_job(job.job_id, "cancelled")
            else:
                stream("info", f"Backup abgeschlossen: {backed_up}/{len(targets)} erfolgreich")
                status = "success" if all_success and backed_up > 0 else "failed"
                client.report_job(job.job_id, status)

        elif job.job_type == JobType.PRUNE:
            client.report_job(
                job.job_id, "success",
                logs=[LogEntry("info", "Prune durch borgmatic beim Backup bereits erledigt")],
            )

        elif job.job_type == JobType.LIST:
            client.report_job(
                job.job_id, "failed",
                logs=[LogEntry("warning", "LIST nicht implementiert in v0.5.0")],
            )

        elif job.job_type == JobType.SCP_TEST:
            from .ssh import test_connection
            params = job.params or {}
            ok, msg = test_connection(
                params.get("host", ""),
                params.get("user", ""),
                int(params.get("port", 22)),
            )
            client.report_job(
                job.job_id,
                "success" if ok else "failed",
                logs=[LogEntry("info" if ok else "error", msg)],
            )

        elif job.job_type == JobType.SCP_INSTALL_KEY:
            from .ssh import install_pubkey
            params = job.params or {}
            ok, msg = install_pubkey(
                params.get("host", ""),
                params.get("user", ""),
                int(params.get("port", 22)),
                params.get("password", ""),
            )
            client.report_job(
                job.job_id,
                "success" if ok else "failed",
                logs=[LogEntry("info" if ok else "error", msg)],
            )

        elif job.job_type == JobType.ARCHIVE_LIST:
            r, archives = worker.run_list_archives(lambda m, lvl="info": stream(lvl, m))
            for log in r.logs:
                client.report_job(job.job_id, "running", logs=[log])
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
                result={"archives": archives},
            )

        elif job.job_type == JobType.VERIFY:
            r = worker.run_check(lambda m, lvl="info": stream(lvl, m))
            for log in r.logs:
                client.report_job(job.job_id, "running", logs=[log])
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
                result=asdict(r.job_result),
            )

        elif job.job_type == JobType.RESTORE:
            archive = job.params.get("archive", "")
            mode = job.params.get("mode", "")
            if mode == "inplace":
                project = job.params.get("project", "")
                mounts = job.params.get("mounts", [])
                compose_dir = job.params.get("compose_dir", "")
                db_hooks = job.params.get("db_hooks", [])
                target_info = next((c for c in containers if c.compose_project == project), None)
                if not target_info:
                    from .models import ContainerInfo
                    target_info = ContainerInfo(
                        container_id="manual", container_name="(manual)",
                        compose_project=project, compose_dir=compose_dir, root_files=[],
                        image="", status="manual",
                    )
                r = worker.run_restore_inplace(
                    target_info, archive, mounts, compose_dir, db_hooks,
                    lambda m, lvl="info": stream(lvl, m))
            else:
                sub_path = job.params.get("sub_path", "")
                host_target = job.params.get("host_target", "")
                structured = bool(job.params.get("structured", False))
                r = worker.run_restore(archive, lambda m, lvl="info": stream(lvl, m),
                                       sub_path=sub_path, host_target=host_target,
                                       structured=structured)
            for log in r.logs:
                client.report_job(job.job_id, "running", logs=[log])
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
            )

        elif job.job_type == JobType.DB_RESTORE:
            archive = job.params.get("archive", "")
            project = job.params.get("project", "")
            db_hooks = job.params.get("db_hooks", [])
            if not archive or not project or not db_hooks:
                client.report_job(job.job_id, "failed",
                    logs=[LogEntry("error", "archive, project und db_hooks sind nötig")])
            else:
                # Container-Info für das Projekt zur Hand haben
                target_info = next((c for c in containers if c.compose_project == project), None)
                if not target_info:
                    from .models import ContainerInfo
                    target_info = ContainerInfo(
                        container_id="manual", container_name="(manual)",
                        compose_project=project, compose_dir="", root_files=[],
                        image="", status="manual",
                    )
                r = worker.run_db_restore(target_info, archive, db_hooks,
                                          lambda m, lvl="info": stream(lvl, m))
                for log in r.logs:
                    client.report_job(job.job_id, "running", logs=[log])
                client.report_job(job.job_id, "success" if r.success else "failed")

    except Exception as e:
        logger.exception("Job %d failed", job.job_id)
        from .models import LogEntry
        client.report_job(job.job_id, "failed", logs=[LogEntry("error", str(e))])


def main():
    parser = argparse.ArgumentParser(description="docker-borg agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("discover", help="List discovered containers")

    bp = sub.add_parser("backup", help="Run backup now")
    bp.add_argument("--project", "-p", help="Only backup this compose project")

    sub.add_parser("list", help="List borg archives")

    sub.add_parser("daemon", help="Run as daemon (poll central for jobs)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "discover": cmd_discover,
        "backup": cmd_backup,
        "list": cmd_list,
        "daemon": cmd_daemon,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
