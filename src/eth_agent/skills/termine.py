"""Prüfungstermine: aus normaler Sprache erkennen, speichern, anzeigen; Stoff einer Prüfung als Fokus.

"Ich habe am 12.12. den Midterm in SST, Stoff: Faltung, LTI, Fourierreihen" -> ein Modellaufruf extrahiert
Art, Datum, Themen und ordnet die Themen den Kapiteln der Lernkarte zu. Danach reine Datenbank:
Countdown beim Fachwechsel, /termine, Fokus für /diagnose, /learn und /lernplan.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Callable

from ..learner.model import STATUS_ORDER, Node
from .check import dump_raw, ensure_skeleton, parse_grade

if TYPE_CHECKING:
    from ..agent.core import Agent
    from ..config import Course

WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
WEEKDAY_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
KIND_DE = {"midterm": "Midterm", "pruefung": "Prüfung", "test": "Test", "abgabe": "Abgabe", "sonstiges": "Termin"}

_KEYWORD = re.compile(r"\b(midterm|zwischenprüfung|zwischenpruefung|prüfung|pruefung|klausur|exam|test|quiz|"
                      r"abgabe|deadline|testat|session|semesterendprüfung)", re.I)
_DATE = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(\d{2,4})?|\b\d{4}-\d{2}-\d{2}\b|"
                   r"\b\d{1,2}\.?\s?(januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|"
                   r"november|dezember|jan|feb|mär|apr|jun|jul|aug|sept?|okt|nov|dez)\b|"
                   r"\b(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b|"
                   r"\b(in|nach)\s+(\d+|einer|zwei|drei|vier)\s+(tag|tagen|woche|wochen)\b|"
                   r"\bnächste[nrs]?\s+woche\b|\bübermorgen\b|\bmorgen\b", re.I)
_STATEMENT = re.compile(r"\b(habe|hab|haben|ist|findet|schreibe|schreiben|steht|kommt|muss|termin|am)\b", re.I)
_QUESTION_START = re.compile(r"^\s*(wann|was|wie|welche[rsn]?|wo|kannst|könntest|gibt es|hast du|erkläre)\b", re.I)


def looks_like_exam(text: str) -> bool:
    """Heuristik: Aussage über einen Termin (Stichwort + Datum + Aussage, keine Frage)."""
    t = (text or "").strip()
    if not t or t.endswith("?") or _QUESTION_START.match(t) or len(t) > 600:
        return False
    return bool(_KEYWORD.search(t) and _DATE.search(t) and _STATEMENT.search(t))


def today() -> date:
    return datetime.now().date()


def fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{WEEKDAY_DE[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


def days_left(iso: str) -> int:
    return (date.fromisoformat(iso) - today()).days


def in_days(n: int) -> str:
    return "heute" if n == 0 else "morgen" if n == 1 else f"in {n} Tagen" if n > 0 else f"vor {-n} Tagen"


# ---------------------------------------------------------------------- Stoff
def scope_nodes(agent: "Agent", course_id: str, exam: dict) -> list[Node]:
    """Alle Knoten im Prüfungsstoff (gewählte Knoten samt Unterknoten)."""
    by_id = {n.id: n for n in agent.learner.nodes(course_id)}
    out, seen = [], set()

    def walk(n: Node) -> None:
        if n.id in seen:
            return
        seen.add(n.id)
        out.append(n)
        for c in n.children:
            walk(c)

    for nid in exam["node_ids"]:
        if nid in by_id:
            walk(by_id[nid])
    return out


def coverage(nodes: list[Node]) -> dict:
    leaves = [n for n in nodes if not n.children] or nodes
    stat = {s: 0 for s in STATUS_ORDER}
    for n in leaves:
        stat[n.own_status] += 1
    return {"total": len(leaves), "tested": len(leaves) - stat["untested"], **stat}


def next_exam(agent: "Agent", course_id: str) -> dict | None:
    ex = agent.learner.exams(course_id, from_date=today().isoformat())
    return ex[0] if ex else None


def exam_line(agent: "Agent", exam: dict, with_course: bool = False) -> str:
    n = days_left(exam["date"])
    course = agent.cfg.courses.get(exam["course"])
    who = f" {course.name}" if (with_course and course) else ""
    when = f"{fmt_date(exam['date'])}{(' ' + exam['time']) if exam.get('time') else ''}"
    line = f"📅 **{exam['title']}{who}** – {when} ({in_days(n)})"
    nodes = scope_nodes(agent, exam["course"], exam)
    if nodes:
        cov = coverage(nodes)
        weak = cov["misconception"] + cov["missing"] + cov["shaky"]
        line += f" · Stoff: {cov['total']} Themen, {cov['tested']} geprüft"
        if weak:
            line += f", {weak} wackeln oder fehlen"
    elif exam["topics"]:
        line += " · Stoff: " + ", ".join(exam["topics"])
    return line


def countdown_line(agent: "Agent", course_id: str) -> str:
    ex = next_exam(agent, course_id)
    return exam_line(agent, ex) if ex else ""


def focus_for_exam(agent: "Agent", course_id: str) -> tuple[dict | None, list[Node]]:
    ex = next_exam(agent, course_id)
    if not ex:
        return None, []
    return ex, scope_nodes(agent, course_id, ex)


# ---------------------------------------------------------------------- Eintragen
def _outline(agent: "Agent", course_id: str) -> str:
    lines = []
    for n in agent.learner.nodes(course_id):
        depth = n.id.split(":", 1)[-1].count("-")
        if depth <= 1:
            lines.append(f"{'  ' * depth}{n.id} | {n.title}")
    return "\n".join(lines[:250]) or "(no outline)"


def _map_topics(agent: "Agent", course_id: str, topics: list[str]) -> list[str]:
    """Rückfall ohne Modell-Zuordnung: Themen über die Titel der Lernkarte finden."""
    from . import diagnose as dg
    ids: list[str] = []
    for t in topics:
        found = dg.focus_nodes(agent, course_id, t)
        if found:
            ids.append(found[0].id)
    return list(dict.fromkeys(ids))


def add_from_text(agent: "Agent", course: "Course", text: str, say: Callable[[str], None]) -> str:
    from ..agent.core import _fill, _template
    from ..agent.routing import est_tokens

    if course.id:
        ensure_skeleton(agent, course.id)
    d = today()
    prompt = _fill(_template("termin.md"), today=d.isoformat(), weekday=WEEKDAY_LONG[d.weekday()],
                   current_course=f"{course.id} | {course.name}" if course.id else "(none selected)",
                   courses="\n".join(f"{c.id} | {c.name}" for c in agent.cfg.courses.values() if c.id),
                   outline=_outline(agent, course.id) if course.id else "(no course selected)", message=text)
    data = None
    for name in [n for n in ("codex", "claude") if n in agent.providers and agent.providers[n].available()]:
        effort = agent.cfg.models.codex_effort_default if name == "codex" else None
        say(f"{name} liest den Termin …")
        res = agent.providers[name].complete(prompt, timeout=agent.cfg.models.timeout_s, effort=effort)
        agent.usage.record(res, skill="termin", rule="termin", prompt_tokens_est=est_tokens(prompt))
        if res.ok:
            data = parse_grade(res.text)
            if data:
                break
            say(f"{name}: Antwort nicht lesbar (abgelegt in {dump_raw(agent, 'termin', res.text)})")
    if not data:
        return "Den Termin konnte ich nicht auslesen. Versuch es so: `/termin Midterm am 12.12.2026, Stoff: Faltung, LTI`."
    if not data.get("exams") and data.get("question"):
        return f"{data['question']}\n\n*(Antworte mit `/termin …` und dem Datum.)*"

    saved = []
    for e in data.get("exams") or []:
        try:
            iso = date.fromisoformat(str(e.get("date"))).isoformat()
        except ValueError:
            continue
        cid = str(e.get("course") or course.id or "")
        if cid not in agent.cfg.courses or not cid:
            cid = course.id
        if not cid:
            return ("Für welches Fach ist der Termin? Wähle zuerst ein Fach (z. B. `/sst`) oder nenne es im Satz.")
        ensure_skeleton(agent, cid)
        known = {n.id for n in agent.learner.nodes(cid)}
        topics = [str(t).strip() for t in (e.get("topics") or []) if str(t).strip()]
        node_ids = [n for n in (e.get("node_ids") or []) if n in known] if cid == course.id else []
        if topics and not node_ids:
            node_ids = _map_topics(agent, cid, topics)
        kind = e.get("kind") if e.get("kind") in KIND_DE else "sonstiges"
        title = str(e.get("title") or KIND_DE[kind]).strip()
        eid = agent.learner.add_exam(cid, kind, title, iso, str(e.get("time") or "") or None, topics, node_ids,
                                     str(e.get("note") or "") or None)
        saved.append(agent.learner.get_exam(eid))
    if not saved:
        return "Ich habe kein gültiges Datum gefunden. Beispiel: `/termin Midterm am 12.12.2026, Stoff: Faltung, LTI`."

    parts = []
    for ex in saved:
        parts.append("Gespeichert: " + exam_line(agent, ex, with_course=True))
        nodes = scope_nodes(agent, ex["course"], ex)
        top = [n for n in nodes if n.id in ex["node_ids"]]
        if top:
            parts.append("Zugeordnet in der Lernkarte: " + ", ".join(n.title for n in top[:12])
                         + (" …" if len(top) > 12 else ""))
        elif ex["topics"]:
            parts.append("*Die Themen konnte ich keinem Kapitel der Lernkarte zuordnen – sie zählen trotzdem als "
                         "Stoff für den Lernplan.*")
        if days_left(ex["date"]) < 0:
            parts.append("⚠ Das Datum liegt in der Vergangenheit – stimmt das Jahr?")
        parts.append(f"*Falsch? `/termine löschen {ex['id']}`*")
    parts.append("Nächster Schritt: `/lernplan` erstellt eine priorisierte Liste mit Übungen bis zum Termin.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------- /termine
def command(agent: "Agent", course: "Course", arg: str, say: Callable[[str], None]) -> str:
    arg = (arg or "").strip()
    m = re.match(r"^(löschen|loeschen|delete|entfernen)\s+#?(\d+)$", arg, re.I)
    if m:
        ex = agent.learner.get_exam(int(m.group(2)))
        if ex and agent.learner.delete_exam(ex["id"]):
            return f"Gelöscht: {ex['title']} am {fmt_date(ex['date'])}."
        return f"Keinen Termin #{m.group(2)} gefunden. `/termine` zeigt alle."
    if arg and not re.match(r"^(alle|all)$", arg, re.I):
        return add_from_text(agent, course, arg, say)
    past = bool(arg)
    exams = agent.learner.exams(course.id or None, from_date=None if past else today().isoformat())
    if not exams:
        return ("Noch keine Termine gespeichert. Schreib einfach z. B. *«Ich habe am 12.12. den Midterm, Stoff: "
                "Faltung, LTI-Systeme»* oder `/termin …`.")
    lines = [f"- `#{ex['id']}` " + exam_line(agent, ex, with_course=not course.id) for ex in exams]
    head = "**Termine**" + (f" – {course.name}" if course.id else "")
    return head + "\n\n" + "\n".join(lines) + "\n\nLöschen: `/termine löschen <nr>` · auch vergangene: `/termine alle`"
