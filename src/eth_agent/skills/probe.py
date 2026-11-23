"""Antwort auf eine Rückfrage auswerten: Gedankengang -> Diagnose -> Lernkarte -> kurze Rückmeldung."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable

from .check import parse_grade

if TYPE_CHECKING:
    from ..agent.core import Agent, Turn

PROBE_MARK = re.compile(r"R(ü|ue)ckfrage #(\d+)")
DIAG_DE = {"slip": "Flüchtigkeitsfehler – das Konzept sitzt", "misconception": "Fehlvorstellung",
           "knowledge_gap": "Wissenslücke", "misread": "Aufgabe falsch gelesen",
           "sound": "Gedankengang stimmt", "unclear": "noch unklar"}
MAX_DEPTH = 2   # höchstens eine Nachfrage auf eine Rückfrage


def pending_probe(history: list["Turn"]) -> int | None:
    """ID der offenen Rückfrage, wenn die letzte Antwort des Agenten eine gestellt hat."""
    for t in reversed(history):
        if t.role == "assistant":
            m = PROBE_MARK.findall(t.text)
            return int(m[-1][1]) if m else None
    return None


def run_probe_answer(agent: "Agent", probe_id: int, answer: str,
                     say: Callable[[str], None]) -> tuple[str, str | None]:
    """Gibt (Antworttext, Kurs-ID) zurück."""
    from ..agent.core import LANG_NAMES, _fill, _template, est_tokens

    probe = agent.learner.get_probe(probe_id)
    if probe is None:
        return "Diese Rückfrage kenne ich nicht (mehr).", None
    course = agent.cfg.courses.get(probe["course"])
    if course is None:
        return "Das Fach dieser Rückfrage gibt es nicht mehr.", None
    if probe["status"] != "open":
        return "Diese Rückfrage ist schon beantwortet.", course.id
    depth = probe["context"].count("### Earlier follow-up") + 1
    follow_rule = ("If – and only if – the answer is still ambiguous and one more precise question would settle "
                   "the diagnosis, set follow_up to {\"question\": \"…\"}. Otherwise null."
                   if depth < MAX_DEPTH else "Set follow_up to null.")
    prompt = _fill(_template("probe_eval.md"), course_name=course.name,
                   output_language_name=LANG_NAMES.get(course.output_language, course.output_language),
                   follow_up_rule=follow_rule, context=probe["context"], question=probe["question"],
                   answer=answer, learner_map=agent.learner.outline(course.id))
    order = [n for n in ("claude", "codex") if n in agent.providers and agent.providers[n].available()]
    data = None
    for name in order:
        say(f"{name} wertet deinen Gedankengang aus …")
        effort = agent.cfg.models.codex_effort_default if name == "codex" else None
        res = agent.providers[name].complete(prompt, timeout=agent.cfg.models.timeout_s, effort=effort)
        agent.usage.record(res, skill="probe", rule="probe", prompt_tokens_est=est_tokens(prompt))
        if res.ok:
            data = parse_grade(res.text)
            if data:
                break
    agent.learner.close_probe(probe_id, answer)
    if not data:
        return "Deine Antwort ist gespeichert, aber die Auswertung hat nicht geklappt.", course.id

    touched = agent.learner.apply_concepts(course.id, data.get("concepts") or [], "probe",
                                           problem_ref=probe["problem_ref"])
    by_id = {n.id: n for n in agent.learner.nodes(course.id)}
    from ..learner.model import STATUS_ICON, STATUS_LABEL
    parts = [f"**Einschätzung:** {DIAG_DE.get(data.get('diagnosis'), data.get('diagnosis', '?'))}",
             str(data.get("reply_markdown") or "").strip()]
    upd = [(by_id[t].title, by_id[t].own_status) for t in dict.fromkeys(touched) if t in by_id]
    if upd:
        parts.append("**Lernkarte aktualisiert:** " + " · ".join(
            f"{STATUS_ICON[st]} {t} ({STATUS_LABEL[st]})" for t, st in upd))
    fu = data.get("follow_up")
    if isinstance(fu, dict) and str(fu.get("question") or "").strip() and depth < MAX_DEPTH:
        ctx = (probe["context"] + f"\n\n### Earlier follow-up\nQ: {probe['question']}\nA: {answer}")
        new_id = agent.learner.open_probe(course.id, str(fu["question"]).strip(), ctx,
                                          probe["node_ids"] + touched, probe["problem_ref"])
        parts.append(f"### Rückfrage #{new_id}\n{str(fu['question']).strip()}\n\n"
                     "*Antworte einfach in deiner nächsten Nachricht. Überspringen: `/weiter`.*")
    return "\n\n".join(p for p in parts if p), course.id
