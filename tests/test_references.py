"""Verweise auf Übungsstunden mit Abschnitten ("Übungsstunde 11, 1.2 und 1.3")."""

from eth_agent.agent.references import _sections, resolve
from eth_agent.llm.providers import FakeProvider

from test_indexer import course_dir, make_cfg, make_pdf  # noqa: F401
from test_learner import _agent


def _add_uebung(tmp):
    body = 11.0
    make_pdf(tmp / "material" / "Physik 2" / "Übungsstunden" / "E11_aaposto.pdf", [
        [("1", 16, True), ("Addition of Angular Momenta", 16, True),
         ("1.1", 13, True), ("Recap: Spin", 13, True)] + [("Spin operators obey [Sx,Sy] = i hbar Sz.", body, False)] * 4,
        [("1.2", 13, True), ("Two spins one half", 13, True)]
        + [("The product basis has four states up-up, up-down, down-up, down-down.", body, False)] * 4,
        [("1.3", 13, True), ("Triplet and singlet", 13, True)]
        + [("Total spin S=1 gives three triplet states, S=0 one singlet state.", body, False)] * 4,
        [("2", 16, True), ("Identical Particles", 16, True),
         ("2.1", 13, True), ("Exchange", 13, True)] + [("Fermions are antisymmetric under exchange.", body, False)] * 4,
    ])
    make_pdf(tmp / "material" / "Physik 2" / "Übungsstunden" / "E10_rstadle.pdf", [
        [("1", 16, True), ("Gyromagnetic ratio", 16, True)] + [("mu = gamma L", body, False)] * 4],
    )


def test_sections():
    assert _sections("erkläre was in 1.2 und 1.3 erklärt wird") == ["1.2", "1.3"]
    assert _sections("Abschnitt 2, 3 und 4") == ["2", "3", "4"]
    assert _sections("Aufgaben 1.2, 1.3") == ["1.2", "1.3"]
    assert _sections("bei E = 1.5 eV") == ["1.5"]           # bewusst grosszügig; nicht vorhandene Nummern stören nicht
    assert _sections("ich verstehe spin nicht") == []


def test_resolve_uebungsstunde_sections(course_dir):  # noqa: F811
    _add_uebung(course_dir)
    cfg = make_cfg(course_dir)
    cfg.folder_doc_types["übungsstunden"] = "uebungsstunde"
    codex = FakeProvider("codex", reply="Erklärung [1] [2].")
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))

    q = "ich verstehe spin noch nicht ganz. erkläre in übungsstunde 11 was genau in 1.2 und 1.3 erklärt wird"
    ref = resolve(agent.store, "physik2", q)
    assert ref and ref.kind == "uebungsstunde" and ref.problem_no == "1.2, 1.3"
    assert [c.problem_no for c in ref.chunks] == ["1.2", "1.3"]

    whole = resolve(agent.store, "physik2", "Fasse die Übungsstunde 11 zusammen")
    assert whole and whole.problem_no is None and len(whole.chunks) >= 4
    assert resolve(agent.store, "physik2", "Übungsstunde 7, Abschnitt 1") is None   # gibt es nicht

    st: list[str] = []
    ans = agent.handle(q, course_id="physik2", on_status=st.append)
    p = codex.prompts[-1]
    assert "UNTERLAGE" in p and "Triplet and singlet" in p and "four states" in p
    assert "Explain exactly those sections" in p
    assert any("Übungsstunde gefunden" in s and "1.2, 1.3" in s for s in st)
    assert "Tipp" not in ans.text                                  # keine Tipp-Leiter für Unterlagen
