"""handle(): eine Nachricht -> Routing -> Material -> Prompt -> Modell -> Prüfcode -> Antwort.

Terminal-Chat und (später) der FastAPI-Server rufen dieselbe Funktion auf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..checks.sandbox import CheckResult, check_answer
from ..config import ALL_COURSES, REPO_ROOT, Config, Course
from ..llm.providers import ClaudeProvider, CodexProvider, LLMResult, Provider
from ..llm.usage import UsageLog
from ..retrieval.search import search
from ..retrieval.store import ChunkRow, Store
from .references import resolve
from .routing import SKILLS, Route, est_tokens, route

PROMPTS = REPO_ROOT / "prompts"
_COURSE_PREFIX = re.compile(r"^\s*[/!]([\wäöüÄÖÜ-]+)\b[ \t]*")


_STOP = re.compile(r"^[/!](stop|abbrechen|ende)\b", re.I)
_SKIP = re.compile(r"^[/!](weiter|skip)\b", re.I)
_FULL_FLAG = re.compile(r"\s*--\s*(l(ö|oe)sung|solution|full)\b", re.I)
_FULL_PREFIX = re.compile(r"^\s*[/!]l(ö|oe)sung\b\s*", re.I)
_FULL_WORDS = re.compile(r"\b(ganze|vollst(ä|ae)ndige|komplette|ganzen|vollst(ä|ae)ndigen|kompletten)\s+"
                         r"(l(ö|oe)sung|l(ö|oe)sungsweg|rechnung|herleitung)|\bmusterl(ö|oe)sung\s+zeigen|"
                         r"\bfull solution\b", re.I)


def wants_full_solution(message: str) -> tuple[str, bool]:
    """'--loesung', '/loesung ...' oder 'zeig mir die ganze Lösung' -> (Nachricht ohne Schalter, True)."""
    full = False
    if _FULL_PREFIX.match(message):
        message, full = _FULL_PREFIX.sub("", message, count=1), True
    if _FULL_FLAG.search(message):
        message, full = _FULL_FLAG.sub("", message), True
    if _FULL_WORDS.search(message):
        full = True
    return message.strip(), full


class _Status(list):
    """Statuszeilen sammeln und sofort weiterreichen (Streaming)."""

    def __init__(self, callback=None):
        super().__init__()
        self.callback = callback

    def append(self, s):
        super().append(s)
        if self.callback:
            self.callback(s)


def slug_token(t: str) -> str:
    from ..config import slug
    return slug(t)
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
    names: dict[str, str] = field(default_factory=dict)   # Kurs-ID -> Name für Zitate
    course: Course | None = None  # Fach, in dem geantwortet wurde

    def render(self, course_name: str | None = None) -> str:
        if self.prerendered:
            return self.text.strip()
        parts = [self.text.strip()]
        if self.sources:
            parts.append("---\n**Quellen**\n" + "\n".join(
                f"[{i}] {c.citation(self.names.get(c.course) or course_name)}"
                for i, c in enumerate(self.sources, 1)))
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


def _format_chunk(n: int, c: ChunkRow, course_name: str | dict, label: str | None = None) -> str:
    name = course_name.get(c.course, c.course) if isinstance(course_name, dict) else course_name
    head = f"[{n}] " + (f"{label} " if label else "") + c.citation(name)
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
        from ..learner.model import LearnerStore
        self.learner = LearnerStore(cfg.data_dir / "learner.sqlite")

    @property
    def names(self) -> dict[str, str]:
        return {cid: c.name for cid, c in self.cfg.courses.items()}

    # ------------------------------------------------------------------ Material
    def _gather(self, course: Course, question: str, history: list[Turn],
                status: list[str]) -> tuple[list[tuple[ChunkRow, str | None]], str | None]:
        """Gibt [(Chunk, Label)] und eine Beschreibung des aufgelösten Verweises zurück."""
        picked: list[tuple[ChunkRow, str | None]] = []
        ref_desc = None
        ref = resolve(self.store, course.id, question) if course.id else None
        if course.id and (ref is None or not ref.chunks) and history:
            # Folgefrage ("noch ein Tipp", "zeig die ganze Lösung"): Aufgabe aus dem Verlauf übernehmen
            for t in reversed(history):
                if t.role == "user":
                    prev = resolve(self.store, course.id, t.text)
                    if prev and prev.chunks:
                        ref = prev
                        break
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
        hits = search(self.store, query, course=course.id or None, top_k=self.cfg.retrieval.top_k,
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
    def hints_mode(self, course: Course, r: Route, picked, full: bool) -> bool:
        """Tipps statt Lösung: bei Aufgaben (Verweis oder Rechen-/Beweisauftrag), ausser ganze Lösung gewünscht."""
        if full or course.solution_mode == "full":
            return False
        is_problem = any(lab == "AUFGABE" for _, lab in picked) or r.rule == "R4"
        return is_problem

    def build_prompt(self, course: Course, r: Route, picked, history: list[Turn], hints: bool = False) -> str:
        has_solution = any(lab == "MUSTERLÖSUNG" for _, lab in picked)
        if hints:
            policy = _template("hints.md") + ("\nThe official solution (marked MUSTERLÖSUNG) is included only so that "
                                              "your hints are correct. Never reveal it." if has_solution else "")
        elif has_solution:
            policy = ("Solution policy: an official solution is included (marked MUSTERLÖSUNG). Treat it as the "
                      "reference for correctness, but explain in your own words with all steps, so the student "
                      "understands why each step holds. If you believe the official solution contains an error, "
                      "say so explicitly and explain why.")
        else:
            policy = ("Solution policy: no official solution is available for this question. Work carefully, "
                      "check limits, units and signs yourself.")
        check = _template("check_block.md") if (r.tool_verify and not hints) else ""
        hist = ""
        if history:
            lines = [f"**{'Student' if t.role == 'user' else 'Assistant'}:** {t.text[:1500]}" for t in history[-6:]]
            hist = "## Conversation so far (for context)\n\n" + "\n\n".join(lines)
        context = "\n\n".join(_format_chunk(i, c, self.names, lab) for i, (c, lab) in enumerate(picked, 1)) \
            or "(no matching course material found)"
        cname = course.name if course.id else "several ETH courses (D-ITET); the excerpts show which course they come from"
        return _fill(_template("answer.md"), course_name=cname,
                     output_language_name=LANG_NAMES.get(course.output_language, course.output_language),
                     solution_policy=policy, check_instructions=check, question=r.question,
                     history_section=hist, context=context)

    # ------------------------------------------------------------------ Fach
    def course_list(self, current: Course | None = None) -> str:
        files = {}
        for row in self.store.files():
            files[row["course"]] = files.get(row["course"], 0) + 1
        lines = ["**Fächer** (wechseln mit `/<kürzel>`, alle Fächer: `/alle`)", "",
                 "| Fach | Semester | Kürzel | Dateien im Index |", "|---|---|---|---|"]
        from ..config import auto_aliases, semester_key
        for c in sorted(self.cfg.courses.values(), key=lambda c: (semester_key(c.semester), c.name), reverse=True):
            al = sorted(set(c.aliases) | {a for a in auto_aliases(c.name) if self.cfg.resolve_course(a) is c},
                        key=len)[:3]
            mark = " ← aktiv" if current is not None and current.id == c.id else ""
            lines.append(f"| {c.name}{mark} | {c.semester or '–'} | {', '.join('/' + a for a in al)} | "
                         f"{files.get(c.id, 0)} |")
        return "\n".join(lines)

    def _course_from_token(self, token: str) -> Course | None:
        if slug_token(token) in {"alle", "all"}:
            return ALL_COURSES
        return self.cfg.resolve_course(token)

    def select_course(self, message: str, history: list[Turn], course_id: str | None
                      ) -> tuple[Course, str, Answer | None]:
        """Fach bestimmen: '/physik ...' in dieser Nachricht, sonst letzter Wechsel im Verlauf,
        sonst --course / default_course, sonst alle Fächer."""
        r0 = Route("FACH", "fach", message)
        m = _COURSE_PREFIX.match(message)
        if m and m.group(1).lower() in {"kurse", "faecher", "fächer", "fach", "courses"}:
            current, _, _ = self.select_course("", history, course_id)
            return current, "", Answer(self.course_list(current), r0, prerendered=True)
        if m and m.group(1).lower() not in SKILLS:
            c = self._course_from_token(m.group(1))
            if c is None:
                return ALL_COURSES, "", Answer(
                    f"Unbekanntes Fach oder Befehl `/{m.group(1)}`.\n\n" + self.course_list(), r0, prerendered=True)
            rest = message[m.end():].strip()
            if not rest:
                txt = (f"Fach gewechselt: **{c.label}**. Frag los." if c.id
                       else "Suche jetzt wieder in **allen Fächern**.")
                return c, "", Answer(txt, r0, prerendered=True)
            return c, rest, None
        if course_id:
            return self.cfg.courses[course_id], message, None
        for t in reversed(history):
            if t.role != "user":
                continue
            hm = _COURSE_PREFIX.match(t.text)
            if hm and hm.group(1).lower() not in SKILLS:
                c = self._course_from_token(hm.group(1))
                if c is not None:
                    return c, message, None
        if self.cfg.default_course and self.cfg.default_course in self.cfg.courses:
            return self.cfg.courses[self.cfg.default_course], message, None
        if len(self.cfg.courses) == 1:
            return next(iter(self.cfg.courses.values())), message, None
        return ALL_COURSES, message, None

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
        holder: dict = {}
        ans = self._handle(message, course_id=course_id, history=history, on_status=on_status, images=images,
                           holder=holder)
        ans.names = self.names
        ans.course = holder.get("course")
        return ans

    def _handle(self, message: str, *, course_id: str | None, history: list[Turn] | None,
                on_status: Callable[[str], None] | None, images: list[str] | None, holder: dict) -> Answer:
        history = history or []
        status = _Status(on_status)

        # Laufende Diagnose? (letzte Agenten-Antwort enthielt "Diagnose #d · Frage n")
        from ..skills import diagnose as dg
        pend = dg.pending_diagnosis(history)
        if pend is not None and message.strip():
            did, qn = pend
            low = message.lstrip()
            text = cid = None
            if _STOP.match(low):
                text, cid = dg.stop(self, did)
            elif _SKIP.match(low):
                text, cid = dg.answer(self, did, qn, "", status.append, skip=True)
            elif not low.startswith(("/", "!", "@")):
                text, cid = dg.answer(self, did, qn, message, status.append)
            if text is not None:
                holder["course"] = self.cfg.courses.get(cid) if cid else None
                return Answer(text, Route("DIAGNOSE", "skill", message, skill="diagnose"), status=list(status),
                              prerendered=True)

        # Antwort auf eine Rückfrage? (letzte Agenten-Antwort enthielt "Rückfrage #n", keine neue /Anweisung)
        from ..skills.probe import pending_probe, run_probe_answer
        pid = pending_probe(history)
        if pid is not None and message.strip() and not message.lstrip().startswith(("/", "!", "@")):
            status = _Status(on_status)
            text, cid = run_probe_answer(self, pid, message, status.append)
            holder["course"] = self.cfg.courses.get(cid) if cid else None
            return Answer(text, Route("PROBE", "probe", message), status=list(status), prerendered=True)

        course, message, early = self.select_course(message, history, course_id)
        holder["course"] = course
        if early is not None:
            return early
        message, full = wants_full_solution(message)
        r = route(message)
        if r.skill == "check" and full:
            r.question += " --loesung"     # /check wertet den Schalter selbst aus
        if r.provider == "skill":
            if r.skill == "help":
                return Answer(HELP, r)
            if r.skill in {"lernkarte", "mindmap", "karte"}:
                if not course.id:
                    return Answer("Für die Lernkarte bitte zuerst ein Fach wählen, z. B. `/physik`. Alle Fächer: "
                                  "`/kurse`.", r)
                from ..learner.render import render_map
                from ..skills.check import ensure_skeleton
                ensure_skeleton(self, course.id)
                return Answer(render_map(self.learner, course.id, course.name, r.question), r, prerendered=True)
            if r.skill == "diagnose":
                if not course.id:
                    return Answer("Für `/diagnose` bitte zuerst ein Fach wählen, z. B. `/physik`. Alle Fächer: "
                                  "`/kurse`.", r)
                text = dg.start(self, course, r.question, status.append)
                return Answer(text, r, status=list(status), prerendered=True)
            if r.skill in {"stop", "abbrechen"}:
                did = self.learner.open_diagnosis_for(course.id) if course.id else None
                if did is None:
                    return Answer("Gerade läuft keine Diagnose.", r, prerendered=True)
                return Answer(dg.stop(self, did)[0], r, prerendered=True)
            if r.skill == "weiter":
                return Answer("Rückfrage übersprungen.", r, prerendered=True)
            if r.skill == "check":
                if not course.id:
                    return Answer("Für `/check` bitte zuerst ein Fach wählen, z. B. `/physik`. Alle Fächer: `/kurse`.",
                                  r)
                from ..skills.check import run_check
                rep = run_check(self, r.question, course_id=course.id, images=images, on_status=status.append)
                return Answer(rep.render(course.name), r, provider=rep.grader, sources=rep.sources,
                              status=list(status), prerendered=True)
            return Answer(f"`/{r.skill}` ist noch nicht gebaut (kommt in Phase 3–6).", r)
        if not r.question:
            return Answer("Bitte eine Frage eingeben.", r)

        if course.id:
            status.append(f"Fach: {course.label}")
        picked, ref_desc = self._gather(course, r.question, history, status)
        hints = self.hints_mode(course, r, picked, full)
        if hints:
            status.append("Tipp-Modus (ganze Lösung: --loesung)")
        prompt = self.build_prompt(course, r, picked, history, hints=hints)

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
        if r.tool_verify and not hints:
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
- **Aufgaben:** Standardmässig gibt es zuerst **Tipps** (Ansatz, erster Schritt, typische Falle). *noch ein Tipp*
  geht eine Stufe weiter. Die ganze Lösung mit `--loesung` am Ende oder `/loesung Serie 3 Aufgabe 1`.
- **Fach wählen:** `/physik`, `/analysis`, `/sst` … (gilt für den ganzen Chat). `/kurse` zeigt alle Fächer und
  Kürzel, `/alle` sucht wieder in allen Fächern. Auch kombinierbar: `/physik Erkläre den Tunneleffekt`.
- **Lernkarte:** `/lernkarte` zeigt pro Fach, welche Themen sitzen, wackeln oder fehlen (wird mit jedem `/check`
  genauer). `/lernkarte voll` oder `/lernkarte <thema>` für Details. Stellt der Agent eine **Rückfrage** zu
  deinem Gedankengang, antworte einfach in der nächsten Nachricht (`/weiter` überspringt).
- **Diagnose:** `/diagnose` stellt bis zu 8 kurze, gezielte Fragen über das Fach (`/diagnose tunneling` nur zu
  einem Thema, `/diagnose 5` mit 5 Fragen). Antworte mit Buchstabe oder eigenen Worten plus kurzem Warum;
  «weiss nicht» ist erlaubt und hilfreich. `/weiter` überspringt eine Frage, `/stop` beendet mit Fazit.
- Tipp für `/check` und `/diagnose`: Gedankengang mitschreiben und `Sicherheit: 70%` angeben – beides macht die
  Karte genauer.
- Geplant: `/practice`, `/learn`.
"""
