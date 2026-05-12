FROM ghcr.io/borgmatic-collective/borgmatic:latest

# rclone for WebDAV targets + sshpass for one-shot key install
# fuse3 is already in the base image
RUN apk add --no-cache rclone sshpass openssh-client tzdata

# Minimal helper script that supports our usage modes
COPY --chmod=755 entrypoint.sh /usr/local/bin/dborg-worker

ENTRYPOINT ["dborg-worker"]
