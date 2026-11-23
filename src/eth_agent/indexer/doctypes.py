"""Dokumenttyp, Paarschlüssel (Aufgabe <-> Lösung) und Metadaten aus Pfad und Deckblatt."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

PROBLEM_TYPES = {"serie", "serie_loesung", "pruefung", "pruefung_loesung", "uebungsstunde"}
SOLUTION_TYPES = {"serie_loesung", "pruefung_loesung"}
ALL_TYPES = PROBLEM_TYPES | {"skript", "slides", "notizen", "formelsammlung", "lernziele", "paper", "sonstiges"}

_EXAM = re.compile(r"exam|pr(ue|ü)fung|klausur|mock", re.I)
_SOL = re.compile(r"(^|[_\s-])(sol|sols|solns?|solutions?|l(oe|ö)sung(en)?|ml)([_\s.-]|$)", re.I)
_FORMULA = re.compile(r"formula|formel", re.I)
_SCRIPT = re.compile(r"script|skript|lecture[_\s-]?notes", re.I)
_SLIDES = re.compile(r"slides|folien", re.I)


def classify(rel_path: str, folder_doc_types: dict[str, str]) -> str:
    """rel_path relativ zum Kursordner, z. B. 'Exercise Solutions/Ex_3_solutions.pdf'."""
    p = PurePosixPath(rel_path)
    name = p.stem
    folder_type = None
    for part in p.parts[:-1]:
        t = folder_doc_types.get(part.lower())
        if t:
            folder_type = t
    is_solution = bool(_SOL.search(name)) or (folder_type in SOLUTION_TYPES)

    if _FORMULA.search(name):
        return "formelsammlung"
    if _EXAM.search(name):
        return "pruefung_loesung" if is_solution else "pruefung"
    if folder_type in {"skript", None}:
        if _SCRIPT.search(name):
            return "skript"
        if _SLIDES.search(name):
            return "slides"
    if folder_type:
        if folder_type == "serie" and is_solution:
            return "serie_loesung"
        return folder_type
    return "sonstiges"


_PAIR_DROP = re.compile(
    r"\b(sol|sols|solns?|solutions?|l(oe|ö)sung(en)?|final|ml|v\d+)\b", re.I)


def pair_key(rel_path: str) -> str:
    """Normalisierter Name, gleich für Aufgabe und zugehörige Lösung.

    'PhysII_ITET_Exam_2024_Jan_final_solns.pdf' und 'PhysII_ITET_Exam_2024_Jan_final.pdf'
    ergeben beide 'physii itet exam 2024 jan'.
    """
    stem = PurePosixPath(rel_path).stem.lower()
    stem = re.sub(r"[_\-.]+", " ", stem)
    stem = re.sub(r"(?<=[a-z])(?=\d)", " ", stem) if re.match(r"^ex\d", stem) else stem
    stem = _PAIR_DROP.sub(" ", stem)
    return re.sub(r"\s+", " ", stem).strip()


_SEMESTER = re.compile(r"\b(HS|FS)\s?(\d{2}(?:\d{2})?)\b")
_YEAR = re.compile(r"\b(20\d{2})\b")
_LECTURER = re.compile(r"Prof\.?\s+(?:Dr\.?\s+)?([A-ZÄÖÜ]\.\s*(?:[A-ZÄÖÜ]\.\s*)?[A-ZÄÖÜ][\wäöüé-]+)")
_TITLE_HINT = re.compile(r"(Final Exam|Resit Exam|Mock Exam|Exercise\s+\d+|Solution\s+\d+|Exercise Class Week\s+\d+)", re.I)


def cover_metadata(first_page_text: str, rel_path: str) -> dict[str, str | None]:
    text = first_page_text[:1500]
    sem = _SEMESTER.search(text)
    semester = None
    if sem:
        y = sem.group(2)
        semester = f"{sem.group(1)}{y if len(y) == 4 else '20' + y}"
    years = _YEAR.findall(text) or _YEAR.findall(rel_path)
    lect = _LECTURER.search(text)
    hint = _TITLE_HINT.search(text)
    return {
        "semester": semester,
        "year": max(years) if years else None,
        "lecturer": re.sub(r"\s+", " ", lect.group(1)) if lect else None,
        "title": hint.group(1) if hint else None,
    }
