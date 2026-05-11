FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends borgbackup openssh-client davfs2 tzdata openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /mnt/webdav \
    && echo "use_locks 0" >> /etc/davfs2/davfs2.conf

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/

RUN mkdir -p /data

ENTRYPOINT ["python", "-m", "agent.main"]
CMD ["daemon"]
