# docker-borg — Lokales Test-Deployment (ARM64 Linux, ohne GHCR)

Ziel: Central (Backend) auf einem normalen ARM64-Linux laufen lassen, Agents auf
Linux-Maschinen — **alle Images lokal gebaut**, kein GitHub/GHCR-Pull. QNAP kommt
später.

Es gibt drei Images:
| Image | Wo gebaut | Wie gestartet |
|-------|-----------|---------------|
| `dborg-central:local` | Central-Host | per compose |
| `dborg-agent:local`   | jeder Agent-Host | per compose |
| `dborg-worker:local`  | jeder Agent-Host | vom Agent zur Laufzeit (Docker-Socket) |

---

## 0. Voraussetzungen (jeder Host)

- ARM64 Linux mit Docker Engine + Compose-Plugin
  ```bash
  docker --version          # >= 24
  docker compose version    # v2
  ```
- Git, um das Repo zu holen.

---

## 1. Repo holen (jeder Host)

Solange wir testen, läuft alles auf dem Branch `local-deploy` (der triggert
**keinen** GHCR-Build — der baut nur auf `main`).

```bash
git clone -b local-deploy https://github.com/chamm-p/docker-borg.git
cd docker-borg
```

---

## 2. Central / Backend (ARM64-Host)

### 2a. Secrets erzeugen
```bash
openssl rand -hex 24   # → DBORG_REGISTRATION_TOKEN  (auch für Agents!)
openssl rand -hex 24   # → DBORG_SECRET_KEY
# Admin-Passwort frei wählen → DBORG_ADMIN_PASSWORD
```

### 2b. Compose anpassen
`deploy/central/docker-compose.local.yml` öffnen und die drei `CHANGE_ME_*`
Werte eintragen (TZ ggf. anpassen).

### 2c. Bauen + starten
```bash
cd deploy/central
docker compose -f docker-compose.local.yml up -d --build
docker compose -f docker-compose.local.yml logs -f
```
Im Log muss die Migration sauber durchlaufen:
`Running upgrade ... -> 0012, discovery v2 ...`

### 2d. Erreichbar?
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/   # 200 oder 401
```
UI: `http://<CENTRAL-IP>:8080` — Login mit `DBORG_ADMIN_PASSWORD`.
Notiere die **Central-IP** und den **Registration-Token** für die Agents.

---

## 3. Agent (jede Linux-Maschine, die gesichert werden soll)

### 3a. Compose anpassen
`deploy/agent/docker-compose.local.yml` öffnen und eintragen:
- `DBORG_AGENT_NAME` — eindeutiger Hostname (z.B. `vzulu2`)
- `DBORG_CENTRAL_URL` — `http://<CENTRAL-IP>:8080`
- `DBORG_REGISTRATION_TOKEN` — **derselbe** wie bei Central
- `DBORG_HOST_BASE_DIR` — Host-Pfad, unter dem deine Compose-Projekte liegen
  (z.B. `/sync/Docker` oder `/home/user/docker`)
- Den **Volume-Mount** `...:/host/docker:ro` auf denselben Host-Pfad setzen
  (Mount-Source MUSS = `DBORG_HOST_BASE_DIR` sein)

### 3b. Beide Images bauen (Agent + Worker)
```bash
cd deploy/agent
docker compose -f docker-compose.local.yml --profile build build
```
Das baut `dborg-agent:local` **und** `dborg-worker:local`.

### 3c. Agent starten
```bash
docker compose -f docker-compose.local.yml up -d
docker compose -f docker-compose.local.yml logs -f
```
Erwartetes Log:
- `Konnte Worker-Image nicht pullen (...) — nutze lokal gecachtes`
  → **harmlos**, genau so gewollt (kein Registry, nutzt das lokal gebaute).
- `Registered with central as '<name>'`
- `Discovered N compose projects`

### 3d. In der UI prüfen
Im Central-UI taucht der Agent auf. Backup-Ziel + Verschlüsselung setzen, dann
Container auswählen und ein Test-Backup starten.

---

## 4. Update einspielen (nach Code-Änderung)

Auf dem betroffenen Host im Repo:
```bash
git pull
# Central:
cd deploy/central && docker compose -f docker-compose.local.yml up -d --build
# Agent (wenn Worker-Code geändert wurde, --profile build mitnehmen):
cd deploy/agent && docker compose -f docker-compose.local.yml --profile build build \
  && docker compose -f docker-compose.local.yml up -d
```

---

## 5. Später: QNAP-Agent + GHCR

Wenn das lokale Setup stabil läuft, mergen wir nach `main` → GHCR baut die
Multi-Arch-Images. Dann nur noch der QNAP-Agent über die GHCR-Variante
(`deploy/agent/docker-compose.yml`). Bis dahin bleibt `main` unangetastet.
