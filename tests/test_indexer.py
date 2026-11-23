"""Tests mit künstlich erzeugten PDFs (kein ETH-Material im Repo)."""

from pathlib import Path

import numpy as np
import pymupdf
import pytest

from eth_agent.config import Config, Course, RetrievalConfig
from eth_agent.indexer.build import index_course
from eth_agent.indexer.doctypes import classify, cover_metadata, pair_key
from eth_agent.retrieval.search import fts_query, search
from eth_agent.retrieval.store import Store

FOLDER_TYPES = {
    "exercises": "serie", "exercise solutions": "serie_loesung", "exams": "pruefung",
    "exam solutions": "pruefung_loesung", "sheets": "skript",
}


def make_pdf(path: Path, pages: list[list[tuple[str, float, bool]]], header: str | None = None):
    """pages: Liste von Seiten, jede Seite eine Liste (Text, Grösse, fett)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for i, lines in enumerate(pages, 1):
        page = doc.new_page()
        y = 60
        if header:
            page.insert_text((60, 40), header, fontsize=10, fontname="helv")
        for text, size, bold in lines:
            page.insert_text((60, y), text, fontsize=size, fontname="hebo" if bold else "helv")
            y += size * 1.6
        page.insert_text((300, 800), str(i), fontsize=10, fontname="helv")
    doc.save(path)


@pytest.fixture()
def course_dir(tmp_path):
    root = tmp_path / "material" / "Physik 2"
    body = 11.0
    make_pdf(root / "Exams" / "PhysII_Exam_2024.pdf", [
        [("HS2024 Physics II D-ITET", body, False), ("Prof. J. Faist", body, False),
         ("Final Exam", body, False), ("This exam should be solved within 180 min.", body, False)],
        [("1", 16, True), ("Multiple choice questions", 16, True),
         ("1.1", 13, True), ("Measuring a voltage (1 pt)", 13, True),
         ("What is the rms Johnson noise voltage of a resistor?", body, False)],
        [("2", 16, True), ("Hydrogen Atom (12 pts)", 16, True),
         ("Compute the expectation value of the radius in the ground state.", body, False),
         ("a) Normalize the wave function.", body, False)],
    ], header="HS2024 Physics II D-ITET")
    make_pdf(root / "Exam Solutions" / "PhysII_Exam_2024_solns.pdf", [
        [("1", 16, True), ("Multiple choice questions", 16, True),
         ("1.1", 13, True), ("Measuring a voltage (1 pt)", 13, True),
         ("Answer a): sqrt(4 kT R B) = 4.07 microvolt.", body, False)],
        [("2", 16, True), ("Hydrogen Atom (12 pts)", 16, True),
         ("The expectation value of the radius is 3a/2.", body, False)],
        [("2.1", 13, True), ("Normalization", 13, True),
         ("Normalization gives N = 1/sqrt(pi a^3).", body, False)],
    ], header="Solutions")
    make_pdf(root / "Sheets" / "script_phys_II.pdf", [
        [("8", 16, True), ("The Infinite Quantum Well", 16, True),
         ("8.1", 13, True), ("Setup", 13, True)]
        + [("The infinite square well has discrete energy levels E_n = n^2 pi^2 hbar^2 / 2 m L^2.", body, False)] * 6,
        [("8.2", 13, True), ("Orthogonality", 13, True)]
        + [("Eigenstates of the well are orthogonal and form a complete basis.", body, False)] * 6,
    ])
    (root / "Exercises").mkdir(parents=True)
    (root / "Exercises" / "Ex_7.pdf").write_bytes(b"")   # OneDrive-Platzhalter
    return tmp_path


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        material_root=tmp_path / "material", data_dir=tmp_path / "data",
        courses={"physik2": Course(id="physik2", folder="Physik 2", name="Physik II")},
        folder_doc_types=FOLDER_TYPES, retrieval=RetrievalConfig(),
    )


def test_classify_and_pairs():
    assert classify("Exercises/Ex_3.pdf", FOLDER_TYPES) == "serie"
    assert classify("Exercise Solutions/Ex_3_solutions.pdf", FOLDER_TYPES) == "serie_loesung"
    assert classify("Exercise Solutions/Mock_exam2_solutions.pdf", FOLDER_TYPES) == "pruefung_loesung"
    assert classify("Sheets/physII_formula_sheet-2.pdf", FOLDER_TYPES) == "formelsammlung"
    assert classify("Sheets/script_phys_II.pdf", FOLDER_TYPES) == "skript"
    assert pair_key("Exams/PhysII_ITET_Exam_2024_Jan_final.pdf") == \
        pair_key("Exam Solutions/PhysII_ITET_Exam_2024_Jan_final_solns.pdf")
    assert pair_key("Exams/PhysII_ITET_Exam_Aug2025_Final.pdf") == \
        pair_key("Exam Solutions/PhysII_ITET_Exam_Aug2025_Solns_final.pdf")
    assert pair_key("Exercises/Ex_1.pdf") == pair_key("Exercise Solutions/Ex_1_solution.pdf")
    assert pair_key("Exams/mock_exam.pdf") != pair_key("Exams/Mock_exam2.pdf")


def test_cover_metadata():
    m = cover_metadata("HS2024 Physics II D-ITET\nProf. G. Scalari\nResit Exam\nAugust 20, 2025", "x.pdf")
    assert m["semester"] == "HS2024" and m["year"] == "2025"
    assert m["lecturer"] == "G. Scalari" and m["title"] == "Resit Exam"


def test_index_search_and_solution(course_dir):
    cfg = make_cfg(course_dir)
    store = Store(cfg.db_path)
    rep = index_course(cfg, store, cfg.courses["physik2"], log=lambda *_: None)
    assert rep.placeholders == ["Exercises/Ex_7.pdf"]
    assert not rep.errors

    rows = {r["rel_path"]: r for r in store.files()}
    assert rows["Exams/PhysII_Exam_2024.pdf"]["semester"] == "HS2024"
    assert rows["Exercises/Ex_7.pdf"]["status"] == "placeholder"

    hits = search(store, "hydrogen radius expectation", course="physik2", doc_types=["pruefung"])
    top = hits[0].chunk
    assert top.problem_no == "2" and top.points == 12.0
    assert top.printed_start == "3"
    assert top.citation("Physik II") == "[Physik II – PhysII_Exam_2024.pdf, Aufgabe 2, S. 3]"

    sols = store.solution_for(top)
    assert [s.problem_no for s in sols] == ["2", "2.1"]   # Unterabschnitte der Lösung kommen mit
    assert "3a/2" in sols[0].text

    mc = search(store, "Johnson noise", doc_types=["pruefung"])[0].chunk
    assert mc.problem_no == "1.1" and mc.points == 1.0
    assert mc.section.startswith("1 Multiple choice questions > 1.1")

    script = search(store, "orthogonal complete basis", doc_types=["skript"])[0].chunk
    assert script.section == "8 The Infinite Quantum Well > 8.2 Orthogonality"
    store.close()


def test_incremental_reindex(course_dir):
    cfg = make_cfg(course_dir)
    store = Store(cfg.db_path)
    index_course(cfg, store, cfg.courses["physik2"], log=lambda *_: None)
    rep = index_course(cfg, store, cfg.courses["physik2"], log=lambda *_: None)
    assert rep.added == [] and rep.unchanged == 3
    (cfg.material_root / "Physik 2" / "Sheets" / "script_phys_II.pdf").unlink()
    rep = index_course(cfg, store, cfg.courses["physik2"], log=lambda *_: None)
    assert rep.removed == ["Sheets/script_phys_II.pdf"]
    assert search(store, "orthogonal complete basis", doc_types=["skript"]) == []
    store.close()


def test_hybrid_search_with_fake_embeddings(course_dir):
    """Semantische Suche findet Treffer auch ohne gemeinsame Wörter (deutsche Anfrage)."""
    cfg = make_cfg(course_dir)
    store = Store(cfg.db_path)
    index_course(cfg, store, cfg.courses["physik2"], log=lambda *_: None)

    def fake_vec(text: str) -> np.ndarray:
        v = np.array([("well" in text.lower() or "potentialtopf" in text.lower()) * 1.0, 0.1], dtype=np.float32)
        return v / np.linalg.norm(v)

    items = [(cid, fake_vec(txt)) for cid, txt, _ in store.chunks_without_embedding("fake")]
    store.set_embeddings("fake", items)
    hits = search(store, "unendlicher Potentialtopf", embedding_model="fake", encode=fake_vec)
    assert hits and hits[0].chunk.doc_type == "skript" and hits[0].kw_rank is None
    store.close()


def test_fts_query_drops_stopwords():
    assert fts_query("Was ist die Unschärferelation?") == '"unschärferelation"*'
    assert fts_query("?!") is None
