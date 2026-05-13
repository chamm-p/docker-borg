FROM ghcr.io/borgmatic-collective/borgmatic:latest

# rclone for WebDAV targets + sshpass for one-shot key install
# fuse3 is already in the base image
# postgresql{14..17}-client side-by-side so we can pick the matching pg_dump/
# pg_restore based on the target server's major version — pg_restore 17 sends
# SET transaction_timeout to PG <17 servers and crashes.
RUN apk add --no-cache rclone sshpass openssh-client tzdata \
        postgresql15-client postgresql16-client postgresql17-client

COPY --chmod=755 entrypoint.sh /usr/local/bin/dborg-worker
COPY --chmod=755 pg-version-shim.sh /usr/local/bin/dborg-pg-shim

ENTRYPOINT ["dborg-worker"]
