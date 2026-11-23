"""PDF -> Seiten mit Zeilen, Überschriften und gedruckter Seitenzahl (PyMuPDF).

Formeln werden dabei nur als flacher Text gelesen (Exponenten/Brüche gehen verloren).
Für die Suche reicht das; für formelgetreue Wiedergabe kommt später eine einmalige
Vision-Umwandlung dazu (siehe docs/indexer.md).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

_HEADING_NUM = re.compile(r"^(\d+(?:\.\d+){0,3})\.?$")
_HEADING_NUM_TITLE = re.compile(r"^(\d+(?:\.\d+){0,3})\.?\s+(\S.*)$")
_PAGE_NUM = re.compile(r"^\d{1,4}$")
_TOC_DOTS = re.compile(r"(\.\s){4,}")
_BOLD_FONT = re.compile(r"(BX|Bold|Heavy|Black|Semibold|SFBX|CMBX)", re.I)


@dataclass
class Line:
    text: str
    size: float
    bold: bool
    heading_no: str | None = None   # "8.2" bei nummerierten Überschriften
    heading_title: str | None = None
    minor_heading: bool = False      # fett, aber ohne Nummer (z. B. "Solution")

    @property
    def level(self) -> int | None:
        return self.heading_no.count(".") + 1 if self.heading_no else None


@dataclass
class Page:
    pdf_page: int                    # 1-basiert
    printed_page: str | None
    lines: list[Line] = field(default_factory=list)
    is_toc: bool = False

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)


@dataclass
class PdfDoc:
    path: Path
    pages: list[Page]
    header_lines: list[str]          # entfernte Kopfzeilen (für Metadaten)
    first_page_text: str
    empty_pages: list[int]           # Seiten ohne Textschicht (Scan) -> OCR nötig

    @property
    def needs_ocr(self) -> bool:
        return bool(self.empty_pages)


def _norm(s: str) -> str:
    # NFKC: PDF-Ligaturen (ﬁ, ﬀ) zu normalen Buchstaben, damit Suche und Titel stimmen
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip()


def _raw_lines(page: pymupdf.Page) -> list[Line]:
    out: list[Line] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = _norm("".join(s["text"] for s in line["spans"]))
            size = max(s["size"] for s in spans)
            bold = all(bool(_BOLD_FONT.search(s["font"])) or bool(s["flags"] & 16) for s in spans)
            out.append(Line(text=text, size=round(size, 1), bold=bold))
    return out


def _body_size(pages: list[list[Line]]) -> float:
    c: Counter[float] = Counter()
    for lines in pages:
        for l in lines:
            c[l.size] += len(l.text)
    return c.most_common(1)[0][0] if c else 12.0


def _running_headers(pages: list[list[Line]]) -> set[str]:
    """Zeilen, die auf den meisten Seiten oben wiederkehren (Kopfzeilen)."""
    if len(pages) < 3:
        return set()
    c: Counter[str] = Counter()
    for lines in pages:
        for l in {x.text for x in lines[:5]}:
            c[l] += 1
    thresh = max(2, int(0.5 * len(pages)))
    return {t for t, n in c.items() if n >= thresh and not _PAGE_NUM.match(t) and len(t) < 80}


def _printed_pages(pages: list[list[Line]]) -> list[str | None]:
    """Gedruckte Seitenzahl je Seite: letzte (sonst erste) reine Zahl, mit Offset-Konsistenz."""
    detected: list[int | None] = []
    for lines in pages:
        num = None
        for cand in (lines[-1:] + lines[:1]):
            # fette Zahl oben ist eher eine Aufgabennummer als eine Seitenzahl
            if _PAGE_NUM.match(cand.text) and not cand.bold:
                num = int(cand.text)
                break
        detected.append(num)
    offsets = Counter(i + 1 - n for i, n in enumerate(detected) if n is not None)
    if offsets:
        off, count = offsets.most_common(1)[0]
        if count >= max(2, 0.6 * sum(1 for n in detected if n is not None)):
            return [str(i + 1 - off) if i + 1 - off >= 1 else None for i in range(len(pages))]
    return [str(n) if n is not None else None for n in detected]


def _mark_headings(lines: list[Line], body: float) -> None:
    """Nummerierte fette Überschriften erkennen; '8' + 'Titel' auf zwei Zeilen zusammenführen."""
    i = 0
    while i < len(lines):
        l = lines[i]
        is_big_bold = l.bold and l.size >= body * 1.1
        if is_big_bold:
            m = _HEADING_NUM.match(l.text)
            m2 = _HEADING_NUM_TITLE.match(l.text)
            if m and i + 1 < len(lines) and lines[i + 1].bold and not _HEADING_NUM.match(lines[i + 1].text):
                l.heading_no, l.heading_title = m.group(1), lines[i + 1].text
                l.text = f"{m.group(1)} {lines[i + 1].text}"
                del lines[i + 1]
            elif m2:
                l.heading_no, l.heading_title = m2.group(1), m2.group(2)
            elif not _PAGE_NUM.match(l.text):
                l.minor_heading = True
            if l.heading_no:
                # Über mehrere Zeilen umbrochene Überschrift ("... Photoelec-" / "tric Effect",
                # "(1" / "pt)") anhängen.
                while (i + 1 < len(lines) and lines[i + 1].bold
                       and abs(lines[i + 1].size - l.size) < 0.6
                       and not _HEADING_NUM.match(lines[i + 1].text)
                       and not _HEADING_NUM_TITLE.match(lines[i + 1].text)
                       and len(lines[i + 1].text) < 60):
                    nxt = lines.pop(i + 1).text
                    joined = (l.heading_title[:-1] + nxt) if l.heading_title.endswith("-") \
                        else f"{l.heading_title} {nxt}"
                    l.heading_title = joined
                    l.text = f"{l.heading_no} {joined}"
        elif l.bold and l.size >= body * 0.95:
            m = _HEADING_NUM.match(l.text)
            if m and i + 1 < len(lines) and lines[i + 1].bold and "." in m.group(1):
                # z. B. Unterabschnitt "8.2.1" + "Setup" in Textgrösse
                l.heading_no, l.heading_title = m.group(1), lines[i + 1].text
                l.text = f"{m.group(1)} {lines[i + 1].text}"
                del lines[i + 1]
        i += 1


def read_pdf(path: Path) -> PdfDoc:
    doc = pymupdf.open(path)
    raw = [_raw_lines(p) for p in doc]
    first_page_text = "\n".join(l.text for l in raw[0]) if raw else ""
    empty = [i + 1 for i, lines in enumerate(raw) if sum(len(l.text) for l in lines) < 50]
    body = _body_size(raw)
    headers = _running_headers(raw)
    printed = _printed_pages(raw)

    pages: list[Page] = []
    for i, lines in enumerate(raw):
        kept = [l for l in lines if l.text not in headers]
        # Seitenzahl-Zeile entfernen
        if printed[i] is not None:
            if kept and kept[-1].text == printed[i]:
                kept = kept[:-1]
            elif kept and kept[0].text == printed[i]:
                kept = kept[1:]
        _mark_headings(kept, body)
        toc_lines = sum(1 for l in kept if _TOC_DOTS.search(l.text))
        pages.append(Page(pdf_page=i + 1, printed_page=printed[i], lines=kept,
                          is_toc=toc_lines >= 5 and toc_lines >= 0.2 * max(1, len(kept))))
    doc.close()
    return PdfDoc(path=path, pages=pages, header_lines=sorted(headers),
                  first_page_text=first_page_text, empty_pages=empty)
