# Base auf festen Digest gepinnt (war :latest) — sonst floatet Alpine unter uns
# weg und apk verliert pg-client-Pakete (z.B. postgresql15-client ab Alpine 3.24).
# Dieser Digest = borgmatic auf Alpine 3.24, bringt pg18 mit; wir ergänzen 16+17.
# Zum Anheben: neuen Digest via `docker inspect --format '{{index .RepoDigests 0}}'
# ghcr.io/borgmatic-collective/borgmatic:latest` holen und Client-Liste prüfen.
FROM ghcr.io/borgmatic-collective/borgmatic@sha256:58b0fe322f46529412924f712773f40a776c956175e30df513b3052dd0bea94d

# rclone (WebDAV) + sshpass (Key-Install) — fuse3 ist im Base.
# postgresql16/17/18-client side-by-side, damit der Shim den zum Server passenden
# pg_dump/pg_restore wählt (pg_restore N sendet SET transaction_timeout an
# PG <N und crasht). pg18 ist im Base, 16+17 ergänzen wir.
RUN apk add --no-cache rclone sshpass openssh-client tzdata \
        postgresql16-client postgresql17-client

COPY --chmod=755 entrypoint.sh /usr/local/bin/dborg-worker
COPY --chmod=755 pg-version-shim.sh /usr/local/bin/dborg-pg-shim

ENTRYPOINT ["dborg-worker"]
