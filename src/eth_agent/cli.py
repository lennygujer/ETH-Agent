"""Kommandozeile: eth-agent index | search | show | files."""

from __future__ import annotations

import argparse
import signal
import sys
import textwrap

from .config import load_config
from .indexer.doctypes import ALL_TYPES
from .retrieval.store import Store


def _store(cfg) -> Store:
    return Store(cfg.db_path)


def cmd_index(cfg, args) -> int:
    from .indexer.build import embed_missing, index_course

    store = _store(cfg)
    courses = [cfg.courses[args.course]] if args.course else list(cfg.courses.values())
    rc = 0
    for course in courses:
        print(f"== {course.name} ({cfg.material_root / course.folder})")
        rep = index_course(cfg, store, course, force=args.force)
        print(f"   neu/geändert: {len(rep.added)}, unverändert: {rep.unchanged}, "
              f"entfernt: {len(rep.removed)}, Chunks neu: {rep.chunks}")
        if rep.placeholders:
            print(f"   ! {len(rep.placeholders)} leere Dateien (in OneDrive nicht heruntergeladen?):")
            for p in rep.placeholders:
                print(f"       {p}")
        if rep.needs_ocr:
            print("   ! Seiten ohne Textschicht (Scan, OCR/Vision nötig):")
            for p, n, tot in rep.needs_ocr:
                print(f"       {p}: {n} von {tot} Seiten")
        for p, e in rep.errors:
            print(f"   ✗ {p}: {e}")
            rc = 1
    if not args.no_embed:
        n = embed_missing(cfg, store)
        if n:
            print(f"   Embeddings berechnet: {n}")
    store.close()
    return rc


def _encoder(cfg):
    from .retrieval import embed

    model = cfg.retrieval.embedding_model
    if model and embed.available():
        return model, (lambda q: embed.encode(model, [q], kind="query")[0])
    return None, None


def cmd_search(cfg, args) -> int:
    from .retrieval.search import search

    store = _store(cfg)
    model, enc = (None, None) if args.keyword_only else _encoder(cfg)
    hits = search(store, args.query, course=args.course, doc_types=args.type,
                  top_k=cfg.retrieval.top_k, final_k=args.k, embedding_model=model, encode=enc,
                  semantic_weight=cfg.retrieval.semantic_weight,
                  keyword_weight=cfg.retrieval.keyword_weight)
    mode = "hybrid" if model else "nur Stichworte (BM25)"
    print(f"Suche: {args.query!r}  –  {mode}, {len(hits)} Treffer\n")
    for i, h in enumerate(hits, 1):
        c = h.chunk
        name = cfg.courses[c.course].name if c.course in cfg.courses else c.course
        print(f"{i}. #{c.id}  {c.citation(name)}  ({c.doc_type})")
        if c.section:
            print(f"   {c.section}")
        preview = c.text if args.full else c.text[:350]
        print(textwrap.indent(preview.strip(), "   │ "))
        if not args.full and len(c.text) > 350:
            print("   │ …")
        print()
    store.close()
    return 0


def cmd_show(cfg, args) -> int:
    store = _store(cfg)
    c = store.get_chunk(args.chunk_id)
    if not c:
        print(f"Chunk {args.chunk_id} nicht gefunden.")
        return 1
    name = cfg.courses[c.course].name if c.course in cfg.courses else c.course
    print(f"#{c.id}  {c.citation(name)}  ({c.doc_type})\n{c.section or ''}\n")
    print(c.text)
    if args.solution:
        sols = store.solution_for(c)
        print("\n" + "=" * 70)
        if not sols:
            print("Keine Musterlösung im Index gefunden.")
        for s in sols:
            print(f"Musterlösung: #{s.id}  {s.citation(name)}\n")
            print(s.text)
    store.close()
    return 0


def cmd_files(cfg, args) -> int:
    store = _store(cfg)
    rows = store.files(args.course)
    print(f"{'Typ':17s} {'Status':11s} {'S.':>4s} {'Chunks':>6s}  {'Semester':8s} {'Paar':24s} Datei")
    for r in rows:
        print(f"{r['doc_type']:17s} {r['status']:11s} {r['n_pages']:>4d} {r['n_chunks']:>6d}  "
              f"{(r['semester'] or ''):8s} {(r['pair_key'] or '')[:24]:24s} {r['rel_path']}")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(signal, "SIGPIPE"):  # sauber beenden bei `| head`
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    p = argparse.ArgumentParser(prog="eth-agent", description="ETH Study Agent")
    p.add_argument("--config", help="Pfad zu config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", help="Kursmaterial (neu) indexieren")
    s.add_argument("--course")
    s.add_argument("--force", action="store_true", help="alles neu einlesen")
    s.add_argument("--no-embed", action="store_true", help="keine Embeddings berechnen")

    s = sub.add_parser("search", help="im Kursmaterial suchen")
    s.add_argument("query")
    s.add_argument("--course")
    s.add_argument("--type", nargs="+", choices=sorted(ALL_TYPES), help="nur diese Dokumenttypen")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--full", action="store_true", help="ganzen Chunk anzeigen")
    s.add_argument("--keyword-only", action="store_true")

    s = sub.add_parser("show", help="einen Chunk anzeigen")
    s.add_argument("chunk_id", type=int)
    s.add_argument("--solution", action="store_true", help="zugehörige Musterlösung mit anzeigen")

    s = sub.add_parser("files", help="indexierte Dateien auflisten")
    s.add_argument("--course")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    handler = {"index": cmd_index, "search": cmd_search, "show": cmd_show, "files": cmd_files}[args.cmd]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
