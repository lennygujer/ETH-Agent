"""Seiten -> Chunks.

- Serien, Prüfungen, Lösungen, Übungsstunden: ein Chunk pro nummerierter Aufgabe
  (Teilaufgaben a), b), ... bleiben zusammen). Übergeordnete Überschriften ohne eigenen
  Text (z. B. "1 Multiple choice questions") werden als Kontext vorangestellt.
- Skript, Notizen, Formelsammlung: nach Abschnitten (x.y), zu grosse Abschnitte werden
  an Zeilengrenzen mit Überlappung geteilt. Inhaltsverzeichnis-Seiten werden übersprungen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .doctypes import PROBLEM_TYPES
from .pdf import Line, PdfDoc

_POINTS = re.compile(r"\((\d+(?:[.,]\d+)?)\s*(?:pt|pts|points?|p|punkte?)\.?\)", re.I)
MAX_PROBLEM_TOKENS = 2500


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Chunk:
    ord: int
    text: str
    title: str | None
    section: str | None          # Pfad der Überschriften, z. B. "8 Infinite Well > 8.2 Particle"
    problem_no: str | None
    points: float | None
    page_start: int
    page_end: int
    printed_start: str | None
    printed_end: str | None

    @property
    def tokens(self) -> int:
        return est_tokens(self.text)


@dataclass
class _Block:
    """Zusammenhängender Text unter einer Überschrift."""
    heading: Line | None
    path: list[Line]
    lines: list[tuple[int, str | None, str]]   # (pdf_page, printed_page, text)

    def text(self) -> str:
        return "\n".join(t for _, _, t in self.lines)


def _blocks(doc: PdfDoc, split_level: int) -> list[_Block]:
    """Teilt den Text an nummerierten Überschriften bis zur Tiefe split_level."""
    blocks: list[_Block] = []
    stack: list[Line] = []
    cur = _Block(heading=None, path=[], lines=[])
    for page in doc.pages:
        if page.is_toc:
            continue
        for line in page.lines:
            lvl = line.level
            if lvl is not None and lvl <= split_level:
                blocks.append(cur)
                stack = [h for h in stack if (h.level or 0) < lvl] + [line]
                cur = _Block(heading=line, path=list(stack), lines=[])
                cur.lines.append((page.pdf_page, page.printed_page, line.text))
            else:
                cur.lines.append((page.pdf_page, page.printed_page, line.text))
    blocks.append(cur)
    return [b for b in blocks if b.lines]


def _split_by_size(lines: list[tuple[int, str | None, str]], target: int, overlap: int):
    """Zeilenweise in Stücke von ~target Tokens mit ~overlap Tokens Überlappung."""
    parts, cur, cur_tok = [], [], 0
    for ln in lines:
        t = est_tokens(ln[2])
        if cur and cur_tok + t > target:
            parts.append(cur)
            back, back_tok = [], 0
            for prev in reversed(cur):
                back_tok += est_tokens(prev[2])
                if back_tok > overlap:
                    break
                back.insert(0, prev)
            cur, cur_tok = list(back), sum(est_tokens(x[2]) for x in back)
        cur.append(ln)
        cur_tok += t
    if cur:
        parts.append(cur)
    return parts


def _section_label(path: list[Line]) -> str | None:
    return " > ".join(h.text for h in path) if path else None


def _make_chunk(ord_: int, lines, title, section, problem_no, points) -> Chunk:
    return Chunk(
        ord=ord_, text="\n".join(t for _, _, t in lines), title=title, section=section,
        problem_no=problem_no, points=points,
        page_start=lines[0][0], page_end=lines[-1][0],
        printed_start=lines[0][1], printed_end=lines[-1][1],
    )


def chunk_problems(doc: PdfDoc) -> list[Chunk]:
    blocks = _blocks(doc, split_level=3)
    chunks: list[Chunk] = []
    for i, b in enumerate(blocks):
        body = b.lines[1:] if b.heading else b.lines
        body_text = "\n".join(t for _, _, t in body).strip()
        if b.heading is None:
            if body_text:
                chunks.append(_make_chunk(len(chunks), b.lines, "Deckblatt / Hinweise", None, None, None))
            continue
        # Überschrift ohne eigenen Text, auf die Unteraufgaben folgen (z. B. "1 Multiple choice
        # questions" -> "1.1 ..."): kein eigener Chunk, sie erscheint als Kontext bei den Kindern.
        nxt = blocks[i + 1].heading if i + 1 < len(blocks) else None
        has_children = bool(nxt and nxt.heading_no and nxt.heading_no.startswith(b.heading.heading_no + "."))
        if has_children and est_tokens(body_text) < 15 and not _POINTS.search(b.heading.text):
            continue
        pts = _POINTS.search(b.heading.text)
        points = float(pts.group(1).replace(",", ".")) if pts else None
        title = _POINTS.sub("", b.heading.heading_title or "").strip() or None
        parents = [h.text for h in b.path[:-1]]
        section = " > ".join(parents + [b.heading.text])
        lines = b.lines
        if parents:
            lines = [(lines[0][0], lines[0][1], p) for p in parents] + lines
        parts = [lines] if est_tokens("\n".join(t for _, _, t in lines)) <= MAX_PROBLEM_TOKENS \
            else _split_by_size(lines, MAX_PROBLEM_TOKENS, 150)
        for part in parts:
            chunks.append(_make_chunk(len(chunks), part, title, section, b.heading.heading_no, points))
    return chunks


def chunk_prose(doc: PdfDoc, target: int, overlap: int) -> list[Chunk]:
    blocks = _blocks(doc, split_level=2)
    chunks: list[Chunk] = []
    carry: list = []   # sehr kurze Blöcke (z. B. Kapitelüberschrift + ein Satz) an den nächsten hängen
    for i, b in enumerate(blocks):
        lines = carry + b.lines
        if est_tokens("\n".join(t for _, _, t in lines)) < 40 and i + 1 < len(blocks):
            carry = lines
            continue
        carry = []
        title = b.heading.heading_title if b.heading else None
        section = _section_label(b.path)
        for part in _split_by_size(lines, target, overlap):
            if est_tokens("\n".join(t for _, _, t in part)) < 10:
                continue
            chunks.append(_make_chunk(len(chunks), part, title, section, None, None))
    return chunks


def chunk_document(doc: PdfDoc, doc_type: str, target: int = 600, overlap: int = 80) -> list[Chunk]:
    if doc_type in PROBLEM_TYPES:
        return chunk_problems(doc)
    if doc_type == "formelsammlung":
        return chunk_prose(doc, target=300, overlap=0)
    return chunk_prose(doc, target=target, overlap=overlap)
