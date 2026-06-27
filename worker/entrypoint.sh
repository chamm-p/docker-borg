#!/bin/sh
# docker-borg worker entrypoint.
# Configuration is provided by the agent via the shared agent-data volume:
#   DBORG_CONFIG_PATH    — path to the borgmatic config file (default /etc/borgmatic.d/config.yaml)
#   BORG_CACHE_DIR       — persistent borg dedup cache
#   BORG_CONFIG_DIR      — persistent borg keys
# Backup-Ziele: SSH/SCP (borg-nativ) oder lokales Volume. WebDAV wurde
# entfernt — borg auf einem rclone-Mount ist nicht zuverlässig (Repo landet
# unvollständig). Robustes Remote-Ziel ist borg über SSH (borg serve auf NAS).
set -e

MODE="${1:-create}"
shift || true

CFG="${DBORG_CONFIG_PATH:-/etc/borgmatic.d/config.yaml}"

# Niedrige CPU/IO-Priorität, damit das Backup laufende Workloads nicht
# ausbremst (ML-Inferenz etc.). DBORG_NICE=1 → ionice idle + nice 19.
NICE=""
if [ "$DBORG_NICE" = "1" ]; then
    NICE="ionice -c 3 nice -n 19"
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
        # Retention: wenn DBORG_PRUNE=1, nach dem create direkt prune + compact
        # (prune entfernt Archive nach keep_*-Regeln, compact gibt den Platz im
        # Repo tatsächlich frei).
        if [ "$DBORG_PRUNE" = "1" ]; then
            exec $NICE borgmatic --config "$CFG" --stats -v 1 --progress create prune compact "$@"
        else
            exec $NICE borgmatic --config "$CFG" --stats -v 1 --progress create "$@"
        fi
        ;;
    check)   exec $NICE borgmatic --config "$CFG" --stats -v 2 --progress check "$@" ;;
    prune)   exec $NICE borgmatic --config "$CFG" --stats -v 1 prune compact "$@" ;;
    list)    exec borgmatic --config "$CFG" list "$@" ;;
    rinfo)   exec borgmatic --config "$CFG" rinfo "$@" ;;
    restore) exec borgmatic --config "$CFG" restore "$@" ;;
    extract)
        # Direktes borg extract. Args: <archive> [subpath]
        # Zielverzeichnis via DBORG_RESTORE_DIR (default /restore).
        ARCHIVE=$1
        SUBPATH=$2
        TARGET="${DBORG_RESTORE_DIR:-/restore}"
        if [ -z "$ARCHIVE" ]; then
            echo "extract: kein Archiv-Name angegeben" >&2
            exit 2
        fi
        mkdir -p "$TARGET"
        cd "$TARGET"
        echo "Extrahiere ${ARCHIVE} nach ${TARGET}${SUBPATH:+ (Pfad: $SUBPATH)}"
        if [ -n "$SUBPATH" ]; then
            exec borg extract --progress --list "$BORG_REPO::$ARCHIVE" "$SUBPATH"
        else
            exec borg extract --progress --list "$BORG_REPO::$ARCHIVE"
        fi
        ;;
    extract-structured)
        # Extrahiert das ganze Archiv und legt es AUFGERÄUMT ab, damit man
        # damit arbeiten kann, statt die rohe borg-interne Struktur:
        #   compose/    ← das Compose-Verzeichnis (mnt/compose)
        #   volumes/    ← die Docker-Volumes (an ihrem Mount-Ziel, z.B. var/lib/…)
        #   databases/  ← die DB-Dumps (borgmatic/)
        ARCHIVE=$1
        TARGET="${DBORG_RESTORE_DIR:-/restore}"
        if [ -z "$ARCHIVE" ]; then echo "extract-structured: kein Archiv" >&2; exit 2; fi
        TMP="$TARGET/.extract.$$"
        mkdir -p "$TMP"; cd "$TMP"
        echo "Extrahiere ${ARCHIVE} (strukturiert) nach ${TARGET}…"
        set +e
        borg extract --progress --list "$BORG_REPO::$ARCHIVE"
        rc=$?
        set -e
        if [ $rc -ne 0 ]; then rm -rf "$TMP"; echo "extract fehlgeschlagen (rc=$rc)" >&2; exit $rc; fi
        # Compose-Verzeichnis (mit dotfiles wie .env) → compose/
        if [ -d "$TMP/mnt/compose" ]; then rm -rf "$TARGET/compose"; mv "$TMP/mnt/compose" "$TARGET/compose"; fi
        # DB-Dumps → databases/
        if [ -d "$TMP/borgmatic" ]; then rm -rf "$TARGET/databases"; mv "$TMP/borgmatic" "$TARGET/databases"; fi
        # Alles Übrige (Volumes an ihren Mount-Zielen) → volumes/
        mkdir -p "$TARGET/volumes"
        for d in "$TMP"/* "$TMP"/.[!.]*; do
            [ -e "$d" ] || continue
            b=$(basename "$d")
            case "$b" in mnt|borgmatic) continue ;; esac
            rm -rf "$TARGET/volumes/$b"; mv "$d" "$TARGET/volumes/"
        done
        rm -rf "$TMP"
        echo "Fertig. Struktur unter ${TARGET}: compose/  volumes/  databases/"
        ;;
    shell)   exec /bin/bash ;;
    *)       echo "Unknown mode: $MODE" >&2 ; exit 2 ;;
esac
