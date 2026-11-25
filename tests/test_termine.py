"""Prüfungstermine (Erkennung, Speicher, Countdown, Fokus) und /lernplan (Plan, P-Verweise, Abhaken)."""

import json
from datetime import timedelta

from eth_agent.agent.core import Turn
from eth_agent.agent.references import resolve
from eth_agent.llm.providers import FakeProvider
from eth_agent.skills import termine as tm
from eth_agent.skills.learn import pick_topic

from test_indexer import course_dir, make_cfg, make_pdf  # noqa: F401
from test_learner import _agent


def _exercises(tmp):
    body = 11.0
    d = tmp / "material" / "Physik 2" / "Exercises"
    make_pdf(d / "Ex_8.pdf", [
        [("1", 16, True), ("Orthogonality of eigenstates", 16, True)]
        + [("Show that eigenstates of the infinite square well are orthogonal.", body, False)] * 3,
        [("2", 16, True), ("Superposition in the well", 16, True)]
        + [("Compute |psi(x,t)|^2 for a superposition of two eigenstates of the infinite well.", body, False)] * 3,
    ])


def js(obj):
    return "```json\n" + json.dumps(obj) + "\n```"


def test_looks_like_exam():
    yes = ["Ich habe am 12.12. den Midterm in SST, Stoff: Faltung und LTI",
           "Midterm ist nächsten Freitag, Kapitel 1-5",
           "Die Prüfung findet am 3. Februar statt",
           "ich schreibe in zwei Wochen einen Test über Fourierreihen"]
    no = ["Was kam in der Prüfung am 15.08.2025 dran?", "Wann ist der Midterm?",
          "Erkläre Aufgabe 2 der Prüfung 2024", "Prüfung Aug 2025 Aufgabe 4", "Serie 3 Aufgabe 1"]
    assert all(tm.looks_like_exam(t) for t in yes), [t for t in yes if not tm.looks_like_exam(t)]
    assert not any(tm.looks_like_exam(t) for t in no), [t for t in no if tm.looks_like_exam(t)]


def test_exam_entry_countdown_and_focus(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    in17 = (tm.today() + timedelta(days=17)).isoformat()
    codex = FakeProvider("codex", reply=lambda p: js({"exams": [{
        "course": "physik2", "kind": "midterm", "title": "Midterm", "date": in17, "time": "09:00",
        "topics": ["unendlicher Potentialtopf"], "node_ids": ["physik2:s8", "physik2:gibtsnicht"], "note": ""}],
        "question": ""}) if "Extract it so the agent can" in p else "Antwort.")
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))

    msg = "Ich habe am Freitag in zweieinhalb Wochen den Midterm, Stoff: unendlicher Potentialtopf"
    out = agent.handle(msg, course_id="physik2").render()
    p = codex.prompts[-1]
    assert "Today is" in p and "physik2:s8 | The Infinite Quantum Well" in p and msg in p
    assert "Gespeichert: 📅 **Midterm Physik II**" in out and "(in 17 Tagen)" in out
    assert "Zugeordnet in der Lernkarte: The Infinite Quantum Well" in out and "/termine löschen 1" in out
    ex = agent.learner.exams("physik2")[0]
    assert ex["node_ids"] == ["physik2:s8"] and ex["time"] == "09:00"

    # Countdown beim Fachwechsel, /termine
    sw = agent.handle("/physik2").render()
    assert "Fach gewechselt" in sw and "📅 **Midterm**" in sw and "Stoff:" in sw and "/lernplan" in sw
    lst = agent.handle("/termine", course_id="physik2").render()
    assert "`#1`" in lst and "in 17 Tagen" in lst

    # /learn ohne Thema nimmt eine Schwäche im Prüfungsstoff vor einer schlimmeren ausserhalb
    agent.learner.add_node("physik2", "Spin", None)
    spin = agent.learner.find_node("physik2", "Spin")
    agent.learner.add_evidence("physik2", spin, "check", "misconception", confidence=0.9)
    agent.learner.add_evidence("physik2", "physik2:s8-2", "check", "partial", confidence=0.5)
    assert pick_topic(agent, "physik2").id == "physik2:s8-2"

    # Löschen
    assert "Gelöscht" in agent.handle("/termine löschen 1", course_id="physik2").render()
    assert tm.next_exam(agent, "physik2") is None and pick_topic(agent, "physik2").id == spin


def test_lernplan_flow(course_dir):  # noqa: F811
    _exercises(course_dir)
    cfg = make_cfg(course_dir)
    day1 = (tm.today() + timedelta(days=1)).isoformat()
    in10 = (tm.today() + timedelta(days=10)).isoformat()

    def reply(p):
        if "Extract it so the agent can" in p:
            return js({"exams": [{"course": "physik2", "kind": "midterm", "title": "Midterm", "date": in10,
                                  "topics": ["Kapitel 8"], "node_ids": ["physik2:s8"]}]})
        if "You build a practice plan" in p:
            lines = [ln for ln in p.split("## Candidate problems")[1].splitlines() if " | " in ln]
            assert any("Ex_8 – Aufgabe 2" in ln for ln in lines)
            n2 = next(int(ln.split(" | ")[0]) for ln in lines if "Ex_8 – Aufgabe 2" in ln)
            return js({"summary": "Orthogonalität wackelt.", "learn_first": ["Orthogonality"],
                       "items": [{"cand": n2, "node_id": "physik2:s8-2", "priority": 1, "why": "wackelt",
                                  "minutes": 30, "day": day1},
                                 {"cand": 999, "priority": 1}],
                       "advice": "Erst ohne Lösung versuchen."})
        return "Tipp: Schau auf die Phasenfaktoren."

    codex = FakeProvider("codex", reply=reply)
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))
    agent.handle("/termin Midterm in 10 Tagen, Kapitel 8", course_id="physik2")
    st: list[str] = []
    out = agent.handle("/lernplan", course_id="physik2", on_status=st.append).render()
    assert any("Ziel: Midterm" in s for s in st) and any("Kandidaten" in s for s in st)
    p = codex.prompts[-1]
    assert "Topics in scope" in p and "physik2:s8-2 | Orthogonality" in p and "{" + "candidates}" not in p
    assert "## Lernplan – Physik II" in out and "**P1** 🔴" in out and "Ex_8 – Aufgabe 2" in out
    assert "`/learn Orthogonality`" in out and "0 von 1 erledigt" in out and "P2" not in out

    # zweiter Aufruf zeigt den gespeicherten Plan ohne Modellaufruf
    n = len(codex.prompts)
    assert "**P1**" in agent.handle("/lernplan", course_id="physik2").render() and len(codex.prompts) == n

    # P1 als Verweis: Tipps und /check finden die Aufgabe
    ref = resolve(agent.store, "physik2", "P1 c", lambda k: agent.learner.plan_item("physik2", k))
    assert ref and ref.problem_no == "2" and ref.part == "c" and "Superposition" in ref.chunks[0].text
    agent.handle("P1 Tipp", course_id="physik2")
    assert "Compute |psi(x,t)|^2" in codex.prompts[-1]

    # Abhaken (wie nach /check)
    c0 = ref.chunks[0]
    assert agent.learner.mark_plan_done("physik2", c0.pair_key, c0.doc_type, c0.problem_no, "correct") == [1]
    assert "**P1** ✅" in agent.handle("/lernplan", course_id="physik2").render()
