"""Lernkarte: Status aus Belegen, Skript-Gerüst, /check-Updates, Rückfrage-Schleife, Darstellung."""

import json

from eth_agent.agent.core import Agent, Turn
from eth_agent.indexer.build import index_course
from eth_agent.learner.model import Evidence, LearnerStore, parse_confidence, status_from_evidence
from eth_agent.learner.render import render_map
from eth_agent.llm.providers import FakeProvider
from eth_agent.llm.usage import UsageLog

from test_check import grade_json
from test_indexer import course_dir, make_cfg  # noqa: F401


def ev(outcome, ts, conf=None):
    return Evidence(ts, "check", outcome, conf, None, None)


def test_status_rules():
    assert status_from_evidence([]) == "untested"
    assert status_from_evidence([ev("correct", "1")]) == "probably"
    assert status_from_evidence([ev("correct", "1"), ev("correct", "2")]) == "solid"
    assert status_from_evidence([ev("correct", "1"), ev("correct", "2", conf=0.4)]) == "shaky"   # unsicher
    assert status_from_evidence([ev("wrong", "1")]) == "missing"
    assert status_from_evidence([ev("wrong", "1"), ev("correct", "2")]) == "shaky"
    mis = [ev("misconception", "1"), ev("correct", "2")]
    assert status_from_evidence(mis) == "misconception"            # erst zweimal richtig danach
    assert status_from_evidence(mis + [ev("correct", "3")]) != "misconception"
    assert status_from_evidence([ev("slip", "1"), ev("correct", "2")]) in {"probably", "shaky"}
    assert parse_confidence("x = 3\nSicherheit: 80%") == 0.8
    assert parse_confidence("ohne Angabe") is None


def test_skeleton_and_concepts(tmp_path):
    s = LearnerStore(tmp_path / "l.sqlite")
    n = s.build_skeleton("phys", ["8 The Infinite Quantum Well > 8.1 Setup",
                                  "8 The Infinite Quantum Well > 8.2 Orthogonality",
                                  "9 Tunneling"])
    assert n == 4
    assert s.find_node("phys", "Orthogonality") == "phys:s8-2"
    touched = s.apply_concepts("phys", [
        {"node_id": "phys:s8-2", "outcome": "correct", "evidence": "Orthogonalität korrekt benutzt"},
        {"name": "Normierung der Wellenfunktion", "parent_id": "phys:s8", "outcome": "misconception",
         "evidence": "glaubt, |psi|^2 muss 1 sein an jedem Ort"},
        {"name": "egal", "outcome": "kaputt"},
    ], "check", confidence=0.9, problem_ref="Serie 3 Aufgabe 1")
    assert len(touched) == 2
    nodes = {x.id: x for x in s.nodes("phys")}
    new = nodes[touched[1]]
    assert new.parent_id == "phys:s8" and new.origin == "auto" and new.own_status == "misconception"
    assert nodes["phys:s8"].status == "misconception"           # schlechtester Unterknoten zählt
    assert "phys:s8-2 | Orthogonality | vermutlich ok" in s.outline("phys")
    # gleicher Name -> gleicher Knoten, keine Dublette
    assert s.apply_concepts("phys", [{"name": "Normierung der Wellenfunktion", "outcome": "correct"}],
                            "check") == [touched[1]]


def _agent(cfg, **prov):
    a = Agent(cfg, providers=prov, usage=UsageLog(cfg.data_dir / "usage.sqlite"))
    index_course(cfg, a.store, cfg.courses["physik2"], log=lambda *_: None)
    return a


def test_check_updates_map_and_probe_loop(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    concepts = [{"node_id": "physik2:s8-2", "outcome": "correct", "evidence": "Ansatz richtig"},
                {"name": "Erwartungswert Radius", "parent_id": "physik2:s8", "outcome": "wrong",
                 "evidence": "Integrationsgrenzen falsch"}]
    grade = grade_json(reference_agreement="official", concepts=concepts,
                       probe={"question": "Wie bist du auf die Grenzen 0 bis a gekommen?", "why": "Konzept oder Slip"})
    probe_reply = "```json\n" + json.dumps({
        "diagnosis": "misconception", "reply_markdown": "Der Radius läuft von 0 bis unendlich.",
        "concepts": [{"name": "Erwartungswert Radius", "outcome": "misconception",
                      "evidence": "hält a für den maximalen Radius"}],
        "follow_up": None}) + "\n```"
    claude = FakeProvider("claude", reply=lambda p: probe_reply if "Student's answer" in p else grade)
    agent = _agent(cfg, codex=FakeProvider("codex"), claude=claude)

    out = agent.handle("/check Prüfung 2024 Aufgabe 2\nr von 0 bis a integriert, <r> = a/2\nSicherheit: 90%").render()
    assert "physik2:s8-2 | Orthogonality" in claude.prompts[-1]          # Gerüst aus dem Skript im Prompt
    assert "**Lernkarte aktualisiert:**" in out and "Erwartungswert Radius" in out
    assert "### Rückfrage #1" in out and "Grenzen 0 bis a" in out

    hist = [Turn("user", "/check …"), Turn("assistant", out)]
    status = []
    ans = agent.handle("Ich dachte, a ist der Rand des Atoms.", history=hist, on_status=status.append)
    txt = ans.render()
    assert "**Einschätzung:** Fehlvorstellung" in txt and "0 bis unendlich" in txt
    assert any("Gedankengang" in s for s in status)
    p = claude.prompts[-1]
    assert "Ich dachte, a ist der Rand des Atoms." in p and "Wie bist du auf die Grenzen" in p
    assert "Set follow_up to null" not in p                                # erste Ebene darf nachfragen

    # Rückfrage ist beantwortet -> nächste Nachricht ist wieder normal
    assert agent.learner.get_probe(1)["status"] == "answered"
    # /weiter überspringt ohne Modellaufruf
    n = len(claude.prompts)
    assert "übersprungen" in agent.handle("/weiter", history=hist).render() and len(claude.prompts) == n

    karte = agent.handle("/lernkarte").render()
    assert "```mermaid" in karte and "mindmap" in karte
    assert "⚠️ Erwartungswert Radius" in karte                           # Fehlvorstellung
    assert "### Das solltest du noch lernen" in karte and "hält a für den maximalen Radius" in karte
    assert "Orthogonality" in agent.handle("/lernkarte quantum well").render()
    assert "Kein Thema" in agent.handle("/lernkarte quatsch").render()


def test_uncertain_reference_does_not_touch_map(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    solve = "## Final results\n- x: 1\n"
    claude = FakeProvider("claude", reply=lambda p: grade_json(
        reference_agreement="disagree", concepts=[{"name": "Etwas", "outcome": "wrong"}],
        probe={"question": "Warum?"}) if "teaching assistant grading" in p else solve)
    agent = _agent(cfg, codex=FakeProvider("codex", reply=solve), claude=claude)
    out = agent.handle("/check Eine eigene Aufgabe mit genug Text, damit sie nicht als Verweis gilt.\n---\nx = 2"
                       ).render()
    assert "Lernkarte aktualisiert" not in out and "Rückfrage" not in out
    assert agent.learner.find_node("physik2", "Etwas") is None


def test_render_empty(tmp_path):
    s = LearnerStore(tmp_path / "l.sqlite")
    assert "noch keine Lernkarte" in render_map(s, "x", "Fach X")
