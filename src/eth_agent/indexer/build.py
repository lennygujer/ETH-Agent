"""Indexierung: Kursordner durchlaufen, geänderte Dateien neu einlesen, gelöschte entfernen."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, Course
from ..retrieval.store import Store
from .chunking import chunk_document
from .doctypes import classify, cover_metadata, pair_key
from .readers import SUPPORTED, read_document, too_large


@dataclass
class IndexReport:
    course: str
    added: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)   # 0-Byte-Dateien (OneDrive nicht geladen)
    needs_ocr: list[tuple[str, int, int]] = field(default_factory=list)  # (Datei, leere Seiten, Seiten)
    errors: list[tuple[str, str]] = field(default_factory=list)
    chunks: int = 0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def index_course(cfg: Config, store: Store, course: Course, *, force: bool = False,
                 log=print) -> IndexReport:
    root = cfg.material_root / course.folder
    rep = IndexReport(course=course.id)
    if not root.is_dir():
        rep.errors.append((str(root), "Kursordner nicht gefunden"))
        return rep

    known = store.file_hashes(course.id)
    seen: set[str] = set()
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                   and not any(cfg.is_ignored(part) for part in p.relative_to(root).parts))
    for path in files:
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        size = path.stat().st_size
        doc_type = classify(rel, cfg.folder_doc_types)

        if size == 0:
            rep.placeholders.append(rel)
            if known.get(rel, "x") is not None or force:
                store.add_file(course.id, rel, doc_type, sha256=None, size=0, n_pages=0,
                               status="placeholder", empty_pages=[], pair_key=pair_key(rel), meta={})
            continue

        if too_large(path):
            seen.discard(rel)
            continue
        digest = sha256(path)
        if not force and known.get(rel) == digest:
            rep.unchanged += 1
            continue
        try:
            doc = read_document(path)
        except Exception as e:  # defekte Dateien nicht den ganzen Lauf abbrechen lassen
            rep.errors.append((rel, str(e)))
            store.add_file(course.id, rel, doc_type, sha256=digest, size=size, n_pages=0,
                           status="error", empty_pages=[], pair_key=pair_key(rel), meta={}, error=str(e))
            continue

        meta = cover_metadata(doc.first_page_text, rel)
        n_pages = len(doc.pages)
        all_empty = len(doc.empty_pages) == n_pages
        status = "needs_ocr" if doc.empty_pages else "ok"
        if doc.empty_pages:
            rep.needs_ocr.append((rel, len(doc.empty_pages), n_pages))
        fid = store.add_file(course.id, rel, doc_type, sha256=digest, size=size, n_pages=n_pages,
                             status=status, empty_pages=doc.empty_pages, pair_key=pair_key(rel), meta=meta)
        store.add_pages(fid, [(p.pdf_page, p.printed_page, p.text) for p in doc.pages])
        chunks = [] if all_empty else chunk_document(
            doc, doc_type, cfg.retrieval.chunk_tokens, cfg.retrieval.overlap_tokens)
        store.add_chunks(fid, course.id, doc_type, chunks)
        rep.added.append(rel)
        rep.chunks += len(chunks)
        log(f"  + {rel}  [{doc_type}]  {n_pages} S., {len(chunks)} Chunks"
            + (f", {len(doc.empty_pages)} S. ohne Text" if doc.empty_pages else ""))

    for rel in set(known) - seen:
        store.delete_file(course.id, rel)
        rep.removed.append(rel)
    store.commit()
    return rep


def embed_missing(cfg: Config, store: Store, log=print) -> int:
    """Berechnet fehlende Embeddings, falls ein Modell konfiguriert und installiert ist."""
    from ..retrieval import embed

    model = cfg.retrieval.embedding_model
    if not model:
        return 0
    if not embed.available():
        log("  Embeddings übersprungen: sentence-transformers nicht installiert (pip install '.[embed]').")
        return 0
    todo = store.chunks_without_embedding(model)
    if not todo:
        return 0
    log(f"  Berechne Embeddings für {len(todo)} Chunks mit {model} (CPU, kann dauern) …")
    batch = 64
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        texts = [(f"{sec}\n{txt}" if sec else txt) for _, txt, sec in part]
        vecs = embed.encode(model, texts, kind="passage")
        store.set_embeddings(model, [(cid, v) for (cid, _, _), v in zip(part, vecs)])
        store.commit()
    return len(todo)
