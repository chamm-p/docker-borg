# docker-borg — Lokales Test-Deployment (ARM64 Linux, ohne GHCR)

Ziel: Central (Backend) auf einem normalen ARM64-Linux, Agents auf Linux-
Maschinen — **alle Images lokal gebaut**, kein GitHub/GHCR-Pull. QNAP kommt
später.

Konfiguration läuft ausschließlich über `.env` (aus `.env.example` kopiert).
Die `docker-compose.yml` wird **nie** manuell editiert.

Drei Images:
| Image | Wo gebaut | Wie gestartet |
|-------|-----------|---------------|
| `dborg-central:local` | Central-Host | per compose |
| `dborg-agent:local`   | jeder Agent-Host | per compose |
| `dborg-worker:local`  | jeder Agent-Host | vom Agent zur Laufzeit (Docker-Socket) |

---

## 0. Voraussetzungen (jeder Host)

ARM64 Linux mit Docker Engine + Compose-Plugin:
```bash
docker --version          # >= 24
docker compose version    # v2
```

---

## 1. Repo holen (jeder Host)

Test läuft auf Branch `local-deploy` (triggert **keinen** GHCR-Build — der
baut nur auf `main`):
```bash
git clone -b local-deploy https://github.com/chamm-p/docker-borg.git
cd docker-borg
```

---

## 2. Central / Backend (ARM64-Host)

```bash
cd deploy/central
cp .env.example .env
```
`.env` ausfüllen:
```bash
openssl rand -hex 24     # → DBORG_REGISTRATION_TOKEN (auch für Agents!)
openssl rand -hex 24     # → DBORG_SECRET_KEY
# DBORG_ADMIN_PASSWORD frei wählen
# DBORG_CENTRAL_PORT bei Bedarf anpassen (Default 8089)
```
Bauen + starten:
```bash
docker compose up -d --build
docker compose logs -f
```
Migration muss durchlaufen: `Running upgrade ... -> 0012, discovery v2 ...`

Erreichbar?
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8089/   # 200 oder 401
```
UI: `http://<CENTRAL-IP>:8089` — Login mit `DBORG_ADMIN_PASSWORD`.
Central-IP, Port und Registration-Token für die Agents notieren.

---

## 3. Agent (jede zu sichernde Linux-Maschine)

```bash
cd deploy/agent
cp .env.example .env
```
`.env` ausfüllen:
- `DBORG_AGENT_NAME` — eindeutiger Name (z.B. `vzulu2`)
- `DBORG_CENTRAL_URL` — `http://<CENTRAL-IP>:8089`
- `DBORG_REGISTRATION_TOKEN` — **derselbe** wie bei Central
- `DBORG_HOST_BASE_DIR` — Host-Pfad deiner Compose-Projekte (z.B. `/sync/Docker`)

Beide Images bauen (Agent + Worker), dann nur den Agent starten:
```bash
docker compose --profile build build
docker compose up -d
docker compose logs -f
```
Erwartetes Log:
- `Konnte Worker-Image nicht pullen (...) — nutze lokal gecachtes` → **harmlos**
- `Registered with central as '<name>'`
- `Discovered N compose projects`

In der UI: Agent erscheint → Backup-Ziel + Verschlüsselung setzen → Container
auswählen → Test-Backup.

---

## 4. Update einspielen (nach Code-Änderung)

```bash
git pull
# Central:
cd deploy/central && docker compose up -d --build
# Agent (--profile build nur nötig, wenn Worker-Code geändert wurde):
cd deploy/agent && docker compose --profile build build && docker compose up -d
```

`.env` bleibt unberührt (gitignored), `git pull` fasst sie nicht an.

---

## 5. Später: QNAP-Agent + GHCR

Wenn das lokale Setup stabil läuft, mergen wir nach `main` → GHCR baut die
Multi-Arch-Images. Auf `main` bleibt die GHCR-Variante der Compose-Dateien.
