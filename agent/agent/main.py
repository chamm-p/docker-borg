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
from .discovery import discover_containers, _host_path_to_local, _collect_root_files
from .borg import backup_all, create_backup, list_archives, prune, extract_archive, init_repo, verify_repo, cancel_active
from .client import CentralClient
from .models import JobType
from .webdav import ensure_mounted, unmount

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
    if not ensure_mounted():
        print("ERROR: Could not mount backup target")
        sys.exit(1)
    init_repo()
    containers = discover_containers()
    if args.project:
        containers = [c for c in containers if c.compose_project == args.project]
        if not containers:
            print(f"Project '{args.project}' not found")
            sys.exit(1)

    results = backup_all(containers)
    for r in results:
        status = "OK" if r.success else "FAILED"
        name = r.job_result.archive_name or "n/a"
        print(f"  [{status}] {name} ({r.job_result.nfiles} files, {r.job_result.size_bytes} bytes)")
        for log in r.logs:
            if log.level == "error":
                print(f"    ERROR: {log.message}")


def cmd_list(args):
    result = list_archives()
    for log in result.logs:
        print(f"  {log.message}")


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
                    ok, detail = ensure_mounted()
                    if not ok:
                        client.report_job(
                            job.job_id,
                            "failed",
                            logs=[LogEntry("error", detail or "Backup-Ziel konnte nicht eingehängt werden")],
                        )
                        continue
                    if detail:
                        client.report_job(job.job_id, "running", logs=[LogEntry("info", detail)])

                    cancel_watchdog_stop = threading.Event()

                    def watchdog():
                        while not cancel_watchdog_stop.wait(2):
                            if job.job_id in client.cancelled_jobs:
                                logger.warning("Cancellation signal for job %d — killing active borg process", job.job_id)
                                cancel_active()
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

    worker = threading.Thread(target=worker_loop, daemon=True, name="job-worker")
    worker.start()

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

    worker.join(timeout=10)
    discovery.join(timeout=10)
    unmount()
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
                    local = _host_path_to_local(manual)
                    if not local.is_dir():
                        stream("error", f"Manueller Pfad {manual} im Agent nicht zugreifbar (Project {c.compose_project})")
                        all_success = False
                        continue
                if not c.compose_dir:
                    stream("warning", f"{c.compose_project}: kein Pfad gesetzt, übersprungen")
                    continue
                local = _host_path_to_local(c.compose_dir)
                if not local.is_dir():
                    stream("error", f"{c.compose_project}: Verzeichnis {local} nicht zugreifbar im Agent")
                    all_success = False
                    continue
                stream("info", f"→ {c.compose_project} (gesamtes Verzeichnis {c.compose_dir})")
                r = create_backup(c)
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
            keep = job.params.get("keep", {})
            r = prune(
                keep_daily=keep.get("daily", 7),
                keep_weekly=keep.get("weekly", 4),
                keep_monthly=keep.get("monthly", 6),
            )
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
                logs=r.logs,
            )

        elif job.job_type == JobType.LIST:
            r = list_archives()
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
                result=asdict(r.job_result),
                logs=r.logs,
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
            verify_data = bool((job.params or {}).get("verify_data", False))
            r = verify_repo(verify_data=verify_data)
            for log in r.logs:
                client.report_job(job.job_id, "running", logs=[log])
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
                result=asdict(r.job_result),
            )

        elif job.job_type == JobType.RESTORE:
            archive = job.params.get("archive", "")
            target = job.params.get("target_dir", "/tmp/restore")
            r = extract_archive(archive, target)
            client.report_job(
                job.job_id,
                "success" if r.success else "failed",
                logs=r.logs,
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
