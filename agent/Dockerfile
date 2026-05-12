FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends borgbackup openssh-client sshpass tzdata openssl fuse3 ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL "https://downloads.rclone.org/rclone-current-linux-${ARCH}.zip" -o /tmp/rclone.zip \
    && unzip -j /tmp/rclone.zip "*/rclone" -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/rclone \
    && rm /tmp/rclone.zip \
    && mkdir -p /mnt/webdav \
    && echo "user_allow_other" >> /etc/fuse.conf

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/

RUN mkdir -p /data

# Reduce glibc malloc fragmentation — keeps RSS low for long-running daemon
ENV MALLOC_ARENA_MAX=2
# Don't buffer stdout/stderr — faster log visibility, no orphan buffers
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "agent.main"]
CMD ["daemon"]
