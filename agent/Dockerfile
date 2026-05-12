FROM python:3.12-slim

# Schmaler Agent: orchestriert nur, macht selbst kein Backup mehr.
# borg/rclone/ssh/fuse leben jetzt im worker-Image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata openssh-client openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/

RUN mkdir -p /data

ENV MALLOC_ARENA_MAX=2
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "agent.main"]
CMD ["daemon"]
