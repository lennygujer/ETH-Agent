"""Verweise wie "Serie 3 Aufgabe 2", "Prüfung Aug 2025 Aufgabe 4" oder "Übungsstunde 11, 1.2 und 1.3"
direkt im Index auflösen.

Statt die Aufgabe per Suche zu raten, wird der passende Chunk exakt geholt (und die Musterlösung,
falls vorhanden).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from dataclasses import dataclass

from ..retrieval.store import ChunkRow, Store

_SERIE = re.compile(r"\b(?:serie|exercise\s*sheet|exercise|übungsblatt|uebungsblatt|blatt|übung|uebung|ex)\s*_?"
                    r"(?:nr\.?\s*)?(\d{1,2})\b", re.I)
_PROBLEM_TYPES = ("serie", "uebungsstunde", "pruefung", "sonstiges", "notizen", "buch", "skript")
_PROBLEM = re.compile(r"\b(?:aufgabe|aufg\.?|problem|task|frage|question|nr\.?)\s*(\d{1,2}(?:\.\d{1,2}){0,2})\s*([a-h])?\)?",
                      re.I)
_UEBUNG = re.compile(r"\b(?:übungsstunde|uebungsstunde|übungsstd\.?|übungsfolien|exercise\s*class|tutorial|"
                     r"recitation)\s*(?:nr\.?|nummer)?\s*_?0?(\d{1,2})\b", re.I)
_SECTION_KW = re.compile(r"\b(?:abschnitt|abschnitte|kapitel|teil|section|punkt|aufgabe|aufgaben|aufg\.?|problem)"
                         r"\s+(\d{1,2}(?:\.\d{1,2}){0,2})", re.I)
_ENUM_NEXT = re.compile(r"\s*(?:,|und|and|&|sowie|bis|-|–)\s*(\d{1,2}(?:\.\d{1,2}){0,2})\b")
_DOTTED = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(?![\d.]*\d)")
_EXAM = re.compile(r"\b(prüfung|pruefung|exam|klausur|mock|probeprüfung|resit)\b", re.I)
_MONTHS = {
    "jan": "jan", "januar": "jan", "january": "jan", "feb": "feb", "februar": "feb", "february": "feb",
    "aug": "aug", "august": "aug", "sep": "sep", "september": "sep", "jun": "jun", "juni": "jun", "june": "jun",
}


@dataclass
class Reference:
    kind: str                  # "serie" | "pruefung" | "uebungsstunde"
    file_pair_key: str
    problem_no: str | None
    part: str | None           # Teilaufgabe "b"
    chunks: list[ChunkRow]


def _exam_tokens(text: str) -> set[str]:
    t = text.lower()
    toks = set(re.findall(r"\b(?:hs|fs)\s?\d{2,4}\b", t))
    toks = {x.replace(" ", "") for x in toks}
    toks |= set(re.findall(r"\b20\d{2}\b", t))
    for w in re.findall(r"[a-zäöü]+", t):
        if w in _MONTHS:
            toks.add(_MONTHS[w])
    if re.search(r"\bmock|probe", t):
        toks.add("mock")
        m = re.search(r"mock\s*(?:exam)?\s*(\d)\b", t)
        if m:
            toks.add(f"mock{m.group(1)}")
    return toks


def _pair_matches(pair_key: str, tokens: set[str]) -> int:
    flat = pair_key.replace(" ", "")
    score = 0
    for tok in tokens:
        m = re.fullmatch(r"mock(\d)", tok)
        if m:
            if re.search(rf"mock(?:exam)?{m.group(1)}(?!\d)", flat):
                score += 3
            continue
        if tok in flat:
            score += 2 if not tok.isdigit() else 1
    # "mock exam" ohne Nummer soll nicht auf "mock exam2" passen
    if "mock" in tokens and not any(t.startswith("mock") and t != "mock" for t in tokens):
        if re.search(r"exam\d", flat):
            score -= 1
    return score


def _sections(text: str) -> list[str]:
    """'1.2 und 1.3', 'Abschnitt 2', 'Aufgaben 1.2, 1.3' -> ['1.2', '1.3'] (Reihenfolge wie im Text)."""
    found: list[tuple[int, str]] = []
    for m in _SECTION_KW.finditer(text):
        found.append((m.start(1), m.group(1)))
        # Aufzählungen direkt dahinter: "Abschnitt 1.2, 1.3 und 1.4"
        pos = m.end()
        while True:
            n = _ENUM_NEXT.match(text, pos)
            if not n:
                break
            found.append((n.start(1), n.group(1)))
            pos = n.end()
    for m in _DOTTED.finditer(text):
        found.append((m.start(1), m.group(1)))
    out: list[str] = []
    for _, sec in sorted(found):
        if sec not in out:
            out.append(sec)
    return out


def _resolve_uebung(store: Store, course: str, question: str, files) -> Reference | None:
    um = _UEBUNG.search(question)
    if not um:
        return None
    n = int(um.group(1))
    hits = [r for r in files if r["doc_type"] == "uebungsstunde"
            and re.search(rf"(^|\D)0?{n}(\D|$)", r["pair_key"] or "")]
    if len(hits) != 1:
        return None
    pair = hits[0]["pair_key"]
    rest = question[:um.start()] + " " + question[um.end():]
    secs = _sections(rest)
    if not secs:
        return Reference("uebungsstunde", pair, None, None, store.file_chunks(course, pair, "uebungsstunde"))
    chunks, seen = [], set()
    for sec in secs:
        for c in store.problem_chunks(course, pair, "uebungsstunde", sec):
            if c.id not in seen:
                chunks.append(c)
                seen.add(c.id)
    return Reference("uebungsstunde", pair, ", ".join(secs), None, chunks)


_PLAN_REF = re.compile(r"(?<![\w.])[Pp](\d{1,2})(?:\s*([a-h])\)?)?(?![\w.])")


def resolve(store: Store, course: str, question: str, plan_lookup=None) -> Reference | None:
    """plan_lookup(n) -> Lernplan-Eintrag P<n> (dict mit pair_key, doc_type, problem_no) oder None."""
    if plan_lookup is not None:
        pm_ = _PLAN_REF.search(question)
        if pm_:
            item = plan_lookup(int(pm_.group(1)))
            if item:
                chunks = store.problem_chunks(course, item["pair_key"], item["doc_type"], item["problem_no"])
                if chunks:
                    return Reference(item["doc_type"], item["pair_key"], item["problem_no"], pm_.group(2), chunks)
    files = [r for r in store.files(course) if r["status"] != "placeholder"]
    ub = _resolve_uebung(store, course, question, files)
    if ub is not None:
        return ub
    pm = _PROBLEM.search(question)
    problem_no = pm.group(1) if pm else None
    part = pm.group(2).lower() if pm and pm.group(2) else None
    if file_hint(question):
        # Datei ausdrücklich genannt ("exercise_Session_5 aufgabe 5"): nur dort suchen, nicht im ganzen Fach
        by_file = _resolve_file(store, course, question, files, problem_no, part)
        if by_file is not None:
            return by_file
    named = _resolve_named(store, course, question, files, problem_no, part)
    if named is not None and named.chunks:
        return named
    by_file = _resolve_file(store, course, question, files, problem_no, part)
    return by_file if by_file is not None else named


def _resolve_named(store: Store, course: str, question: str, files, problem_no: str | None,
                   part: str | None) -> Reference | None:
    """Serie/Prüfung über Stichwort + Nummer/Datum ("Serie 3", "Prüfung Aug 2025")."""

    if _EXAM.search(question):
        tokens = _exam_tokens(question)
        exams = [r for r in files if r["doc_type"] == "pruefung"]
        scored = sorted(((_pair_matches(r["pair_key"] or "", tokens), r) for r in exams),
                        key=lambda x: -x[0])
        if not scored or (tokens and scored[0][0] <= 0):
            return None
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None   # mehrdeutig -> lieber normale Suche
        best = scored[0][1]
        kind, pair = "pruefung", best["pair_key"]
    else:
        sm = _SERIE.search(question)
        series = []
        if sm:
            n = int(sm.group(1))
            series = [r for r in files if r["doc_type"] == "serie"
                      and re.search(rf"(^|\D){n}$", (r["pair_key"] or ""))]
        if sm is None and problem_no:
            # "Aufgabe 45" ohne Blatt: durchnummerierte Aufgaben über alle Blätter
            return _resolve_by_number(store, course, question, problem_no, part)
        if len(series) != 1:
            return None            # genannte Serie gibt es nicht (eindeutig) -> nicht raten
        kind, pair = "serie", series[0]["pair_key"]

    if problem_no is None:
        # ganzes Blatt / ganze Prüfung ("Was ist in Serie 3 wichtig?")
        return Reference("datei", pair, None, part, store.file_chunks(course, pair, kind))
    chunks = store.problem_chunks(course, pair, kind, problem_no)
    return Reference(kind, pair, problem_no, part, chunks)


# ---------------------------------------------------------------------- Dateinamen
def _norm(text: str) -> str:
    """'Exercise_Session_5.pdf' / 'ecercise_Session_5' -> 'exercise session 5' (Kleinbuchstaben, Trennzeichen
    -> Leerzeichen, Ziffern von Buchstaben getrennt, Umlaute vereinheitlicht)."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    t = re.sub(r"\.(pdf|docx?|pptx?|md|txt|tex|ipynb)\b", " ", t)
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


_FILE_HINT = re.compile(r"\b[\w\-]*[a-zA-Z][\w\-]*_[\w\-]+\b|\b[\w\-]+\.(?:pdf|docx|pptx|ipynb)\b")


_DOC_WORDS = ("exercise", "session", "serie", "series", "sheet", "uebung", "übung", "exam", "pruefung", "prüfung",
              "quiz", "lecture", "vorlesung", "script", "skript", "solution", "solutions", "loesung", "lösung",
              "blatt", "homework", "assignment", "lab", "tutorial", "notes", "mock", "midterm", "slides", "folien")


def file_hint(question: str) -> str | None:
    """Token, das wie ein Dateiname aussieht ('Exercise_Session_5', 'ecercise_Session_5', 'blatt3.pdf');
    Formelnamen wie 'x_1' oder 'lambda_max' zählen nicht."""
    for m in _FILE_HINT.finditer(question or ""):
        tok = m.group(0)
        if re.search(r"\.(pdf|docx|pptx|ipynb)$", tok, re.I):
            return tok
        for part in re.split(r"[_\-]+", tok.lower()):
            word = re.sub(r"\d+", "", part)
            if len(word) >= 3 and any(SequenceMatcher(None, word, w).ratio() >= 0.75 for w in _DOC_WORDS):
                return tok
    return None


def _file_score(qtokens: list[str], stem: str) -> float:
    st = stem.split()
    if len(st) < 2 and not (len(st) == 1 and len(st[0]) >= 6):
        return 0.0                         # zu kurz ("ex 8"), dafür gibt es die Stichwort-Regeln
    if f" {stem} " in f" {' '.join(qtokens)} ":
        return 1.0
    digits = [t for t in st if t.isdigit()]
    best = 0.0
    k = len(st)
    for i in range(0, max(1, len(qtokens) - k + 1)):
        win = qtokens[i:i + k]
        if [t for t in win if t.isdigit()] != digits:
            continue                       # Nummern müssen exakt stimmen (Session 5 ≠ Session 15)
        r = SequenceMatcher(None, " ".join(win), stem).ratio()
        best = max(best, r)
    return best if best >= 0.85 else 0.0


def _resolve_file(store: Store, course: str, question: str, files, problem_no: str | None,
                  part: str | None) -> Reference | None:
    """Datei über ihren Namen, auch mit Tippfehler: 'ecercise_Session_5' -> Exercise_Session_5.pdf."""
    qtokens = _norm(question).split()
    if not qtokens:
        return None
    scored = []
    for r in files:
        sc = _file_score(qtokens, _norm(Path(r["rel_path"]).stem))
        if sc:
            is_solution = (r["doc_type"] or "").endswith("_loesung")
            wants_solution = bool(re.search(r"l(ö|oe)sung|solution", question, re.I))
            scored.append((sc + (0.01 if is_solution == wants_solution else 0), r))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-9:
        return None
    r = scored[0][1]
    whole = store.chunks_of_file(course, r["rel_path"])
    if problem_no:
        chunks = store.problem_chunks(course, r["pair_key"], r["doc_type"], problem_no)
        if chunks:
            return Reference(r["doc_type"], r["pair_key"], problem_no, part, chunks)
        # Aufgabe nicht in dieser Datei: ganze Datei mit der gesuchten Nummer zurück (Aufrufer entscheidet)
        return Reference("datei", r["pair_key"], problem_no, part, whole)
    return Reference("datei", r["pair_key"], None, part, whole)


def _resolve_by_number(store: Store, course: str, question: str, problem_no: str,
                       part: str | None) -> Reference | None:
    """Aufgabennummer ohne Blatt (z. B. durchnummerierte SST-Übungen): genau eine Datei mit dieser Aufgabe,
    bei mehreren entscheiden Wörter aus der Frage, die im Dateinamen vorkommen."""
    chunks = [c for c in store.chunks_with_problem(course, problem_no) if c.doc_type in _PROBLEM_TYPES]
    groups: dict[tuple[str, str], list] = {}
    for c in chunks:
        groups.setdefault((c.rel_path, c.doc_type), []).append(c)
    if not groups:
        return None
    if len(groups) > 1:
        words = {w for w in re.findall(r"[a-zäöü]{3,}", question.lower())
                 if w not in {"aufgabe", "check", "bitte", "erkläre", "löse", "zeige", "von", "und", "der", "die",
                              "das", "den", "mit", "für", "ich", "mir"}}
        def score(key):
            name = key[0].lower().replace("ue", "ü")
            s = sum(1 for w in words if w.replace("ue", "ü").rstrip("en").rstrip("n") in name)
            return (s, bool(store.solution_for(groups[key][0])), key[1] == "serie")
        ranked = sorted(groups, key=score, reverse=True)
        if score(ranked[0]) == score(ranked[1]):
            return None            # mehrdeutig -> normale Suche
        key = ranked[0]
    else:
        key = next(iter(groups))
    found = groups[key]
    kind = "serie" if key[1] == "serie" else key[1]
    return Reference(kind, found[0].pair_key, problem_no, part, found)
