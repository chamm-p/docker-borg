#!/bin/sh
# Wrapper that detects the target Postgres server major version and execs
# the matching pg_dump/pg_restore binary. Without this, pg_restore 17 against
# a PG <17 server crashes on `SET transaction_timeout = 0`.
#
# Usage: dborg-pg-shim pg_dump|pg_restore <borgmatic-passed-args>
# We parse --host/-h, --port/-p, --username/-U, --dbname from the args to
# query SHOW server_version_num via psql, then exec the right binary.

set -e

CMD="${1:?usage: dborg-pg-shim pg_dump|pg_restore ARGS}"
shift

# Parse host/port/user from args we forward
HOST=""
PORT=""
USER=""
DBNAME=""
for arg in "$@"; do
    case "$prev" in
        --host|-h) HOST="$arg" ;;
        --port|-p) PORT="$arg" ;;
        --username|-U) USER="$arg" ;;
        --dbname|-d) DBNAME="$arg" ;;
    esac
    case "$arg" in
        --host=*) HOST="${arg#--host=}" ;;
        --port=*) PORT="${arg#--port=}" ;;
        --username=*) USER="${arg#--username=}" ;;
        --dbname=*) DBNAME="${arg#--dbname=}" ;;
    esac
    prev="$arg"
done

# Determine server major version. PGPASSWORD is in env (set by borgmatic).
# Der Verbindungstest ist zugleich Diagnose: schlägt er fehl, wird pg_dump auch
# scheitern (0 Bytes → borgmatic crasht mit kryptischem NoneType-Traceback).
# Deshalb den psql-Fehler NICHT verschlucken, sondern klar nach stderr melden.
MAJOR=""
if [ -n "$HOST" ] && [ -n "$USER" ]; then
    VER_NUM=$(PGCONNECT_TIMEOUT=5 psql -h "$HOST" ${PORT:+-p $PORT} -U "$USER" \
        -d "${DBNAME:-postgres}" -t -A -c "SHOW server_version_num;" 2>/tmp/dborg-pgconn.err) || true
    if [ -n "$VER_NUM" ]; then
        # server_version_num: e.g. 160013 → 16, 170002 → 17
        MAJOR=$(echo "$VER_NUM" | awk '{print int($1/10000)}')
    else
        echo "dborg-pg-shim: DB-VERBINDUNG FEHLGESCHLAGEN zu ${USER}@${HOST}${PORT:+:$PORT}/${DBNAME:-postgres} — der Dump wird scheitern. Grund:" >&2
        sed 's/^/dborg-pg-shim:   /' /tmp/dborg-pgconn.err >&2 2>/dev/null || true
        echo "dborg-pg-shim: Prüfe DB-Hook (Host/Container-Name im Compose-Netz, User, Passwort, DB-Name) im Central-UI." >&2
    fi
fi

# Pick binary dynamisch — Pfad aus der erkannten Major-Version. Damit ist der
# Shim unabhängig davon, welche postgresqlNN-client genau im Image liegen
# (Alpine wechselt die mitgelieferten Versionen). Ist der passende Client nicht
# da, fällt er auf den Default-Client zurück.
BIN=""
if [ -n "$MAJOR" ] && [ -x "/usr/libexec/postgresql$MAJOR/$CMD" ]; then
    BIN="/usr/libexec/postgresql$MAJOR/$CMD"
fi

if [ -z "$BIN" ]; then
    echo "dborg-pg-shim: PG major='$MAJOR' nicht als Client installiert (host=$HOST) — nutze Default $CMD" >&2
    BIN="/usr/bin/$CMD"
fi

echo "dborg-pg-shim: using $BIN (server PG $MAJOR)" >&2
exec "$BIN" "$@"
