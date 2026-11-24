"""/learn: Planung, Bestätigung, Schritte mit Vorhersage/Kontrollfrage, Belege, Plan, Stopp, Abschluss."""

import json

from eth_agent.agent.core import Turn
from eth_agent.llm.providers import FakeProvider
from eth_agent.skills.learn import _clean_plan, pending_lesson, plan_mermaid

from test_indexer import course_dir, make_cfg  # noqa: F401
from test_learner import _agent

PLAN = [{"id": "eigen", "label": "Eigenzustand: |psi|^2 zeitlich konstant", "requires": []},
        {"id": "superpos", "label": "Überlagerung: relative Phase", "requires": ["eigen"]},
        {"id": "ziel", "label": "Ziel: Dichte berechnen", "requires": ["superpos", "nicht-da"]}]


def js(**kw):
    base = {"evaluation": None, "reply_markdown": "", "question": None, "goal": "", "plan": None,
            "done": False, "summary_markdown": ""}
    base.update(kw)
    return "```json\n" + json.dumps(base) + "\n```"


def test_plan_helpers():
    plan = _clean_plan(PLAN + [{"id": "Eigen", "label": "doppelt"}, {"id": "leer", "label": ""}, "kaputt"])
    assert [x["id"] for x in plan] == ["eigen", "superpos", "ziel"]
    assert plan[2]["requires"] == ["superpos"]                      # unbekannte Abhängigkeit entfernt
    m = plan_mermaid(plan, [{"q_type": "check", "q_node": "eigen", "outcome": "correct"},
                            {"q_type": "predict", "q_node": "superpos", "outcome": None}])
    assert "graph BT" in m and "n_eigen --> n_superpos" in m
    assert "class n_eigen ok" in m and "class n_superpos cur" in m
    assert "|psi|" not in m                                          # Mermaid-unsichere Zeichen ersetzt
    assert pending_lesson([Turn("assistant", "… *Lernen #3 · Schritt 2* · `/plan`")]) == (3, 2)


def test_learn_flow(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    steps = iter([
        js(goal="Dichte einer Überlagerung berechnen", plan=PLAN,
           reply_markdown="Wir starten bei dem, was du über Eigenzustände weisst."),
        js(reply_markdown="Gut, dann los.",
           question={"type": "predict", "node": "eigen", "text": "Was passiert mit $|\\psi_1|^2$ über die Zeit?",
                     "options": [], "answer_key": "konstant"}),
        js(reply_markdown="Genau: der Phasenfaktor hat Betrag 1.",
           question={"type": "check", "node": "eigen", "text": "Ist $|\\psi_2(x,t)|^2$ zeitabhängig?",
                     "options": ["Ja", "Nein"], "answer_key": "B – Betrag des Phasenfaktors ist 1"}),
        js(evaluation={"outcome": "correct", "concepts": [
               {"node_id": "physik2:s8-2", "outcome": "correct", "evidence": "Du hast den Phasenfaktor richtig benutzt."}]},
           reply_markdown="Richtig.", plan=PLAN[:2],
           question={"type": "check", "node": "superpos", "text": "Und bei der Summe?", "options": [],
                     "answer_key": "oszilliert"}),
        js(evaluation={"outcome": "correct", "concepts": []}, reply_markdown="Sitzt.", done=True,
           summary_markdown="- Eigenzustände: Dichte konstant\n- Transfer: Prüfung 2024 Aufgabe 2"),
    ])
    codex = FakeProvider("codex", reply=lambda p: next(steps))
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))
    agent.learner.build_skeleton("physik2", ["8 The Infinite Quantum Well > 8.2 Orthogonality"])
    agent.learner.add_evidence("physik2", "physik2:s8-2", "diagnose", "misconception", confidence=0.7,
                               detail="Du meinst, die Energiewerte schwingen.")

    status: list[str] = []
    out = agent.handle("/learn", course_id="physik2", on_status=status.append).render()
    p = codex.prompts[-1]
    assert "This is the PLANNING turn" in p and 'topic "Orthogonality"' in p     # dringendste Schwäche
    assert "Du meinst, die Energiewerte schwingen." in p and "## Formula sheet allowed" in p
    assert "{" + "transcript}" not in p and codex.efforts[-1] == cfg.models.codex_effort_hard
    assert any("Dringendstes Thema" in s for s in status)
    assert "## Lernen: Orthogonality" in out and "**Ziel:** Dichte einer Überlagerung" in out
    assert "graph BT" in out and "Passt der Plan?" in out and "*Lernen #1 · Schritt 0*" in out

    hist = [Turn("user", "/learn"), Turn("assistant", out)]
    out1 = agent.handle("ja", course_id="physik2", history=hist).render()
    assert "The student just replied to your plan" in codex.prompts[-1]
    assert "**Vorhersage:** Was passiert" in out1 and "Raten ist hier erwünscht" in out1
    assert "*Lernen #1 · Schritt 1*" in out1

    hist += [Turn("user", "ja"), Turn("assistant", out1)]
    out2 = agent.handle("bleibt gleich", course_id="physik2", history=hist).render()
    assert "**Kontrollfrage:** Ist" in out2 and "- A) Ja" in out2 and "Schritt 2*" in out2
    assert "Lernkarte" not in out2                                        # Vorhersage wird nicht bewertet

    # /plan zwischendurch: kein Modellaufruf, Frage wird wiederholt
    hist += [Turn("user", "bleibt gleich"), Turn("assistant", out2)]
    n = len(codex.prompts)
    pv = agent.handle("/plan", course_id="physik2", history=hist).render()
    assert len(codex.prompts) == n and "class n_eigen cur" in pv and "**Kontrollfrage:** Ist" in pv
    assert "*Lernen #1 · Schritt 2*" in pv

    out3 = agent.handle("B, Betrag 1. Sicherheit: 80%", course_id="physik2", history=hist).render()
    p = codex.prompts[-1]
    assert "Student: B, Betrag 1. Sicherheit: 80% [confidence 80 %]" in p and "Answer key (hidden): B" in p
    assert "**Lernkarte:** 🟢 Orthogonality" in out3 or "**Lernkarte:** ⚠️ Orthogonality" in out3
    assert "**Plan angepasst:**" in out3
    ev = [e for x in agent.learner.nodes("physik2") for e in x.evidence if e.kind == "learn"]
    assert len(ev) == 1 and ev[0].confidence == 0.8

    hist += [Turn("user", "B"), Turn("assistant", out3)]
    out4 = agent.handle("Sie oszilliert wegen der relativen Phase.", course_id="physik2", history=hist).render()
    assert "## Lektion abgeschlossen" in out4 and "2 Kontrollfragen, davon 2 richtig" in out4
    assert "Transfer: Prüfung 2024 Aufgabe 2" in out4 and "Lernen #" not in out4.split("abgeschlossen")[1]
    assert agent.learner.get_lesson(1)["status"] == "done"


def test_learn_stop_skip_and_errors(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    q = {"type": "check", "node": "eigen", "text": "Frage?", "options": [], "answer_key": "x"}
    codex = FakeProvider("codex", reply=lambda p: js(goal="G", plan=PLAN, reply_markdown="Ansatz.")
                         if "PLANNING" in p else js(reply_markdown="Weiter.", question=q))
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))
    assert "noch keine Schwäche" in agent.handle("/learn", course_id="physik2").render()
    out = agent.handle("/learn tunneling", course_id="physik2").render()
    assert "## Lernen: tunneling" in out
    hist = [Turn("user", "/learn tunneling"), Turn("assistant", out)]
    out1 = agent.handle("ja", course_id="physik2", history=hist).render()
    hist += [Turn("user", "ja"), Turn("assistant", out1)]
    out2 = agent.handle("/weiter", course_id="physik2", history=hist).render()
    assert "übersprungen – nicht bewerten" in codex.prompts[-1] and "Schritt 2*" in out2
    # anderer Befehl dazwischen -> /plan holt die Lektion zurück, /stop beendet
    assert "Schritt 2*" in agent.handle("/plan", course_id="physik2").render()
    n = len(codex.prompts)
    out3 = agent.handle("/stop", course_id="physik2").render()
    assert len(codex.prompts) == n and "## Lektion beendet" in out3 and "/learn tunneling" in out3
    assert "keine Diagnose oder Lektion" in agent.handle("/stop", course_id="physik2").render()

    # kaputte Planung
    bad = _agent(cfg, codex=FakeProvider("codex", reply="kein json"), claude=FakeProvider("claude", fail=True))
    assert "nicht geplant werden" in bad.handle("/learn tunneling", course_id="physik2").render()


def test_learn_tolerates_model_variants(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    # Plan als {"nodes": [...]} mit title/depends_on und LaTeX ohne doppelte Backslashes
    raw = ('```json\n{"evaluation": null, "reply_markdown": "Wir beginnen bei $\\hbar\\omega$.", "question": null, '
           '"goal": "$|\\psi|^2$ verstehen", "plan": {"nodes": [{"title": "Photonenenergie", "depends_on": []}, '
           '{"id": "kraft", "title": "Kraft = Impulsfluss", "depends_on": "photonenenergie"}]}, '
           '"done": false, "summary_markdown": ""}\n```')
    agent = _agent(cfg, codex=FakeProvider("codex", reply=raw), claude=FakeProvider("claude"))
    out = agent.handle("/learn photon momentum", course_id="physik2").render()
    assert "## Lernen: photon momentum" in out and "\\hbar\\omega" in out and "$|\\psi|^2$ verstehen" in out
    assert "n_photonenenergie --> n_kraft" in out

    # gültiges JSON ganz ohne Plan -> Ein-Knoten-Plan statt Abbruch, Antwort wird abgelegt
    st: list[str] = []
    agent2 = _agent(cfg, codex=FakeProvider("codex", reply=js(goal="Kraft auf Spiegel", reply_markdown="Los.")),
                    claude=FakeProvider("claude"))
    out2 = agent2.handle("/learn photon momentum", course_id="physik2", on_status=st.append).render()
    assert "Passt der Plan?" in out2 and 'n_ziel["Kraft auf Spiegel"]' in out2
    assert any("Plan fehlte" in s for s in st)
    assert list((cfg.data_dir / "logs").glob("learn-plan-*.txt"))
