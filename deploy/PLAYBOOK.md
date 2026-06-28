# docker-borg — Deployment (GHCR-Images, Pull)

Produktiv-Setup mit vorgebauten Multi-Arch-Images von GHCR (amd64 + arm64 →
läuft auf normalen Linux-Servern UND QNAP/Synology-arm64). Kein git, kein
lokales Bauen nötig — nur die Compose-Datei + `.env` + `docker compose pull`.

Voraussetzung je Host: Docker + Compose-Plugin, und der ausführende User hat
Docker-Zugriff (in der `docker`-Gruppe).

---

## Central / Backend (einmal, z.B. auf dem QNAP oder einem Linux-Host)

```bash
mkdir -p ~/dborg-central && cd ~/dborg-central
curl -O https://raw.githubusercontent.com/chamm-p/docker-borg/main/deploy/central/docker-compose.yml
curl -O https://raw.githubusercontent.com/chamm-p/docker-borg/main/deploy/central/.env.example
cp .env.example .env
# .env ausfüllen:  Registration-Token, Admin-Passwort, Secret  (openssl rand -hex 24)
docker compose pull
docker compose up -d
```
UI: `http://<HOST>:8089` (Port via `DBORG_CENTRAL_PORT` in `.env`).
Registration-Token + Central-URL für die Agents notieren.

---

## Agent (auf jeder zu sichernden Maschine — mail, QNAP, …)

```bash
mkdir -p ~/dborg-agent && cd ~/dborg-agent
curl -O https://raw.githubusercontent.com/chamm-p/docker-borg/main/deploy/agent/docker-compose.yml
curl -O https://raw.githubusercontent.com/chamm-p/docker-borg/main/deploy/agent/.env.example
cp .env.example .env
# .env ausfüllen:
#   DBORG_AGENT_NAME       eindeutiger Name (z.B. mail)
#   DBORG_CENTRAL_URL      http://<CENTRAL-HOST>:8089
#   DBORG_REGISTRATION_TOKEN  derselbe wie bei Central
#   DBORG_HOST_BASE_DIR    Host-Pfad deiner Compose-Projekte (z.B. /home/chamm/docker)
docker compose pull
docker compose up -d
docker compose logs -f
```
Der Agent pullt das Worker-Image automatisch und meldet sich bei Central.
Erwartet: `Registered with central as '<name>'` + `Discovered N compose projects`.

---

## Update (neue Version)

```bash
docker compose pull && docker compose up -d
```
Die Version steht im Central-UI-Header. `.env` bleibt unangetastet.

---

## QNAP Container Station

Container Station kann GHCR-Images direkt ziehen. Entweder die obige
`docker compose`-Variante über SSH, oder in der GUI das Image
`ghcr.io/chamm-p/docker-borg-agent:latest` mit denselben Env-Variablen und
den Volumes (`/var/run/docker.sock`, `<HOST_BASE_DIR>:/host/docker:ro`,
`agent-data:/data`) anlegen.
