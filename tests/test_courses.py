"""Mehrere Fächer: automatische Erkennung, Kürzel, /fach-Umschaltung, weitere Dateitypen."""

import json
from pathlib import Path

import pytest
import yaml

from eth_agent.agent.core import Agent, Turn
from eth_agent.config import load_config
from eth_agent.indexer.build import index_course
from eth_agent.llm.providers import FakeProvider
from eth_agent.llm.usage import UsageLog

from test_indexer import make_pdf


def _pdf(path: Path, text: str):
    make_pdf(path, [[("1", 16, True), ("Aufgabe", 16, True), (text, 11, False),
                     ("Some more filler text so that the page is clearly not empty.", 11, False)]])


@pytest.fixture()
def eth(tmp_path):
    root = tmp_path / "material"
    _pdf(root / "HS26" / "Physik 2" / "Exams" / "exam.pdf", "Quantum tunneling through a barrier of height V0.")
    _pdf(root / "HS26" / "System und Signaltheorie" / "Übungen" / "Serie 1.pdf",
         "Compute the Fourier transform of a rectangular pulse.")
    _pdf(root / "HS26" / "Analysis 3" / "Skript" / "skript.pdf", "Separation of variables for the heat equation.")
    _pdf(root / "FS26" / "Physik 1" / "Vorlesung" / "VL_01.pdf", "Newton's laws and harmonic oscillator.")
    _pdf(root / "FS26" / "Analysis" / "a.pdf", "Integration by parts.")
    _pdf(root / "HS25" / "Analysis" / "a.pdf", "Limits of sequences.")
    _pdf(root / "Code-Demo-Windows-1.1" / "x.pdf", "should be ignored")
    info = root / "HS26" / "Informatik 2"
    (info / "code").mkdir(parents=True)
    (info / "code" / "dijkstra.py").write_text("def dijkstra(graph, start):\n    # shortest paths with a heap\n"
                                               "    pass\n")
    (info / "notes.md").write_text("# Graphen\n\nBreitensuche (BFS) besucht Knoten schichtweise.\n" * 3)
    nb = {"cells": [{"cell_type": "markdown", "source": ["# Hashing\n", "Open addressing with linear probing."]},
                    {"cell_type": "code", "source": ["h = hash(key) % m"]}]}
    (info / "uebung.ipynb").write_text(json.dumps(nb))
    import docx
    d = docx.Document()
    d.add_heading("Zusammenfassung Sortieren", level=1)
    d.add_paragraph("Mergesort hat Laufzeit O(n log n) und ist stabil.")
    d.save(info / "zusammenfassung.docx")
    from pptx import Presentation
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "AVL-Bäume"
    s.placeholders[1].text = "Rotationen halten den Baum balanciert."
    prs.save(info / "vl_avl.pptx")

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "material_root": str(root), "data_dir": str(tmp_path / "data"),
        "course_aliases": {"sst": "System und Signaltheorie"},
        "course_defaults": {"solution_mode": "full"},
        "courses": {"physik2": {"folder": "Physik 2", "name": "Physik II"}},
    }, allow_unicode=True))
    return load_config(cfg_file)


def test_discovery_and_aliases(eth):
    ids = set(eth.courses)
    assert {"physik2", "systemundsignaltheorie", "analysis3", "physik1", "analysis-fs26", "analysis-hs25",
            "informatik2"} == ids
    assert eth.courses["physik2"].folder == "HS26/Physik 2"          # konfiguriertes Fach umgebogen
    assert eth.courses["physik2"].semester == "HS26"
    assert eth.courses["systemundsignaltheorie"].solution_mode == "full"  # course_defaults
    r = eth.resolve_course
    assert r("physik").id == "physik2"            # neuestes Semester gewinnt
    assert r("physik1").id == "physik1"
    assert r("sst").id == "systemundsignaltheorie"
    assert r("analysis").id == "analysis3"
    assert r("analysis-hs25").id == "analysis-hs25"
    assert r("info") is None or r("info").id == "informatik2"
    assert r("informatik").id == "informatik2"
    assert r("quatsch") is None


def _agent(cfg, **prov):
    a = Agent(cfg, providers=prov or {"codex": FakeProvider("codex")},
              usage=UsageLog(cfg.data_dir / "usage.sqlite"))
    for c in cfg.courses.values():
        index_course(cfg, a.store, c, log=lambda *_: None)
    return a


def test_other_file_types_indexed(eth):
    a = _agent(eth)
    rows = {r["rel_path"]: r for r in a.store.files("informatik2")}
    assert rows["code/dijkstra.py"]["doc_type"] == "code"
    assert rows["zusammenfassung.docx"]["doc_type"] == "notizen"
    assert all(r["n_chunks"] >= 1 for r in rows.values()), rows
    from eth_agent.retrieval.search import search
    for q, f in (("Mergesort stabil", "zusammenfassung.docx"), ("AVL Rotationen", "vl_avl.pptx"),
                 ("linear probing", "uebung.ipynb"), ("Breitensuche", "notes.md")):
        assert search(a.store, q, course="informatik2")[0].chunk.rel_path == f
    assert not any("Code-Demo" in r["rel_path"] for r in a.store.files())


def test_course_switching(eth):
    codex = FakeProvider("codex", reply="Antwort")
    a = _agent(eth, codex=codex)

    ans = a.handle("/physik")
    assert "Fach gewechselt: **Physik II (HS26)**" in ans.render()

    hist = [Turn("user", "/physik"), Turn("assistant", ans.text)]
    ans = a.handle("quantum tunneling barrier", history=hist)
    assert ans.course.id == "physik2"
    assert "exam.pdf" in codex.prompts[-1] and "Serie 1.pdf" not in codex.prompts[-1]

    ans = a.handle("/sst Fourier transform of a rectangular pulse")
    assert ans.course.id == "systemundsignaltheorie"
    assert "Serie 1.pdf" in codex.prompts[-1] and "exam.pdf" not in codex.prompts[-1]

    hist += [Turn("user", "/alle"), Turn("assistant", "…")]
    ans = a.handle("heat equation separation of variables", history=hist)
    assert ans.course.id == ""                                   # alle Fächer
    assert "[Analysis 3 – skript.pdf" in ans.render()            # Zitat mit dem richtigen Fachnamen

    lst = a.handle("/kurse").render()
    assert "System und Signaltheorie" in lst and "/sst" in lst and "HS25" in lst

    assert "Unbekanntes Fach" in a.handle("/quatsch was ist das").render()
    assert "zuerst ein Fach wählen" in a.handle("/check Serie 1 Aufgabe 1\nx").render()
    out = a.handle("/sst /check Serie 1 Aufgabe 1\nF = sinc").render()
    assert "zuerst ein Fach" not in out


def test_resolve_by_letter_sequence():
    from eth_agent.config import Config, Course, ModelsConfig, RetrievalConfig
    from pathlib import Path
    courses = {cid: Course(id=cid, folder=name, name=name) for cid, name in [
        ("systemundsignaltheorie", "System und Signaltheorie"), ("springerbuecher", "Springer Bücher"),
        ("diskretemathematik", "Diskrete Mathematik"), ("digitech", "Digitech")]}
    cfg = Config(material_root=Path("/tmp"), courses=courses, data_dir=Path("/tmp"), folder_doc_types={},
                 retrieval=RetrievalConfig(), models=ModelsConfig())
    assert cfg.resolve_course("sst").id == "systemundsignaltheorie"
    assert cfg.resolve_course("ss").id == "systemundsignaltheorie"
    assert cfg.resolve_course("dm").id == "diskretemathematik"
    assert cfg.resolve_course("dit") is None or cfg.resolve_course("dit").id in {"digitech", "diskretemathematik"}
