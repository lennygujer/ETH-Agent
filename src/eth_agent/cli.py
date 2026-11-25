"""Kommandozeile: eth-agent index | search | show | files."""

from __future__ import annotations

import argparse
import re
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


def _agent(cfg, keyword_only: bool = False):
    from .agent.core import Agent

    model, enc = (None, None) if keyword_only else _encoder(cfg)
    if model:
        print(f"(lade Embedding-Modell {model} …)", file=sys.stderr)
        enc("warmup")
    return Agent(cfg, encode=enc, embedding_model=model)


def _print_answer(cfg, ans, course_id, debug: bool) -> None:
    if debug:
        print(f"\n[Route {ans.route.rule} -> {ans.route.provider}"
              + (f", Verweis: {ans.reference}" if ans.reference else "") + "]")
        if ans.llm:
            print(f"[{ans.llm.provider}: {ans.llm.duration_s:.1f}s, in={ans.llm.input_tokens}, "
                  f"out={ans.llm.output_tokens}]")
        if ans.check.status != "none":
            print("[Prüfcode-Ausgabe]\n" + textwrap.indent(ans.check.output or "(leer)", "  "))
    print("\n" + "─" * 70)
    print(ans.render())
    print("─" * 70)


def cmd_ask(cfg, args) -> int:
    agent = _agent(cfg, args.keyword_only)
    ans = agent.handle(args.message, course_id=args.course,
                       on_status=lambda s: print(f"· {s}", file=sys.stderr))
    if args.show_prompt and ans.prompt:
        print("=" * 70 + "\nPROMPT\n" + "=" * 70 + "\n" + ans.prompt)
    _print_answer(cfg, ans, args.course, args.debug)
    return 0 if ans.llm is None or ans.llm.ok else 1


def cmd_chat(cfg, args) -> int:
    from .agent.core import Turn

    agent = _agent(cfg, args.keyword_only)
    history: list[Turn] = []
    print("ETH-Agent – Terminal-Chat. /help für Befehle, /neu für neues Gespräch, /exit zum Beenden.")
    while True:
        try:
            msg = input("\nDu> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not msg:
            continue
        if msg in {"/exit", "/quit", "exit"}:
            return 0
        if msg in {"/neu", "/new", "/reset"}:
            history.clear()
            print("(neues Gespräch)")
            continue
        images: list[str] = []
        if re.match(r"^[/!]check\b", msg, re.I):
            msg, images = _read_check_attempt(msg)
        ans = agent.handle(msg, course_id=args.course, history=history, images=images,
                           on_status=lambda s: print(f"· {s}"))
        _print_answer(cfg, ans, args.course, args.debug)
        history += [Turn("user", msg), Turn("assistant", ans.text)]


def _read_check_attempt(first_line: str) -> tuple[str, list[str]]:
    """Mehrzeiligen Lösungsversuch im Terminal einlesen. Ende: Zeile mit nur '.'.

    Eine Zeile 'bild: <pfad>' hängt ein Foto an (wird von Codex gelesen).
    """
    print("Dein Lösungsweg (mehrere Zeilen; 'bild: pfad.jpg' für ein Foto; Ende mit einer Zeile '.'):")
    lines, images = [], []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        m = re.match(r"^\s*(bild|image|foto):\s*(.+)$", line, re.I)
        if m:
            images.append(m.group(2).strip().strip('"'))
            continue
        lines.append(line)
    return first_line + "\n" + "\n".join(lines), images


def cmd_check(cfg, args) -> int:
    from pathlib import Path

    attempt = args.attempt or ""
    if args.attempt_file:
        attempt = Path(args.attempt_file).read_text(encoding="utf-8")
    if not attempt and not args.image:
        attempt = sys.stdin.read() if not sys.stdin.isatty() else ""
    text = f"/check {args.task}\n---\n{attempt}" + (" --loesung" if args.loesung else "")
    agent = _agent(cfg, args.keyword_only)
    ans = agent.handle(text, course_id=args.course, images=args.image or [],
                       on_status=lambda s: print(f"· {s}", file=sys.stderr))
    _print_answer(cfg, ans, args.course, args.debug)
    return 0


def cmd_serve(cfg, args) -> int:
    import uvicorn

    from .server import api_key_from_env, create_app

    import socket

    # Port prüfen, BEVOR das Embedding-Modell (2–3 GB) geladen wird: sonst lädt der Dienst bei
    # belegtem Port in einer Neustart-Schleife immer wieder das Modell und bremst den Server aus.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((args.host, args.port))
        except OSError as e:
            print(f"✗ Port {args.host}:{args.port} ist belegt ({e}). Anderen Port wählen (--port).",
                  file=sys.stderr)
            return 2
    key = api_key_from_env(cfg)
    if not key:
        print("! Kein ETH_AGENT_API_KEY gesetzt (Umgebung oder .env) – Server läuft OHNE Schlüssel.",
              file=sys.stderr)
    agent = _agent(cfg, args.keyword_only)
    app = create_app(cfg, agent, key)
    print(f"ETH-Agent hört auf http://{args.host}:{args.port}/v1", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_forget(cfg, args) -> int:
    """Belege einer falschen Korrektur aus Lernkarte und Fehlerkatalog entfernen."""
    import json as _json
    from .learner.model import LearnerStore
    store = LearnerStore(cfg.data_dir / "learner.sqlite")
    n = store.forget(args.course, args.ref)
    path = cfg.data_dir / "profile" / "errors.jsonl"
    removed = 0
    if path.exists():
        keep = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = _json.loads(line)
            except _json.JSONDecodeError:
                keep.append(line)
                continue
            if e.get("course") == args.course and args.ref in (e.get("problem_ref") or ""):
                removed += 1
            else:
                keep.append(line)
        path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    print(f"{n} Belege aus der Lernkarte und {removed} Einträge aus dem Fehlerkatalog entfernt.")
    return 0


def cmd_usage(cfg, args) -> int:
    from .llm.usage import UsageLog

    rows = UsageLog(cfg.data_dir / "usage.sqlite").summary(args.hours)
    print(f"Cloud-Aufrufe der letzten {args.hours} h")
    print(f"{'Modell':8s} {'Aufrufe':>7s} {'ok':>4s} {'Limit':>5s} {'Tokens in':>10s} {'Tokens out':>10s} {'USD':>7s}")
    for prov, n, ok, rl, tin, tout, usd in rows:
        print(f"{prov:8s} {n:>7d} {ok or 0:>4d} {rl or 0:>5d} {tin or 0:>10d} {tout or 0:>10d} {usd or 0:>7.3f}")
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

    for name, hlp in (("chat", "Terminal-Chat mit dem Agenten"), ("ask", "eine einzelne Frage stellen")):
        s = sub.add_parser(name, help=hlp)
        if name == "ask":
            s.add_argument("message")
            s.add_argument("--show-prompt", action="store_true", help="vollständigen Prompt anzeigen")
        s.add_argument("--course")
        s.add_argument("--debug", action="store_true", help="Route, Dauer, Tokens, Prüfcode-Ausgabe zeigen")
        s.add_argument("--keyword-only", action="store_true", help="ohne Embeddings (schneller Start)")

    s = sub.add_parser("check", help="eigenen Lösungsversuch korrigieren lassen")
    s.add_argument("task", help='z. B. "Serie 3 Aufgabe 1" oder der Aufgabentext')
    s.add_argument("--attempt", help="Lösungsversuch als Text")
    s.add_argument("--attempt-file", help="Lösungsversuch aus Datei (Text/LaTeX/Markdown)")
    s.add_argument("--image", nargs="+", help="Foto(s) des handschriftlichen Versuchs")
    s.add_argument("--loesung", action="store_true", help="Referenzlösung mit anzeigen")
    s.add_argument("--course")
    s.add_argument("--debug", action="store_true")
    s.add_argument("--keyword-only", action="store_true")

    s = sub.add_parser("serve", help="OpenAI-kompatiblen Server für Open WebUI starten")
    s.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1 = nur lokal; 0.0.0.0 = auch für Docker/Tailscale erreichbar")
    s.add_argument("--port", type=int, default=8100)
    s.add_argument("--keyword-only", action="store_true")

    s = sub.add_parser("forget", help="Belege einer falschen Korrektur entfernen")
    s.add_argument("--course", required=True)
    s.add_argument("--ref", required=True, help="Teil der Aufgabenangabe, z. B. 'Exam2024fall.pdf, Aufgabe 5'")

    s = sub.add_parser("usage", help="Cloud-Aufrufe und Tokens anzeigen")
    s.add_argument("--hours", type=int, default=5)

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "course", None):
        course = cfg.resolve_course(args.course)
        if course is None:
            ids = ", ".join(sorted(cfg.courses))
            print(f"Unbekanntes Fach «{args.course}». Verfügbare IDs: {ids}", file=sys.stderr)
            return 2
        if course.id != args.course:
            print(f"(Fach: {course.name} → --course {course.id})", file=sys.stderr)
        args.course = course.id
    handler = {"index": cmd_index, "search": cmd_search, "show": cmd_show, "files": cmd_files,
               "chat": cmd_chat, "ask": cmd_ask, "check": cmd_check, "serve": cmd_serve, "usage": cmd_usage,
               "forget": cmd_forget}[args.cmd]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
