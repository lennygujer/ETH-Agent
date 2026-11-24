"""/diagnose – gezielte Fragen, um die Lernkarte schnell und genau zu machen (nach LearnHarness-Prober).

Ablauf:
1. `/diagnose [thema] [n]` wählt die Stränge (Knoten der Lernkarte; bevorzugt ungeprüfte und wacklige),
   sucht einmal Kursmaterial und Prüfungsauszüge und lässt ein Modell die erste Frage stellen.
2. Jede Antwort (Buchstabe oder eigene Worte, gern mit Begründung und `Sicherheit: 70%`) wird in EINEM
   Modellaufruf ausgewertet; derselbe Aufruf liefert die nächste Frage oder das Fazit.
3. Belege gehen als kind="diagnose" in die Lernkarte. `/stop` beendet vorzeitig, `/weiter` überspringt.

Messen, nicht unterrichten: Rückmeldung nur kurz, die Lösung jeder Frage bleibt bis zur Auswertung verborgen.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Callable

from ..config import slug
from ..learner.model import STATUS_ICON, STATUS_LABEL, STATUS_ORDER, Node, parse_confidence
from ..retrieval.search import search
from .check import dump_raw, ensure_skeleton, parse_grade

if TYPE_CHECKING:
    from ..agent.core import Agent, Turn
    from ..config import Course

DIAG_MARK = re.compile(r"Diagnose #(\d+) · Frage (\d+)")
DEFAULT_Q = 8
MIN_Q, MAX_Q = 3, 15
MAX_FOCUS = 40
MATERIAL_TYPES = ["skript", "slides", "notizen", "uebungsstunde", "buch"]
SHEET_TYPES = ["formelsammlung"]
EXAM_TYPES = ["pruefung"]
MATERIAL_BUDGET, SHEET_BUDGET, EXAM_BUDGET = 6_000, 2_500, 3_000      # Tokens
_DONT_KNOW = re.compile(r"^\s*(ich\s+)?(wei(ss|ß)\s+(ich\s+)?(es\s+)?nicht|keine\s+ahnung|k\.?\s?a\.?$|"
                        r"(i\s+)?don'?t\s+know|\?+$)", re.I)
_COUNT = re.compile(r"(?:^|\s)(?:--n\s*)?(\d{1,2})\s*(fragen|questions|q)?\s*$", re.I)
OUTCOME_DE = {"correct": "richtig", "partial": "teilweise", "wrong": "falsch", "misconception": "Fehlvorstellung",
              "slip": "Flüchtigkeitsfehler"}
_OUTCOME_RANK = ["misconception", "wrong", "partial", "slip", "correct"]


# ---------------------------------------------------------------------- Hilfen
def pending_diagnosis(history: list["Turn"]) -> tuple[int, int] | None:
    """(Diagnose-ID, Fragenummer), wenn die letzte Agenten-Antwort eine Diagnosefrage gestellt hat."""
    for t in reversed(history):
        if t.role == "assistant":
            m = DIAG_MARK.findall(t.text)
            return (int(m[-1][0]), int(m[-1][1])) if m else None
    return None


def parse_args(arg: str) -> tuple[str, int]:
    """'tunneling 5' -> ('tunneling', 5); '' -> ('', 8)."""
    arg = (arg or "").strip()
    n = DEFAULT_Q
    m = _COUNT.search(arg)
    if m:
        n = int(m.group(1))
        arg = arg[:m.start()].strip()
    return arg, max(MIN_Q, min(MAX_Q, n))


def dont_know(text: str) -> bool:
    return bool(_DONT_KNOW.match(text or ""))


def _subtree(n: Node) -> list[Node]:
    out = [n]
    for c in n.children:
        out += _subtree(c)
    return out


def focus_nodes(agent: "Agent", course_id: str, topic: str) -> list[Node]:
    """Stränge für die Diagnose. Mit Thema: passende Knoten samt Unterknoten. Ohne: ganzes Fach,
    Oberthemen immer, Unterthemen nur wenn nicht schon gefestigt."""
    nodes = agent.learner.nodes(course_id)
    if topic:
        exact = agent.learner.find_node(course_id, topic)
        if exact:
            return _subtree(next(n for n in nodes if n.id == exact))[:MAX_FOCUS]
        words = [w for w in slug(topic).split("-") if len(w) >= 3]
        scored = []
        for n in nodes:
            t = slug(n.title)
            s = sum(1 for w in words if w in t)
            if s:
                scored.append((s, n))
        if not scored:
            return []
        best = max(s for s, _ in scored)
        out, seen = [], set()
        for s, n in scored:
            if s == best:
                for x in _subtree(n):
                    if x.id not in seen:
                        out.append(x)
                        seen.add(x.id)
        return out[:MAX_FOCUS]
    roots = agent.learner.tree(course_id)
    out = list(roots)
    for r in roots:
        out += [c for c in r.children if c.status != "solid"]
    # Ordnung wie im Skript, aber Oberthemen zuerst, damit nichts Wichtiges abgeschnitten wird
    return out[:MAX_FOCUS]


def _focus_text(focus: list[Node], topic: str) -> str:
    lines = [f"- {n.id} | {n.title} | {STATUS_ICON[n.status]} {STATUS_LABEL[n.status]}" for n in focus]
    if topic and not focus:
        lines.append(f"(The student asked for the topic \"{topic}\", which is not yet in the map. Diagnose that "
                     "topic; create new nodes with fitting parents.)")
    elif topic:
        lines.insert(0, f"(Topic requested by the student: \"{topic}\")")
    if not lines:
        lines.append("(The learning map is still empty. Choose the most exam-relevant strands from the course "
                     "material and the exam excerpts and create nodes for them.)")
    return "\n".join(lines)


def _gather(agent: "Agent", course_id: str, course_name: str, focus: list[Node], topic: str
            ) -> tuple[str, str, str]:
    """Kursmaterial und Prüfungsauszüge einmal suchen (wird für die ganze Diagnose gespeichert)."""
    from ..agent.core import _format_chunk
    from ..agent.routing import est_tokens

    weak_first = sorted(focus, key=lambda n: (STATUS_ORDER.index(n.status) if n.status != "untested" else -1))
    queries = [topic] if topic else []
    queries += [n.title for n in weak_first[:5] if n.title not in queries]
    if not queries:
        queries = [course_name]

    def run(q: str, types: list[str], k: int):
        try:
            return search(agent.store, q, course=course_id, doc_types=types, final_k=k,
                          embedding_model=agent.embedding_model, encode=agent.encode,
                          semantic_weight=agent.cfg.retrieval.semantic_weight,
                          keyword_weight=agent.cfg.retrieval.keyword_weight)
        except Exception:            # Suche darf eine Diagnose nie verhindern
            return []

    def collect(hit_lists, budget: int) -> str:
        parts, seen, used = [], set(), 0
        for hits in hit_lists:
            for h in hits:
                c = h.chunk
                if c.id in seen or used + est_tokens(c.text) > budget:
                    continue
                seen.add(c.id)
                used += est_tokens(c.text)
                parts.append(_format_chunk(len(parts) + 1, c, course_name))
        return "\n\n".join(parts)

    material = collect([run(q, MATERIAL_TYPES, 2) for q in queries], MATERIAL_BUDGET)
    sheet = collect([run(q, SHEET_TYPES, 2) for q in queries], SHEET_BUDGET)
    exam = collect([run(" ".join(queries[:3]), EXAM_TYPES, 3)], EXAM_BUDGET)
    return (material or "(no matching course material found)",
            sheet or "(no formula sheet in the course material – assume formulas must be known)",
            exam or "(no exam material found)")


def _transcript(items: list[dict]) -> str:
    if not items:
        return "(no questions asked yet – ask the first one; evaluation must be null)"
    out = []
    for it in items:
        lines = [f"### Question {it['n']} (strands: {', '.join(it['node_ids']) or '–'}; format: {it['format']})",
                 it["question"]]
        if it["options"]:
            lines.append("Options: " + " | ".join(it["options"]))
        lines.append(f"Answer key (hidden from the student): {it['answer_key'] or '–'}")
        if it["discriminates"]:
            lines.append(f"Discriminates: {it['discriminates']}")
        if it["answer"] is None:
            lines.append("Student answer: (not answered)")
        else:
            conf = f" [confidence: {round(it['confidence'] * 100)} %]" if it["confidence"] is not None else ""
            lines.append(f"Student answer: {it['answer']}{conf}")
        if it["outcome"] or it["feedback"]:
            lines.append(f"Your evaluation: {it['outcome'] or '–'} – {it['feedback'] or ''}")
        elif it["answer"] is not None:
            lines.append("→ Evaluate this answer now (field \"evaluation\").")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _ask(agent: "Agent", course: "Course", diag: dict, items: list[dict], budget_left: int,
         say: Callable[[str], None]) -> dict | None:
    from ..agent.core import LANG_NAMES, _fill, _template
    from ..agent.routing import est_tokens

    ctx = json.loads(diag["context"] or "{}")
    prompt = _fill(_template("diagnose.md"), course_name=course.name,
                   output_language_name=LANG_NAMES.get(course.output_language, course.output_language),
                   budget_left=str(max(0, budget_left)), focus=diag["focus"] or "",
                   learner_map=agent.learner.outline(course.id), context=ctx.get("material", ""),
                   formula_sheet=ctx.get("sheet", "(not available)"),
                   exam_context=ctx.get("exam", ""), transcript=_transcript(items))
    order = [n for n in ("codex", "claude") if n in agent.providers and agent.providers[n].available()]
    for name in order:
        effort = agent.cfg.models.codex_effort_hard if name == "codex" else None
        say(f"{name} {'wertet aus und ' if items else ''}stellt die nächste Frage …")
        res = agent.providers[name].complete(prompt, timeout=agent.cfg.models.timeout_s, effort=effort)
        agent.usage.record(res, skill="diagnose", rule="diagnose", prompt_tokens_est=est_tokens(prompt))
        if res.ok:
            data = parse_grade(res.text)
            if data:
                return data
            say(f"{name}: Antwort nicht lesbar (abgelegt in {dump_raw(agent, 'diagnose', res.text)})")
        else:
            say(f"{name} fehlgeschlagen: {(res.error or 'unbekannter Fehler')[:200]}")
    return None


def _valid_next(data: dict) -> dict | None:
    nx = data.get("next")
    if not isinstance(nx, dict) or not str(nx.get("question") or "").strip():
        return None
    opts = [str(o).strip() for o in (nx.get("options") or []) if str(o).strip()]
    fmt = "mc" if str(nx.get("format")) == "mc" and len(opts) >= 2 else "short"
    if fmt == "mc":
        opts = [o if re.match(r"^[A-Za-z][\).:]\s", o) else f"{chr(65 + i)}) {o}" for i, o in enumerate(opts)]
    else:
        opts = []
    return {"question": str(nx["question"]).strip(), "format": fmt, "options": opts,
            "node_ids": [str(x) for x in (nx.get("node_ids") or []) if str(x).strip()],
            "answer_key": str(nx.get("answer_key") or "").strip(),
            "discriminates": str(nx.get("discriminates") or "").strip()}


def _render_question(agent: "Agent", course_id: str, diag_id: int, n: int, max_q: int, q: dict) -> str:
    by_id = {x.id: x for x in agent.learner.nodes(course_id)}
    nodes = [by_id[i] for i in q["node_ids"] if i in by_id]
    ids = {x.id for x in nodes}
    specific = [x.title for x in nodes if not any(c.id in ids for c in x.children)]   # Eltern weglassen
    head = f"### Diagnose · Frage {n}/{max_q}" + (f" — {' · '.join(specific[:2])}" if specific else "")
    parts = [head, q["question"]]
    if q["options"]:
        parts.append("\n".join(f"- {o}" for o in q["options"]))
    how = "Buchstabe oder eigene Worte" if q["options"] else "Kurz in eigenen Worten"
    parts.append(f"*{how}, mit einem Satz warum; gern `Sicherheit: 70%`. «weiss nicht» ist eine gute Antwort. "
                 "Überspringen: `/weiter` · Beenden: `/stop`.*")
    parts.append(f"*Diagnose #{diag_id} · Frage {n}*")
    return "\n\n".join(parts)


def _map_lines(agent: "Agent", course_id: str, node_ids: list[str]) -> list[str]:
    by_id = {x.id: x for x in agent.learner.nodes(course_id)}
    ns = [by_id[i] for i in dict.fromkeys(node_ids) if i in by_id]
    ns.sort(key=lambda n: STATUS_ORDER.index(n.own_status))
    return [f"{STATUS_ICON[n.own_status]} {n.title} ({STATUS_LABEL[n.own_status]})" for n in ns]


def _session_overview(agent: "Agent", course_id: str, items: list[dict]) -> str:
    touched = [t for it in items for t in it["touched"]]
    lines = _map_lines(agent, course_id, touched)
    if not lines:
        return ""
    return "**Lernkarte nach dieser Diagnose:**\n" + "\n".join(f"- {x}" for x in lines)


def _worst(concepts: list[dict]) -> str | None:
    outs = [str(c.get("outcome")) for c in concepts if isinstance(c, dict) and c.get("outcome") in OUTCOME_DE]
    return min(outs, key=_OUTCOME_RANK.index) if outs else None


# ---------------------------------------------------------------------- Ablauf
def start(agent: "Agent", course: "Course", arg: str, say: Callable[[str], None]) -> str:
    topic, max_q = parse_args(arg)
    ensure_skeleton(agent, course.id)
    focus = focus_nodes(agent, course.id, topic)
    say(f"Diagnose {('zu «' + topic + '»') if topic else 'über das ganze Fach'}: {len(focus)} Themen im Fokus")
    say("Suche Skript und Prüfungsaufgaben …")
    material, sheet, exam = _gather(agent, course.id, course.name, focus, topic)
    did = agent.learner.open_diagnosis(course.id, topic, max_q, _focus_text(focus, topic),
                                       json.dumps({"material": material, "sheet": sheet, "exam": exam},
                                                  ensure_ascii=False))
    diag = agent.learner.get_diagnosis(did)
    data = _ask(agent, course, diag, [], max_q, say)
    q = _valid_next(data) if data else None
    if q is None:
        agent.learner.finish_diagnosis(did, "stopped", None)
        return "Die Diagnose konnte nicht starten (keine gültige Frage vom Modell erhalten). Versuch es nochmal."
    agent.learner.add_diag_item(did, 1, question=q["question"], node_ids=q["node_ids"], fmt=q["format"],
                                options=q["options"], answer_key=q["answer_key"], discriminates=q["discriminates"])
    intro = (f"**Diagnose{' · ' + topic if topic else ''}** – bis zu {max_q} kurze Fragen. Es geht ums Messen, "
             "nicht um Punkte: Raten verfälscht die Karte, «weiss nicht» ist ehrlich und hilfreich.")
    return intro + "\n\n" + _render_question(agent, course.id, did, 1, max_q, q)


def answer(agent: "Agent", diag_id: int, n: int, text: str, say: Callable[[str], None],
           skip: bool = False) -> tuple[str, str | None]:
    """Antwort auf Frage n auswerten und nächste Frage oder Fazit liefern. Gibt (Text, Kurs-ID) zurück."""
    diag = agent.learner.get_diagnosis(diag_id)
    if diag is None:
        return "Diese Diagnose kenne ich nicht (mehr).", None
    course = agent.cfg.courses.get(diag["course"])
    if course is None:
        return "Das Fach dieser Diagnose gibt es nicht mehr.", None
    if diag["status"] != "open":
        return "Diese Diagnose ist schon beendet. Neue starten: `/diagnose`.", course.id
    items = agent.learner.diag_items(diag_id)
    if not items or items[-1]["n"] != n or items[-1]["answer"] is not None:
        return "Diese Frage ist schon beantwortet.", course.id
    item = items[-1]

    if skip:
        ans, conf = "(Frage übersprungen – daraus nichts über das Wissen ableiten)", None
    else:
        ans = text.strip()
        conf = 0.0 if dont_know(ans) else parse_confidence(ans)
    agent.learner.answer_diag_item(item["id"], ans, conf)
    item["answer"], item["confidence"] = ans, conf

    budget_left = diag["max_q"] - n
    data = _ask(agent, course, diag, items, budget_left, say)
    if data is None:
        agent.learner.answer_diag_item(item["id"], None, None)       # nochmal schicken möglich
        return ("Die Auswertung hat nicht geklappt. Schick deine Antwort bitte nochmal (oder `/stop`).\n\n"
                f"*Diagnose #{diag_id} · Frage {n}*"), course.id

    parts: list[str] = []
    ev = data.get("evaluation") if isinstance(data.get("evaluation"), dict) else {}
    concepts = [] if skip else (ev.get("concepts") or [])
    touched = agent.learner.apply_concepts(course.id, concepts, "diagnose", confidence=conf,
                                           problem_ref=f"Diagnose #{diag_id} Frage {n}")
    feedback = str(ev.get("feedback") or "").strip()
    agent.learner.evaluate_diag_item(item["id"], _worst(concepts), feedback, touched)
    item.update(outcome=_worst(concepts), feedback=feedback, touched=touched)
    if feedback and not skip:
        parts.append(f"**Rückmeldung:** {feedback}")
    upd = _map_lines(agent, course.id, touched)
    if upd:
        parts.append("**Lernkarte:** " + " · ".join(upd))

    q = _valid_next(data)
    if bool(data.get("done")) or budget_left <= 0 or q is None:
        summary = str(data.get("summary_markdown") or "").strip()
        if not summary and q is None and not data.get("done") and budget_left > 0:
            summary = "(Das Modell hat keine weitere Frage geliefert.)"
        agent.learner.finish_diagnosis(diag_id, "done", summary or None)
        parts.append(_final(agent, course, items, summary))
        return "\n\n".join(parts), course.id

    agent.learner.add_diag_item(diag_id, n + 1, question=q["question"], node_ids=q["node_ids"], fmt=q["format"],
                                options=q["options"], answer_key=q["answer_key"], discriminates=q["discriminates"])
    parts.append(_render_question(agent, course.id, diag_id, n + 1, diag["max_q"], q))
    return "\n\n".join(parts), course.id


def _final(agent: "Agent", course: "Course", items: list[dict], summary: str) -> str:
    parts = ["## Diagnose abgeschlossen"]
    answered = [it for it in items if it["answer"] is not None]
    tally: dict[str, int] = {}
    for it in answered:
        if it["outcome"]:
            tally[it["outcome"]] = tally.get(it["outcome"], 0) + 1
    if answered:
        parts.append(f"{len(answered)} Fragen beantwortet" + (": " + ", ".join(
            f"{v}× {OUTCOME_DE[k]}" for k, v in sorted(tally.items(), key=lambda kv: _OUTCOME_RANK.index(kv[0])))
            if tally else "") + ".")
    if summary:
        parts.append(summary)
    ov = _session_overview(agent, course.id, items)
    if ov:
        parts.append(ov)
    parts.append("Gesamtbild: `/lernkarte` · gezielt weiter: `/diagnose <thema>`")
    return "\n\n".join(parts)


def stop(agent: "Agent", diag_id: int) -> tuple[str, str | None]:
    """Vorzeitig beenden: Fazit ohne weiteren Modellaufruf, nur aus den bisherigen Bewertungen."""
    diag = agent.learner.get_diagnosis(diag_id)
    if diag is None or diag["status"] != "open":
        return "Gerade läuft keine Diagnose.", diag["course"] if diag else None
    course = agent.cfg.courses.get(diag["course"])
    items = agent.learner.diag_items(diag_id)
    agent.learner.finish_diagnosis(diag_id, "stopped", None)
    if course is None:
        return "Diagnose beendet.", None
    if not any(it["outcome"] for it in items):
        return "Diagnose beendet – noch keine Antwort ausgewertet, die Lernkarte ist unverändert.", course.id
    weak = [it for it in items if it["outcome"] in {"misconception", "wrong", "partial"}]
    summary = ""
    if weak:
        summary = "**Daran hapert es:**\n" + "\n".join(f"- Frage {it['n']}: {it['feedback'] or OUTCOME_DE[it['outcome']]}"
                                                       for it in weak)
    return _final(agent, course, items, summary), course.id
