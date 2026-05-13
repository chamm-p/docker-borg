__version__ = "0.5.18"
# v0.5.18 — Mailcow + Self-Backup-Fixes
# - mariadb-/mysql-Hook bekommt Default tls=false (restore_tls=false),
#   weil mariadb-client 11.x TLS by default verlangt und mailcow's mariadb 10.x
#   gar kein TLS supportet. Lokal mit mariadb:10.5 verifiziert: Backup + Restore
#   gehen jetzt, 100 Rows kommen sauber zurück.
# - Discovery skipped das eigene Compose-Projekt des Agents (Container-Label
#   com.docker.compose.project). Self-Backup von 1.95 kB Müll fällt weg.
APP_VERSION = __version__
