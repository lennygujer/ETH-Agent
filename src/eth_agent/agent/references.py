"""Verweise wie "Serie 3 Aufgabe 2" oder "Prüfung Aug 2025 Aufgabe 4" direkt im Index auflösen.

Statt die Aufgabe per Suche zu raten, wird der passende Chunk exakt geholt (und die Musterlösung,
falls vorhanden).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..retrieval.store import ChunkRow, Store

_SERIE = re.compile(r"\b(?:serie|exercise\s*sheet|exercise|übungsblatt|uebungsblatt|blatt|ex)\s*_?(\d{1,2})\b", re.I)
_PROBLEM = re.compile(r"\b(?:aufgabe|aufg\.?|problem|task|frage|question|nr\.?)\s*(\d{1,2}(?:\.\d{1,2}){0,2})\s*([a-h])?\)?",
                      re.I)
_EXAM = re.compile(r"\b(prüfung|pruefung|exam|klausur|mock|probeprüfung|resit)\b", re.I)
_MONTHS = {
    "jan": "jan", "januar": "jan", "january": "jan", "feb": "feb", "februar": "feb", "february": "feb",
    "aug": "aug", "august": "aug", "sep": "sep", "september": "sep", "jun": "jun", "juni": "jun", "june": "jun",
}


@dataclass
class Reference:
    kind: str                  # "serie" | "pruefung"
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


def resolve(store: Store, course: str, question: str) -> Reference | None:
    pm = _PROBLEM.search(question)
    problem_no = pm.group(1) if pm else None
    part = pm.group(2).lower() if pm and pm.group(2) else None

    files = [r for r in store.files(course) if r["status"] != "placeholder"]
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
        if not sm:
            return None
        n = int(sm.group(1))
        series = [r for r in files if r["doc_type"] == "serie"
                  and re.search(rf"(^|\D){n}$", (r["pair_key"] or ""))]
        if len(series) != 1:
            return None
        kind, pair = "serie", series[0]["pair_key"]

    if problem_no is None:
        return Reference(kind, pair, None, part, [])
    chunks = store.problem_chunks(course, pair, kind, problem_no)
    return Reference(kind, pair, problem_no, part, chunks)
