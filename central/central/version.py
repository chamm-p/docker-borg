__version__ = "0.5.17"
# v0.5.17 — Hotfix: DB-Replay funktioniert jetzt gegen Postgres <17 Server
# (pg_restore 17 schickt SET transaction_timeout an PG 16, kennt der nicht).
# Worker hat jetzt postgresql15/16/17-client side-by-side, dborg-pg-shim wählt
# zur Laufzeit den passenden Client auf Basis SHOW server_version_num.
# Lokal E2E getestet: PG 16 Server → Shim wählt pg_restore 16.13 → 1000 Rows
# konsistent zurückgespielt.
APP_VERSION = __version__
