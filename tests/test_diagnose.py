"""/diagnose: Start, Antwort-Schleife, Lernkarte, Überspringen, Abbrechen, Budget, Fehlerfälle."""

import json

from eth_agent.agent.core import Turn
from eth_agent.llm.providers import FakeProvider
from eth_agent.skills.diagnose import dont_know, parse_args, pending_diagnosis

from test_indexer import course_dir, make_cfg  # noqa: F401
from test_learner import _agent


def reply(evaluation=None, nxt=None, done=False, summary=""):
    return "```json\n" + json.dumps({"evaluation": evaluation, "next": nxt, "done": done,
                                     "summary_markdown": summary}) + "\n```"


Q1 = {"node_ids": ["physik2:s8-2"], "question": "Sind $\\psi_1$ und $\\psi_2$ im Topf orthogonal?",
      "format": "mc", "options": ["Ja, immer", "Nur bei gleicher Energie", "Nein"],
      "answer_key": "A – verschiedene Eigenwerte eines hermiteschen Operators", "discriminates": "Orthogonalität"}
Q2 = {"node_ids": ["physik2:s8-1"], "question": "Wie hängt $E_n$ von $L$ ab?", "format": "short",
      "options": [], "answer_key": "E_n ~ 1/L^2", "discriminates": "Skalierung"}


def test_helpers():
    assert parse_args("") == ("", 8)
    assert parse_args("tunneling 5") == ("tunneling", 5)
    assert parse_args("Wasserstoffatom 40 Fragen") == ("Wasserstoffatom", 15)
    assert parse_args("2") == ("", 3)
    assert dont_know("weiss nicht") and dont_know("Keine Ahnung, ehrlich") and dont_know("?")
    assert not dont_know("B, weil die Energien verschieden sind")
    hist = [Turn("user", "/diagnose"), Turn("assistant", "… *Diagnose #4 · Frage 2*")]
    assert pending_diagnosis(hist) == (4, 2)
    assert pending_diagnosis(hist + [Turn("user", "x"), Turn("assistant", "normale Antwort")]) is None


def test_diagnose_loop(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    replies = iter([
        reply(nxt=Q1),
        reply(evaluation={"feedback": "Stimmt – verschiedene Eigenwerte, also orthogonal.",
                          "concepts": [{"node_id": "physik2:s8-2", "outcome": "correct", "evidence": "sauber begründet"}]},
              nxt=Q2),
        reply(evaluation={"feedback": "Nein, $E_n \\propto 1/L^2$.",
                          "concepts": [{"node_id": "physik2:s8-1", "outcome": "misconception",
                                        "evidence": "glaubt E_n wächst mit L"}]},
              done=True, summary="**Infinite Quantum Well:** Orthogonalität sitzt, Skalierung von $E_n$ falsch."),
    ])
    codex = FakeProvider("codex", reply=lambda p: next(replies))
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))

    status: list[str] = []
    out = agent.handle("/diagnose quantum well 4", course_id="physik2", on_status=status.append).render()
    p = codex.prompts[-1]
    assert "Topic requested by the student: \"quantum well\"" in p
    assert "physik2:s8-2 | Orthogonality" in p                 # Fokus aus dem Skript-Gerüst
    assert "infinite square well" in p                         # Kursmaterial gesucht
    assert "evaluation must be null" in p and "4 more question(s)" in p
    assert codex.efforts[-1] == cfg.models.codex_effort_hard
    assert "### Diagnose · Frage 1/4 — Orthogonality" in out
    assert "- A) Ja, immer" in out and "*Diagnose #1 · Frage 1*" in out
    assert "verschiedene Eigenwerte" not in out                # Lösung bleibt verborgen
    assert any("Themen im Fokus" in s for s in status)

    hist = [Turn("user", "/diagnose quantum well 4"), Turn("assistant", out)]
    out2 = agent.handle("A, weil verschiedene Eigenwerte. Sicherheit: 90%", course_id="physik2", history=hist).render()
    p = codex.prompts[-1]
    assert "Student answer: A, weil verschiedene Eigenwerte. Sicherheit: 90% [confidence: 90 %]" in p
    assert "Answer key (hidden from the student): A –" in p and "→ Evaluate this answer now" in p
    assert "3 more question(s)" in p
    assert "**Rückmeldung:** Stimmt" in out2 and "**Lernkarte:** 🟢 Orthogonality" in out2
    assert "### Diagnose · Frage 2/4" in out2 and "Kurz in eigenen Worten" in out2
    assert "*Diagnose #1 · Frage 2*" in out2

    hist += [Turn("user", "A"), Turn("assistant", out2)]
    out3 = agent.handle("Je grösser L, desto grösser E", course_id="physik2", history=hist).render()
    assert "## Diagnose abgeschlossen" in out3 and "2 Fragen beantwortet: 1× Fehlvorstellung, 1× richtig" in out3
    assert "Skalierung von $E_n$ falsch" in out3 and "⚠️ Setup (Fehlvorstellung)" in out3
    assert agent.learner.get_diagnosis(1)["status"] == "done"
    ev = agent.learner.nodes("physik2")
    kinds = {e.kind for n in ev for e in n.evidence}
    assert kinds == {"diagnose"}

    # Fazit trägt keinen Marker mehr -> nächste Nachricht ist wieder eine normale Frage
    assert pending_diagnosis(hist + [Turn("user", "x"), Turn("assistant", out3)]) is None


def test_skip_stop_and_dont_know(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    nxt = reply(evaluation={"feedback": "Ok.", "concepts": [
        {"name": "Tunneleffekt", "outcome": "wrong", "evidence": "weiss es nicht"}]}, nxt=Q2)
    codex = FakeProvider("codex", reply=lambda p: reply(nxt=Q1) if "evaluation must be null" in p else nxt)
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))
    out = agent.handle("/diagnose", course_id="physik2").render()
    assert "Frage 1/8" in out                                   # ganzes Fach, Standardlänge
    hist = [Turn("user", "/diagnose"), Turn("assistant", out)]

    # /weiter: übersprungen, keine Belege
    out2 = agent.handle("/weiter", course_id="physik2", history=hist).render()
    assert "Frage übersprungen" in codex.prompts[-1]
    assert "Rückmeldung" not in out2 and "Frage 2/8" in out2
    assert not any(n.evidence for n in agent.learner.nodes("physik2"))

    # "weiss nicht" -> Sicherheit 0 im Beleg
    hist += [Turn("user", "/weiter"), Turn("assistant", out2)]
    out3 = agent.handle("weiss nicht", course_id="physik2", history=hist).render()
    assert "[confidence: 0 %]" in codex.prompts[-1] and "Tunneleffekt" in out3
    node = agent.learner.find_node("physik2", "Tunneleffekt")
    ev = next(n for n in agent.learner.nodes("physik2") if n.id == node).evidence
    assert ev[0].confidence == 0.0 and ev[0].kind == "diagnose"

    # /stop: Fazit ohne Modellaufruf
    hist += [Turn("user", "weiss nicht"), Turn("assistant", out3)]
    n = len(codex.prompts)
    out4 = agent.handle("/stop", course_id="physik2", history=hist).render()
    assert len(codex.prompts) == n
    assert "## Diagnose abgeschlossen" in out4 and "Frage 2: Ok." in out4 and "🔴 Tunneleffekt" in out4
    assert agent.learner.get_diagnosis(1)["status"] == "stopped"
    assert "keine Diagnose" in agent.handle("/stop", course_id="physik2").render()


def test_budget_and_failures(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    ok = {"feedback": "Gut.", "concepts": [{"node_id": "physik2:s8-2", "outcome": "correct"}]}
    # Modell will immer weiterfragen – nach 3 Fragen ist trotzdem Schluss
    codex = FakeProvider("codex", reply=lambda p: reply(evaluation=None if "evaluation must be null" in p else ok,
                                                         nxt=Q1))
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))
    out = agent.handle("/diagnose 3", course_id="physik2").render()
    hist = [Turn("user", "/diagnose 3"), Turn("assistant", out)]
    for i in range(3):
        out = agent.handle("A", course_id="physik2", history=hist).render()
        hist += [Turn("user", "A"), Turn("assistant", out)]
    assert "## Diagnose abgeschlossen" in out and "3 Fragen beantwortet: 3× richtig" in out
    assert "0 more question(s)" in codex.prompts[-1]

    # Neue Diagnose ersetzt eine offene; kaputte Auswertung -> Antwort kann nochmal geschickt werden
    bad = FakeProvider("codex", reply=lambda p: reply(nxt=Q1) if "evaluation must be null" in p else "kein json")
    agent2 = _agent(cfg, codex=bad, claude=FakeProvider("claude", fail=True))
    first = agent2.handle("/diagnose", course_id="physik2").render()
    agent2.handle("/diagnose", course_id="physik2")
    did_old = int(first.split("Diagnose #")[1].split(" ")[0])
    assert agent2.learner.get_diagnosis(did_old)["status"] == "abandoned"
    out = agent2.handle("/diagnose", course_id="physik2").render()
    hist = [Turn("user", "/diagnose"), Turn("assistant", out)]
    out2 = agent2.handle("B", course_id="physik2", history=hist).render()
    assert "nochmal" in out2 and "· Frage 1*" in out2
    did = int(out.split("Diagnose #")[1].split(" ")[0])
    assert agent2.learner.diag_items(did)[0]["answer"] is None

    # ohne Fach
    agent3 = _agent(cfg, codex=codex, claude=FakeProvider("claude"))
    agent3.cfg.courses["andere"] = agent3.cfg.courses["physik2"].__class__(id="andere", folder="X", name="X")
    assert "zuerst ein Fach" in agent3.handle("/diagnose").render()
