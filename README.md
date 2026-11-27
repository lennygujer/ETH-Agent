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
| – | Alle Fächer: automatische Erkennung, `/fach`-Umschaltung, OneDrive-Sync, Word/PowerPoint/Code/Notebooks | ✅ erste Version |

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

## Fächer

Der Agent erkennt jedes Fach im gespiegelten OneDrive-Ordner (`material/<Semester>/<Fach>/`) automatisch.

- `/kurse` – alle Fächer mit Kürzeln
- `/physik`, `/analysis`, `/sst`, `/dm` … – Fach wählen (gilt für den ganzen Chat; bei gleichem Namen gewinnt das neueste Semester)
- `/physik Erkläre den Tunneleffekt` – Fach und Frage in einer Nachricht
- `/alle` – wieder über alle Fächer suchen (Standard, solange kein Fach gewählt ist)

Eigene Kürzel: `course_aliases` in `config.yaml`.

## Lernkarte

Pro Fach führt der Agent eine Lernkarte: das Gerüst aus den Skript-Kapiteln, ergänzt um die Konzepte aus deinen
Korrekturen. Jedes `/check` trägt ein, welche Konzepte die Aufgabe geprüft hat und mit welchem Ergebnis
(richtig, teilweise, falsch, Fehlvorstellung, Flüchtigkeitsfehler). Deine Sicherheit (`Sicherheit: 70%`) und dein
aufgeschriebener Gedankengang fliessen ein. Ist die Ursache eines Fehlers unklar, stellt der Agent eine
**Rückfrage** zu deinem Gedankengang; deine Antwort wird ausgewertet und präzisiert die Karte (`/weiter` überspringt).

- `/lernkarte` – Mindmap mit Status (✅ sitzt · 🟢 vermutlich ok · 🟡 wackelt · 🔴 fehlt · ⚠️ Fehlvorstellung · ⚪ ungeprüft)
  und Liste „Das solltest du noch lernen“
- `/lernkarte voll`, `/lernkarte <thema>`

Belege werden nur angehängt, nie überschrieben (`data/learner.sqlite`); der Status wird daraus berechnet
(neuere Belege zählen mehr, eine Fehlvorstellung gilt erst nach zwei richtigen Antworten als behoben).

## Diagnose

`/diagnose` misst gezielt, was du kannst – nach dem Prober von LearnHarness: kurze Fragen, die verschiedene
Denkmodelle auseinanderhalten (Multiple Choice mit plausiblen Fehlvorstellungen als Optionen oder ein Satz),
beginnend auf Prüfungsniveau und je nach Antwort enger. Ungeprüfte und wacklige Themen der Lernkarte kommen
zuerst. Deine Begründung zählt mehr als die gewählte Option; sicher und falsch gilt als Fehlvorstellung,
«weiss nicht» als ehrliche Lücke. Pro Antwort ein Modellaufruf (Auswertung + nächste Frage), die Lösung jeder
Frage bleibt bis zur Auswertung verborgen.

- `/diagnose` – ganzes Fach, bis zu 8 Fragen
- `/diagnose tunneling`, `/diagnose wasserstoff 5` – nur ein Thema, eigene Anzahl (3–15)
- Antwort: Buchstabe oder eigene Worte, kurz warum, optional `Sicherheit: 70%`
- `/weiter` überspringt eine Frage (ohne Eintrag), `/stop` beendet mit Fazit ohne weiteren Modellaufruf

Am Ende: Fazit pro Thema (worauf du aufbauen kannst, wo es bricht, Fehlvorstellungen, was als Nächstes) und die
aktualisierten Knoten der Lernkarte.

## Lernen

`/learn <thema>` schliesst eine Lücke nach dem Lehrer von LearnHarness. Ohne Thema nimmt der Agent die dringendste
Schwäche der Lernkarte (Fehlvorstellungen zuerst).

1. **Plan:** Der Agent liest, was die Lernkarte über dich weiss – auch deine eigenen Antworten aus Diagnosen und
   Rückfragen –, dazu Skript, Formelblatt und alte Prüfungen. Er zeigt ein konkretes Prüfungsziel und einen
   kleinen Abhängigkeitsplan (Grundlagen unten, Ziel oben). Du bestätigst mit *ja* oder sagst, was du schon kannst.
2. **Schritte:** pro Nachricht ein Denkschritt. Grundlagen als einfache, immer wahre Aussagen, alles Weitere
   motiviert hergeleitet. Rhythmus: **Vorhersage** (raten erwünscht, wird nicht bewertet) → Erklärung →
   **Kontrollfrage** (wird bewertet und in die Lernkarte eingetragen) → Reparatur oder weiter.
3. **Ende:** wenn du eine reduzierte Prüfungsaufgabe richtig und begründet löst. Das Fazit nennt eine Aufgabe aus
   einer alten Prüfung zum Festigen mit `/check`.

Zwischendurch eigene Fragen stellen ist erlaubt. `/plan` zeigt den Plan (blau: aktuell, grün: gezeigt) und holt
die Lektion auch nach einem Umweg zurück. `/weiter` überspringt eine Frage, `/stop` beendet. Unterrichtet wird mit
Codex (`models.learn_provider: codex`), auf `claude` umstellbar; das andere Modell ist Rückfall.

## Termine und Lernplan

**Termine:** Schreib im Chat einfach, was ansteht – *«Ich habe am 12.12. den Midterm in SST, Stoff: Faltung,
LTI-Systeme, Fourierreihen»* – oder `/termin …`. Ein kurzer Codex-Aufruf liest Art, Datum und Themen und ordnet die
Themen den Kapiteln der Lernkarte zu. Danach:

- beim Fachwechsel steht der Countdown mit Stand des Stoffs («17 Tage, 9 Themen, 3 geprüft, 2 wackeln»)
- `/diagnose` und `/learn` ohne Thema richten sich nach dem Stoff des nächsten Termins
- `/termine` zeigt alle, `/termine löschen <nr>` entfernt einen, `/termine alle` auch vergangene

**Lernplan:** `/lernplan` sucht zu den Themen des nächsten Termins passende Aufgaben aus Serien, Übungsskripten und
alten Prüfungen und lässt sie von Codex priorisieren und auf die Tage bis zum Termin verteilen (ca. 90 min/Tag):
schwache Themen zuerst, Fehlvorstellungen erst mit `/learn` verstehen, Themen gemischt statt blockweise, alte
Prüfungen gegen Ende, Aufgaben mit Musterlösung bevorzugt. Die Aufgaben heissen P1, P2 …:

- `/check P3` (mit Foto oder Text) korrigiert und hakt P3 im Plan ab
- `P3 Tipp` gibt Tipps zur Aufgabe
- `/lernplan` zeigt den gespeicherten Plan mit Fortschritt (ohne Modellaufruf), `/lernplan neu` plant neu

**Verbrauch:** `/verbrauch` (ohne Modellaufruf) zeigt für die letzten 7 Tage, wie viele Cloud-Aufrufe und Tokens
Codex (ChatGPT-Plus-Kontingent) und Claude (Pro-Kontingent) verbraucht haben – pro Modell, pro Befehl (mit Ø pro
Nachricht) und pro Tag. `/verbrauch heute`, `/verbrauch 30`. Codex läuft dafür mit `--json` (genaue Zahlen inkl.
Cache- und Denk-Tokens); ältere Codex-Versionen ohne `--json` werden erkannt und liefern nur eine Gesamtzahl.
Daten: `data/usage.sqlite` (Tabellen `requests` = Nachrichten, `calls` = Modellaufrufe).

## OneDrive-Sync

Siehe `docs/sync.md`: entweder holt der Server den ETH-Ordner direkt aus OneDrive (rclone, alle 3 h),
oder der Windows-PC schiebt ihn per SFTP. Danach indexiert der Agent neue Dateien (zusätzlich jede Nacht 03:30).

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
eth-agent usage                                  # Verbrauch der letzten 7 Tage (--days 1 = heute)

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
4. **Prompt** aus `prompts/answer.md`: Frage wörtlich, Material mit Zitat-Labels, Lösungsregel (bei Aufgaben zuerst Tipps, `prompts/hints.md`).
5. **Prüfcode**: Das Modell hängt einen ```` ```python check ```` -Block an, der die Ergebnisse unabhängig nachrechnet. Er läuft in einer Sandbox (eigener Prozess, Zeit-/Speicherlimit, wenn möglich ohne Netz). Ergebnis als Badge: `Tool ✓`, `Tool ✗`, `ungeprüft`. Der Prüfcode stammt in Phase 2 noch vom selben Modell; ab Phase 3 schreibt ihn das andere Modell.

**Tipps zuerst:** Bei Aufgaben gibt der Agent zuerst Tipps (Ansatz, erster Schritt, typische Falle); *noch ein Tipp* geht eine Stufe weiter, die ganze Lösung gibt es mit `--loesung` oder `/loesung …`. Pro Fach umstellbar mit `solution_mode: full`.

## Aufbau

```
src/eth_agent/
  config.py            Konfiguration
  cli.py               Kommandozeile
  indexer/pdf.py       PDF -> Zeilen, Überschriften, gedruckte Seitenzahlen
  indexer/doctypes.py  Dokumenttyp, Paarschlüssel Aufgabe <-> Lösung, Deckblatt-Metadaten
  indexer/chunking.py  Chunks pro Aufgabe bzw. Abschnitt
  indexer/readers.py   Word, PowerPoint, Markdown/LaTeX, Code, Jupyter
  indexer/build.py     inkrementelle Indexierung, Embeddings
  retrieval/store.py   SQLite + FTS5 + Vektoren
  retrieval/search.py  hybride Suche (gewichtete RRF)
  agent/routing.py     Regeln R1–R8
  agent/references.py  "Serie 3 Aufgabe 1" -> exakter Chunk
  agent/core.py        handle(): Material, Prompt, Modell, Prüfcode, Antwort
  llm/providers.py     codex exec / claude -p (Abo-Login), Fake für Tests
  llm/usage.py         Protokoll aller Cloud-Aufrufe
  checks/sandbox.py    Prüfcode ausführen
  skills/check.py      /check: Referenz, Bewertung, Fehlerkatalog, Lernkarte, Rückfrage
  skills/probe.py      Antwort auf Rückfragen auswerten
  learner/             Lernkarte: Knoten, Belege, Status, Mindmap
  server.py            OpenAI-kompatibler Server für Open WebUI (Streaming, Bilder, Hilfsanfragen)
deploy/                systemd-Dienst
scripts/               setup_service.sh, fix_zip_names.py
prompts/               Prompt-Vorlagen
docs/indexer.md        Details und bekannte Grenzen
```

Kursmaterial kommt **nie** ins Repo (`.gitignore`).
