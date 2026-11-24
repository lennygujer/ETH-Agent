"""/learn – eine Lücke schrittweise schliessen (nach dem LearnHarness-Lehrer).

Ablauf:
1. `/learn <thema>` (ohne Thema: die dringendste Schwäche der Lernkarte). Der Agent sammelt einmal: was die
   Lernkarte über dich weiss (inkl. deiner eigenen Antworten aus Diagnosen und Rückfragen), Skript,
   Formelblatt und Prüfungsauszüge. Ein Modellaufruf plant: konkretes Prüfungsziel + kleiner Abhängigkeitsplan.
   Du bestätigst den Plan oder änderst ihn.
2. Danach ein Denkschritt pro Nachricht im Rhythmus Vorhersage → Erklärung → Kontrollfrage → Reaktion.
   Ein Modellaufruf pro Nachricht. Kontrollfragen tragen Belege (kind="learn") in die Lernkarte ein;
   Vorhersagen nicht.
3. Fertig, wenn du eine reduzierte Prüfungsaufgabe richtig und begründet löst. `/plan` zeigt den Plan,
   `/weiter` überspringt eine Frage, `/stop` beendet.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Callable

from ..learner.model import STATUS_ICON, STATUS_LABEL, Node, parse_confidence
from ..learner.render import short, weak_spots
from . import diagnose as dg
from .check import dump_raw, ensure_skeleton, parse_grade

if TYPE_CHECKING:
    from ..agent.core import Agent, Turn
    from ..config import Course

LEARN_MARK = re.compile(r"Lernen #(\d+) · Schritt (\d+)")
MAX_TURNS = 30
FULL_TRANSCRIPT = 14          # so viele letzte Schritte vollständig im Prompt, ältere verdichtet
_ID = re.compile(r"[^a-z0-9_-]")
_MERMAID_UNSAFE = re.compile(r"[()\[\]{}\"`<>#;|]")


# ---------------------------------------------------------------------- Hilfen
def pending_lesson(history: list["Turn"]) -> tuple[int, int] | None:
    for t in reversed(history):
        if t.role == "assistant":
            m = LEARN_MARK.findall(t.text)
            return (int(m[-1][0]), int(m[-1][1])) if m else None
    return None


def _clean_plan(raw) -> list[dict]:
    """Plan aus der Modellantwort; tolerant gegenüber üblichen Varianten ({nodes: [...]}, title statt label …)."""
    if isinstance(raw, dict):
        raw = raw.get("nodes") or raw.get("plan") or raw.get("steps") or []
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for x in raw:
        if not isinstance(x, dict):
            continue
        label = str(x.get("label") or x.get("title") or x.get("name") or "").strip()
        nid = _ID.sub("-", str(x.get("id") or x.get("slug") or label).lower()).strip("-")[:40]
        if not nid or not label or nid in seen:
            continue
        seen.add(nid)
        req = x.get("requires") or x.get("depends_on") or x.get("deps") or x.get("prerequisites") or []
        if isinstance(req, str):
            req = [req]
        out.append({"id": nid, "label": label[:80],
                    "requires": [_ID.sub("-", str(r).lower()).strip("-") for r in req]})
    for x in out:
        x["requires"] = [r for r in x["requires"] if r in seen and r != x["id"]]
    return out


def plan_mermaid(plan: list[dict], turns: list[dict] | None = None) -> str:
    """Plan als Flussdiagramm: Grundlagen unten, Ziel oben; aktueller Knoten blau, gezeigte grün."""
    if not plan:
        return ""
    turns = turns or []
    shown = {t["q_node"] for t in turns if t["q_type"] == "check" and t["outcome"] == "correct"}
    current = next((t["q_node"] for t in reversed(turns) if t["q_node"]), None)
    mid = {x["id"]: "n_" + x["id"].replace("-", "_") for x in plan}
    lines = ["```mermaid", "graph BT"]
    for x in plan:
        lines.append(f'  {mid[x["id"]]}["{_MERMAID_UNSAFE.sub(" ", x["label"])}"]')
    for x in plan:
        for r in x["requires"]:
            lines.append(f"  {mid[r]} --> {mid[x['id']]}")
    lines += ["  classDef ok fill:#15803d,stroke:#15803d,color:#fff",
              "  classDef cur fill:#1d4ed8,stroke:#1d4ed8,color:#fff"]
    ok = [mid[i] for i in shown if i in mid and i != current]
    if ok:
        lines.append(f"  class {','.join(ok)} ok")
    if current in mid:
        lines.append(f"  class {mid[current]} cur")
    lines.append("```")
    return "\n".join(lines)


def _valid_question(raw) -> dict | None:
    if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
        return None
    qtype = "predict" if str(raw.get("type")) == "predict" else "check"
    opts = [str(o).strip() for o in (raw.get("options") or []) if str(o).strip()]
    if len(opts) >= 2:
        opts = [o if re.match(r"^[A-Za-z][\).:]\s", o) else f"{chr(65 + i)}) {o}" for i, o in enumerate(opts)]
    else:
        opts = []
    return {"type": qtype, "node": _ID.sub("-", str(raw.get("node") or "").lower()).strip("-") or None,
            "text": str(raw["text"]).strip(), "options": opts, "answer_key": str(raw.get("answer_key") or "").strip()}


def _evidence_text(agent: "Agent", course_id: str, focus: list[Node]) -> str:
    lines = []
    for n in focus:
        if not n.evidence:
            continue
        lines.append(f"- {n.id} | {n.title} | {STATUS_LABEL[n.own_status]}")
        for e in sorted(n.evidence, key=lambda e: e.ts)[-3:]:
            conf = f", Sicherheit {round(e.confidence * 100)} %" if e.confidence is not None else ""
            lines.append(f"    - {e.kind}: {e.outcome}{conf} – {e.detail or ''}")
    words = agent.learner.student_words(course_id, [n.id for n in focus])
    if words:
        lines.append("\nThe student's own answers on these topics (most recent first):")
        for w in words:
            conf = f" [Sicherheit {round(w['confidence'] * 100)} %]" if w["confidence"] is not None else ""
            lines.append(f"- Q ({w['source']}): {short(w['question'], 400)}\n  A: {w['answer']}{conf}"
                         + (f"\n  → {w['outcome']}: {w['feedback']}" if w["outcome"] else ""))
    return "\n".join(lines) or "(nothing recorded yet – find out where they stand with your first questions)"


def _transcript(lesson: dict, turns: list[dict]) -> str:
    if not turns:
        return "(nothing yet – this is the planning turn)"
    out = [f"Goal: {lesson['goal'] or '–'}",
           "Plan: " + "; ".join(f"{x['id']} = {x['label']} (requires {', '.join(x['requires']) or '–'})"
                                for x in lesson["plan"])]
    cut = len(turns) - FULL_TRANSCRIPT
    for t in turns:
        if t["n"] < cut:
            out.append(f"Turn {t['n']}: {t['q_type'] or 'plan'} on {t['q_node'] or '–'} → "
                       f"{t['outcome'] or ('answered' if t['answer'] else '–')}")
            continue
        block = [f"### Turn {t['n']} – you wrote", t["tutor"]]
        if t["q_type"]:
            block.append(f"Your question ({t['q_type']}, node {t['q_node'] or '–'}): {t['q_text']}")
            if t["q_options"]:
                block.append("Options: " + " | ".join(t["q_options"]))
            block.append(f"Answer key (hidden): {t['answer_key'] or '–'}")
        if t["answer"] is not None:
            conf = f" [confidence {round(t['confidence'] * 100)} %]" if t["confidence"] is not None else ""
            block.append(f"Student: {t['answer']}{conf}")
            if t["outcome"]:
                block.append(f"(evaluated: {t['outcome']})")
        out.append("\n".join(block))
    return "\n\n".join(out)


PHASE_PLAN = ("This is the PLANNING turn. Think hard about the minimal path from what the student holds to the goal. "
              "Return goal, plan, and reply_markdown = 2-4 sentences on the approach (what, in which order, why – "
              "given what the evidence shows). question: null. Do not teach yet; the app asks them to confirm.")
PHASE_CONFIRM = ("The student just replied to your plan. If they want changes (e.g. they already know a part), revise "
                 "the plan, send it, say briefly what changed, question: null. Otherwise begin teaching the first "
                 "node – usually with a predict question; \"plan\": null.")
PHASE_TEACH = "The plan is agreed; teach. Send \"plan\" only when its structure has to change."


def _ask(agent: "Agent", course: "Course", lesson: dict, turns: list[dict], phase: str,
         say: Callable[[str], None]) -> dict | None:
    from ..agent.core import LANG_NAMES, _fill, _template
    from ..agent.routing import est_tokens

    ctx = json.loads(lesson["context"] or "{}")
    prompt = _fill(_template("learn.md"), course_name=course.name, topic=lesson["topic"],
                   output_language_name=LANG_NAMES.get(course.output_language, course.output_language),
                   phase_rule=phase, turns_left=str(max(0, MAX_TURNS - len(turns))),
                   evidence=ctx.get("evidence", ""), learner_map=agent.learner.outline(course.id),
                   context=ctx.get("material", ""), formula_sheet=ctx.get("sheet", ""),
                   exam_context=ctx.get("exam", ""), transcript=_transcript(lesson, turns))
    first = agent.cfg.models.learn_provider if agent.cfg.models.learn_provider in ("codex", "claude") else "codex"
    order = [n for n in (first, "claude" if first == "codex" else "codex")
             if n in agent.providers and agent.providers[n].available()]
    for name in order:
        effort = agent.cfg.models.codex_effort_hard if name == "codex" else None
        say(f"{name} {'plant die Lektion' if phase == PHASE_PLAN else 'bereitet den nächsten Schritt vor'} …")
        res = agent.providers[name].complete(prompt, timeout=agent.cfg.models.timeout_s, effort=effort)
        agent.usage.record(res, skill="learn", rule="learn", prompt_tokens_est=est_tokens(prompt))
        if res.ok:
            data = parse_grade(res.text)
            if data:
                return data
            say(f"{name}: Antwort nicht lesbar (abgelegt in {dump_raw(agent, 'learn', res.text)})")
        else:
            say(f"{name} fehlgeschlagen: {short(res.error or 'unbekannter Fehler', 200)}")
    return None


def _footer(lesson_id: int, n: int) -> str:
    return f"*Lernen #{lesson_id} · Schritt {n}* · `/plan` zeigt den Plan · `/weiter` überspringt · `/stop` beendet"


def _render_question(q: dict) -> str:
    if q["type"] == "predict":
        head, how = "**Vorhersage:** ", "*Schreib deine Vermutung – Raten ist hier erwünscht.*"
    else:
        head, how = "---\n**Kontrollfrage:** ", "*Mit kurzer Begründung; gern `Sicherheit: 70%`. «weiss nicht» ist ok.*"
    parts = [head + q["text"]]
    if q["options"]:
        parts.append("\n".join(f"- {o}" for o in q["options"]))
    parts.append(how)
    return "\n\n".join(parts)


def _map_lines(agent: "Agent", course_id: str, node_ids: list[str]) -> list[str]:
    by_id = {x.id: x for x in agent.learner.nodes(course_id)}
    return [f"{STATUS_ICON[by_id[i].own_status]} {by_id[i].title} ({STATUS_LABEL[by_id[i].own_status]})"
            for i in dict.fromkeys(node_ids) if i in by_id]


# ---------------------------------------------------------------------- Ablauf
def pick_topic(agent: "Agent", course_id: str) -> Node | None:
    spots = weak_spots(agent.learner.nodes(course_id), limit=1)
    return spots[0] if spots else None


def start(agent: "Agent", course: "Course", arg: str, say: Callable[[str], None]) -> str:
    ensure_skeleton(agent, course.id)
    topic = (arg or "").strip()
    if not topic:
        node = pick_topic(agent, course.id)
        if node is None:
            return ("Die Lernkarte kennt noch keine Schwäche in diesem Fach. Gib ein Thema an, z. B. "
                    "`/learn tunneling`, oder finde Lücken zuerst mit `/diagnose`.")
        topic = node.title
        say(f"Dringendstes Thema aus der Lernkarte: {topic} ({STATUS_LABEL[node.own_status]})")
    focus = dg.focus_nodes(agent, course.id, topic)
    say(f"Lektion zu «{topic}»: {len(focus)} Themen der Lernkarte betroffen")
    say("Suche Skript, Formelblatt und Prüfungsaufgaben …")
    material, sheet, exam = dg._gather(agent, course.id, course.name, focus, topic)
    ctx = {"material": material, "sheet": sheet, "exam": exam,
           "evidence": _evidence_text(agent, course.id, focus)}
    lid = agent.learner.open_lesson(course.id, topic, [n.id for n in focus],
                                    json.dumps(ctx, ensure_ascii=False))
    lesson = agent.learner.get_lesson(lid)
    data = _ask(agent, course, lesson, [], PHASE_PLAN, say)
    if not data:
        agent.learner.finish_lesson(lid, "stopped", None)
        return ("Die Lektion konnte nicht geplant werden (keine lesbare Antwort vom Modell; Details im Block "
                "«Nachgedacht»). Versuch es nochmal.")
    goal = str(data.get("goal") or "").strip()
    plan = _clean_plan(data.get("plan"))
    if not plan:                     # ohne Plan trotzdem unterrichten – der Plan kann später nachkommen
        say(f"Plan fehlte in der Antwort (abgelegt in {dump_raw(agent, 'learn-plan', json.dumps(data, ensure_ascii=False))})")
        plan = [{"id": "ziel", "label": short(goal or topic, 80), "requires": []}]
    agent.learner.set_lesson_plan(lid, goal, plan)
    approach = str(data.get("reply_markdown") or "").strip()
    agent.learner.add_lesson_turn(lid, 0, approach, None)
    parts = [f"## Lernen: {topic}"]
    if goal:
        parts.append(f"**Ziel:** {goal}")
    parts += [approach, plan_mermaid(plan),
              "Passt der Plan? Antworte mit *ja*, oder sag, was anders sein soll (z. B. «das kann ich schon»).",
              _footer(lid, 0)]
    return "\n\n".join(p for p in parts if p)


def answer(agent: "Agent", lesson_id: int, n: int, text: str, say: Callable[[str], None],
           skip: bool = False) -> tuple[str, str | None]:
    lesson = agent.learner.get_lesson(lesson_id)
    if lesson is None:
        return "Diese Lektion kenne ich nicht (mehr).", None
    course = agent.cfg.courses.get(lesson["course"])
    if course is None:
        return "Das Fach dieser Lektion gibt es nicht mehr.", None
    if lesson["status"] != "open":
        return "Diese Lektion ist schon beendet. Neue starten: `/learn <thema>`.", course.id
    turns = agent.learner.lesson_turns(lesson_id)
    if not turns or turns[-1]["n"] != n or turns[-1]["answer"] is not None:
        return "Auf diesen Schritt hast du schon geantwortet.", course.id
    last = turns[-1]
    if skip:
        ans, conf = "(übersprungen – nicht bewerten; kurz auflösen und weitermachen)", None
    else:
        ans = text.strip()
        conf = 0.0 if dg.dont_know(ans) else parse_confidence(ans)
    agent.learner.answer_lesson_turn(last["id"], ans, conf)
    last.update(answer=ans, confidence=conf)

    phase = PHASE_CONFIRM if last["q_type"] is None else PHASE_TEACH
    data = _ask(agent, course, lesson, turns, phase, say)
    if data is None:
        agent.learner.answer_lesson_turn(last["id"], None, None)
        return ("Das hat nicht geklappt. Schick deine Antwort bitte nochmal (oder `/stop`).\n\n"
                + _footer(lesson_id, n)), course.id

    parts: list[str] = []
    ev = data.get("evaluation") if isinstance(data.get("evaluation"), dict) else None
    if ev and last["q_type"] == "check" and not skip:
        outcome = str(ev.get("outcome") or "")
        outcome = outcome if outcome in dg.OUTCOME_DE else None
        agent.learner.answer_lesson_turn(last["id"], ans, conf, outcome)
        last["outcome"] = outcome
        touched = agent.learner.apply_concepts(course.id, ev.get("concepts") or [], "learn", confidence=conf,
                                               problem_ref=f"Lernen #{lesson_id} Schritt {n}")
        lines = _map_lines(agent, course.id, touched)
    else:
        lines = []

    reply = str(data.get("reply_markdown") or "").strip()
    if reply:
        parts.append(reply)
    if lines:
        parts.append("**Lernkarte:** " + " · ".join(lines))
    new_plan = _clean_plan(data.get("plan"))
    if new_plan and new_plan != lesson["plan"]:
        agent.learner.set_lesson_plan(lesson_id, str(data.get("goal") or "").strip() or None, new_plan)
        lesson["plan"] = new_plan
        parts.append("**Plan angepasst:**\n\n" + plan_mermaid(new_plan, turns))

    q = _valid_question(data.get("question"))
    out_of_turns = len(turns) >= MAX_TURNS
    if bool(data.get("done")) or out_of_turns:
        summary = str(data.get("summary_markdown") or "").strip()
        agent.learner.finish_lesson(lesson_id, "done", summary or None)
        parts.append(_final(agent, course, lesson, agent.learner.lesson_turns(lesson_id), summary,
                            out_of_turns and not data.get("done")))
        return "\n\n".join(parts), course.id

    if phase == PHASE_CONFIRM and q is None:          # Plan überarbeitet, erneut bestätigen lassen
        agent.learner.add_lesson_turn(lesson_id, n + 1, reply, None)
        parts.append("Passt es so? *ja* startet die Lektion.")
        parts.append(_footer(lesson_id, n + 1))
        return "\n\n".join(parts), course.id
    if q is None:
        q = {"type": "check", "node": last["q_node"], "options": [], "answer_key": "",
             "text": "Fass in einem Satz zusammen, was du aus diesem Schritt mitnimmst."}
    agent.learner.add_lesson_turn(lesson_id, n + 1, reply, q)
    parts.append(_render_question(q))
    parts.append(_footer(lesson_id, n + 1))
    return "\n\n".join(parts), course.id


def _final(agent: "Agent", course: "Course", lesson: dict, turns: list[dict], summary: str,
           cut_short: bool = False) -> str:
    checks = [t for t in turns if t["q_type"] == "check" and t["answer"] is not None]
    right = sum(1 for t in checks if t["outcome"] == "correct")
    parts = ["## Lektion abgeschlossen" if not cut_short else "## Lektion beendet (Schrittlimit erreicht)"]
    if checks:
        parts.append(f"{len(checks)} Kontrollfragen, davon {right} richtig.")
    if summary:
        parts.append(summary)
    if lesson["plan"]:
        parts.append(plan_mermaid(lesson["plan"], turns))
    lines = _map_lines(agent, course.id, lesson["node_ids"][:1] + [
        n.id for n in agent.learner.nodes(course.id)
        if any(e.kind == "learn" and (e.problem_ref or "").startswith(f"Lernen #{lesson['id']} ")
               for e in n.evidence)])
    if lines:
        parts.append("**Lernkarte:**\n" + "\n".join(f"- {x}" for x in lines))
    parts.append("Festigen: die genannte Prüfungsaufgabe selbst lösen und mit `/check` korrigieren lassen. "
                 "In ein paar Tagen zeigt `/diagnose " + lesson["topic"] + " 3`, ob es sitzt.")
    return "\n\n".join(parts)


def plan_view(agent: "Agent", lesson_id: int) -> str:
    """Plan zeigen und die offene Frage wiederholen – so geht die Lektion auch nach einem Umweg weiter."""
    lesson = agent.learner.get_lesson(lesson_id)
    if lesson is None:
        return "Diese Lektion kenne ich nicht (mehr)."
    turns = agent.learner.lesson_turns(lesson_id)
    parts = [f"**Plan: {lesson['topic']}**"]
    if lesson["goal"]:
        parts.append(f"**Ziel:** {lesson['goal']}")
    parts.append(plan_mermaid(lesson["plan"], turns))
    parts.append("Blau: hier sind wir · Grün: mit einer Kontrollfrage gezeigt.")
    last = turns[-1] if turns else None
    if lesson["status"] == "open" and last and last["answer"] is None:
        if last["q_type"]:
            parts.append(_render_question({"type": last["q_type"], "text": last["q_text"],
                                           "options": last["q_options"]}))
        else:
            parts.append("Passt der Plan? *ja* startet die Lektion.")
        parts.append(_footer(lesson_id, last["n"]))
    return "\n\n".join(parts)


def stop(agent: "Agent", lesson_id: int) -> tuple[str, str | None]:
    lesson = agent.learner.get_lesson(lesson_id)
    if lesson is None or lesson["status"] != "open":
        return "Gerade läuft keine Lektion.", lesson["course"] if lesson else None
    agent.learner.finish_lesson(lesson_id, "stopped", None)
    course = agent.cfg.courses.get(lesson["course"])
    if course is None:
        return "Lektion beendet.", None
    turns = agent.learner.lesson_turns(lesson_id)
    text = _final(agent, course, lesson, turns, "", cut_short=False).replace(
        "## Lektion abgeschlossen", "## Lektion beendet")
    return text + f"\n\nWeitermachen später: `/learn {lesson['topic']}` (die Lernkarte merkt sich den Stand).", course.id
