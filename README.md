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

## Quick Start

### 1. Central Server starten

```bash
cd central
cp ../env.example .env
# DBORG_REGISTRATION_TOKEN und DBORG_SECRET_KEY anpassen
docker compose up -d
```

Web UI: http://localhost:8080

### 2. Agent auf jedem Host starten

```bash
cd agent
cp ../env.example .env
# Anpassen: DBORG_AGENT_NAME, DBORG_CENTRAL_URL, DBORG_REGISTRATION_TOKEN, etc.
docker compose up -d
```

### 3. Oder beides zusammen (gleicher Host)

```bash
cp .env.example .env
# Anpassen
docker compose -f docker-compose.prod.yml up -d
```

## Konfiguration

Alle Einstellungen via Umgebungsvariablen (Prefix `DBORG_`):

### Agent

| Variable | Default | Beschreibung |
|---|---|---|
| `DBORG_AGENT_NAME` | hostname | Name des Agents |
| `DBORG_CENTRAL_URL` | `http://central:8080` | URL des Central Servers |
| `DBORG_REGISTRATION_TOKEN` | - | Token für die Registrierung |
| `DBORG_BORG_REPO` | `/backups` | Borg Repository Pfad/URL |
| `DBORG_BORG_PASSPHRASE` | - | Borg Verschlüsselungspassphrase |
| `DBORG_DOCKER_HOST_DIR` | `/host/docker` | Gemountetes Docker-Verzeichnis |
| `DBORG_POLL_INTERVAL` | `30` | Poll-Intervall in Sekunden |

### Central

| Variable | Default | Beschreibung |
|---|---|---|
| `DBORG_REGISTRATION_TOKEN` | - | Token für Agent-Registrierung |
| `DBORG_SECRET_KEY` | - | Geheimer Schlüssel |
| `DBORG_LOG_LEVEL` | `INFO` | Log-Level |

## Agent CLI

Der Agent kann auch standalone (ohne Central) genutzt werden:

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

## Backup Target

Das Target wird pro Agent konfiguriert und ist unabhängig vom Central Server:

- **Lokal:** `/backups` (als Volume gemountet)
- **SFTP:** `ssh://user@backup-server/path/to/repo`
- **WebDAV:** Verzeichnis als FUSE mounten, dann lokalen Pfad verwenden
- **Borg Server:** `ssh://borg@server/./repo`

## Was wird gesichert?

Pro Compose-Projekt werden die Root-Dateien gesichert:
- `docker-compose*.yml` / `compose*.yml`
- `.env`, `.env.*`
- `Dockerfile`, `Dockerfile.*`
- `.dockerignore`
- `*.conf`, `*.toml`, `*.ini`

Volume-Daten werden bewusst **nicht** gesichert (diese werden separat über die Container selbst gehandhabt).

## Multi-Arch

Beide Images unterstützen `linux/amd64` und `linux/arm64`:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t dborg-central ./central
docker buildx build --platform linux/amd64,linux/arm64 -t dborg-agent ./agent
```
