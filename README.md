# ETH Study Agent

Lernagent für die Prüfungsvorbereitung an der ETH (D-ITET). Läuft zentral auf dem Ubuntu-Server, nutzt das eigene Kursmaterial aus OneDrive und die Abos ChatGPT Plus (Codex CLI) und Claude Pro (`claude -p`).

Design und Entscheidungen: Projekt „Eigener Agent“ → `Agent Architecture v5 – Entscheidung.md` (baut auf v4 auf).

## Stand

| Phase | Inhalt | Status |
|---|---|---|
| 0 | Repo, Installationsskript, Konfiguration | ✅ |
| 1 | Indexer + Suche (BM25, optional Embeddings), Musterlösung zu Aufgabe finden | ✅ erste Version |
| 2 | `handle()`, Terminal-Chat, Codex/Claude-Aufruf, Prüfcode-Sandbox, Aufgaben-Verweise | ✅ erste Version |
| 3 | `/check` mit Referenz-Herstellung, Fehlerkatalog | ✅ erste Version |
| 7 (vorgezogen) | Server für Open WebUI, Dienst mit Autostart | ✅ erste Version |

## Installation (Server)

```bash
git clone https://github.com/lennygujer/ETH-Agent.git ~/eth-agent/app
cd ~/eth-agent/app
./install.sh              # mit Embeddings (CPU)
# EMBED=0 ./install.sh    # nur Stichwortsuche
```

## Open WebUI

```bash
ETH_AGENT_HOST=127.0.0.1 ./scripts/setup_service.sh   # 172.17.0.1, falls Open WebUI in Docker (Bridge) läuft
```

Danach in Open WebUI eine OpenAI-Verbindung mit `http://<host>:8100/v1` und dem angezeigten Schlüssel anlegen.
Details: `docs/open-webui.md`.

## Material bereitstellen

Erwartete Struktur (anpassbar in `config.yaml`):

```
~/eth-agent/material/
  Physik 2/
    Exercises/            -> serie
    Exercise Solutions/   -> serie_loesung
    Exams/                -> pruefung
    Exam Solutions/       -> pruefung_loesung
    Sheets/               -> skript / formelsammlung (nach Dateiname)
    Übungsstunden/        -> uebungsstunde
```

Vorläufig per ZIP (vom Windows-PC aus, mit Tailscale):

```powershell
scp .\Physik_2.zip <user>@<server>:~/eth-agent/material/
```

```bash
cd ~/eth-agent/material && unzip Physik_2.zip
python ~/eth-agent/app/scripts/fix_zip_names.py .   # nur falls Namen wie "#U00dcbungsstunden" auftauchen
```

Später spiegelt der Mac den OneDrive-Ordner automatisch (siehe v5, Variante A).

## Benutzung

```bash
source .venv/bin/activate
eth-agent index                                  # neu/geänderte Dateien einlesen
eth-agent files                                  # was ist im Index, was fehlt (Platzhalter, Scans)
eth-agent search "hydrogen atom expectation value radius"
eth-agent search "spin measurement" --type pruefung -k 3
eth-agent show 606 --solution                    # Aufgabe + zugehörige Musterlösung

eth-agent chat                                   # Terminal-Chat (lädt beim Start das Embedding-Modell)
eth-agent chat --debug                           # zusätzlich Route, Dauer, Tokens, Prüfcode-Ausgabe
eth-agent ask "Prüfung Aug 2025 Aufgabe 4" --debug
eth-agent ask "Erkläre den Tunneleffekt" --show-prompt   # zeigt den kompletten Prompt
eth-agent usage                                  # Cloud-Aufrufe der letzten 5 h

eth-agent check "Serie 3 Aufgabe 1" --attempt-file versuch.md
eth-agent check "Prüfung Aug 2025 Aufgabe 2" --image foto1.jpg foto2.jpg --loesung
```

Im Chat: `/check Serie 3 Aufgabe 1` eingeben, danach den Lösungsweg über mehrere Zeilen, Ende mit einer Zeile `.`
(`bild: pfad.jpg` hängt ein Foto an). Eigene Aufgabe: `/check <Aufgabentext>`, Zeile `---`, dann der Versuch.

### Wie `/check` korrigiert

1. Aufgabe aus dem Material holen (oder eigener Aufgabentext).
2. **Referenz:** Musterlösung, falls vorhanden. Sonst lösen Codex und Claude die Aufgabe unabhängig voneinander
   (parallel), jeweils mit Prüfcode.
3. **Bewertung** durch Claude (bei Fotos Codex): geschätzte Punkte, was richtig ist, erster Fehler mit Fehlerklasse
   (Konzept, Ansatz, Algebra, Vorzeichen, Einheiten, …), kleinster Hinweis. Ein anderer korrekter Weg zählt als richtig.
4. Deine **Endergebnisse** werden zusätzlich per Prüfcode nachgerechnet.
5. **Vertrauensstufe** der Referenz: `Musterlösung`, `2 Modelle einig + Tool`, `2 Modelle einig (ohne Tool)`,
   `1 Modell + Tool`, `unsicher`. Bei `unsicher` gibt es nur eine vorläufige Einschätzung.
6. Jeder Fehler landet im Fehlerkatalog `data/profile/errors.jsonl` (Grundlage für `/practice` und Wiederholung).

Kosten: mit Musterlösung 1 Cloud-Aufruf, ohne Musterlösung 3.

### Wie eine Antwort entsteht

1. **Routing** (`agent/routing.py`): `@codex`/`@claude` erzwingt ein Modell, `/befehl` ist ein Skill, sehr lange Texte gehen an Claude, sonst Codex. Fällt Codex aus (Limit, Fehler), übernimmt Claude, ausser das Modell wurde ausdrücklich gewählt.
2. **Verweise** (`agent/references.py`): *Serie 3 Aufgabe 1*, *Prüfung Aug 2025 Aufgabe 4*, *Prüfung HS2022 Aufgabe 2*, *Mock Exam 2 Aufgabe 1* holen die Aufgabe und die Musterlösung exakt aus dem Index.
3. **Suche**: weiteres Material (Skript, Übungsstunden, ähnliche Prüfungsaufgaben), maximal ~12 000 Tokens.
4. **Prompt** aus `prompts/answer.md`: Frage wörtlich, Material mit Zitat-Labels, Lösungsregel (Serien im Modus `tutor` nur Hinweise).
5. **Prüfcode**: Das Modell hängt einen ```` ```python check ```` -Block an, der die Ergebnisse unabhängig nachrechnet. Er läuft in einer Sandbox (eigener Prozess, Zeit-/Speicherlimit, wenn möglich ohne Netz). Ergebnis als Badge: `Tool ✓`, `Tool ✗`, `ungeprüft`. Der Prüfcode stammt in Phase 2 noch vom selben Modell; ab Phase 3 schreibt ihn das andere Modell.

`serien_mode` pro Kurs in `config.yaml`: `tutor` (Standard, für Serien mit Bonus/Testat) oder `full`.

## Aufbau

```
src/eth_agent/
  config.py            Konfiguration
  cli.py               Kommandozeile
  indexer/pdf.py       PDF -> Zeilen, Überschriften, gedruckte Seitenzahlen
  indexer/doctypes.py  Dokumenttyp, Paarschlüssel Aufgabe <-> Lösung, Deckblatt-Metadaten
  indexer/chunking.py  Chunks pro Aufgabe bzw. Abschnitt
  indexer/build.py     inkrementelle Indexierung, Embeddings
  retrieval/store.py   SQLite + FTS5 + Vektoren
  retrieval/search.py  hybride Suche (gewichtete RRF)
  agent/routing.py     Regeln R1–R8
  agent/references.py  "Serie 3 Aufgabe 1" -> exakter Chunk
  agent/core.py        handle(): Material, Prompt, Modell, Prüfcode, Antwort
  llm/providers.py     codex exec / claude -p (Abo-Login), Fake für Tests
  llm/usage.py         Protokoll aller Cloud-Aufrufe
  checks/sandbox.py    Prüfcode ausführen
  skills/check.py      /check: Referenz, Bewertung, Fehlerkatalog
  server.py            OpenAI-kompatibler Server für Open WebUI (Streaming, Bilder, Hilfsanfragen)
deploy/                systemd-Dienst
scripts/               setup_service.sh, fix_zip_names.py
prompts/               Prompt-Vorlagen
docs/indexer.md        Details und bekannte Grenzen
```

Kursmaterial kommt **nie** ins Repo (`.gitignore`).
