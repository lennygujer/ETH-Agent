"""Dokumenttyp, Paarschlüssel (Aufgabe <-> Lösung) und Metadaten aus Pfad und Deckblatt."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

PROBLEM_TYPES = {"serie", "serie_loesung", "pruefung", "pruefung_loesung", "uebungsstunde"}
SOLUTION_TYPES = {"serie_loesung", "pruefung_loesung"}
ALL_TYPES = PROBLEM_TYPES | {"skript", "slides", "notizen", "formelsammlung", "lernziele", "paper", "buch",
                             "code", "sonstiges"}

_EXAM = re.compile(r"exam|pr(ue|ü)fung|klausur|mock|probepr", re.I)
_SOL = re.compile(r"(^|[_\s-])(sol|sols|solns?|solutions?|l(oe|ö)sung(en)?|lsg|ml|musterl\w*)([_\s.-]|$)", re.I)
_SOL_FOLDER = re.compile(r"l(ö|oe)sung|solution|musterl|^sol$|^ml$", re.I)
_FORMULA = re.compile(r"formula|formel|cheat", re.I)
_SCRIPT = re.compile(r"script|skript|lecture[_\s-]?notes|manuscript", re.I)
_SLIDES = re.compile(r"slides|folien|^vl[_\s-]?\d|vorlesung|lecture[_\s-]?\d", re.I)

# Unscharfe Zuordnung beliebiger Ordner-/Dateinamen (Reihenfolge = Vorrang)
_FUZZY = [
    (re.compile(r"(ü|ue)bungsstunde|exercise[_\s-]?class|tutorium|tutorial", re.I), "uebungsstunde"),
    (_EXAM, "pruefung"),
    (re.compile(r"(ü|ue)bung|exercise|serie|sheet|blatt|assignment|homework|hausaufgabe|problem[_\s-]?set|"
                r"^ex[_\s-]?\d", re.I), "serie"),
    (_FORMULA, "formelsammlung"),
    (_SCRIPT, "skript"),
    (_SLIDES, "slides"),
    (re.compile(r"notiz|notes|zusammenfassung|summary|mitschrift|onenote", re.I), "notizen"),
    (re.compile(r"b(ü|ue)cher|buch|book|springer|literatur", re.I), "buch"),
    (re.compile(r"paper|artikel|article", re.I), "paper"),
    (re.compile(r"lernziel|learning[_\s-]?objective", re.I), "lernziele"),
]
CODE_EXT = {".py", ".m", ".java", ".c", ".cpp", ".h", ".hpp", ".v", ".sv", ".vhd", ".vhdl", ".jl", ".rs", ".js",
            ".ts", ".ipynb"}


def _fuzzy(name: str) -> str | None:
    for rx, t in _FUZZY:
        if rx.search(name):
            return t
    return None


def classify(rel_path: str, folder_doc_types: dict[str, str]) -> str:
    """rel_path relativ zum Kursordner, z. B. 'Exercise Solutions/Ex_3_solutions.pdf'.

    Vorrang: Dateiname (Prüfung/Formelsammlung) > exakte Ordnerzuordnung aus config > unscharfe
    Erkennung des tiefsten passenden Ordners > unscharfe Erkennung des Dateinamens.
    """
    p = PurePosixPath(rel_path)
    name = p.stem
    folders = list(p.parts[:-1])
    if p.suffix.lower() in CODE_EXT:
        return "code"

    folder_type = None
    for part in folders:                      # exakt (config), tiefster gewinnt
        t = folder_doc_types.get(part.lower())
        if t:
            folder_type = t
    if folder_type is None:
        for part in reversed(folders):        # unscharf, tiefster passender Ordner
            t = _fuzzy(part)
            if t:
                folder_type = t
                break
    is_solution = (bool(_SOL.search(name)) or folder_type in SOLUTION_TYPES
                   or any(_SOL_FOLDER.search(f) for f in folders))

    if _FORMULA.search(name):
        return "formelsammlung"
    if _EXAM.search(name) or folder_type in {"pruefung", "pruefung_loesung"}:
        return "pruefung_loesung" if is_solution else "pruefung"
    if folder_type in {"skript", "slides", "notizen", "buch", None}:
        if _SCRIPT.search(name):
            return "skript"
        if _SLIDES.search(name):
            return "slides"
    if folder_type is None:
        folder_type = _fuzzy(name)
        if folder_type is None and is_solution:
            folder_type = "serie_loesung"
    if folder_type:
        if folder_type == "serie" and is_solution:
            return "serie_loesung"
        return folder_type
    return "sonstiges"


_PAIR_DROP = re.compile(
    r"\b(sol|sols|solns?|solutions?|l(oe|ö)sung(en)?|lsg|final|ml|v\d+)\b", re.I)


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
