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
                r = worker.run_backup(c, lambda m, lvl="info": stream(lvl, m))
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
                job.job_id, "failed",
                logs=[LogEntry("warning", "PRUNE wird in v0.5.0 vom borgmatic-Config gesteuert (kommt in 0.5.x)")],
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
            r = worker.run_restore(archive, lambda m, lvl="info": stream(lvl, m))
            for log in r.logs:
                client.report_job(job.job_id, "running", logs=[log])
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
            )

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
