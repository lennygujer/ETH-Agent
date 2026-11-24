"""Verweise wie "Serie 3 Aufgabe 2", "Prüfung Aug 2025 Aufgabe 4" oder "Übungsstunde 11, 1.2 und 1.3"
direkt im Index auflösen.

Statt die Aufgabe per Suche zu raten, wird der passende Chunk exakt geholt (und die Musterlösung,
falls vorhanden).
"""

from __future__ import annotations

import re
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


def resolve(store: Store, course: str, question: str) -> Reference | None:
    files = [r for r in store.files(course) if r["status"] != "placeholder"]
    ub = _resolve_uebung(store, course, question, files)
    if ub is not None:
        return ub
    pm = _PROBLEM.search(question)
    problem_no = pm.group(1) if pm else None
    part = pm.group(2).lower() if pm and pm.group(2) else None

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
        return Reference(kind, pair, None, part, [])
    chunks = store.problem_chunks(course, pair, kind, problem_no)
    return Reference(kind, pair, problem_no, part, chunks)


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
