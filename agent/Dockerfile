FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends borgbackup openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/

RUN mkdir -p /data

ENTRYPOINT ["python", "-m", "agent.main"]
CMD ["daemon"]
