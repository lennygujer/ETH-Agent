# Open WebUI anbinden

Der ETH-Agent erscheint in Open WebUI als eigenes Modell „eth-agent“, neben den anderen Modellen.
Open WebUI und der Agent laufen beide auf dem Ubuntu-Server.

## 1. Wie läuft Open WebUI?

```bash
docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' | grep -i webui
```

| Ergebnis | Fall | Server-Adresse (`ETH_AGENT_HOST`) | URL in Open WebUI |
|---|---|---|---|
| keine Zeile (Open WebUI direkt installiert, z. B. `pip`) | nativ | `127.0.0.1` | `http://127.0.0.1:8000/v1` |
| Container mit `--network host` | Docker, Host-Netz | `127.0.0.1` | `http://127.0.0.1:8000/v1` |
| Container mit Ports wie `0.0.0.0:3000->8080/tcp` | Docker, Bridge | `172.17.0.1` | `http://172.17.0.1:8000/v1` |

`172.17.0.1` ist die Adresse des Servers im Docker-Standardnetz (prüfen: `ip -4 addr show docker0`).
So ist der Agent für den Container erreichbar, aber nicht aus dem WLAN oder Internet.

## 2. Dienst einrichten

```bash
cd ~/eth-agent/app
ETH_AGENT_HOST=127.0.0.1 ./scripts/setup_service.sh     # bzw. 172.17.0.1 bei Docker-Bridge
```

Das Skript erzeugt einen API-Schlüssel in `.env`, installiert den Dienst und startet ihn. Am Ende zeigt es den Schlüssel an.

Prüfen:

```bash
curl -s http://127.0.0.1:8000/health      # bzw. 172.17.0.1
journalctl --user -u eth-agent -f         # Logs live
```

Nach einem `git pull` den Dienst neu starten: `systemctl --user restart eth-agent`.

## 3. Verbindung in Open WebUI

1. Als Admin: **Einstellungen → Admin-Einstellungen → Verbindungen**.
2. Bei **OpenAI API** auf **+** (Verbindung hinzufügen).
3. URL aus der Tabelle oben, Schlüssel = `ETH_AGENT_API_KEY` aus `.env`. Speichern.
4. Im Modell-Auswahlmenü erscheint **eth-agent**.

Empfohlen:

- **Admin-Einstellungen → Oberfläche → Aufgabenmodell**: für Titel/Tags ein lokales oder günstiges Modell wählen.
  Der Agent beantwortet solche Hilfsanfragen zwar ohne Cloud-Aufruf, ein eigenes Modell gibt aber bessere Titel.
- Falls lange Antworten (z. B. `/check` ohne Musterlösung, 2–3 Minuten) abbrechen: in der Umgebung von Open WebUI
  `AIOHTTP_CLIENT_TIMEOUT=900` setzen und Open WebUI neu starten.

## Benutzung

- Normale Fragen, z. B. *Erkläre den Tunneleffekt* oder *Löse Prüfung Aug 2025 Aufgabe 2*.
- Korrektur: erste Zeile `/check Serie 3 Aufgabe 1`, darunter der Lösungsweg (Shift+Enter für neue Zeilen).
  Ein Foto vom handschriftlichen Lösungsweg kann einfach angehängt werden.
- Open WebUI nutzt `/` für eigene Prompt-Vorlagen. Falls das stört: `!check` statt `/check`.
- Die Arbeitsschritte (Suche, Codex rechnet, Prüfcode) erscheinen als einklappbarer Block über der Antwort.
