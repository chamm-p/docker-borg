__version__ = "0.5.19"
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
