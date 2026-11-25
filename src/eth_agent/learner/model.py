"""Lernermodell: pro Fach eine Lernkarte (Themen/Konzepte) und Belege über dein Verständnis.

Nach dem Vorbild von LearnHarness getrennt:
- Knoten = Themen/Konzepte des Fachs (Gerüst aus dem Skript, ergänzt durch die Bewertungen).
- Belege = was über DICH bekannt ist (Korrekturen, Rückfragen, Sicherheit, Denkspuren). Nur anhängen,
  nie überschreiben. Der Status eines Knotens wird jedes Mal aus den Belegen berechnet.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import slug

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,           -- "<kurs>:<schlüssel>"
    course TEXT NOT NULL,
    title TEXT NOT NULL,
    parent_id TEXT,
    origin TEXT NOT NULL,          -- skript | auto
    ord INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS nodes_course ON nodes(course);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    course TEXT NOT NULL,
    node_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,            -- check | probe | diagnose | learn | self
    outcome TEXT NOT NULL,         -- correct | partial | wrong | misconception | slip
    confidence REAL,               -- eigene Sicherheit 0..1, falls angegeben
    detail TEXT,                   -- was genau (in Worten des Bewerters / deinen Worten)
    problem_ref TEXT
);
CREATE INDEX IF NOT EXISTS evidence_node ON evidence(node_id);
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY,
    course TEXT NOT NULL,
    ts TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL,         -- Aufgabe, Versuch, erster Fehler (für die Auswertung)
    node_ids TEXT,                 -- betroffene Konzepte, kommagetrennt
    problem_ref TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT
);
CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY,
    course TEXT NOT NULL,
    scope TEXT,                    -- Thema oder leer (ganzes Fach)
    ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | done | stopped | abandoned
    max_q INTEGER NOT NULL,
    focus TEXT,                    -- Stränge (id | Titel | Status) zu Beginn
    context TEXT,                  -- JSON: Kursmaterial + Prüfungsauszüge (einmal gesucht)
    summary TEXT
);
CREATE TABLE IF NOT EXISTS diag_items (
    id INTEGER PRIMARY KEY,
    diag_id INTEGER NOT NULL,
    n INTEGER NOT NULL,
    ts TEXT NOT NULL,
    node_ids TEXT,
    question TEXT NOT NULL,
    format TEXT,
    options TEXT,                  -- JSON-Liste
    answer_key TEXT,               -- nie an dich ausgegeben
    discriminates TEXT,
    answer TEXT,
    confidence REAL,
    outcome TEXT,
    feedback TEXT,
    touched TEXT                   -- eingetragene Knoten, kommagetrennt
);
CREATE INDEX IF NOT EXISTS diag_items_diag ON diag_items(diag_id);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    course TEXT NOT NULL,
    topic TEXT NOT NULL,
    node_ids TEXT,                 -- Zielknoten der Lernkarte
    ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | done | stopped | abandoned
    goal TEXT,
    plan TEXT,                     -- JSON: [{id, label, requires: []}]
    context TEXT,                  -- JSON: Material, Formelblatt, Prüfung, Belege (einmal gesucht)
    summary TEXT
);
CREATE TABLE IF NOT EXISTS lesson_turns (
    id INTEGER PRIMARY KEY,
    lesson_id INTEGER NOT NULL,
    n INTEGER NOT NULL,            -- Schritt (0 = Plan)
    ts TEXT NOT NULL,
    tutor TEXT NOT NULL,           -- was der Agent geschrieben hat (ohne Frage)
    q_type TEXT,                   -- predict | check | NULL
    q_node TEXT,
    q_text TEXT,
    q_options TEXT,                -- JSON-Liste
    answer_key TEXT,               -- nie an dich ausgegeben
    answer TEXT,                   -- deine Antwort
    confidence REAL,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS lesson_turns_lesson ON lesson_turns(lesson_id);
CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY,
    course TEXT NOT NULL,
    kind TEXT NOT NULL,            -- midterm | pruefung | test | abgabe | sonstiges
    title TEXT NOT NULL,
    date TEXT NOT NULL,            -- YYYY-MM-DD
    time TEXT,
    topics TEXT,                   -- JSON-Liste, wie du sie genannt hast
    node_ids TEXT,                 -- zugeordnete Knoten der Lernkarte
    note TEXT,
    ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    course TEXT NOT NULL,
    exam_id INTEGER,
    ts TEXT NOT NULL,
    summary TEXT,
    advice TEXT,
    learn_first TEXT,              -- JSON: Themen, die zuerst mit /learn verstanden werden sollten
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL,
    n INTEGER NOT NULL,            -- P1, P2, …
    pair_key TEXT, doc_type TEXT, problem_no TEXT, rel_path TEXT, label TEXT,
    node_id TEXT, priority INTEGER, why TEXT, minutes INTEGER, day TEXT,
    done_ts TEXT, outcome TEXT
);
CREATE INDEX IF NOT EXISTS plan_items_plan ON plan_items(plan_id);
"""

OUTCOMES = {"correct", "partial", "wrong", "misconception", "slip"}
STATUS_ORDER = ["misconception", "missing", "shaky", "probably", "solid", "untested"]
STATUS_LABEL = {
    "solid": "sitzt", "probably": "vermutlich ok", "shaky": "wackelt", "missing": "fehlt",
    "misconception": "Fehlvorstellung", "untested": "ungeprüft",
}
STATUS_ICON = {"solid": "✅", "probably": "🟢", "shaky": "🟡", "missing": "🔴", "misconception": "⚠️",
               "untested": "⚪"}
_OUTCOME_SCORE = {"correct": 1.0, "slip": 0.8, "partial": 0.5, "wrong": 0.0, "misconception": 0.0}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Evidence:
    ts: str
    kind: str
    outcome: str
    confidence: float | None
    detail: str | None
    problem_ref: str | None


@dataclass
class Node:
    id: str
    course: str
    title: str
    parent_id: str | None
    origin: str
    ord: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    @property
    def own_status(self) -> str:
        return status_from_evidence(self.evidence)

    @property
    def status(self) -> str:
        """Eigener Status, sonst der schlechteste bekannte Status der Unterknoten."""
        own = self.own_status
        kids = [c.status for c in self.children if c.status != "untested"]
        if not kids:
            return own
        worst = min(kids, key=STATUS_ORDER.index)
        if own == "untested":
            # Gesamtbild aus den Kindern: schlechtester Status, ausser alle sitzen
            return worst if worst in {"misconception", "missing", "shaky"} else \
                ("solid" if all(k == "solid" for k in kids) else "probably")
        return min([own, worst], key=STATUS_ORDER.index) if worst in {"misconception", "missing"} else own


def status_from_evidence(ev: list[Evidence]) -> str:
    """Neuere Belege zählen mehr. Fehlvorstellung bleibt, bis zweimal danach richtig."""
    if not ev:
        return "untested"
    ev = sorted(ev, key=lambda e: e.ts)
    last_mis = max((i for i, e in enumerate(ev) if e.outcome == "misconception"), default=None)
    if last_mis is not None:
        after = [e for e in ev[last_mis + 1:] if e.outcome == "correct"]
        if len(after) < 2:
            return "misconception"
    recent = ev[-4:]
    weights = [0.5 ** (len(recent) - 1 - i) for i in range(len(recent))]
    score = sum(w * _OUTCOME_SCORE[e.outcome] for w, e in zip(weights, recent)) / sum(weights)
    last = recent[-1]
    # richtig, aber unsicher -> noch nicht gefestigt
    if last.outcome == "correct" and last.confidence is not None and last.confidence < 0.6:
        score = min(score, 0.7)
    n_correct = sum(1 for e in ev if e.outcome == "correct")
    if score >= 0.85 and n_correct >= 2:
        return "solid"
    if score >= 0.75:
        return "probably"
    if score >= 0.4:
        return "shaky"
    return "missing"


def _norm(title: str) -> str:
    t = re.sub(r"^\d+(\.\d+)*\s*", "", title.strip())
    return slug(t)


class LearnerStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ Knoten
    def nodes(self, course: str) -> list[Node]:
        rows = self.db.execute("SELECT id, course, title, parent_id, origin, ord FROM nodes WHERE course=? "
                               "ORDER BY ord, created_at", (course,)).fetchall()
        nodes = {r[0]: Node(*r) for r in rows}
        for nid, ts, kind, outcome, conf, detail, ref in self.db.execute(
                "SELECT node_id, ts, kind, outcome, confidence, detail, problem_ref FROM evidence "
                "WHERE course=? ORDER BY ts", (course,)):
            if nid in nodes:
                nodes[nid].evidence.append(Evidence(ts, kind, outcome, conf, detail, ref))
        for n in nodes.values():
            if n.parent_id in nodes:
                nodes[n.parent_id].children.append(n)
        return list(nodes.values())

    def tree(self, course: str) -> list[Node]:
        all_nodes = self.nodes(course)
        ids = {n.id for n in all_nodes}
        return [n for n in all_nodes if n.parent_id not in ids]

    def add_node(self, course: str, title: str, parent_id: str | None = None, origin: str = "auto",
                 key: str | None = None, ord_: int = 0) -> str:
        title = unicodedata.normalize("NFKC", title)       # Ligaturen aus PDFs (ﬀ -> ff)
        nid = f"{course}:{key or _norm(title) or 'thema'}"
        with self.lock:
            base, i = nid, 2
            while True:
                row = self.db.execute("SELECT title FROM nodes WHERE id=?", (nid,)).fetchone()
                if row is None:
                    break
                if _norm(row[0]) == _norm(title):
                    return nid
                nid, i = f"{base}-{i}", i + 1
            self.db.execute("INSERT INTO nodes (id, course, title, parent_id, origin, ord) VALUES (?,?,?,?,?,?)",
                            (nid, course, title.strip(), parent_id, origin, ord_))
            self.db.commit()
        return nid

    def find_node(self, course: str, ref: str) -> str | None:
        """ID oder Titel -> Knoten-ID (unscharf über normalisierten Titel)."""
        if not ref:
            return None
        row = self.db.execute("SELECT id FROM nodes WHERE id=? AND course=?", (ref, course)).fetchone()
        if row:
            return row[0]
        if not ref.startswith(course + ":"):
            row = self.db.execute("SELECT id FROM nodes WHERE id=? AND course=?", (f"{course}:{ref}", course)).fetchone()
            if row:
                return row[0]
        target = _norm(ref)
        for nid, title in self.db.execute("SELECT id, title FROM nodes WHERE course=?", (course,)):
            if _norm(title) == target:
                return nid
        return None

    def build_skeleton(self, course: str, sections: list[str]) -> int:
        """Gerüst aus Skript-Abschnitten ('8 Titel > 8.2 Untertitel'). Gibt Anzahl neuer Knoten zurück."""
        before = self.db.execute("SELECT count(*) FROM nodes WHERE course=?", (course,)).fetchone()[0]
        for i, sec in enumerate(sections):
            parent = None
            for part in [p.strip() for p in sec.split(">")][:2]:
                m = re.match(r"^(\d+(?:\.\d+)*)\s+(.+)$", part)
                if not m:
                    continue
                num, title = m.group(1), m.group(2)
                parent = self.add_node(course, title, parent_id=parent, origin="skript",
                                       key="s" + num.replace(".", "-"), ord_=_ord(num))
        after = self.db.execute("SELECT count(*) FROM nodes WHERE course=?", (course,)).fetchone()[0]
        return after - before

    # ------------------------------------------------------------------ Belege
    def add_evidence(self, course: str, node_id: str, kind: str, outcome: str, *,
                     confidence: float | None = None, detail: str | None = None,
                     problem_ref: str | None = None) -> None:
        if outcome not in OUTCOMES:
            return
        with self.lock:
            self.db.execute("INSERT INTO evidence (course, node_id, ts, kind, outcome, confidence, detail, "
                            "problem_ref) VALUES (?,?,?,?,?,?,?,?)",
                            (course, node_id, now(), kind, outcome, confidence, detail, problem_ref))
            self.db.commit()

    def apply_concepts(self, course: str, concepts: list[dict], kind: str, *,
                       confidence: float | None = None, problem_ref: str | None = None) -> list[str]:
        """Konzepte aus einer Bewertung eintragen: bestehenden Knoten zuordnen oder neu anlegen."""
        touched = []
        for c in concepts or []:
            if not isinstance(c, dict):
                continue
            outcome = str(c.get("outcome", "")).strip()
            if outcome not in OUTCOMES:
                continue
            nid = self.find_node(course, str(c.get("node_id") or "")) or \
                self.find_node(course, str(c.get("name") or ""))
            if nid is None:
                name = str(c.get("name") or "").strip()
                if not name:
                    continue
                parent = self.find_node(course, str(c.get("parent_id") or c.get("parent") or ""))
                nid = self.add_node(course, name, parent_id=parent, origin="auto")
            self.add_evidence(course, nid, kind, outcome, confidence=confidence,
                              detail=str(c.get("evidence") or c.get("detail") or "")[:500] or None,
                              problem_ref=problem_ref)
            touched.append(nid)
        return touched

    # ------------------------------------------------------------------ Rückfragen
    def open_probe(self, course: str, question: str, context: str, node_ids: list[str],
                   problem_ref: str | None) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO probes (course, ts, question, context, node_ids, problem_ref) "
                                  "VALUES (?,?,?,?,?,?)",
                                  (course, now(), question, context, ",".join(node_ids), problem_ref))
            self.db.commit()
            return cur.lastrowid

    def get_probe(self, probe_id: int) -> dict | None:
        r = self.db.execute("SELECT id, course, question, context, node_ids, problem_ref, status FROM probes "
                            "WHERE id=?", (probe_id,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "course": r[1], "question": r[2], "context": r[3],
                "node_ids": [x for x in (r[4] or "").split(",") if x], "problem_ref": r[5], "status": r[6]}

    def close_probe(self, probe_id: int, answer: str) -> None:
        with self.lock:
            self.db.execute("UPDATE probes SET status='answered', answer=? WHERE id=?", (answer, probe_id))
            self.db.commit()

    # ------------------------------------------------------------------ Diagnosen
    def open_diagnosis(self, course: str, scope: str | None, max_q: int, focus: str, context: str) -> int:
        """Neue Diagnose; eine noch offene im selben Fach gilt als abgebrochen."""
        with self.lock:
            self.db.execute("UPDATE diagnoses SET status='abandoned' WHERE course=? AND status='open'", (course,))
            cur = self.db.execute("INSERT INTO diagnoses (course, scope, ts, max_q, focus, context) "
                                  "VALUES (?,?,?,?,?,?)", (course, scope or None, now(), max_q, focus, context))
            self.db.commit()
            return cur.lastrowid

    def get_diagnosis(self, diag_id: int) -> dict | None:
        r = self.db.execute("SELECT id, course, scope, status, max_q, focus, context, summary FROM diagnoses "
                            "WHERE id=?", (diag_id,)).fetchone()
        if not r:
            return None
        return dict(zip(("id", "course", "scope", "status", "max_q", "focus", "context", "summary"), r))

    def open_diagnosis_for(self, course: str) -> int | None:
        r = self.db.execute("SELECT id FROM diagnoses WHERE course=? AND status='open' ORDER BY id DESC LIMIT 1",
                            (course,)).fetchone()
        return r[0] if r else None

    def diag_items(self, diag_id: int) -> list[dict]:
        cols = ("id", "n", "node_ids", "question", "format", "options", "answer_key", "discriminates",
                "answer", "confidence", "outcome", "feedback", "touched")
        out = []
        for r in self.db.execute(f"SELECT {', '.join(cols)} FROM diag_items WHERE diag_id=? ORDER BY n",
                                 (diag_id,)):
            d = dict(zip(cols, r))
            d["node_ids"] = [x for x in (d["node_ids"] or "").split(",") if x]
            d["touched"] = [x for x in (d["touched"] or "").split(",") if x]
            try:
                d["options"] = json.loads(d["options"] or "[]")
            except json.JSONDecodeError:
                d["options"] = []
            out.append(d)
        return out

    def add_diag_item(self, diag_id: int, n: int, *, question: str, node_ids: list[str], fmt: str,
                      options: list[str], answer_key: str, discriminates: str) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO diag_items (diag_id, n, ts, node_ids, question, format, options, answer_key, "
                "discriminates) VALUES (?,?,?,?,?,?,?,?,?)",
                (diag_id, n, now(), ",".join(node_ids), question, fmt, json.dumps(options, ensure_ascii=False),
                 answer_key, discriminates))
            self.db.commit()
            return cur.lastrowid

    def answer_diag_item(self, item_id: int, answer: str | None, confidence: float | None) -> None:
        with self.lock:
            self.db.execute("UPDATE diag_items SET answer=?, confidence=? WHERE id=?", (answer, confidence, item_id))
            self.db.commit()

    def evaluate_diag_item(self, item_id: int, outcome: str | None, feedback: str, touched: list[str]) -> None:
        with self.lock:
            self.db.execute("UPDATE diag_items SET outcome=?, feedback=?, touched=? WHERE id=?",
                            (outcome, feedback, ",".join(touched), item_id))
            self.db.commit()

    def finish_diagnosis(self, diag_id: int, status: str, summary: str | None) -> None:
        with self.lock:
            self.db.execute("UPDATE diagnoses SET status=?, summary=? WHERE id=?", (status, summary, diag_id))
            self.db.commit()

    # ------------------------------------------------------------------ Lektionen (/learn)
    def open_lesson(self, course: str, topic: str, node_ids: list[str], context: str) -> int:
        with self.lock:
            self.db.execute("UPDATE lessons SET status='abandoned' WHERE course=? AND status='open'", (course,))
            cur = self.db.execute("INSERT INTO lessons (course, topic, node_ids, ts, context) VALUES (?,?,?,?,?)",
                                  (course, topic, ",".join(node_ids), now(), context))
            self.db.commit()
            return cur.lastrowid

    def get_lesson(self, lesson_id: int) -> dict | None:
        cols = ("id", "course", "topic", "node_ids", "status", "goal", "plan", "context", "summary")
        r = self.db.execute(f"SELECT {', '.join(cols)} FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        if not r:
            return None
        d = dict(zip(cols, r))
        d["node_ids"] = [x for x in (d["node_ids"] or "").split(",") if x]
        try:
            d["plan"] = json.loads(d["plan"] or "[]")
        except json.JSONDecodeError:
            d["plan"] = []
        return d

    def open_lesson_for(self, course: str) -> int | None:
        r = self.db.execute("SELECT id FROM lessons WHERE course=? AND status='open' ORDER BY id DESC LIMIT 1",
                            (course,)).fetchone()
        return r[0] if r else None

    def set_lesson_plan(self, lesson_id: int, goal: str | None, plan: list[dict]) -> None:
        with self.lock:
            if goal:
                self.db.execute("UPDATE lessons SET goal=? WHERE id=?", (goal, lesson_id))
            self.db.execute("UPDATE lessons SET plan=? WHERE id=?",
                            (json.dumps(plan, ensure_ascii=False), lesson_id))
            self.db.commit()

    def finish_lesson(self, lesson_id: int, status: str, summary: str | None) -> None:
        with self.lock:
            self.db.execute("UPDATE lessons SET status=?, summary=? WHERE id=?", (status, summary, lesson_id))
            self.db.commit()

    def lesson_turns(self, lesson_id: int) -> list[dict]:
        cols = ("id", "n", "tutor", "q_type", "q_node", "q_text", "q_options", "answer_key", "answer",
                "confidence", "outcome")
        out = []
        for r in self.db.execute(f"SELECT {', '.join(cols)} FROM lesson_turns WHERE lesson_id=? ORDER BY n",
                                 (lesson_id,)):
            d = dict(zip(cols, r))
            try:
                d["q_options"] = json.loads(d["q_options"] or "[]")
            except json.JSONDecodeError:
                d["q_options"] = []
            out.append(d)
        return out

    def add_lesson_turn(self, lesson_id: int, n: int, tutor: str, q: dict | None) -> int:
        q = q or {}
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO lesson_turns (lesson_id, n, ts, tutor, q_type, q_node, q_text, q_options, answer_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (lesson_id, n, now(), tutor, q.get("type"), q.get("node"), q.get("text"),
                 json.dumps(q.get("options") or [], ensure_ascii=False), q.get("answer_key")))
            self.db.commit()
            return cur.lastrowid

    def answer_lesson_turn(self, turn_id: int, answer: str | None, confidence: float | None,
                           outcome: str | None = None) -> None:
        with self.lock:
            self.db.execute("UPDATE lesson_turns SET answer=?, confidence=?, outcome=? WHERE id=?",
                            (answer, confidence, outcome, turn_id))
            self.db.commit()

    def student_words(self, course: str, node_ids: list[str], limit: int = 8) -> list[dict]:
        """Deine eigenen Antworten aus Diagnosen und Rückfragen zu diesen Knoten (für /learn)."""
        if not node_ids:
            return []
        out = []
        rows = self.db.execute(
            "SELECT i.question, i.answer, i.confidence, i.outcome, i.feedback, i.touched FROM diag_items i "
            "JOIN diagnoses d ON d.id = i.diag_id WHERE d.course=? AND i.answer IS NOT NULL ORDER BY i.id DESC",
            (course,)).fetchall()
        for q, a, conf, outc, fb, touched in rows:
            if set((touched or "").split(",")) & set(node_ids):
                out.append({"source": "diagnose", "question": q, "answer": a, "confidence": conf,
                            "outcome": outc, "feedback": fb})
        rows = self.db.execute("SELECT question, answer, node_ids FROM probes WHERE course=? AND answer IS NOT NULL "
                               "ORDER BY id DESC", (course,)).fetchall()
        for q, a, nids in rows:
            if set((nids or "").split(",")) & set(node_ids):
                out.append({"source": "rueckfrage", "question": q, "answer": a, "confidence": None,
                            "outcome": None, "feedback": None})
        return out[:limit]

    # ------------------------------------------------------------------ Prüfungstermine
    _EXAM_COLS = ("id", "course", "kind", "title", "date", "time", "topics", "node_ids", "note", "status")

    def _exam(self, r) -> dict:
        d = dict(zip(self._EXAM_COLS, r))
        d["topics"] = json.loads(d["topics"] or "[]")
        d["node_ids"] = [x for x in (d["node_ids"] or "").split(",") if x]
        return d

    def add_exam(self, course: str, kind: str, title: str, date: str, time: str | None, topics: list[str],
                 node_ids: list[str], note: str | None) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO exams (course, kind, title, date, time, topics, node_ids, note, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (course, kind, title, date, time, json.dumps(topics, ensure_ascii=False), ",".join(node_ids), note,
                 now()))
            self.db.commit()
            return cur.lastrowid

    def exams(self, course: str | None = None, from_date: str | None = None) -> list[dict]:
        q = f"SELECT {', '.join(self._EXAM_COLS)} FROM exams WHERE status='active'"
        args: list = []
        if course:
            q += " AND course=?"
            args.append(course)
        if from_date:
            q += " AND date>=?"
            args.append(from_date)
        return [self._exam(r) for r in self.db.execute(q + " ORDER BY date, id", args)]

    def get_exam(self, exam_id: int) -> dict | None:
        r = self.db.execute(f"SELECT {', '.join(self._EXAM_COLS)} FROM exams WHERE id=?", (exam_id,)).fetchone()
        return self._exam(r) if r else None

    def delete_exam(self, exam_id: int) -> bool:
        with self.lock:
            cur = self.db.execute("UPDATE exams SET status='deleted' WHERE id=? AND status='active'", (exam_id,))
            self.db.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ Lernpläne
    _ITEM_COLS = ("id", "n", "pair_key", "doc_type", "problem_no", "rel_path", "label", "node_id", "priority",
                  "why", "minutes", "day", "done_ts", "outcome")

    def new_plan(self, course: str, exam_id: int | None, summary: str, advice: str, learn_first: list[str],
                 items: list[dict]) -> int:
        with self.lock:
            self.db.execute("UPDATE plans SET status='replaced' WHERE course=? AND status='active'", (course,))
            cur = self.db.execute(
                "INSERT INTO plans (course, exam_id, ts, summary, advice, learn_first) VALUES (?,?,?,?,?,?)",
                (course, exam_id, now(), summary, advice, json.dumps(learn_first, ensure_ascii=False)))
            pid = cur.lastrowid
            for i, it in enumerate(items, 1):
                self.db.execute(
                    "INSERT INTO plan_items (plan_id, n, pair_key, doc_type, problem_no, rel_path, label, node_id, "
                    "priority, why, minutes, day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, i, it["pair_key"], it["doc_type"], it["problem_no"], it["rel_path"], it["label"],
                     it.get("node_id"), it.get("priority"), it.get("why"), it.get("minutes"), it.get("day")))
            self.db.commit()
            return pid

    def active_plan(self, course: str) -> dict | None:
        r = self.db.execute("SELECT id, course, exam_id, ts, summary, advice, learn_first FROM plans "
                            "WHERE course=? AND status='active' ORDER BY id DESC LIMIT 1", (course,)).fetchone()
        if not r:
            return None
        d = dict(zip(("id", "course", "exam_id", "ts", "summary", "advice", "learn_first"), r))
        d["learn_first"] = json.loads(d["learn_first"] or "[]")
        d["items"] = [dict(zip(self._ITEM_COLS, x)) for x in self.db.execute(
            f"SELECT {', '.join(self._ITEM_COLS)} FROM plan_items WHERE plan_id=? ORDER BY n", (d["id"],))]
        return d

    def plan_item(self, course: str, n: int) -> dict | None:
        plan = self.active_plan(course)
        return next((it for it in plan["items"] if it["n"] == n), None) if plan else None

    def mark_plan_done(self, course: str, pair_key: str, doc_type: str, problem_no: str, outcome: str | None
                       ) -> list[int]:
        """Aufgabe im aktiven Lernplan abhaken (nach /check). Gibt die Nummern (P…) zurück."""
        plan = self.active_plan(course)
        if not plan or not problem_no:
            return []
        top = problem_no.split(".")[0]
        hits = [it for it in plan["items"] if it["pair_key"] == pair_key and it["doc_type"] == doc_type
                and (it["problem_no"] or "").split(".")[0] == top]
        with self.lock:
            for it in hits:
                self.db.execute("UPDATE plan_items SET done_ts=?, outcome=? WHERE id=?", (now(), outcome, it["id"]))
            self.db.commit()
        return [it["n"] for it in hits]

    # ------------------------------------------------------------------ für Prompts
    def outline(self, course: str, max_nodes: int = 250) -> str:
        """Kompakte Knotenliste für den Bewerter: id | Titel | Status."""
        lines = []

        def walk(n: Node, depth: int) -> None:
            if len(lines) >= max_nodes:
                return
            lines.append(f"{'  ' * depth}- {n.id} | {n.title} | {STATUS_LABEL[n.status]}")
            for c in n.children:
                walk(c, depth + 1)

        for root in self.tree(course):
            walk(root, 0)
        return "\n".join(lines) or "(noch leer)"


def _ord(num: str) -> int:
    parts = [int(x) for x in num.split(".")] + [0, 0]
    return parts[0] * 10000 + parts[1] * 100 + parts[2]


_CONF = re.compile(r"(sicherheit|konfidenz|confidence|sicher)\s*[:=]?\s*(\d{1,3})\s*%?", re.I)


def parse_confidence(text: str) -> float | None:
    m = _CONF.search(text or "")
    if not m:
        return None
    v = int(m.group(2))
    return max(0.0, min(1.0, v / 100)) if v <= 100 else None
