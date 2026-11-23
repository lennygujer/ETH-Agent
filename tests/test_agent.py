"""Routing, Sandbox, Verweise und handle() mit Fake-Modellen (keine echten Cloud-Aufrufe)."""

from dataclasses import replace

from eth_agent.agent.core import Agent, Turn
from eth_agent.agent.references import _exam_tokens, _pair_matches, resolve
from eth_agent.agent.routing import route
from eth_agent.checks.sandbox import check_answer, extract_check_block, run_python
from eth_agent.indexer.build import index_course
from eth_agent.llm.providers import FakeProvider
from eth_agent.llm.usage import UsageLog

from test_indexer import course_dir, make_cfg  # noqa: F401  (Fixture)

CHECK_OK = "Ergebnis: 4\n\n```python check\nassert 2 + 2 == 4\nprint('CHECK sum: ok')\n```"
CHECK_FAIL = "Ergebnis: 5\n\n```python check\nassert 2 + 2 == 5, 'falsch'\n```"


def test_routing_rules():
    assert route("@claude erkläre Spin").provider == "claude"
    r = route("@codex: was ist ein Operator?")
    assert (r.rule, r.provider, r.question) == ("R1", "codex", "was ist ein Operator?")
    assert route("/check Serie 3 Aufgabe 1").skill == "check"
    assert route("!practice physik2").skill == "practice"
    assert route("Berechne die Wellenlänge").rule == "R4"
    assert route("Traceback (most recent call last):\n  x").rule == "R6"
    assert route("Was ist Spin?").rule == "R8"
    assert route("x " * 40_000).provider == "claude"


def test_sandbox():
    assert run_python("print(1+1)").status == "pass"
    assert run_python("assert False, 'nein'").status == "fail"
    assert run_python("raise ValueError('kaputt')").status == "error"
    assert run_python("import sympy as sp\nx=sp.symbols('x')\nassert sp.diff(x**2,x)==2*x").status == "pass"
    assert run_python("while True: pass", timeout=2).status == "error"
    text, code = extract_check_block(CHECK_OK)
    assert code.strip().startswith("assert") and "```" not in text
    assert check_answer("keine Rechnung")[1].status == "none"


def test_exam_matching():
    assert _pair_matches("mock exam2", _exam_tokens("Mock Exam 2 Aufgabe 1")) > \
        _pair_matches("physii itet mock 2025", _exam_tokens("Mock Exam 2 Aufgabe 1"))
    assert _pair_matches("physii itet exam aug2025", _exam_tokens("Prüfung August 2025")) == 3


def _agent(cfg, **providers):
    return Agent(cfg, providers=providers, usage=UsageLog(cfg.data_dir / "usage.sqlite"))


def test_handle_with_reference_and_check(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    agent = _agent(cfg, codex=FakeProvider("codex", reply=CHECK_OK), claude=FakeProvider("claude"))
    index_course(cfg, agent.store, cfg.courses["physik2"], log=lambda *_: None)

    ref = resolve(agent.store, "physik2", "Prüfung 2024 Aufgabe 2")
    assert ref and ref.problem_no == "2" and ref.chunks[0].doc_type == "pruefung"

    status = []
    ans = agent.handle("Löse Prüfung 2024 Aufgabe 2", on_status=status.append)
    assert ans.provider == "codex" and ans.check.status == "pass"
    assert "```" not in ans.text
    prompt = agent.providers["codex"].prompts[-1]
    assert "## Question (verbatim)\n\nLöse Prüfung 2024 Aufgabe 2" in prompt
    assert "### AUFGABE [Physik II – PhysII_Exam_2024.pdf, Aufgabe 2, S. 3]" in prompt
    assert "### MUSTERLÖSUNG [Physik II – PhysII_Exam_2024_solns.pdf, Aufgabe 2" in prompt
    assert any("Musterlösung gefunden" in s for s in status)
    rendered = ans.render("Physik II")
    assert "Geprüft: Tool ✓" in rendered and "**Quellen**" in rendered
    assert agent.usage.summary()[0][0] == "codex"


def test_failed_check_and_fallback(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    agent = _agent(cfg, codex=FakeProvider("codex", fail=True, rate_limited=True),
                   claude=FakeProvider("claude", reply=CHECK_FAIL))
    index_course(cfg, agent.store, cfg.courses["physik2"], log=lambda *_: None)
    status = []
    ans = agent.handle("Berechne die Johnson noise", on_status=status.append)
    assert ans.provider == "claude"                    # Fallback bei Limit
    assert any("Limit erreicht" in s for s in status)
    assert ans.check.status == "fail" and "Tool ✗" in ans.render("Physik II")


def test_named_model_no_silent_fallback(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    agent = _agent(cfg, codex=FakeProvider("codex"), claude=FakeProvider("claude", fail=True))
    ans = agent.handle("@claude erkläre den Spin")
    assert ans.provider == "claude" and ans.llm and not ans.llm.ok


def test_tutor_mode_for_series(course_dir):  # noqa: F811
    cfg = make_cfg(course_dir)
    root = cfg.material_root / "Physik 2"
    import pymupdf
    for name, txt in (("Exercises/Ex_2.pdf", "Compute the de Broglie wavelength of an electron accelerated through 50 kV."),
                      ("Exercise Solutions/Ex_2_solutions.pdf", "The de Broglie wavelength is lambda = h/p = h/sqrt(2 m q V) = 5.5 pm.")):
        doc = pymupdf.open()
        pg = doc.new_page()
        pg.insert_text((60, 60), "1", fontsize=16, fontname="hebo")
        pg.insert_text((60, 85), "Matter waves", fontsize=16, fontname="hebo")
        pg.insert_text((60, 110), txt, fontsize=11, fontname="helv")
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        doc.save(root / name)
    agent = _agent(cfg, codex=FakeProvider("codex"))
    index_course(cfg, agent.store, cfg.courses["physik2"], log=lambda *_: None)
    agent.handle("Löse Serie 2 Aufgabe 1")
    assert "TUTOR mode" in agent.providers["codex"].prompts[-1]

    cfg.courses["physik2"] = replace(cfg.courses["physik2"], serien_mode="full")
    agent.handle("Löse Serie 2 Aufgabe 1", history=[Turn("user", "Hallo")])
    p = agent.providers["codex"].prompts[-1]
    assert "TUTOR mode" not in p and "Conversation so far" in p


def test_skills_not_yet_built(course_dir):  # noqa: F811
    agent = _agent(make_cfg(course_dir), codex=FakeProvider("codex"))
    assert "noch nicht gebaut" in agent.handle("/practice physik2").text
    assert "Befehle" in agent.handle("/help").text
