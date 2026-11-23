"""Optionale Embeddings (sentence-transformers, CPU). Ohne Paket: None -> nur BM25."""

from __future__ import annotations

import numpy as np

_MODELS: dict[str, object] = {}


def available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _prefix(model: str, kind: str) -> str:
    # E5-Modelle erwarten Präfixe, bge-m3 nicht.
    if "e5" in model.lower():
        return "query: " if kind == "query" else "passage: "
    return ""


def _load(model: str):
    if model not in _MODELS:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(model, device="cpu")
        # bge-m3 erlaubt bis 8192 Tokens; der Aufwand wächst quadratisch. Für die Suche reichen
        # die ersten 1024 Tokens eines Chunks (Überschrift + Aufgabenstellung) völlig.
        m.max_seq_length = min(m.max_seq_length or 1024, 1024)
        _MODELS[model] = m
    return _MODELS[model]


def encode(model: str, texts: list[str], kind: str = "passage", batch_size: int = 16) -> np.ndarray:
    m = _load(model)
    pre = _prefix(model, kind)
    vecs = m.encode([pre + t for t in texts], batch_size=batch_size,
                    normalize_embeddings=True, show_progress_bar=len(texts) > 50)
    return np.asarray(vecs, dtype=np.float32)
