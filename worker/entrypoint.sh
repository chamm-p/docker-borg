#!/bin/sh
# docker-borg worker entrypoint.
# Configuration is provided by the agent via the shared agent-data volume:
#   DBORG_CONFIG_PATH    — path to the borgmatic config file (default /etc/borgmatic.d/config.yaml)
#   BORG_CACHE_DIR       — persistent borg dedup cache
#   BORG_CONFIG_DIR      — persistent borg keys
#   DBORG_WEBDAV_*       — optional WebDAV mount params
set -e

MODE="${1:-create}"
shift || true

CFG="${DBORG_CONFIG_PATH:-/etc/borgmatic.d/config.yaml}"

# Optional WebDAV mount preparation
if [ -n "$DBORG_WEBDAV_URL" ]; then
    mkdir -p /mnt/webdav
    RCLONE_CONFIG=/tmp/rclone.conf
    OBSCURED_PW=$(rclone obscure "$DBORG_WEBDAV_PASSWORD")
    cat >"$RCLONE_CONFIG" <<EOF
[webdav]
type = webdav
url = $DBORG_WEBDAV_URL
vendor = other
user = $DBORG_WEBDAV_USER
pass = $OBSCURED_PW
EOF
    EXTRA=""
    if [ "$DBORG_WEBDAV_VERIFY_SSL" = "false" ]; then
        EXTRA="--no-check-certificate"
    fi
    echo "Preflight WebDAV..."
    if ! rclone --config "$RCLONE_CONFIG" $EXTRA lsd webdav: >/dev/null 2>&1; then
        echo "ERROR: WebDAV preflight failed (auth, network, or cert)" >&2
        rclone --config "$RCLONE_CONFIG" $EXTRA lsd webdav: 1>&2 || true
        exit 1
    fi
    echo "Mounting WebDAV..."
    rclone --config "$RCLONE_CONFIG" $EXTRA mount webdav: /mnt/webdav \
        --vfs-cache-mode full --vfs-cache-max-size 2G --vfs-cache-max-age 24h \
        --vfs-write-back 10s --dir-cache-time 1m \
        --retries 5 --low-level-retries 10 --timeout 60s --daemon
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if mountpoint -q /mnt/webdav; then break; fi
        sleep 0.5
    done
    if ! mountpoint -q /mnt/webdav; then
        echo "ERROR: WebDAV mount did not become ready" >&2
        exit 1
    fi
    echo "WebDAV mounted."
fi

case "$MODE" in
    create)
        if [ -n "$BORG_REPO" ]; then
            # Stale Cache/Repo-Locks von abgebrochenen Vorgänger-Jobs lösen
            borg break-lock 2>/dev/null || true
            echo "Auto-init: borg init --encryption=repokey-blake2 $BORG_REPO"
            # `set -e` darf hier nicht abbrechen wenn borg init failed
            set +e
            init_out=$(borg init --encryption=repokey-blake2 2>&1)
            init_rc=$?
            set -e
            if [ $init_rc -ne 0 ]; then
                if echo "$init_out" | grep -qi "already exists\|already initialized"; then
                    echo "Repo bereits initialisiert (OK)."
                else
                    echo "----- borg init Ausgabe (rc=$init_rc) -----"
                    echo "$init_out"
                    echo "----- Ende borg init Ausgabe -----"
                    # Nicht abbrechen — borgmatic create läuft sowieso und meldet eigene Diagnose
                fi
            else
                echo "Repository initialisiert."
            fi
        fi
        exec borgmatic --config "$CFG" --stats -v 1 --progress create "$@"
        ;;
    check)   exec borgmatic --config "$CFG" --stats -v 2 --progress check "$@" ;;
    prune)   exec borgmatic --config "$CFG" --stats -v 1 prune  "$@" ;;
    list)    exec borgmatic --config "$CFG" list "$@" ;;
    rinfo)   exec borgmatic --config "$CFG" rinfo "$@" ;;
    restore) exec borgmatic --config "$CFG" restore "$@" ;;
    shell)   exec /bin/bash ;;
    *)       echo "Unknown mode: $MODE" >&2 ; exit 2 ;;
esac
