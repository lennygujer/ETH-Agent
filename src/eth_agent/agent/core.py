"""handle(): eine Nachricht -> Routing -> Material -> Prompt -> Modell -> Prüfcode -> Antwort.

Terminal-Chat und (später) der FastAPI-Server rufen dieselbe Funktion auf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..checks.sandbox import CheckResult, check_answer
from ..config import REPO_ROOT, Config, Course
from ..llm.providers import ClaudeProvider, CodexProvider, LLMResult, Provider
from ..llm.usage import UsageLog
from ..retrieval.search import search
from ..retrieval.store import ChunkRow, Store
from .references import resolve
from .routing import Route, est_tokens, route

PROMPTS = REPO_ROOT / "prompts"
LANG_NAMES = {"de": "German (Swiss conventions: 'ss' instead of 'ß')", "en": "English"}
CONTEXT_BUDGET = 12_000   # Tokens Kursmaterial pro Cloud-Aufruf (v4)


@dataclass
class Turn:
    role: str      # "user" | "assistant"
    text: str


@dataclass
class Answer:
    text: str
    route: Route
    provider: str | None = None
    sources: list[ChunkRow] = field(default_factory=list)
    check: CheckResult = field(default_factory=lambda: CheckResult("none", ""))
    reference: str | None = None
    llm: LLMResult | None = None
    prompt: str | None = None
    status: list[str] = field(default_factory=list)
    prerendered: bool = False     # Skills liefern fertig formatierten Text

    def render(self, course_name: str) -> str:
        if self.prerendered:
            return self.text.strip()
        parts = [self.text.strip()]
        if self.sources:
            parts.append("---\n**Quellen**\n" + "\n".join(
                f"[{i}] {c.citation(course_name)}" for i, c in enumerate(self.sources, 1)))
        footer = [f"Geprüft: {self.check.badge}"]
        if self.check.status == "pass":
            footer[0] += " (Prüfcode vom selben Modell)"
        if self.provider:
            footer.append(f"Modell: {self.provider}")
        parts.append(" · ".join(footer))
        return "\n\n".join(parts)


def _template(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _fill(template: str, **values: str) -> str:
    # Kein str.format: LaTeX-Klammern im Material dürfen nicht stören.
    for k, v in values.items():
        template = template.replace("{" + k + "}", v)
    return template


def _format_chunk(n: int, c: ChunkRow, course_name: str, label: str | None = None) -> str:
    head = f"[{n}] " + (f"{label} " if label else "") + c.citation(course_name)
    if c.section:
        head += f"\n({c.section})"
    return f"### {head}\n{c.text.strip()}"


class Agent:
    def __init__(self, cfg: Config, *, providers: dict[str, Provider] | None = None,
                 encode: Callable | None = None, embedding_model: str | None = None,
                 store: Store | None = None, usage: UsageLog | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.usage = usage or UsageLog(cfg.data_dir / "usage.sqlite")
        self.providers: dict[str, Provider] = providers or {
            "codex": CodexProvider(model=cfg.models.codex_model),
            "claude": ClaudeProvider(model=cfg.models.claude_model)}
        self.encode = encode
        self.embedding_model = embedding_model

    # ------------------------------------------------------------------ Material
    def _gather(self, course: Course, question: str, history: list[Turn],
                status: list[str]) -> tuple[list[tuple[ChunkRow, str | None]], str | None]:
        """Gibt [(Chunk, Label)] und eine Beschreibung des aufgelösten Verweises zurück."""
        picked: list[tuple[ChunkRow, str | None]] = []
        ref_desc = None
        ref = resolve(self.store, course.id, question)
        if ref and ref.chunks:
            ref_desc = f"{ref.kind} '{ref.file_pair_key}', Aufgabe {ref.problem_no}"
            status.append(f"Aufgabe gefunden: {ref.chunks[0].citation(course.name)}")
            picked += [(c, "AUFGABE") for c in ref.chunks]
            sols = self.store.solution_for(ref.chunks[0])
            if sols:
                status.append(f"Musterlösung gefunden: {sols[0].citation(course.name)}")
                picked += [(s, "MUSTERLÖSUNG") for s in sols]

        query = question
        if ref and ref.chunks:
            # Mit dem Aufgabentext suchen, nicht mit "Serie 3 Aufgabe 1".
            first = ref.chunks[0]
            query = f"{first.title or ''} {first.text[:600]}"
        elif history and len(question.split()) < 8:
            prev = [t.text for t in history if t.role == "user"][-1:]
            query = " ".join(prev + [question])
        status.append("Suche im Kursmaterial …")
        hits = search(self.store, query, course=course.id, top_k=self.cfg.retrieval.top_k,
                      final_k=self.cfg.retrieval.final_k + 3, embedding_model=self.embedding_model,
                      encode=self.encode, semantic_weight=self.cfg.retrieval.semantic_weight,
                      keyword_weight=self.cfg.retrieval.keyword_weight)
        have = {c.id for c, _ in picked}
        used = sum(est_tokens(c.text) for c, _ in picked)
        for h in hits:
            if h.chunk.id in have:
                continue
            if used + est_tokens(h.chunk.text) > CONTEXT_BUDGET:
                break
            picked.append((h.chunk, None))
            have.add(h.chunk.id)
            used += est_tokens(h.chunk.text)
            if sum(1 for _, lab in picked if lab is None) >= self.cfg.retrieval.final_k:
                break
        return picked, ref_desc

    # ------------------------------------------------------------------ Prompt
    def build_prompt(self, course: Course, r: Route, picked, history: list[Turn]) -> str:
        has_solution = any(lab == "MUSTERLÖSUNG" for _, lab in picked)
        if has_solution and course.serien_mode == "tutor" and any(
                c.doc_type == "serie" for c, lab in picked if lab == "AUFGABE"):
            policy = ("Solution policy: this exercise sheet is in TUTOR mode. Do NOT give the full solution. "
                      "Give hints, explain the concepts needed and check the student's own attempt if provided. "
                      "Use the official solution (marked MUSTERLÖSUNG) only to make sure your hints are correct.")
        elif has_solution:
            policy = ("Solution policy: an official solution is included (marked MUSTERLÖSUNG). Treat it as the "
                      "reference for correctness, but explain in your own words with all steps, so the student "
                      "understands why each step holds. If you believe the official solution contains an error, "
                      "say so explicitly and explain why.")
        else:
            policy = ("Solution policy: no official solution is available for this question. Work carefully, "
                      "check limits, units and signs yourself.")
        check = _template("check_block.md") if r.tool_verify else ""
        hist = ""
        if history:
            lines = [f"**{'Student' if t.role == 'user' else 'Assistant'}:** {t.text[:1500]}" for t in history[-6:]]
            hist = "## Conversation so far (for context)\n\n" + "\n\n".join(lines)
        context = "\n\n".join(_format_chunk(i, c, course.name, lab) for i, (c, lab) in enumerate(picked, 1)) \
            or "(no matching course material found)"
        return _fill(_template("answer.md"), course_name=course.name,
                     output_language_name=LANG_NAMES.get(course.output_language, course.output_language),
                     solution_policy=policy, check_instructions=check, question=r.question,
                     history_section=hist, context=context)

    # ------------------------------------------------------------------ Ablauf
    def _call(self, name: str, prompt: str, r: Route, status: list[str]) -> LLMResult:
        p = self.providers[name]
        effort = None
        if name == "codex":
            effort = (self.cfg.models.codex_effort_hard if r.rule in {"R4", "R6"}
                      else self.cfg.models.codex_effort_default)
        status.append(f"{name} arbeitet …" + (f" (Denkaufwand: {effort})" if effort else ""))
        res = p.complete(prompt, timeout=self.cfg.models.timeout_s, effort=effort)
        self.usage.record(res, skill=r.skill or "answer", rule=r.rule, prompt_tokens_est=est_tokens(prompt))
        return res

    def handle(self, message: str, *, course_id: str | None = None, history: list[Turn] | None = None,
               on_status: Callable[[str], None] | None = None, images: list[str] | None = None) -> Answer:
        history = history or []
        status: list[str] = []

        class _Status(list):
            def append(self, s):  # Status sofort weiterreichen (Streaming)
                super().append(s)
                if on_status:
                    on_status(s)
        status = _Status()

        course = self.cfg.courses[course_id] if course_id else next(iter(self.cfg.courses.values()))
        r = route(message)
        if r.provider == "skill":
            if r.skill == "help":
                return Answer(HELP, r)
            if r.skill == "check":
                from ..skills.check import run_check
                rep = run_check(self, r.question, course_id=course.id, images=images, on_status=status.append)
                return Answer(rep.render(course.name), r, provider=rep.grader, sources=rep.sources,
                              status=list(status), prerendered=True)
            return Answer(f"`/{r.skill}` ist noch nicht gebaut (kommt in Phase 3–6).", r)
        if not r.question:
            return Answer("Bitte eine Frage eingeben.", r)

        picked, ref_desc = self._gather(course, r.question, history, status)
        prompt = self.build_prompt(course, r, picked, history)

        order = [r.provider] + [p for p in ("codex", "claude") if p != r.provider]
        missing = [p for p in order if p not in self.providers or not self.providers[p].available()]
        for p in missing:
            status.append(f"{p} nicht installiert – übersprungen")
        order = [p for p in order if p not in missing]
        if not order:
            return Answer("Weder `codex` noch `claude` ist installiert/angemeldet.", r, prompt=prompt,
                          status=list(status))
        res = None
        for name in order:
            res = self._call(name, prompt, r, status)
            if res.ok:
                break
            why = "Limit erreicht" if res.rate_limited else f"Fehler: {(res.error or '')[:200]}"
            status.append(f"{name} fehlgeschlagen ({why})")
            if r.rule == "R1":   # ausdrücklich gewähltes Modell: nicht stillschweigend wechseln
                break
        assert res is not None
        if not res.ok:
            return Answer(f"Keine Antwort erhalten. {res.error or ''}".strip(), r, provider=res.provider,
                          sources=[c for c, _ in picked], llm=res, prompt=prompt, status=list(status))

        text, chk = res.text, CheckResult("none", "")
        if r.tool_verify:
            text, chk = check_answer(res.text)
            if chk.status != "none":
                status.append(f"Prüfcode: {chk.badge}")
        return Answer(text, r, provider=res.provider, sources=[c for c, _ in picked], check=chk,
                      reference=ref_desc, llm=res, prompt=prompt, status=list(status))


HELP = """**ETH-Agent – Befehle**

- Einfach fragen, z. B. *Erkläre den Tunneleffekt* oder *Löse Serie 3 Aufgabe 1*.
- `@codex …` / `@claude …` – Modell erzwingen.
- Verweise wie *Serie 4 Aufgabe 3*, *Prüfung Aug 2025 Aufgabe 4*, *Prüfung HS2022 Aufgabe 2* holen die Aufgabe
  und (falls vorhanden) die Musterlösung direkt aus dem Material.
- `/check Serie 3 Aufgabe 1` und darunter dein Lösungsweg: Korrektur mit Punkteschätzung, erstem Fehler und
  Hinweis. Eigene Aufgabe: Aufgabentext, dann eine Zeile `---`, dann dein Versuch. `--loesung` zeigt die Lösung.
- Geplant: `/repeat`, `/practice`, `/diagnose`, `/learn`, `/rewrite`.
"""
