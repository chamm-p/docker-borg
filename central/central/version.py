__version__ = "0.7.8"
# v0.7.0 — WebDAV entfernt, SCP/SSH ist der Weg:
# - WebDAV-Backup-Ziel komplett raus (UI-Option, Worker-rclone-Mount, fuse-Caps,
#   Connection-Check). borg auf einem rclone-Mount ist nachweislich unzuverlässig
#   (Repo landet unvollständig → "Repository has no manifest"), lokal mit zwei
#   WebDAV-Servern reproduziert. Robustes Remote-Ziel = borg über SSH (borg serve
#   auf dem NAS), E2E getestet: Backup über mehrere Läufe + Prune + Restore sauber.
# - Worker-Image: rclone entfernt (war nur für WebDAV).
# v0.6.0 — Stabilitäts-Block (Items 1-4 aus dem Praxis-Feedback):
# - PRUNE+RETENTION endlich scharf: agent-weite Aufbewahrung (einfach 'letzte N'
#   ODER erweitert täglich/wöchentlich/monatlich). Jeder Backup-Lauf macht
#   create→prune→compact. match_archives scoped pro Compose-Projekt, sodass
#   Projekte sich nicht gegenseitig wegprunen. (Vorher: NIE geprunt → Platte voll.)
# - DB dump-only: bei aktivem DB-Hook wird das rohe Daten-Verzeichnis automatisch
#   aus dem Backup ausgeschlossen (kein pg_wal-Müll mehr, massiv kleiner). Discovery
#   ermittelt das Daten-Verzeichnis und liefert raw_exclude.
# - Resource-Discipline: Worker läuft mit ionice idle + nice 19 (sprengt das
#   System nicht mehr), RAM-Limit (default 1024MB) + optionales CPU-Limit, beides
#   pro Agent im UI einstellbar.
# - Größenschätzung pro Agent + pro Container im Container-Tab (DB-Raw rausgerechnet).
# - Scheduler nutzt jetzt dieselbe Param-Logik wie manuelle Backups (vorher
#   ignorierten geplante Backups Excludes UND DB-Hooks komplett).
APP_VERSION = __version__
# v0.5.20 — Stale-State-Cleanup im UI:
# - Connection-Fehler-Banner blendet sich aus, sobald ein neueres erfolg-
#   reiches backup/verify/archive_list existiert (Banner hängt nicht mehr
#   ewig nach altem SCP-Timeout)
# - Re-Registration und Backup-Target-Änderung löschen last_connection_*
#   sofort — frischer Zustand bei jeder Neu-Initialisierung
# - Traffic-Light: nur backup/verify/archive_list zählen (kein SCP_INSTALL_KEY
#   Failure hält die Ampel gelb), und nur Failures der letzten 24h
# - last_verify-Widget: nur recent (< 30 Tage), sonst "Noch keine Prüfung"
# - Jobs-Tab: per-Zeile Löschen-Button + Bulk "Fehlgeschlagene löschen" /
#   "Alle erledigten löschen" pro Agent
# v0.5.19 — UX-Cleanup nach echter Nutzung:
# - Worker-Log-Klassifizierung: borgmatic mit -v 2 loggt subprocess-commands
#   im Format "<repo>: ENV=*** ENV=*** borg <subcmd> --critical --log-json ..."
#   Die naive "error in line"-Heuristik hatte das fälschlich als ERROR gestempelt
#   (--critical, --log-json triggern). Jetzt: command-dump-Pattern wird vorab
#   als info erkannt, sonst Wortgrenzen-Match für error/critical/fatal/warning.
# - last_connection_error wird nach erfolgreichem backup/verify/archive_list
#   automatisch gecleart. Veraltete "Verbindung fehlgeschlagen"-Banner gehen weg
#   sobald ein Backup gegen das Ziel sauber durchgelaufen ist.
# - "Mount fehlt"-Badge umformuliert. Steht eigentlich für "Compose-Dir nicht im
#   Agent gemountet" — Backup läuft trotzdem via --volumes-from. Neue Badges:
#   "Compose-Dir gemountet", "nur Volumes", "nichts zu sichern". Tooltip
#   erklärt's. Banner im Container-Tab analog entschärft (info statt warning).
APP_VERSION = __version__
