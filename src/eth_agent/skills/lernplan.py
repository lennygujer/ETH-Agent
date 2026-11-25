"""/lernplan – priorisierte Liste mit Übungsaufgaben bis zum nächsten Termin.

1. Stoff: Themen des nächsten Prüfungstermins (sonst das ganze Fach), mit Status aus der Lernkarte.
2. Kandidaten: pro Thema die passendsten Aufgaben aus Serien, Übungsskripten und alten Prüfungen
   (Suche im Index), mit Angabe, ob eine Musterlösung existiert und ob du sie schon korrigiert hast.
3. Ein Modellaufruf priorisiert und verteilt sie auf die Tage bis zum Termin (Codex, mittlerer Denkaufwand).
4. Der Plan wird gespeichert. Aufgaben heissen P1, P2 …: `/check P3` korrigiert, `P3 Tipp` gibt Tipps,
   und jede Korrektur hakt die Aufgabe im Plan ab.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..learner.model import STATUS_ICON, STATUS_LABEL, STATUS_ORDER, Node
from ..learner.render import short
from ..retrieval.search import search
from . import termine as tm
from .check import dump_raw, ensure_skeleton, parse_grade

if TYPE_CHECKING:
    from ..agent.core import Agent
    from ..config import Course
    from ..retrieval.store import ChunkRow

EXERCISE_TYPES = ["serie", "pruefung"]
MAX_TOPICS = 14
PER_TOPIC = 4
MAX_CANDIDATES = 60
MINUTES_PER_DAY = 90
PRIO_ICON = {1: "🔴", 2: "🟡", 3: "⚪"}


def item_label(c: "ChunkRow") -> str:
    """Menschlich lesbare Quelle, z. B. 'SST1-Uebung – Aufgabe 45 (S. 17)'."""
    top = (c.problem_no or "").split(".")[0]
    page = f" (S. {c.printed_start or c.page_start})" if (c.printed_start or c.page_start) else ""
    return f"{Path(c.rel_path).stem} – Aufgabe {top}{page}"


def _targets(agent: "Agent", course_id: str, scope: list[Node]) -> list[Node]:
    """Themen für den Plan: schwach vor ungeprüft vor ok; eher konkrete Unterthemen als ganze Kapitel."""
    nodes = scope or agent.learner.nodes(course_id)
    leaves = [n for n in nodes if not n.children] or nodes
    rank = {"misconception": 0, "missing": 1, "shaky": 2, "untested": 3, "probably": 4, "solid": 5}
    leaves.sort(key=lambda n: (rank[n.own_status], -len(n.evidence), n.ord))
    return leaves[:MAX_TOPICS]


def _candidates(agent: "Agent", course: "Course", targets: list[Node]) -> list[dict]:
    done = _done_problems(agent, course.id)
    by_id = {n.id: n for n in agent.learner.nodes(course.id)}
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for node in targets:
        parent = by_id.get(node.parent_id) if node.parent_id else None
        query = f"{node.title} {parent.title}" if parent else node.title   # "Setup" allein findet nichts
        try:
            hits = search(agent.store, query, course=course.id, doc_types=EXERCISE_TYPES, final_k=PER_TOPIC * 2,
                          embedding_model=agent.embedding_model, encode=agent.encode,
                          semantic_weight=agent.cfg.retrieval.semantic_weight,
                          keyword_weight=agent.cfg.retrieval.keyword_weight)
        except Exception:
            hits = []
        n_for_node = 0
        for h in hits:
            c = h.chunk
            if not c.problem_no or not c.pair_key:
                continue
            key = (c.rel_path, c.problem_no.split(".")[0])
            if key in seen:
                continue
            seen.add(key)
            out.append({"chunk": c, "node": node, "has_solution": bool(agent.store.solution_for(c)),
                        "done": key in done})
            n_for_node += 1
            if n_for_node >= PER_TOPIC or len(out) >= MAX_CANDIDATES:
                break
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def _done_problems(agent: "Agent", course_id: str) -> set[tuple[str, str]]:
    """Schon mit /check korrigierte Aufgaben (aus dem Fehlerkatalog), als (Datei, Aufgabe)."""
    path = agent.cfg.data_dir / "profile" / "errors.jsonl"
    out: set[tuple[str, str]] = set()
    if not path.exists():
        return out
    import re
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("course") != course_id:
            continue
        m = re.search(r"– ([^,\]]+?), Aufgabe (\d+)", e.get("problem_ref") or "")
        if m:
            out.add((m.group(1).strip(), m.group(2)))
    # Zitate enthalten nur den Dateinamen; auf rel_path abbilden
    files = {Path(r["rel_path"]).name: r["rel_path"] for r in agent.store.files(course_id)}
    return {(files.get(f, f), n) for f, n in out}


def generate(agent: "Agent", course: "Course", say: Callable[[str], None]) -> str:
    from ..agent.core import LANG_NAMES, _fill, _template
    from ..agent.routing import est_tokens

    ensure_skeleton(agent, course.id)
    exam, scope = tm.focus_for_exam(agent, course.id)
    if exam:
        say(f"Ziel: {exam['title']} am {tm.fmt_date(exam['date'])} – {len(scope)} Themen im Stoff")
    else:
        say("Kein Termin gespeichert – plane über das ganze Fach für die nächsten 14 Tage")
    targets = _targets(agent, course.id, scope)
    say(f"Suche Aufgaben zu {len(targets)} Themen in Serien, Übungsskripten und alten Prüfungen …")
    cands = _candidates(agent, course, targets)
    if not cands:
        return ("Ich habe im Material dieses Fachs keine nummerierten Übungsaufgaben gefunden. Sind Serien oder alte "
                "Prüfungen indexiert? `eth-agent files --course …` zeigt es.")
    say(f"{len(cands)} Kandidaten gefunden – priorisiere …")

    horizon_date = date.fromisoformat(exam["date"]) if exam else tm.today() + timedelta(days=14)
    if exam:
        horizon = (f"the {exam['title']} on {exam['date']} ({tm.days_left(exam['date'])} days from today; "
                   f"plan until the day before)")
        exam_txt = (f"{exam['title']} ({exam['kind']}) on {exam['date']}"
                    + (f"; topics as the student named them: {', '.join(exam['topics'])}" if exam["topics"] else ""))
    else:
        horizon = f"{horizon_date.isoformat()} (no exam date known – a 14-day plan over the whole course)"
        exam_txt = "(no exam date stored – plan over the whole course; weakest topics first)"
    topic_lines = []
    for n in (scope or targets):
        last = sorted(n.evidence, key=lambda e: e.ts)[-1].detail if n.evidence else ""
        topic_lines.append(f"- {n.id} | {n.title} | {STATUS_ICON[n.own_status]} {STATUS_LABEL[n.own_status]}"
                           + (f" | {short(last, 160)}" if last else ""))
    cand_lines = []
    for i, cd in enumerate(cands, 1):
        c = cd["chunk"]
        cand_lines.append(f"{i} | {item_label(c)} [{c.doc_type}] | {short(c.text, 260)} | {cd['node'].title} | "
                          f"{'yes' if cd['has_solution'] else 'no'} | {'done' if cd['done'] else ''}")
    prompt = _fill(_template("lernplan.md"), course_name=course.name, horizon=horizon,
                   today=tm.today().isoformat(), minutes_per_day=str(MINUTES_PER_DAY),
                   output_language_name=LANG_NAMES.get(course.output_language, course.output_language),
                   exam=exam_txt, topics="\n".join(topic_lines[:120]), candidates="\n".join(cand_lines))

    data = None
    for name in [n for n in ("codex", "claude") if n in agent.providers and agent.providers[n].available()]:
        effort = agent.cfg.models.codex_effort_hard if name == "codex" else None
        say(f"{name} erstellt den Plan …")
        res = agent.providers[name].complete(prompt, timeout=agent.cfg.models.timeout_s, effort=effort)
        agent.usage.record(res, skill="lernplan", rule="lernplan", prompt_tokens_est=est_tokens(prompt))
        if res.ok:
            data = parse_grade(res.text)
            if data and data.get("items"):
                break
            say(f"{name}: Antwort nicht verwertbar (abgelegt in {dump_raw(agent, 'lernplan', res.text)})")
            data = None
    if not data:
        return "Der Lernplan konnte nicht erstellt werden (keine verwertbare Antwort). Versuch es nochmal."

    known = {n.id for n in agent.learner.nodes(course.id)}
    items, used = [], set()
    for it in data.get("items") or []:
        try:
            idx = int(it.get("cand")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(cands) or idx in used:
            continue
        used.add(idx)
        c = cands[idx]["chunk"]
        day = str(it.get("day") or "")
        try:
            day = date.fromisoformat(day).isoformat()
        except ValueError:
            day = ""
        prio = it.get("priority") if it.get("priority") in (1, 2, 3) else 2
        node_id = it.get("node_id") if it.get("node_id") in known else cands[idx]["node"].id
        items.append({"pair_key": c.pair_key, "doc_type": c.doc_type, "problem_no": c.problem_no.split(".")[0],
                      "rel_path": c.rel_path, "label": item_label(c), "node_id": node_id, "priority": prio,
                      "why": str(it.get("why") or "").strip(), "minutes": int(it.get("minutes") or 0) or None,
                      "day": day})
    if not items:
        return "Der Lernplan war leer. Versuch es nochmal mit `/lernplan neu`."
    items.sort(key=lambda x: (x["day"] or "9999", x["priority"]))
    learn_first = [str(x) for x in (data.get("learn_first") or [])][:3]
    agent.learner.new_plan(course.id, exam["id"] if exam else None, str(data.get("summary") or "").strip(),
                           str(data.get("advice") or "").strip(), learn_first, items)
    return render(agent, course)


def render(agent: "Agent", course: "Course") -> str:
    plan = agent.learner.active_plan(course.id)
    if not plan:
        return "Noch kein Lernplan. `/lernplan` erstellt einen."
    by_id = {n.id: n for n in agent.learner.nodes(course.id)}
    parts = [f"## Lernplan – {course.name}"]
    exam = agent.learner.get_exam(plan["exam_id"]) if plan["exam_id"] else None
    if exam:
        parts.append(tm.exam_line(agent, exam))
    if plan["summary"]:
        parts.append(plan["summary"])
    if plan["learn_first"]:
        parts.append("**Zuerst verstehen:** " + " · ".join(f"`/learn {t}`" for t in plan["learn_first"]))
    rows = ["| | Aufgabe | Thema | Warum | Zeit | Tag |", "|---|---|---|---|---|---|"]
    for it in plan["items"]:
        node = by_id.get(it["node_id"])
        topic = f"{STATUS_ICON[node.own_status]} {node.title}" if node else "–"
        mark = "✅" if it["done_ts"] else PRIO_ICON.get(it["priority"], "")
        day = ""
        if it["day"]:
            d = date.fromisoformat(it["day"])
            day = f"{tm.WEEKDAY_DE[d.weekday()]} {d.strftime('%d.%m.')}"
        rows.append(f"| **P{it['n']}** {mark} | {it['label']} | {topic} | {(it['why'] or '').replace('|', '/')} | "
                    f"{(str(it['minutes']) + ' min') if it['minutes'] else ''} | {day} |")
    parts.append("\n".join(rows))
    done = [it for it in plan["items"] if it["done_ts"]]
    total_min = sum(it["minutes"] or 0 for it in plan["items"] if not it["done_ts"])
    parts.append(f"**Fortschritt:** {len(done)} von {len(plan['items'])} erledigt"
                 + (f" · noch ca. {round(total_min / 60, 1)} h" if total_min else ""))
    if plan["advice"]:
        parts.append(plan["advice"])
    parts.append("🔴 muss · 🟡 sollte · ⚪ wenn Zeit bleibt. Lösen, dann `/check P1` mit Foto oder Text – das hakt "
                 "die Aufgabe ab. Tipps: `P1 Tipp`. Neu planen (z. B. nach einer Diagnose): `/lernplan neu`.")
    return "\n\n".join(parts)


def command(agent: "Agent", course: "Course", arg: str, say: Callable[[str], None]) -> str:
    arg = (arg or "").strip().lower()
    if arg in {"", "zeigen", "show"} and agent.learner.active_plan(course.id):
        return render(agent, course)
    return generate(agent, course, say)
