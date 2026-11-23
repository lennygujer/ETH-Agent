"""Konfiguration laden (config.yaml, Fallback config.example.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Course:
    id: str
    folder: str
    name: str
    output_language: str = "de"
    notation_source: str | None = None
    # tutor = bei Serien nur Hinweise (für Bonus/Testat-Serien), full = vollständige Lösungen
    serien_mode: str = "tutor"


@dataclass
class RetrievalConfig:
    chunk_tokens: int = 600
    overlap_tokens: int = 80
    top_k: int = 20
    final_k: int = 5
    embedding_model: str | None = None
    semantic_weight: float = 0.7
    keyword_weight: float = 0.3


@dataclass
class Config:
    material_root: Path
    data_dir: Path
    courses: dict[str, Course]
    folder_doc_types: dict[str, str]
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite"

    def course_for_folder(self, folder_name: str) -> Course | None:
        for c in self.courses.values():
            if c.folder == folder_name:
                return c
        return None


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Lädt die Konfiguration.

    Reihenfolge: explizites Argument, $ETH_AGENT_CONFIG, ./config.yaml, Repo-config.yaml,
    Repo-config.example.yaml.
    """
    candidates = [
        path,
        os.environ.get("ETH_AGENT_CONFIG"),
        Path.cwd() / "config.yaml",
        REPO_ROOT / "config.yaml",
        REPO_ROOT / "config.example.yaml",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            cfg_path = Path(c)
            break
    else:
        raise FileNotFoundError("Keine config.yaml gefunden (siehe config.example.yaml).")

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    courses = {
        cid: Course(id=cid, **{k: v for k, v in (c or {}).items()})
        for cid, c in (raw.get("courses") or {}).items()
    }
    retrieval = RetrievalConfig(**(raw.get("retrieval") or {}))
    if not retrieval.embedding_model:
        retrieval.embedding_model = None
    return Config(
        material_root=_expand(raw.get("material_root", "~/eth-agent/material")),
        data_dir=_expand(raw.get("data_dir", "~/eth-agent/data")),
        courses=courses,
        folder_doc_types={k.lower(): v for k, v in (raw.get("folder_doc_types") or {}).items()},
        retrieval=retrieval,
    )
