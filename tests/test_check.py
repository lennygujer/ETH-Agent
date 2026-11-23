"""/check mit Fake-Modellen: Musterlösung, zwei unabhängige Referenzen, Uneinigkeit, Fehlerkatalog."""

import json

from eth_agent.agent.core import Agent
from eth_agent.checks.sandbox import CheckResult
from eth_agent.indexer.build import index_course
from eth_agent.llm.providers import FakeProvider, LLMResult
from eth_agent.llm.usage import UsageLog
from eth_agent.skills.check import Solution, parse_grade, parse_input, trust_level

from test_indexer import course_dir, make_cfg  # noqa: F401

SOLVE_OK = ("Derivation ...\n## Final results\n- a) N: 2\n\n```python check\nassert 1 + 1 == 2\n"
            "print('CHECK N: ok')\n```")


def grade_json(**over) -> str:
    g = {"reference_agreement": "agree", "reference_disagreement_note": "", "points_awarded": 4,
         "points_max": 12, "verdict": "partially_correct", "correct_parts": ["Ansatz richtig"],
         "first_error": {"location": "b)", "description": "Vorzeichen", "error_type": "sign"},
         "further_errors": [], "hint": "Schau dir das Vorzeichen an.", "feedback_markdown": "Gut begonnen.",
         "student_check_code": "student = -3.0\ncorrect = 3.0\nassert abs(student - correct) < 1e-6"}
    g.update(over)
    return "Some text\n```json\n" + json.dumps(g, ensure_ascii=False) + "\n```"


def _agent(cfg, **providers):
    a = Agent(cfg, providers=providers, usage=UsageLog(cfg.data_dir / "usage.sqlite"))
    index_course(cfg, a.store, cfg.courses["physik2"], log=lambda *_: None)
    return a


def test_parse_input():
    i = parse_input("Serie 3 Aufgabe 1\nmein Weg\nZeile 2")
    assert (i.task, i.attempt, i.reveal) == ("Serie 3 Aufgabe 1", "mein Weg\nZeile 2", False)
    i = parse_input("Ein Elektron wird mit 50 kV beschleunigt.\nBerechne lambda.\n---\nlambda = 5 pm --loesung")
    assert i.task.startswith("Ein Elektron") and i.attempt == "lambda = 5 pm" and i.reveal


def test_parse_grade_robust():
    assert parse_grade(grade_json())["points_awarded"] == 4
    assert parse_grade('blabla {"verdict": "correct"} blabla')["verdict"] == "correct"
    assert parse_grade("kein json") is None


def test_trust_levels():
    ok = CheckResult("pass", "")
    bad = CheckResult("fail", "")
    none = CheckResult("none", "")
    s = lambda c: Solution("x", "", c, LLMResult("x", "", True, 0))  # noqa: E731
    assert trust_level(True, [], None) == "official"
    assert trust_level(False, [s(ok), s(none)], "agree") == "two_tool"
    assert trust_level(False, [s(none), s(none)], "agree") == "two"
    assert trust_level(False, [s(ok), s(bad)], "agree") == "uncertain"
    assert trust_level(False, [s(ok), s(ok)], "disagree") == "uncertain"
    assert trust_level(False, [s(ok)], "single") == "one_tool"


def test_check_with_official_solution(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    codex, claude = FakeProvider("codex"), FakeProvider("claude", reply=grade_json(reference_agreement="official"))
    agent = _agent(cfg, codex=codex, claude=claude)
    status = []
    ans = agent.handle("/check Prüfung 2024 Aufgabe 2\nr = a\nN = 1/pi", on_status=status.append)
    out = ans.render("Physik II")
    assert codex.prompts == []                         # mit Musterlösung: kein Lösen nötig
    p = claude.prompts[-1]
    assert "official solution (Musterlösung)" in p and "3a/2" in p and "N = 1/pi" in p
    assert "Point value of this problem in the exam: 12 points." in p
    assert "**Punkte, geschätzt: 4 / 12**" in out and "Vorzeichen" in out
    assert "Referenz: Musterlösung" in out and "Dein Endergebnis: Tool ✗" in out
    assert "Lösung anzeigen" in out and "### Lösung" not in out
    log = (cfg.data_dir / "profile" / "errors.jsonl").read_text().strip().splitlines()
    entry = json.loads(log[-1])
    assert entry["error_type"] == "sign" and entry["points"] == 4

    ans = agent.handle("/check Prüfung 2024 Aufgabe 2 --loesung\nr = a")
    assert "### Lösung (Referenz)" in ans.render("Physik II")


def test_check_without_official_two_solvers(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    codex = FakeProvider("codex", reply=SOLVE_OK)
    claude = FakeProvider("claude", reply=lambda p: grade_json(student_check_code="assert 2 == 2")
                          if "teaching assistant grading" in p else SOLVE_OK)
    agent = _agent(cfg, codex=codex, claude=claude)
    text = ("/check Ein Elektron wird mit einer Spannung von 50 kV beschleunigt. Berechne seine de-Broglie-Wellenlänge."
            "\n---\nlambda = h / sqrt(2 m e U) = 5.5 pm")
    out = agent.handle(text).render("Physik II")
    assert len(codex.prompts) == 1 and len(claude.prompts) == 2   # 2x lösen, 1x bewerten
    grade_prompt = claude.prompts[-1]
    assert "### Reference A" in grade_prompt and "### Reference B" in grade_prompt
    assert "```python check" not in grade_prompt                   # Prüfcode der Löser nicht an Bewerter
    assert "Referenz: 2 Modelle einig + Tool" in out and "Dein Endergebnis: Tool ✓" in out


def test_check_disagreement_is_uncertain(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    claude = FakeProvider("claude", reply=lambda p: grade_json(
        reference_agreement="disagree", reference_disagreement_note="A: 5.5 pm, B: 7.1 pm")
        if "teaching assistant grading" in p else SOLVE_OK)
    agent = _agent(cfg, codex=FakeProvider("codex", reply=SOLVE_OK), claude=claude)
    out = agent.handle("/check Berechne die Wellenlänge eines Elektrons nach 50 kV Beschleunigungsspannung.\n---\n"
                       "5.5 pm").render("Physik II")
    assert "Referenz unsicher" in out and "A: 5.5 pm, B: 7.1 pm" in out and "Referenz: unsicher" in out


def test_check_errors(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    agent = _agent(cfg, codex=FakeProvider("codex"), claude=FakeProvider("claude", reply="kein json"))
    assert "Kein Lösungsversuch" in agent.handle("/check Prüfung 2024 Aufgabe 2").text
    assert "nicht im Material gefunden" in agent.handle("/check Serie 9 Aufgabe 1\nx = 1").text
    assert "Bewertung fehlgeschlagen" in agent.handle("/check Prüfung 2024 Aufgabe 2\nx = 1").text


def test_check_with_image_uses_codex(course_dir, tmp_path):  # noqa: F811
    cfg = make_cfg(course_dir)
    codex = FakeProvider("codex", reply=grade_json(reference_agreement="official"))
    claude = FakeProvider("claude")
    agent = _agent(cfg, codex=codex, claude=claude)
    img = tmp_path / "foto.jpg"
    img.write_bytes(b"\xff\xd8fake")
    out = agent.handle("/check Prüfung 2024 Aufgabe 2", images=[str(img)]).render("Physik II")
    assert "Bewertet von: codex" in out and claude.prompts == []


def test_partial_attempt_rendering(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    claude = FakeProvider("claude", reply=grade_json(reference_agreement="official", points_max=3,
                                                     not_attempted=["b)", "c)"]))
    agent = _agent(cfg, codex=FakeProvider("codex"), claude=claude)
    out = agent.handle("/check Prüfung 2024 Aufgabe 2\na) r = a").render("Physik II")
    assert "**Punkte (bearbeitete Teile), geschätzt: 4 / 3**" in out
    assert "*Nicht bearbeitet:* b), c)" in out
    assert "Grade ONLY the sub-tasks the student actually attempted" in claude.prompts[-1]
