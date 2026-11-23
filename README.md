# ETH Study Agent

Lernagent für die Prüfungsvorbereitung an der ETH (D-ITET). Läuft zentral auf dem Ubuntu-Server, nutzt das eigene Kursmaterial aus OneDrive und die Abos ChatGPT Plus (Codex CLI) und Claude Pro (`claude -p`).

Design und Entscheidungen: Projekt „Eigener Agent“ → `Agent Architecture v5 – Entscheidung.md` (baut auf v4 auf).

## Stand

| Phase | Inhalt | Status |
|---|---|---|
| 0 | Repo, Installationsskript, Konfiguration | ✅ |
| 1 | Indexer + Suche (BM25, optional Embeddings), Musterlösung zu Aufgabe finden | ✅ erste Version |
| 2 | `handle()`, Terminal-Chat, Codex-Aufruf, Tool-Checks | offen |
| 3 | `/check` mit Referenz-Herstellung | offen |

## Installation (Server)

```bash
git clone https://github.com/lennygujer/ETH-Agent.git ~/eth-agent/app
cd ~/eth-agent/app
./install.sh              # mit Embeddings (CPU)
# EMBED=0 ./install.sh    # nur Stichwortsuche
```

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
```

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
docs/indexer.md        Details und bekannte Grenzen
```

Kursmaterial kommt **nie** ins Repo (`.gitignore`).
