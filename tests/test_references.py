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
    assert "Verification code:" not in p and "Geprüft" not in ans.render()   # Unterlage erklären: kein Prüfcode


def _add_sst_uebungen(tmp):
    body = 11.0
    d = tmp / "material" / "Physik 2" / "Übungen"
    make_pdf(d / "Übung4.pdf", [
        [("44", 16, True), ("Faltung", 16, True)] + [("Berechne die Faltung von x und h.", body, False)] * 3,
        [("45", 16, True), ("Fourier-Reihe", 16, True), ("a) Periode bestimmen.", body, False),
         ("b) Koeffizienten berechnen.", body, False), ("c) Parseval anwenden.", body, False)],
    ])
    make_pdf(d / "Übung5.pdf", [
        [("46", 16, True), ("Abtastung", 16, True)] + [("Bestimme die Nyquist-Rate.", body, False)] * 3,
    ])


def test_resolve_continuous_numbering_and_images(course_dir):  # noqa: F811
    _add_sst_uebungen(course_dir)
    cfg = make_cfg(course_dir)
    cfg.folder_doc_types["übungen"] = "serie"
    codex = FakeProvider("codex", reply="Tipp.")
    agent = _agent(cfg, codex=codex, claude=FakeProvider("claude"))

    ref = resolve(agent.store, "physik2", "aufgabe 45 c in ss übungen")
    assert ref and ref.problem_no == "45" and ref.part == "c" and "Übung4.pdf" in ref.chunks[0].rel_path
    assert "Parseval" in "\n".join(c.text for c in ref.chunks)
    ref5 = resolve(agent.store, "physik2", "Übung 5 Aufgabe 46")
    assert ref5 and "Übung5.pdf" in ref5.chunks[0].rel_path
    assert resolve(agent.store, "physik2", "Aufgabe 99") is None

    # Bild im normalen Pfad geht an Codex, Prompt erwähnt es
    img = course_dir / "foto.png"
    img.write_bytes(b"x")
    st: list[str] = []
    agent.handle("aufgabe 45 c in ss übungen", course_id="physik2", images=[str(img)], on_status=st.append)
    assert codex.images[-1] == [str(img)] and "attached 1 image" in codex.prompts[-1]
    assert any("Aufgabe gefunden" in s and "Übung4.pdf" in s for s in st)


def test_server_unwraps_openwebui_template():
    from eth_agent.server import request_snapshot, unwrap_user_text
    wrapped = "### Task:\nRespond …\n<context>\n<source>foto.jpg</source>\n</context>\n<user_query>\n/check aufgabe 45 c\n</user_query>"
    assert unwrap_user_text(wrapped) == "/check aufgabe 45 c"
    assert unwrap_user_text("/check x") == "/check x"
    owui = ('<attached_files>\n<file type="file" id="92e5" content_type="image/png" name="image.png"/>\n'
            '</attached_files>\n\n\n/check aufgabe 45 c in ss übungen')
    assert unwrap_user_text(owui) == "/check aufgabe 45 c in ss übungen"
    snap = request_snapshot({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "/check a"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 5000}}]}]})
    parts = snap["messages"][0]["parts"]
    assert parts[0]["start"] == "/check a" and parts[1]["len"] > 5000 and len(parts[1]["url_start"]) == 40


def test_uebungsskript_with_solutions_in_same_file(course_dir):  # noqa: F811
    """SST-Übungsskriptum: 'Aufgabe 45∗' (nicht fett) vorne, 'Lösung 45' hinten in derselben Datei."""
    body = 12.0
    make_pdf(course_dir / "material" / "Physik 2" / "SST1-Uebung.pdf", [
        [("Übungsskriptum Signal- und Systemtheorie I", 16, True)],
        [("Aufgabe 44∗", body, False), ("Echo entfernen durch Rückkopplung.", body, False),
         ("Aufgabe 45", body, True), ("Ein LTI-System mit Impulsantwort h(t).", body, False),
         ("c) Bestimmen Sie die Antwort y(t) auf x(t) = exp(-t) sigma(t).", body, False)],
        [("Lösung 44", body, True), ("Die Idee ist, das Echo abzuziehen.", body, False)],
        [("Lösung 45", body, True), ("c) y(t) ergibt sich durch Faltung: y(t) = 2 - exp(-t).", body, False)],
    ])
    cfg = make_cfg(course_dir)
    agent = _agent(cfg, codex=FakeProvider("codex"), claude=FakeProvider("claude"))
    ref = resolve(agent.store, "physik2", "aufgabe 45 c in ss übungen")
    assert ref and ref.part == "c" and [c.doc_type for c in ref.chunks] == ["serie"]
    assert "LTI-System" in ref.chunks[0].text and "Lösung" not in ref.chunks[0].text
    sol = agent.store.solution_for(ref.chunks[0])
    assert sol and sol[0].doc_type == "serie_loesung" and "Faltung" in sol[0].text
    ref44 = resolve(agent.store, "physik2", "Aufgabe 44")
    assert ref44 and "Echo" in ref44.chunks[0].text
