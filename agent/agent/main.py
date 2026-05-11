from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import asdict

from .config import settings
from .discovery import discover_containers, _host_path_to_local, _collect_root_files
from .borg import backup_all, create_backup, list_archives, prune, extract_archive, init_repo
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

    client = CentralClient()

    if not client.is_registered:
        logger.info("Not registered, attempting registration...")
        while not client.is_registered and not _shutdown:
            if client.register():
                break
            logger.info("Retrying registration in %ds...", settings.poll_interval)
            time.sleep(settings.poll_interval)

    logger.info("Agent daemon started (poll interval: %ds)", settings.poll_interval)

    while not _shutdown:
        try:
            containers = discover_containers()
            client.heartbeat(containers)

            jobs = client.poll_jobs()
            for job in jobs:
                if ensure_mounted():
                    _execute_job(job, containers, client)
                else:
                    logger.error("Backup target not available, skipping job %d", job.job_id)
                    from .models import LogEntry
                    client.report_job(job.job_id, "failed", logs=[LogEntry("error", "Backup target mount failed")])

        except Exception:
            logger.exception("Error in poll loop")

        for _ in range(settings.poll_interval):
            if _shutdown:
                break
            time.sleep(1)

    unmount()
    client.close()
    logger.info("Agent daemon stopped")


def _execute_job(job, containers, client: CentralClient):
    logger.info("Executing job %d: %s", job.job_id, job.job_type)
    client.report_job(job.job_id, "running")

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

            all_logs = []
            all_success = True
            for c in targets:
                manual = overrides.get(c.compose_project)
                if manual:
                    c.compose_dir = manual
                    local = _host_path_to_local(manual)
                    if local.is_dir():
                        from .discovery import _get_volume_mount_dirs  # not used; keep simple
                        c.root_files = _collect_root_files(local, set())
                    else:
                        from .models import LogEntry
                        all_logs.append(LogEntry("error", f"Manueller Pfad {manual} im Agent nicht zugreifbar (Project {c.compose_project})"))
                        all_success = False
                        continue
                if not c.compose_dir or not c.root_files:
                    continue
                r = create_backup(c)
                all_logs.extend(r.logs)
                if not r.success:
                    all_success = False

            status = "success" if all_success else "failed"
            client.report_job(job.job_id, status, logs=all_logs)

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
