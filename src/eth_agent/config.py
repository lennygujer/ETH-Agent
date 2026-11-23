"""Konfiguration laden (config.yaml, Fallback config.example.yaml)."""

from __future__ import annotations

import fnmatch
import os
import re
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
    # hints = bei Aufgaben zuerst nur Tipps, ganze Lösung erst auf Wunsch (--loesung); full = sofort ganze Lösung
    solution_mode: str = "hints"
    serien_mode: str | None = None          # veraltet: tutor -> hints, full -> full
    semester: str | None = None              # "HS26", aus dem Ordner erkannt
    aliases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.serien_mode:
            self.solution_mode = "full" if self.serien_mode == "full" else "hints"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.semester})" if self.semester else self.name


# Pseudo-Fach, wenn keins gewählt ist: Suche über alle Fächer
ALL_COURSES = Course(id="", folder="", name="ETH (alle Fächer)")

_SEMESTER_DIR = re.compile(r"^(HS|FS)(\d{2})$", re.I)
_ALIAS_STOP = {"und", "and", "of", "der", "die", "das", "für", "fuer", "in", "the", "de"}
DEFAULT_IGNORE = [".*", "__pycache__", "node_modules", "venv", ".venv", "Code-Demo-*"]


def slug(text: str) -> str:
    t = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e"), ("è", "e")):
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", t)


def semester_key(sem: str | None) -> tuple[int, int]:
    """HS26 > FS26 > HS25 > ...; ohne Semester ganz hinten."""
    m = _SEMESTER_DIR.match(sem or "")
    if not m:
        return (-1, -1)
    return (int(m.group(2)), 1 if m.group(1).upper() == "HS" else 0)


def auto_aliases(name: str) -> set[str]:
    words = [w for w in re.split(r"[\s_\-]+", name) if w]
    out = {slug(name), slug(re.sub(r"\d", "", name))}
    content = [w for w in words if w.lower() not in _ALIAS_STOP]
    if content and len(slug(content[0])) >= 3:
        out.add(slug(content[0]))
    if len(content) >= 2:
        out.add("".join(slug(w)[:1] for w in content if slug(w)))
    return {a for a in out if len(a) >= 2}


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
class ModelsConfig:
    codex_model: str | None = None          # None = Codex-Standard
    codex_effort_hard: str = "medium"       # Aufgaben, Herleitungen, Code (R4/R6)
    codex_effort_default: str = "low"       # Erklärungen, Fragen (R8)
    claude_model: str | None = None
    timeout_s: int = 600


@dataclass
class Config:
    material_root: Path
    data_dir: Path
    courses: dict[str, Course]
    folder_doc_types: dict[str, str]
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    default_course: str | None = None        # None = ohne /fach über alle Fächer suchen

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite"

    def course_for_folder(self, folder_name: str) -> Course | None:
        for c in self.courses.values():
            if c.folder == folder_name:
                return c
        return None

    def is_ignored(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in self.ignore)

    def resolve_course(self, token: str) -> Course | None:
        """'/physik' -> Physik 2 (neuestes Semester bei Mehrdeutigkeit), '/sst', '/analysis3', ..."""
        t = slug(token)
        if not t:
            return None
        by_id = {slug(cid): c for cid, c in self.courses.items()}
        if token.lower() in self.courses:
            return self.courses[token.lower()]
        if t in by_id:
            return by_id[t]
        exact = [c for c in self.courses.values() if t in {slug(a) for a in c.aliases}]
        if not exact:
            exact = [c for c in self.courses.values() if t in auto_aliases(c.name)]
        if not exact and len(t) >= 3:
            exact = [c for c in self.courses.values() if slug(c.name).startswith(t)]
        if not exact:
            return None
        exact.sort(key=lambda c: semester_key(c.semester), reverse=True)
        if len(exact) > 1 and semester_key(exact[0].semester) == semester_key(exact[1].semester):
            # gleiches Semester: kürzester Name gewinnt nur, wenn eindeutig ("analysis" vs "analysis3")
            same = [c for c in exact if semester_key(c.semester) == semester_key(exact[0].semester)]
            same.sort(key=lambda c: len(c.name))
            if len(same[0].name) == len(same[1].name):
                return None
            return same[0]
        return exact[0]


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
    configured = {
        cid: Course(id=cid, **{k: v for k, v in (c or {}).items()})
        for cid, c in (raw.get("courses") or {}).items()
    }
    retrieval = RetrievalConfig(**(raw.get("retrieval") or {}))
    if not retrieval.embedding_model:
        retrieval.embedding_model = None
    cfg = Config(
        material_root=_expand(raw.get("material_root", "~/eth-agent/material")),
        data_dir=_expand(raw.get("data_dir", "~/eth-agent/data")),
        courses={},
        folder_doc_types={k.lower(): v for k, v in (raw.get("folder_doc_types") or {}).items()},
        retrieval=retrieval,
        models=ModelsConfig(**(raw.get("models") or {})),
        ignore=list(raw.get("ignore") or DEFAULT_IGNORE),
        default_course=raw.get("default_course"),
    )
    defaults = raw.get("course_defaults") or {}
    discovered = discover_courses(cfg, defaults) if raw.get("discover_courses", True) else []
    cfg.courses = merge_courses(configured, discovered, cfg.material_root)
    for alias, target in (raw.get("course_aliases") or {}).items():
        c = cfg.courses.get(slug(target)) or next(
            (x for x in cfg.courses.values() if x.name == target or x.folder == target), None)
        if c:
            c.aliases.append(alias)
    return cfg


def discover_courses(cfg: Config, defaults: dict | None = None) -> list[Course]:
    """Fächer im Material-Ordner finden: <Semester>/<Fach>/ (HS26/Physik 2) oder direkt <Fach>/."""
    root = cfg.material_root
    if not root.is_dir():
        return []
    found: list[tuple[str, str | None]] = []   # (Ordner relativ zu root, Semester)
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not cfg.is_ignored(p.name)):
        if _SEMESTER_DIR.match(d.name):
            for sub in sorted(p for p in d.iterdir() if p.is_dir() and not cfg.is_ignored(p.name)):
                found.append((f"{d.name}/{sub.name}", d.name.upper()))
        else:
            found.append((d.name, None))
    names = [Path(f).name for f, _ in found]
    courses = []
    for folder, sem in found:
        name = Path(folder).name
        cid = slug(name) or slug(folder)
        if names.count(name) > 1 and sem:
            cid = f"{cid}-{sem.lower()}"
        courses.append(Course(id=cid, folder=folder, name=name, semester=sem, **(defaults or {})))
    return courses


def merge_courses(configured: dict[str, Course], discovered: list[Course], root: Path) -> dict[str, Course]:
    """Konfigurierte Fächer behalten ihre ID und Einstellungen; der Ordner wird bei Bedarf auf den
    gefundenen Ordner mit gleichem Namen umgebogen (z. B. 'Physik 2' -> 'HS26/Physik 2')."""
    out: dict[str, Course] = {}
    used: set[str] = set()
    for cid, c in configured.items():
        if not (root / c.folder).is_dir():
            match = [d for d in discovered if Path(d.folder).name == Path(c.folder).name]
            if match:
                best = max(match, key=lambda d: semester_key(d.semester))
                c.folder, c.semester = best.folder, c.semester or best.semester
        else:
            sem = next((d.semester for d in discovered if d.folder == c.folder), None)
            c.semester = c.semester or sem
        out[cid] = c
        used.add(c.folder)
    for d in discovered:
        if d.folder in used:
            continue
        cid = d.id
        while cid in out:
            cid += "x"
        d.id = cid
        out[cid] = d
    return out
