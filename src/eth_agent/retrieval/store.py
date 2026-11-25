"""SQLite-Index: Dateien, Seiten, Chunks, Volltext (FTS5/BM25) und optionale Embeddings.

Bewusst SQLite statt ChromaDB: eine Datei, keine zusätzlichen Dienste, BM25 eingebaut,
und für einige tausend Chunks reicht ein Vektorvergleich mit numpy völlig (schwacher Server).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    course TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    sha256 TEXT,
    size INTEGER,
    n_pages INTEGER,
    status TEXT NOT NULL,          -- ok | needs_ocr | placeholder | error
    empty_pages TEXT,              -- Seiten ohne Textschicht, kommagetrennt
    pair_key TEXT,
    semester TEXT, year TEXT, lecturer TEXT, title TEXT,
    error TEXT,
    indexed_at TEXT DEFAULT (datetime('now')),
    UNIQUE(course, rel_path)
);
CREATE TABLE IF NOT EXISTS pages (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    pdf_page INTEGER NOT NULL,
    printed_page TEXT,
    text TEXT,
    PRIMARY KEY(file_id, pdf_page)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    course TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    ord INTEGER NOT NULL,
    problem_no TEXT, title TEXT, section TEXT, points REAL,
    page_start INTEGER, page_end INTEGER, printed_start TEXT, printed_end TEXT,
    text TEXT NOT NULL,
    tokens INTEGER,
    embedding BLOB,
    embedding_model TEXT
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, section, text, tokenize = "unicode61 remove_diacritics 2"
);
"""


@dataclass
class ChunkRow:
    id: int
    course: str
    doc_type: str
    rel_path: str
    problem_no: str | None
    title: str | None
    section: str | None
    points: float | None
    page_start: int
    page_end: int
    printed_start: str | None
    printed_end: str | None
    text: str
    pair_key: str | None
    semester: str | None
    year: str | None

    @property
    def file_name(self) -> str:
        return Path(self.rel_path).name

    def page_label(self) -> str:
        a = self.printed_start or str(self.page_start)
        b = self.printed_end or str(self.page_end)
        return f"S. {a}" if a == b else f"S. {a}–{b}"

    def citation(self, course_name: str | None = None) -> str:
        where = [f"Aufgabe {self.problem_no}"] if self.problem_no else []
        where.append(self.page_label())
        return f"[{course_name or self.course} – {self.file_name}, {', '.join(where)}]"


_CHUNK_SELECT = """
SELECT c.id, c.course, c.doc_type, f.rel_path, c.problem_no, c.title, c.section, c.points,
       c.page_start, c.page_end, c.printed_start, c.printed_end, c.text,
       f.pair_key, f.semester, f.year
FROM chunks c JOIN files f ON f.id = c.file_id
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Server: Anfragen laufen in Worker-Threads (seriell, siehe server.py)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---------- Dateien ----------
    def file_hashes(self, course: str) -> dict[str, str | None]:
        rows = self.db.execute("SELECT rel_path, sha256 FROM files WHERE course=?", (course,))
        return dict(rows.fetchall())

    def delete_file(self, course: str, rel_path: str) -> None:
        row = self.db.execute("SELECT id FROM files WHERE course=? AND rel_path=?",
                              (course, rel_path)).fetchone()
        if not row:
            return
        fid = row[0]
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE file_id=?", (fid,))]
        self.db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        self.db.execute("DELETE FROM files WHERE id=?", (fid,))

    def add_file(self, course: str, rel_path: str, doc_type: str, *, sha256: str | None, size: int,
                 n_pages: int, status: str, empty_pages: list[int], pair_key: str | None,
                 meta: dict, error: str | None = None) -> int:
        self.delete_file(course, rel_path)
        cur = self.db.execute(
            """INSERT INTO files (course, rel_path, doc_type, sha256, size, n_pages, status,
               empty_pages, pair_key, semester, year, lecturer, title, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (course, rel_path, doc_type, sha256, size, n_pages, status,
             ",".join(map(str, empty_pages)) or None, pair_key,
             meta.get("semester"), meta.get("year"), meta.get("lecturer"), meta.get("title"), error))
        return cur.lastrowid

    def add_pages(self, file_id: int, pages: list[tuple[int, str | None, str]]) -> None:
        self.db.executemany("INSERT INTO pages VALUES (?,?,?,?)",
                            [(file_id, p, pp, t) for p, pp, t in pages])

    def add_chunks(self, file_id: int, course: str, doc_type: str, chunks) -> None:
        for c in chunks:
            # Lösungsteil im selben Dokument (Übungsskript mit "Lösung 45"): als Lösung speichern,
            # damit solution_for() ihn über denselben Paarschlüssel findet.
            ctype = f"{doc_type}_loesung" if getattr(c, "solution", False) and doc_type in {"serie", "pruefung"} \
                else doc_type
            cur = self.db.execute(
                """INSERT INTO chunks (file_id, course, doc_type, ord, problem_no, title, section,
                   points, page_start, page_end, printed_start, printed_end, text, tokens)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (file_id, course, ctype, c.ord, c.problem_no, c.title, c.section, c.points,
                 c.page_start, c.page_end, c.printed_start, c.printed_end, c.text, c.tokens))
            self.db.execute("INSERT INTO chunks_fts(rowid, title, section, text) VALUES (?,?,?,?)",
                            (cur.lastrowid, c.title or "", c.section or "", c.text))

    def commit(self) -> None:
        self.db.commit()

    # ---------- Lesen ----------
    def get_chunk(self, chunk_id: int) -> ChunkRow | None:
        r = self.db.execute(_CHUNK_SELECT + " WHERE c.id=?", (chunk_id,)).fetchone()
        return ChunkRow(*r) if r else None

    def get_chunks(self, ids: list[int]) -> dict[int, ChunkRow]:
        if not ids:
            return {}
        q = _CHUNK_SELECT + f" WHERE c.id IN ({','.join('?' * len(ids))})"
        return {r[0]: ChunkRow(*r) for r in self.db.execute(q, ids)}

    def solution_for(self, chunk: ChunkRow) -> list[ChunkRow]:
        """Musterlösung zu einer Aufgabe: gleicher Paarschlüssel, Lösungstyp, gleiche Aufgabennr."""
        if not chunk.problem_no or not chunk.pair_key:
            return []
        sol_type = {"serie": "serie_loesung", "pruefung": "pruefung_loesung"}.get(chunk.doc_type)
        if not sol_type:
            return []
        # Lösungen haben oft zusätzliche Unterüberschriften (2.2 -> 2.2.1, 2.2.2): mitnehmen.
        return self.problem_chunks(chunk.course, chunk.pair_key, sol_type, chunk.problem_no)

    def problem_chunks(self, course: str, pair: str, doc_type: str, problem_no: str) -> list[ChunkRow]:
        """Alle Chunks einer Aufgabe (inkl. Unteraufgaben 2 -> 2.1, 2.2) in einer Datei."""
        rows = self.db.execute(
            _CHUNK_SELECT + " WHERE f.course=? AND f.pair_key=? AND c.doc_type=?"
            " AND (c.problem_no=? OR c.problem_no LIKE ?) ORDER BY c.ord",
            (course, pair, doc_type, problem_no, problem_no + ".%"))
        return [ChunkRow(*r) for r in rows]

    def chunks_with_problem(self, course: str, problem_no: str) -> list[ChunkRow]:
        """Alle Chunks mit dieser Aufgabennummer (inkl. Unteraufgaben) im ganzen Fach."""
        rows = self.db.execute(
            _CHUNK_SELECT + " WHERE f.course=? AND (c.problem_no=? OR c.problem_no LIKE ?) ORDER BY f.rel_path, c.ord",
            (course, problem_no, problem_no + ".%"))
        return [ChunkRow(*r) for r in rows]

    def chunks_of_file(self, course: str, rel_path: str) -> list[ChunkRow]:
        rows = self.db.execute(_CHUNK_SELECT + " WHERE f.course=? AND f.rel_path=? ORDER BY c.ord", (course, rel_path))
        return [ChunkRow(*r) for r in rows]

    def file_chunks(self, course: str, pair: str, doc_type: str) -> list[ChunkRow]:
        """Alle Chunks einer Datei (z. B. einer ganzen Übungsstunde) in Lesereihenfolge."""
        rows = self.db.execute(_CHUNK_SELECT + " WHERE f.course=? AND f.pair_key=? AND c.doc_type=? ORDER BY c.ord",
                               (course, pair, doc_type))
        return [ChunkRow(*r) for r in rows]

    def files(self, course: str | None = None) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        try:
            q = "SELECT f.*, (SELECT count(*) FROM chunks c WHERE c.file_id=f.id) AS n_chunks FROM files f"
            args: tuple = ()
            if course:
                q += " WHERE course=?"
                args = (course,)
            return list(self.db.execute(q + " ORDER BY course, doc_type, rel_path", args))
        finally:
            self.db.row_factory = None

    # ---------- Suche ----------
    def keyword_search(self, fts_query: str, *, course: str | None, doc_types: list[str] | None,
                       limit: int) -> list[tuple[int, float]]:
        q = """SELECT c.id, bm25(chunks_fts, 4.0, 2.0, 1.0) AS score
               FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
               WHERE chunks_fts MATCH ?"""
        args: list = [fts_query]
        if course:
            q += " AND c.course=?"
            args.append(course)
        if doc_types:
            q += f" AND c.doc_type IN ({','.join('?' * len(doc_types))})"
            args += doc_types
        q += " ORDER BY score LIMIT ?"
        args.append(limit)
        return [(i, -s) for i, s in self.db.execute(q, args)]

    def chunks_without_embedding(self, model: str) -> list[tuple[int, str, str | None]]:
        return list(self.db.execute(
            "SELECT id, text, section FROM chunks WHERE embedding IS NULL OR embedding_model IS NOT ?",
            (model,)))

    def set_embeddings(self, model: str, items: list[tuple[int, np.ndarray]]) -> None:
        self.db.executemany("UPDATE chunks SET embedding=?, embedding_model=? WHERE id=?",
                            [(v.astype(np.float32).tobytes(), model, i) for i, v in items])

    def embeddings(self, model: str, *, course: str | None, doc_types: list[str] | None):
        q = "SELECT id, embedding FROM chunks WHERE embedding_model=?"
        args: list = [model]
        if course:
            q += " AND course=?"
            args.append(course)
        if doc_types:
            q += f" AND doc_type IN ({','.join('?' * len(doc_types))})"
            args += doc_types
        rows = list(self.db.execute(q, args))
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        ids = [r[0] for r in rows]
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        return ids, mat
