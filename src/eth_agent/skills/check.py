"""/check – eigenen Lösungsversuch korrigieren lassen (v5: auch ohne Musterlösung).

Ablauf:
1. Aufgabe bestimmen (Verweis wie "Serie 3 Aufgabe 1" oder eingegebener Aufgabentext).
2. Referenz herstellen:
   - Musterlösung vorhanden -> sie ist die Referenz.
   - sonst: Codex und Claude lösen unabhängig (parallel), jeweils mit Prüfcode.
3. Bewertung durch ein Modell (bevorzugt Claude, bei Bildern Codex): Punkte, erster Fehler,
   Fehlerklasse, Hinweis, Prüfcode für die Endergebnisse des Studenten.
4. Prüfcode des Studenten-Ergebnisses in der Sandbox ausführen.
5. Vertrauensstufe der Referenz bestimmen; bei "unsicher" keine harte Bewertung.
6. Fehler in den Fehlerkatalog schreiben (data_dir/profile/errors.jsonl).
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from ..agent.references import resolve
from ..checks.sandbox import CheckResult, check_answer, run_python
from ..llm.providers import LLMResult
from ..retrieval.search import search
from ..retrieval.store import ChunkRow

if TYPE_CHECKING:
    from ..agent.core import Agent

_REVEAL = re.compile(r"\s--(l(ö|oe)sung|solution|reveal)\b", re.I)
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)

TRUST_LABELS = {
    "official": "Musterlösung",
    "two_tool": "2 Modelle einig + Tool",
    "two": "2 Modelle einig (ohne Tool)",
    "one_tool": "1 Modell + Tool",
    "uncertain": "unsicher",
}
VERDICT_DE = {"correct": "richtig", "partially_correct": "teilweise richtig", "incorrect": "falsch"}
ERROR_DE = {
    "concept": "Konzept", "setup": "Ansatz", "algebra": "Algebra", "sign": "Vorzeichen", "units": "Einheiten",
    "boundary_condition": "Rand-/Anfangsbedingung", "misread_task": "Aufgabe falsch gelesen",
    "careless": "Flüchtigkeitsfehler", "incomplete": "unvollständig",
}


@dataclass
class CheckInput:
    task: str
    attempt: str
    reveal: bool = False


def parse_input(text: str) -> CheckInput:
    """'Serie 3 Aufgabe 1\\n<Versuch>' oder '<Aufgabentext>\\n---\\n<Versuch>'. '--loesung' zeigt die Lösung."""
    reveal = bool(_REVEAL.search(text))
    text = _REVEAL.sub("", text).strip()
    m = re.search(r"\n\s*-{3,}\s*\n", text)
    if m:
        return CheckInput(text[:m.start()].strip(), text[m.end():].strip(), reveal)
    first, _, rest = text.partition("\n")
    return CheckInput(first.strip(), rest.strip(), reveal)


@dataclass
class Solution:
    provider: str
    text: str
    check: CheckResult
    llm: LLMResult


@dataclass
class CheckReport:
    task_label: str
    ok: bool
    message: str = ""
    trust: str = "uncertain"
    grade: dict = field(default_factory=dict)
    grader: str | None = None
    student_check: CheckResult = field(default_factory=lambda: CheckResult("none", ""))
    solutions: list[Solution] = field(default_factory=list)
    reference_text: str = ""
    reveal: bool = False
    reveal_blocked: bool = False
    sources: list[ChunkRow] = field(default_factory=list)
    map_updates: list[tuple[str, str]] = field(default_factory=list)   # (Titel, Status)
    probe_id: int | None = None
    probe_question: str | None = None
    plan_done: list[int] = field(default_factory=list)    # abgehakte Lernplan-Aufgaben (P…)

    def render(self, course_name: str) -> str:
        if not self.ok:
            return self.message
        g = self.grade
        out = [f"## Korrektur – {self.task_label}"]
        if self.trust == "uncertain":
            out.append("> ⚠ **Referenz unsicher** – die Modelle sind sich nicht einig oder nichts liess sich per "
                       "Code prüfen. Die Punkte unten sind nur eine vorläufige Einschätzung.")
            if g.get("reference_disagreement_note"):
                out.append(f"> {g['reference_disagreement_note']}")
        pts = f"{_num(g.get('points_awarded'))} / {_num(g.get('points_max'))}"
        scope = " (bearbeitete Teile)" if g.get("not_attempted") else ""
        out.append(f"**Punkte{scope}, geschätzt: {pts}** · Bewertung: "
                   f"{VERDICT_DE.get(g.get('verdict'), g.get('verdict', '?'))}")
        if g.get("correct_parts"):
            out.append("**Richtig:**\n" + "\n".join(f"- {x}" for x in g["correct_parts"]))
        fe = g.get("first_error")
        if fe:
            typ = ERROR_DE.get(fe.get("error_type"), fe.get("error_type", ""))
            out.append(f"**Erster Fehler** ({fe.get('location', '?')} · {typ}): {fe.get('description', '')}")
        if g.get("further_errors"):
            out.append("**Weitere Punkte:**\n" + "\n".join(f"- {x}" for x in g["further_errors"]))
        if g.get("hint"):
            out.append(f"**Hinweis:** {g['hint']}")
        if g.get("feedback_markdown"):
            out.append(g["feedback_markdown"])
        if g.get("not_attempted"):
            out.append("*Nicht bearbeitet:* " + ", ".join(map(str, g["not_attempted"])))
        if self.trust != "uncertain" and g.get("reference_disagreement_note"):
            out.append(f"*Anmerkung zur Referenz:* {g['reference_disagreement_note']}")
        if self.reveal and not self.reveal_blocked:
            out.append("### Lösung (Referenz)\n" + self.reference_text)
        elif self.reveal_blocked:
            out.append("*Lösung nicht angezeigt.*")
        else:
            out.append("*Lösung anzeigen: dieselbe Eingabe mit `--loesung` wiederholen.*")
        if self.map_updates:
            from ..learner.model import STATUS_ICON, STATUS_LABEL
            out.append("**Lernkarte aktualisiert:** " + " · ".join(
                f"{STATUS_ICON[st]} {t} ({STATUS_LABEL[st]})" for t, st in self.map_updates))
        if self.plan_done:
            out.append("**Lernplan:** " + ", ".join(f"P{n}" for n in self.plan_done) + " erledigt ✅ "
                       "(`/lernplan` zeigt den Rest)")
        if self.probe_question:
            out.append(f"### Rückfrage #{self.probe_id}\n{self.probe_question}\n\n"
                       "*Antworte einfach in deiner nächsten Nachricht – je genauer du deinen Gedankengang "
                       "beschreibst, desto genauer wird die Lernkarte. Überspringen: `/weiter`.*")
        if self.sources:
            out.append("---\n**Quellen**\n" + "\n".join(
                f"[{i}] {c.citation(course_name)}" for i, c in enumerate(self.sources, 1)))
        footer = [f"Referenz: {TRUST_LABELS[self.trust]}", f"Dein Endergebnis: {_student_badge(self.student_check)}"]
        if self.grader:
            footer.append(f"Bewertet von: {self.grader}")
        out.append(" · ".join(footer))
        return "\n\n".join(out)


def _num(x) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return "?" if x is None else str(x)


def _student_badge(c: CheckResult) -> str:
    return {"pass": "Tool ✓", "fail": "Tool ✗", "error": "Tool-Check fehlerhaft", "none": "nicht per Code prüfbar"}[c.status]


def parse_grade(text: str) -> dict | None:
    m = _JSON_BLOCK.findall(text)
    candidates = m[::-1] if m else []
    if not candidates:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates = [text[start:end + 1]]
    for c in candidates:
        for attempt in (c, _repair_escapes(c)):
            try:
                data = json.loads(attempt)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    return None


_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _repair_escapes(text: str) -> str:
    """LaTeX in JSON-Strings ohne doppelten Backslash (\\psi, \\hbar …) retten."""
    return _BAD_ESCAPE.sub(r"\\\\", text)


def dump_raw(agent, skill: str, text: str) -> str:
    """Unlesbare Modellantwort zur Fehlersuche ablegen; gibt den Pfad zurück."""
    import time
    d = agent.cfg.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{skill}-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    path.write_text(text or "(leer)", encoding="utf-8")
    return str(path)


def trust_level(official: bool, solutions: list[Solution], agreement: str | None) -> str:
    if official:
        return "official"
    tool_ok = any(s.check.status == "pass" for s in solutions)
    tool_failed = any(s.check.status == "fail" for s in solutions)
    if len(solutions) >= 2 and agreement == "agree" and not tool_failed:
        return "two_tool" if tool_ok else "two"
    if len(solutions) == 1 and solutions[0].check.status == "pass":
        return "one_tool"
    return "uncertain"


def _log_error(agent: "Agent", course: str, label: str, grade: dict) -> None:
    fe = grade.get("first_error")
    if not fe and grade.get("verdict") == "correct":
        entry_type = None
    else:
        entry_type = (fe or {}).get("error_type")
    path = agent.cfg.data_dir / "profile" / "errors.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "date": datetime.now().isoformat(timespec="seconds"), "course": course, "problem_ref": label,
        "verdict": grade.get("verdict"), "points": grade.get("points_awarded"),
        "points_max": grade.get("points_max"), "error_type": entry_type,
        "location": (fe or {}).get("location"), "description": (fe or {}).get("description"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_check(agent: "Agent", text: str, *, course_id: str | None = None, images: list[str] | None = None,
              on_status: Callable[[str], None] | None = None) -> CheckReport:
    from ..agent.core import LANG_NAMES, _fill, _format_chunk, _template, est_tokens

    say = on_status or (lambda s: None)
    course = agent.cfg.courses[course_id] if course_id else next(iter(agent.cfg.courses.values()))
    inp = parse_input(text)
    images = images or []
    if not inp.task:
        return CheckReport("", False, "Bitte Aufgabe angeben, z. B. `/check Serie 3 Aufgabe 1` und darunter "
                                      "deinen Lösungsweg.")
    if not inp.attempt and not images:
        return CheckReport(inp.task, False, "Kein Lösungsversuch gefunden. Schreibe deinen Lösungsweg unter die "
                                            "erste Zeile (oder nach einer Zeile `---`).")

    # 1. Aufgabe -----------------------------------------------------------
    ref = resolve(agent.store, course.id, inp.task, lambda n: agent.learner.plan_item(course.id, n))
    if ref is not None and ref.kind == "datei" and ref.problem_no and ref.chunks:
        # Datei gefunden, aber diese Aufgabennummer nicht darin
        have = sorted({(c.problem_no or "").split(".")[0] for c in ref.chunks if c.problem_no},
                      key=lambda x: (len(x), x))
        name = ref.chunks[0].file_name
        return CheckReport(inp.task, False, f"In **{name}** gibt es keine Aufgabe {ref.problem_no}."
                           + (f" Erkannte Aufgaben: {', '.join(have)}." if have else
                              " Ich erkenne dort keine nummerierten Aufgaben – falls die Datei schon länger "
                              "indexiert ist: `eth-agent index --course <fach> --force` liest sie neu ein.")
                           + " Alternativ: Aufgabentext, dann `---`, dann dein Versuch.")
    if ref is not None and ref.kind == "datei" and not ref.problem_no:
        return CheckReport(inp.task, False, f"Welche Aufgabe aus **{ref.chunks[0].file_name if ref.chunks else inp.task}"
                                            f"**? Z. B. `/check {inp.task} Aufgabe 2`.")
    problem_chunks: list[ChunkRow] = ref.chunks if ref and ref.chunks else []
    official: list[ChunkRow] = agent.store.solution_for(problem_chunks[0]) if problem_chunks else []
    if problem_chunks:
        label = problem_chunks[0].citation(course.name)
        say(f"Aufgabe gefunden: {label}")
        problem = "\n\n".join(c.text for c in problem_chunks)
        if ref.part:
            problem += f"\n\n(The student only attempts sub-task {ref.part}.)"
        points = problem_chunks[0].points
        if points:
            problem += f"\n\n(Point value of this problem in the exam: {_num(points)} points.)"
    elif ref is not None or len(inp.task) < 40:
        return CheckReport(inp.task, False, f"Aufgabe „{inp.task}“ nicht im Material gefunden. Gib eine Aufgabe wie "
                                            "*Serie 3 Aufgabe 1* an oder schreibe den Aufgabentext, dann `---`, dann "
                                            "deinen Versuch.")
    else:
        label = "eigene Aufgabe"
        problem = inp.task

    hits = search(agent.store, problem[:800], course=course.id, final_k=4,
                  embedding_model=agent.embedding_model, encode=agent.encode,
                  doc_types=["skript", "formelsammlung", "uebungsstunde"])
    context_chunks = [h.chunk for h in hits]
    context = "\n\n".join(_format_chunk(i, c, course.name) for i, c in enumerate(context_chunks, 1)) \
        or "(none)"

    # 2. Referenz ------------------------------------------------------------
    solutions: list[Solution] = []
    if official:
        say(f"Musterlösung gefunden: {official[0].citation(course.name)}")
        reference = "\n\n".join(c.text for c in official)
        ref_status = "official solution (Musterlösung)"
        ref_note = ("Treat the official solution as ground truth for grading. It was extracted from a PDF, so "
                    "formulas may be flattened. If it is clearly wrong somewhere, say so in "
                    "reference_disagreement_note and grade against the correct physics. Set reference_agreement "
                    "to \"official\".")
    else:
        avail = [n for n in ("codex", "claude") if n in agent.providers and agent.providers[n].available()]
        if not avail:
            return CheckReport(label, False, "Weder `codex` noch `claude` ist verfügbar.")
        say("Keine Musterlösung – erstelle Referenz mit " + " und ".join(avail) + " (unabhängig) …")
        solve_prompt = _fill(_template("check_solve.md"), course_name=course.name,
                             check_instructions=_template("check_block.md"), problem=problem, context=context)

        def solve(name: str) -> Solution:
            effort = agent.cfg.models.codex_effort_hard if name == "codex" else None
            res = agent.providers[name].complete(solve_prompt, timeout=agent.cfg.models.timeout_s, effort=effort)
            agent.usage.record(res, skill="check-solve", rule="check", prompt_tokens_est=est_tokens(solve_prompt))
            if not res.ok:
                return Solution(name, "", CheckResult("none", ""), res)
            txt, chk = check_answer(res.text)
            return Solution(name, txt, chk, res)

        with ThreadPoolExecutor(max_workers=2) as pool:
            solutions = [s for s in pool.map(solve, avail) if s.llm.ok]
        for s in solutions:
            say(f"Referenz {s.provider}: Prüfcode {s.check.badge}")
        if not solutions:
            return CheckReport(label, False, "Keine Referenzlösung erhalten (beide Modelle fehlgeschlagen).")
        reference = "\n\n".join(
            f"### Reference {chr(65 + i)} (independent solver, tool check: {s.check.badge})\n{s.text}"
            for i, s in enumerate(solutions))
        if len(solutions) >= 2:
            ref_status = "two independent model solutions (no official solution available)"
            ref_note = ("First compare the final results of Reference A and B. Set reference_agreement to \"agree\" "
                        "if all final results match (up to rounding and equivalent forms), else \"disagree\" and "
                        "explain in reference_disagreement_note which one is right, using the tool check status "
                        "and your own derivation.")
        else:
            ref_status = "one model solution (no official solution available)"
            ref_note = "Set reference_agreement to \"single\". Re-derive critical steps yourself before grading."

    # 3. Bewertung -------------------------------------------------------------
    order = ["codex"] if images else ["claude", "codex"]
    order = [n for n in order if n in agent.providers and agent.providers[n].available()]
    if not order:
        return CheckReport(label, False, "Kein Modell für die Bewertung verfügbar"
                           + (" (Bilder brauchen Codex)." if images else "."))
    attempt = inp.attempt or "(see attached image(s) of the student's handwritten work)"
    from ..learner.model import parse_confidence
    ensure_skeleton(agent, course.id)
    confidence = parse_confidence(inp.attempt)
    grade_prompt = _fill(_template("check_grade.md"), course_name=course.name,
                         learner_map=agent.learner.outline(course.id),
                         output_language_name=LANG_NAMES.get(course.output_language, course.output_language),
                         reference_status=ref_status, reference_note=ref_note, problem=problem,
                         reference=reference, attempt=attempt)
    grade, grader = None, None
    for name in order:
        say(f"{name} bewertet deinen Versuch …")
        effort = agent.cfg.models.codex_effort_hard if name == "codex" else None
        res = agent.providers[name].complete(grade_prompt, timeout=agent.cfg.models.timeout_s, effort=effort,
                                             images=images if name == "codex" else None)
        agent.usage.record(res, skill="check-grade", rule="check", prompt_tokens_est=est_tokens(grade_prompt))
        if res.ok:
            grade = parse_grade(res.text)
            if grade:
                grader = name
                break
            say(f"{name}: Antwort nicht lesbar (kein JSON)")
        else:
            say(f"{name} fehlgeschlagen" + (f": {res.error[:200]}" if res.error else ""))
    if not grade:
        return CheckReport(label, False, "Bewertung fehlgeschlagen (keine auswertbare Antwort).")

    # Versuch gehört gar nicht zu dieser Aufgabe (falsche Aufgabe aufgelöst oder falsches Foto)?
    if grade.get("attempt_matches_problem") is False or (
            not grade.get("points_max") and not grade.get("correct_parts") and grade.get("not_attempted")):
        note = str(grade.get("mismatch_note") or "").strip()
        return CheckReport(label, False,
                           f"**Dein Versuch passt nicht zu {label}.**"
                           + (f" {note}" if note else "")
                           + "\n\nNichts wurde bewertet oder in die Lernkarte eingetragen. Stimmt die Aufgabe? Gib "
                             "Datei und Nummer genauer an (z. B. `/check Exercise_Session_5 Aufgabe 2`) oder "
                             "schick das passende Foto.")

    # 4. Endergebnis des Studenten per Code prüfen -----------------------------
    student_check = CheckResult("none", "")
    code = (grade.get("student_check_code") or "").strip()
    if code:
        code = re.sub(r"^```(?:python)?\s*|\s*```$", "", code)
        student_check = run_python(code)
        say(f"Dein Endergebnis: {_student_badge(student_check)}")

    # 5./6. Vertrauensstufe, Fehlerkatalog -------------------------------------
    trust = trust_level(bool(official), solutions, grade.get("reference_agreement"))
    _log_error(agent, course.id, label, grade)

    # Lernplan: korrigierte Aufgabe abhaken
    plan_done: list[int] = []
    if problem_chunks:
        c0 = problem_chunks[0]
        plan_done = agent.learner.mark_plan_done(course.id, c0.pair_key, c0.doc_type, c0.problem_no or "",
                                                 grade.get("verdict"))
        if plan_done:
            say("Lernplan: " + ", ".join(f"P{n}" for n in plan_done) + " abgehakt")

    # 7. Lernkarte und Rückfrage (nur bei verlässlicher Referenz) -----------------
    map_updates, probe_id, probe_q = [], None, None
    if trust != "uncertain":
        touched = agent.learner.apply_concepts(course.id, grade.get("concepts") or [], "check",
                                               confidence=confidence, problem_ref=label)
        by_id = {n.id: n for n in agent.learner.nodes(course.id)}
        map_updates = [(by_id[t].title, by_id[t].own_status) for t in dict.fromkeys(touched) if t in by_id]
        probe = grade.get("probe")
        if isinstance(probe, dict) and str(probe.get("question") or "").strip():
            probe_q = str(probe["question"]).strip()
            fe = grade.get("first_error") or {}
            ctx = (f"### Problem\n{problem[:3000]}\n\n### Student attempt\n{attempt[:3000]}\n\n"
                   f"### Grading\nverdict: {grade.get('verdict')}; first error: {fe.get('description', '-')} "
                   f"({fe.get('error_type', '-')}); probe purpose: {probe.get('why', '')}")
            probe_id = agent.learner.open_probe(course.id, probe_q, ctx, touched, label)
            say(f"Rückfrage #{probe_id} gestellt")

    blocked = False   # ganze Lösung gibt es immer auf ausdrücklichen Wunsch (--loesung)
    # Skript-Auszüge wurden nur den unabhängigen Lösern gegeben, nicht dem Bewerter bei Musterlösung.
    sources = problem_chunks[:1] + official[:1] + ([] if official else context_chunks)
    return CheckReport(label, True, trust=trust, grade=grade, grader=grader, student_check=student_check,
                       solutions=solutions, reference_text=reference, reveal=inp.reveal,
                       reveal_blocked=blocked, sources=sources, map_updates=map_updates,
                       probe_id=probe_id, probe_question=probe_q, plan_done=plan_done)


def ensure_skeleton(agent: "Agent", course_id: str) -> int:
    """Lernkarten-Gerüst aus den nummerierten Skript-Abschnitten anlegen (einmalig, danach billig)."""
    have = agent.learner.db.execute("SELECT count(*) FROM nodes WHERE course=? AND origin='skript'",
                                    (course_id,)).fetchone()[0]
    if have:
        return 0
    rows = agent.store.db.execute(
        "SELECT section FROM chunks WHERE course=? AND doc_type='skript' AND section IS NOT NULL "
        "ORDER BY file_id, ord", (course_id,)).fetchall()
    return agent.learner.build_skeleton(course_id, [r[0] for r in rows])
