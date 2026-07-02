"""E-Mail-Benachrichtigung über Job-Ergebnisse.

Konfiguration komplett über .env (DBORG_SMTP_* + DBORG_NOTIFY):
  notify=failure  → nur fehlgeschlagene Jobs (Default)
  notify=always   → jeder abgeschlossene Job (Erfolg + Fehler)
  notify=off      → aus

Versand läuft in einem Daemon-Thread, damit der Heartbeat-/Job-Request des
Agents nie auf einen (ggf. hängenden) SMTP-Server warten muss.
"""
from __future__ import annotations

import logging
import smtplib
import threading
from email.message import EmailMessage

from ..config import settings

logger = logging.getLogger(__name__)

# Job-Typen, über die benachrichtigt wird — bewusst ohne archive_list/scp_test
# u.ä. (Rauschen).
NOTIFY_JOB_TYPES = ("backup", "restore", "db_restore", "verify", "prune")


def enabled() -> bool:
    return bool(settings.smtp_host and settings.smtp_to) and settings.notify.lower() != "off"


def should_notify(job_type: str, status: str) -> bool:
    if not enabled() or job_type not in NOTIFY_JOB_TYPES:
        return False
    mode = (settings.notify or "failure").lower()
    if mode == "always":
        return status in ("success", "failed")
    if mode == "failure":
        return status == "failed"
    return False


def send_job_mail(agent_hostname: str, job_id: int, job_type: str, status: str,
                  log_tail: list[str] | None = None) -> None:
    """Baut die Mail und verschickt sie asynchron (Daemon-Thread)."""
    icon = "OK" if status == "success" else "FEHLER"
    subject = f"[docker-borg] {icon}: {job_type} auf {agent_hostname} — {status}"
    lines = [
        f"Agent:  {agent_hostname}",
        f"Job:    #{job_id} ({job_type})",
        f"Status: {status}",
        "",
    ]
    if log_tail:
        lines.append("Letzte Log-Zeilen:")
        lines.extend(f"  {ln}" for ln in log_tail)
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = settings.smtp_to
    msg.set_content(body)

    threading.Thread(target=_send, args=(msg,), daemon=True, name="notify-mail").start()


def _send(msg: EmailMessage) -> None:
    try:
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        try:
            if settings.smtp_tls and not settings.smtp_ssl:
                server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
            logger.info("Benachrichtigung an %s verschickt (%s)", msg["To"], msg["Subject"])
        finally:
            server.quit()
    except Exception as e:  # noqa: BLE001
        logger.warning("E-Mail-Versand fehlgeschlagen: %s", e)
