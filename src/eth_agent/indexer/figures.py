"""Seitenbilder für Aufgaben mit Abbildungen (Schaltungen, Skizzen, Plots).

Der Index enthält nur den Text einer PDF-Seite; bei "Bestimme den Arbeitspunkt in Figure 5" fehlt dem Modell
sonst die Schaltung. Für die Seiten einer Aufgabe, die Bilder oder viele Vektorzeichnungen enthalten, wird ein
PNG gerendert (zwischengespeichert) und Codex als Bild mitgegeben.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from ..config import Config, Course
    from ..retrieval.store import ChunkRow

MIN_DRAWINGS = 15          # so viele Vektor-Elemente gelten als Abbildung (eine Schaltung hat Dutzende)
DPI = 110


def has_figure(page) -> bool:
    try:
        if page.get_images(full=True):
            return True
        return len(page.get_drawings()) >= MIN_DRAWINGS
    except Exception:
        return False


def page_images(cfg: "Config", course: "Course", chunks: Iterable["ChunkRow"], max_pages: int = 2) -> list[str]:
    """PNG-Pfade der Seiten mit Abbildungen, in Lesereihenfolge, höchstens max_pages."""
    import pymupdf

    wanted: list[tuple[str, int]] = []
    for c in chunks:
        if not c.rel_path.lower().endswith(".pdf") or not c.page_start:
            continue
        for p in range(c.page_start, (c.page_end or c.page_start) + 1):
            if (c.rel_path, p) not in wanted:
                wanted.append((c.rel_path, p))
    out: list[str] = []
    cache = cfg.data_dir / "cache" / "figures"
    for rel, pno in wanted:
        if len(out) >= max_pages:
            break
        src = cfg.material_root / course.folder / rel
        if not src.exists():
            continue
        key = hashlib.sha1(f"{src}|{src.stat().st_mtime_ns}|{pno}|{DPI}".encode()).hexdigest()[:16]
        png = cache / f"{key}.png"
        if png.exists():
            out.append(str(png))
            continue
        try:
            with pymupdf.open(src) as doc:
                if not 1 <= pno <= doc.page_count:
                    continue
                page = doc[pno - 1]
                if not has_figure(page):
                    continue
                cache.mkdir(parents=True, exist_ok=True)
                page.get_pixmap(dpi=DPI).save(str(png))
                out.append(str(png))
        except Exception:
            continue
    return out


def figure_note(n: int) -> str:
    return (f"\n\n(The first {n} attached image(s) are the course PDF page(s) of this problem, including its "
            "figures/circuits – use them for values and topology that the extracted text lacks.)")
