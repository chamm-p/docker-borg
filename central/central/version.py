__version__ = "0.5.16"
# v0.5.16 changes:
# - Recovery-Tab: Auto-Refresh nach Archive-List Job (kein manueller Browser-Reload)
# - Discovery v2: Compose-Dir Top-Level-Inhalt mit Größen statt File-Globs;
#   externe Mounts mit Größe (über docker exec im Target-Container ermittelt)
# - DB-Auto-Detection: Postgres/MySQL/MariaDB/MongoDB Container per Image+ENV
#   erkannt; DB-Hooks werden beim Heartbeat automatisch angelegt
# - Auto-Exclude von DB-Storage-Pfaden entfernt — Raw-Files bleiben drin als
#   Fallback neben dem konsistenten Dump
# - DB-Replay-Flow: Button "DB-Dumps zurückspielen" pro Archive (im Recovery-Tab)
#   → borgmatic restore --archive X gegen die laufenden DB-Container
APP_VERSION = __version__
