"""Andere Dateitypen als PDF -> dieselbe Seitenstruktur (PdfDoc), damit Chunking/Index gleich bleiben.

- .txt .md .tex und Code: Text, je ~80 Zeilen eine "Seite"
- .ipynb: Markdown- und Code-Zellen
- .docx: Absätze (Überschriften-Stile werden erkannt)
- .pptx: eine Folie = eine Seite
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .doctypes import CODE_EXT
from .pdf import Line, Page, PdfDoc, read_pdf

TEXT_EXT = {".txt", ".md", ".tex"}
OFFICE_EXT = {".docx", ".pptx"}
SUPPORTED = {".pdf"} | TEXT_EXT | OFFICE_EXT | CODE_EXT
MAX_TEXT_BYTES = 2_000_000
MAX_CODE_BYTES = 300_000
LINES_PER_PAGE = 80
_MD_HEADING = re.compile(r"^(#{1,3})\s+(.+)$")


def _doc_from_lines(path: Path, lines: list[str], page_breaks: list[int] | None = None) -> PdfDoc:
    pages: list[Page] = []
    if page_breaks is None:
        page_breaks = list(range(0, len(lines), LINES_PER_PAGE))
    bounds = page_breaks + [len(lines)]
    for i in range(len(bounds) - 1):
        chunk = [l for l in lines[bounds[i]:bounds[i + 1]]]
        pg = Page(pdf_page=i + 1, printed_page=str(i + 1))
        for l in chunk:
            if not l.strip():
                continue
            m = _MD_HEADING.match(l.strip())
            line = Line(text=l.rstrip(), size=12.0, bold=bool(m))
            if m:
                line.minor_heading = True
            pg.lines.append(line)
        pages.append(pg)
    first = "\n".join(l.text for l in pages[0].lines) if pages else ""
    empty = [p.pdf_page for p in pages if sum(len(l.text) for l in p.lines) < 20]
    return PdfDoc(path=path, pages=pages, header_lines=[], first_page_text=first,
                  empty_pages=empty if len(empty) == len(pages) else [])


def _read_text(path: Path) -> PdfDoc:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return _doc_from_lines(path, text.splitlines())


def _read_ipynb(path: Path) -> PdfDoc:
    nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    lines: list[str] = []
    for cell in nb.get("cells", []):
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else str(src)
        if cell.get("cell_type") == "code":
            lines += ["```python", *src.splitlines(), "```"]
        else:
            lines += src.splitlines()
        lines.append("")
    return _doc_from_lines(path, lines)


def _read_docx(path: Path) -> PdfDoc:
    import docx  # python-docx

    d = docx.Document(str(path))
    lines: list[str] = []
    for para in d.paragraphs:
        t = para.text.strip()
        if not t:
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        if style.startswith("heading") or style.startswith("überschrift"):
            lines.append("## " + t)
        else:
            lines.append(t)
    for table in d.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return _doc_from_lines(path, lines)


def _read_pptx(path: Path) -> PdfDoc:
    from pptx import Presentation  # python-pptx

    prs = Presentation(str(path))
    lines: list[str] = []
    breaks: list[int] = []
    for i, slide in enumerate(prs.slides, 1):
        breaks.append(len(lines))
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        lines.append(f"## Folie {i}" + (f": {title}" if title else ""))
        for shape in slide.shapes:
            if shape.has_text_frame and shape is not slide.shapes.title:
                for para in shape.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip()
                    if t:
                        lines.append(t)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append("Notizen: " + notes)
    return _doc_from_lines(path, lines, page_breaks=breaks or None)


def too_large(path: Path) -> bool:
    ext = path.suffix.lower()
    size = path.stat().st_size
    if ext in CODE_EXT:
        return size > MAX_CODE_BYTES
    if ext in TEXT_EXT:
        return size > MAX_TEXT_BYTES
    return size > 200_000_000


def read_document(path: Path) -> PdfDoc:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return read_pdf(path)
    if ext == ".ipynb":
        return _read_ipynb(path)
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".pptx":
        return _read_pptx(path)
    return _read_text(path)
