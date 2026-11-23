# Indexer (Phase 1)

## Ablauf

1. Pro Kurs wird `material_root/<Kursordner>/` rekursiv nach PDFs durchsucht.
2. Jede Datei bekommt einen SHA-256-Hash. Unveränderte Dateien werden übersprungen, gelöschte aus dem Index entfernt.
3. **Dokumenttyp** aus dem Unterordner (`folder_doc_types` in `config.yaml`), mit Vorrang für den Dateinamen:
   `exam`/`mock`/`prüfung` → Prüfung, `sol`/`solution`/`lösung` → Lösung, `formula`/`formel` → Formelsammlung, `script`/`skript` → Skript.
4. **Paarschlüssel**: Aufgabe und Lösung bekommen denselben normalisierten Namen
   (`PhysII_ITET_Exam_2024_Jan_final.pdf` ↔ `…_final_solns.pdf`). Damit findet `solution_for()` die Musterlösung zu einer Aufgabe.
5. **Text** mit PyMuPDF, Zeile für Zeile mit Schriftgrösse und Fettdruck:
   - Kopfzeilen, die auf den meisten Seiten wiederkehren, werden entfernt.
   - Gedruckte Seitenzahl = letzte (oder erste) reine Zahl der Seite, mit Konsistenzprüfung über das ganze Dokument.
   - Nummerierte fette Überschriften (`1`, `1.2`, `8.2.1`) werden erkannt, auch wenn Nummer und Titel auf getrennten Zeilen stehen oder der Titel umbricht.
   - Inhaltsverzeichnis-Seiten werden übersprungen.
6. **Chunks**
   - Serien, Prüfungen, Lösungen, Übungsstunden: ein Chunk pro nummerierter Aufgabe, mit Punktzahl aus `(12 pts)`. Übergeordnete Überschriften (z. B. „1 Multiple choice questions“) stehen als Kontext im Chunk.
   - Skript: ein Chunk pro Abschnitt `x.y`, zu lange Abschnitte werden mit Überlappung geteilt, sehr kurze mit dem nächsten zusammengelegt.
   - Formelsammlung: kleine Chunks pro Block.
7. **Index**: SQLite (`data_dir/index.sqlite`) mit FTS5/BM25 für Stichworte und optional Embeddings (bge-m3, CPU) als Vektoren.

## Bekannte Grenzen

| Problem | Folge | Lösung (später) |
|---|---|---|
| Formeln werden flach gelesen (`n2 1` statt `n_1^2`) | Für Suche ok, für exakte Wiedergabe nicht | Einmalige Vision-Umwandlung formelreicher Seiten nach LaTeX/Markdown, Ergebnis cachen |
| Gescannte PDFs ohne Textschicht | Werden als `needs_ocr` markiert, keine Chunks | Vision-Transkription pro Seite |
| Manche PDFs haben kaputte Schriftzuordnung (z. B. `coe!cient`, `ϑ` statt `κ`) | Suchwörter fehlen teilweise | wie oben |
| Deutsche Anfrage, englisches Material | BM25 findet nichts | Embeddings (bge-m3, mehrsprachig) auf dem Server |
| 0-Byte-Dateien | OneDrive-Platzhalter, nicht heruntergeladen | Ordner in OneDrive auf „Immer auf diesem Gerät behalten“ |

## Warum SQLite statt ChromaDB

Eine Datei, kein zusätzlicher Dienst, BM25 ist eingebaut. Für einige tausend Chunks ist ein Vektorvergleich mit numpy schneller als jeder Netzwerkaufruf. Passt zum schwachen Server (7 GB RAM, keine GPU).
