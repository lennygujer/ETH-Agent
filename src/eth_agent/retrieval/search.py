"""Hybride Suche: BM25 (FTS5) + optional Embeddings, zusammengeführt per gewichteter RRF."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .store import ChunkRow, Store

_WORD = re.compile(r"[\wäöüÄÖÜß]+", re.UNICODE)
_STOP = {
    # deutsch
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einer", "und", "oder", "ist",
    "sind", "was", "wie", "warum", "wieso", "von", "mit", "für", "fuer", "im", "in", "zu", "zum",
    "zur", "auf", "bei", "aus", "es", "man", "ich", "du", "nicht", "kann", "wird", "werden", "an",
    "als", "auch", "dass", "bitte", "erkläre", "erklaere", "mir",
    # englisch
    "the", "a", "an", "of", "and", "or", "is", "are", "what", "how", "why", "to", "for", "with",
    "on", "at", "by", "it", "this", "that", "be", "do", "does",
}
RRF_K = 60


@dataclass
class Hit:
    chunk: ChunkRow
    score: float
    kw_rank: int | None
    sem_rank: int | None


def fts_query(text: str) -> str | None:
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP and (len(w) > 1 or w.isdigit())]
    if not words:
        return None
    # Präfixsuche für längere Wörter (Plural, Flexion), exakt für kurze/Zahlen.
    terms = [f'"{w}"*' if len(w) >= 5 and not w.isdigit() else f'"{w}"' for w in dict.fromkeys(words)]
    return " OR ".join(terms)


def search(store: Store, query: str, *, course: str | None = None, doc_types: list[str] | None = None,
           top_k: int = 20, final_k: int = 5, embedding_model: str | None = None,
           encode: Callable[[str], np.ndarray] | None = None,
           semantic_weight: float = 0.7, keyword_weight: float = 0.3) -> list[Hit]:
    """Gibt die besten final_k Chunks zurück.

    encode: Funktion Text -> normierter Vektor (für Tests injizierbar). Ohne encode oder
    ohne gespeicherte Embeddings wird nur BM25 verwendet.
    """
    kw: list[tuple[int, float]] = []
    q = fts_query(query)
    if q:
        kw = store.keyword_search(q, course=course, doc_types=doc_types, limit=top_k)

    sem: list[tuple[int, float]] = []
    if embedding_model and encode is not None:
        ids, mat = store.embeddings(embedding_model, course=course, doc_types=doc_types)
        if ids:
            qv = encode(query)
            sims = mat @ qv
            order = np.argsort(-sims)[:top_k]
            sem = [(ids[i], float(sims[i])) for i in order]

    if not sem:
        semantic_weight, keyword_weight = 0.0, 1.0
    scores: dict[int, float] = {}
    kw_rank = {cid: r for r, (cid, _) in enumerate(kw)}
    sem_rank = {cid: r for r, (cid, _) in enumerate(sem)}
    for cid, r in kw_rank.items():
        scores[cid] = scores.get(cid, 0.0) + keyword_weight / (RRF_K + r)
    for cid, r in sem_rank.items():
        scores[cid] = scores.get(cid, 0.0) + semantic_weight / (RRF_K + r)

    best = sorted(scores, key=scores.get, reverse=True)[:final_k]
    rows = store.get_chunks(best)
    return [Hit(chunk=rows[c], score=scores[c], kw_rank=kw_rank.get(c), sem_rank=sem_rank.get(c))
            for c in best if c in rows]
