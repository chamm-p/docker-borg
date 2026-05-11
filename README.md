# docker-borg

Zentrales Backup-System für Docker-Container mit BorgBackup.

Sichert automatisch die Konfigurationsdateien (docker-compose.yml, .env, Dockerfile etc.) aller laufenden Container — nicht die Daten-Volumes.

## Architektur

```
Central Server (Web UI + API + Scheduler)
         |
    REST API (Agent pollt alle 30s)
         |
   +-----+-----+-----+
   |     |     |     |
 Agent Agent Agent Agent    (je Docker-Host)
   |     |     |     |
   v     v     v     v
      Backup Target (SFTP / WebDAV / Lokal)
```

## Deployment

Fertige Multi-Arch Images (AMD64 + ARM64) werden automatisch via GitHub Actions gebaut und auf GHCR veröffentlicht. Kein `git clone` oder Build nötig.

### 1. Central Server

`deploy/central/docker-compose.yml` herunterladen, Platzhalter anpassen, starten:

```yaml
services:
  central:
    image: ghcr.io/chamm-p/docker-borg-central:latest
    container_name: dborg-central
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - DBORG_REGISTRATION_TOKEN=<dein-token>
      - DBORG_ADMIN_PASSWORD=<dein-admin-passwort>
      - DBORG_SECRET_KEY=<dein-secret>
      - DBORG_LOG_LEVEL=INFO
    volumes:
      - central-data:/app/data

volumes:
  central-data:
```

Web UI: `http://<host>:8080` (Login: `admin` / dein Passwort)

### 2. Agent (pro Host)

`deploy/agent/docker-compose.yml` herunterladen, Platzhalter anpassen, starten:

```yaml
services:
  agent:
    image: ghcr.io/chamm-p/docker-borg-agent:latest
    container_name: dborg-agent
    restart: unless-stopped
    environment:
      - DBORG_AGENT_NAME=<hostname>
      - DBORG_CENTRAL_URL=http://<central-ip>:8080
      - DBORG_REGISTRATION_TOKEN=<gleiches-token-wie-central>
      - DBORG_DOCKER_HOST_DIR=/host/docker
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /pfad/zu/docker:/host/docker:ro
      - agent-data:/data
    command: ["daemon"]

volumes:
  agent-data:
```

### 3. Tokens generieren

```bash
openssl rand -base64 32   # REGISTRATION_TOKEN (gleich auf Central + Agent)
openssl rand -base64 16   # ADMIN_PASSWORD
openssl rand -base64 32   # SECRET_KEY
```

## Konfiguration

### Central (Umgebungsvariablen)

| Variable | Beschreibung |
|---|---|
| `DBORG_REGISTRATION_TOKEN` | Token für Agent-Registrierung |
| `DBORG_ADMIN_PASSWORD` | Admin-Passwort für Web UI |
| `DBORG_SECRET_KEY` | Interner Schlüssel |
| `DBORG_LOG_LEVEL` | Log-Level (default: `INFO`) |

### Agent (Umgebungsvariablen)

| Variable | Beschreibung |
|---|---|
| `DBORG_AGENT_NAME` | Name des Agents (in Web UI sichtbar) |
| `DBORG_CENTRAL_URL` | URL des Central Servers |
| `DBORG_REGISTRATION_TOKEN` | Token (muss mit Central übereinstimmen) |

### Backup-Ziel

Wird **zentral in der Web UI** pro Agent konfiguriert (nicht am Agent selbst):
- **SFTP:** `ssh://user@backup-server/path/to/repo`
- **Borg Server:** `ssh://borg@server/./repo`
- **Lokal/Mount:** Pfad zu einem gemounteten Verzeichnis (WebDAV, NFS, etc.)

## Was wird gesichert?

Pro Compose-Projekt werden die Root-Dateien gesichert:
- `docker-compose*.yml` / `compose*.yml`
- `.env`, `.env.*`
- `Dockerfile`, `Dockerfile.*`
- `.dockerignore`
- `*.conf`, `*.toml`, `*.ini`

Volume-Daten werden bewusst **nicht** gesichert (diese werden separat über die Container selbst gehandhabt).

## Agent CLI

Der Agent kann auch direkt im Container ausgeführt werden:

```bash
# Container erkennen
docker exec dborg-agent python -m agent.main discover

# Manuelles Backup aller Projekte
docker exec dborg-agent python -m agent.main backup

# Nur ein Projekt
docker exec dborg-agent python -m agent.main backup --project myapp

# Archive auflisten
docker exec dborg-agent python -m agent.main list
```
