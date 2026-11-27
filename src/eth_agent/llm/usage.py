"""Protokoll jedes Cloud-Aufrufs (Kontingent-Überblick, `/verbrauch`, `eth-agent usage`).

Jede Chat-Nachricht ist eine *Anfrage* (Tabelle `requests`); jeder Modellaufruf, den sie auslöst, ein
*Aufruf* (Tabelle `calls`) mit Verweis auf die Anfrage. So lässt sich zeigen, wie viel eine Nachricht pro
Befehl kostet – auch wenn z. B. `/check` drei Aufrufe macht.

Token-Konvention (beide Modelle gleich): `input_tokens` = alle Eingabe-Tokens inkl. Cache,
`cached_input_tokens` = davon aus dem Cache gelesen (zählen bei den Anbietern deutlich weniger),
`reasoning_tokens` = davon Denk-Tokens in der Ausgabe (nur Codex meldet sie).
"""

from __future__ import annotations

import contextvars
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .providers import LLMResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    ts TEXT DEFAULT (datetime('now')),
    provider TEXT, skill TEXT, route_rule TEXT,
    prompt_tokens_est INTEGER, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    duration_s REAL, ok INTEGER, rate_limited INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    ts TEXT DEFAULT (datetime('now')),
    command TEXT, course TEXT
);
"""
_NEW_CALL_COLUMNS = {"request_id": "INTEGER", "cached_input_tokens": "INTEGER", "reasoning_tokens": "INTEGER"}

# Laufende Anfrage. Threads aus einem ThreadPoolExecutor erben das nicht automatisch:
# dort mit contextvars.copy_context() weiterreichen (siehe skills/check.py).
_current_request: contextvars.ContextVar[int | None] = contextvars.ContextVar("eth_agent_request", default=None)

# Interne Skill-Namen -> Befehl, wie ihn der Student sieht
COMMAND_OF_SKILL = {
    "answer": "Frage", "check-solve": "/check", "check-grade": "/check", "probe": "Rückfrage (/check)",
    "diagnose": "/diagnose", "learn": "/learn", "lernen": "/learn", "termin": "Termin", "termine": "Termin",
    "lernplan": "/lernplan",
}


def command_label(skill: str | None) -> str:
    if not skill:
        return "Frage"
    return COMMAND_OF_SKILL.get(skill, f"/{skill}")


@dataclass
class Totals:
    calls: int = 0
    ok: int = 0
    rate_limited: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0


class UsageLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # /check ruft Codex und Claude parallel auf -> Zugriff aus mehreren Threads
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            have = {row[1] for row in self.db.execute("PRAGMA table_info(calls)")}
            for col, typ in _NEW_CALL_COLUMNS.items():
                if col not in have:          # ältere Datenbank ergänzen, Daten bleiben erhalten
                    self.db.execute(f"ALTER TABLE calls ADD COLUMN {col} {typ}")
            self.db.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts)")
            self.db.execute("CREATE INDEX IF NOT EXISTS calls_req ON calls(request_id)")
            self.db.commit()

    # ------------------------------------------------------------------ Schreiben
    @contextmanager
    def request(self, command: str = "Frage") -> Iterator[int]:
        """Eine Chat-Nachricht: alle Aufrufe darin werden ihr zugeordnet."""
        with self.lock:
            cur = self.db.execute("INSERT INTO requests (command) VALUES (?)", (command,))
            self.db.commit()
            rid = int(cur.lastrowid)
        token = _current_request.set(rid)
        try:
            yield rid
        finally:
            _current_request.reset(token)

    def finish_request(self, rid: int, *, fallback: str, course: str | None = None) -> None:
        """Befehl und Fach nachtragen, sobald bekannt. Der Befehl kommt aus dem ersten Modellaufruf
        (eine Antwort in einer laufenden Lektion ist so /learn); ohne Aufruf gilt `fallback`."""
        with self.lock:
            row = self.db.execute("SELECT skill FROM calls WHERE request_id=? ORDER BY id LIMIT 1",
                                  (rid,)).fetchone()
            command = command_label(row[0]) if row else fallback
            self.db.execute("UPDATE requests SET command=?, course=? WHERE id=?", (command, course, rid))
            self.db.commit()

    def record(self, r: LLMResult, *, skill: str, rule: str, prompt_tokens_est: int) -> None:
        with self.lock:
            self.db.execute(
                """INSERT INTO calls (provider, skill, route_rule, prompt_tokens_est, input_tokens,
                   cached_input_tokens, output_tokens, reasoning_tokens, cost_usd, duration_s, ok, rate_limited,
                   error, request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.provider, skill, rule, prompt_tokens_est, r.input_tokens, r.cached_input_tokens,
                 r.output_tokens, r.reasoning_tokens, r.cost_usd, round(r.duration_s, 2), int(r.ok),
                 int(r.rate_limited), (r.error or "")[:500] or None, _current_request.get()))
            self.db.commit()

    # ------------------------------------------------------------------ Lesen
    def summary(self, hours: int = 5) -> list[tuple]:
        return list(self.db.execute(
            """SELECT provider, count(*), sum(ok), sum(rate_limited), sum(coalesce(input_tokens, prompt_tokens_est)),
                      sum(output_tokens), round(sum(coalesce(cost_usd,0)),3)
               FROM calls WHERE ts >= datetime('now', ?) GROUP BY provider""", (f"-{hours} hours",)))

    _TOTALS = """count(*), coalesce(sum(ok),0), coalesce(sum(rate_limited),0),
                 coalesce(sum(coalesce(input_tokens, prompt_tokens_est)),0), coalesce(sum(cached_input_tokens),0),
                 coalesce(sum(output_tokens),0), coalesce(sum(reasoning_tokens),0), coalesce(sum(cost_usd),0),
                 coalesce(sum(duration_s),0)"""

    def by_provider(self, since: str) -> dict[str, Totals]:
        rows = self.db.execute(f"SELECT provider, {self._TOTALS} FROM calls WHERE ts >= ? GROUP BY provider",
                               (since,))
        return {row[0]: Totals(*row[1:]) for row in rows}

    def by_command(self, since: str) -> list[tuple[str, int, dict[str, Totals]]]:
        """[(Befehl, Anzahl Nachrichten, {Modell: Totals})], teuerster Befehl zuerst."""
        msgs = dict(self.db.execute(
            "SELECT coalesce(command,'Frage'), count(*) FROM requests WHERE ts >= ? GROUP BY 1", (since,)))
        per: dict[str, dict[str, Totals]] = {}
        rows = self.db.execute(
            f"""SELECT r.command, c.skill, c.provider, {self._TOTALS}
                FROM calls c LEFT JOIN requests r ON r.id = c.request_id
                WHERE c.ts >= ? GROUP BY 1, 2, 3""", (since,))
        for cmd, skill, prov, *vals in rows:
            cmd = cmd or command_label(skill)       # Aufrufe ohne Anfrage (CLI, ältere Daten)
            t = per.setdefault(cmd, {}).setdefault(prov, Totals())
            for k, v in zip(Totals.__dataclass_fields__, vals):
                setattr(t, k, getattr(t, k) + v)
        out = [(cmd, msgs.get(cmd, 0), per.get(cmd, {})) for cmd in set(msgs) | set(per)]
        return sorted(out, key=lambda x: -sum(t.input_tokens + t.output_tokens for t in x[2].values()))

    def by_day(self, since: str) -> list[tuple[str, str, Totals]]:
        rows = self.db.execute(
            f"""SELECT date(ts, 'localtime'), provider, {self._TOTALS} FROM calls WHERE ts >= ?
                GROUP BY 1, 2 ORDER BY 1 DESC, 2""", (since,))
        return [(d, p, Totals(*vals)) for d, p, *vals in rows]

    def message_count(self, since: str) -> tuple[int, int]:
        """(Nachrichten, davon ohne Cloud-Aufruf)."""
        n, none = self.db.execute(
            """SELECT count(*), coalesce(sum(NOT EXISTS (SELECT 1 FROM calls c WHERE c.request_id = r.id)),0)
               FROM requests r WHERE r.ts >= ?""", (since,)).fetchone()
        return int(n), int(none)

    def since(self, days: int) -> str:
        """UTC-Zeitstempel für 'die letzten n Tage' (n=1: seit heute 00:00 Ortszeit)."""
        return self.db.execute("SELECT datetime('now', 'localtime', 'start of day', ?, 'utc')",
                               (f"-{max(days, 1) - 1} days",)).fetchone()[0]

    def report(self, days: int = 7) -> str:
        return render_report(self, days)


# ---------------------------------------------------------------------- Darstellung
PROVIDER_NAMES = {"codex": "Codex (ChatGPT Plus)", "claude": "Claude (Pro)"}


def _k(n: float) -> str:
    """12345 -> '12.3k'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def _provider_cell(t: Totals) -> str:
    s = f"{_k(t.input_tokens)} → {_k(t.output_tokens)}"
    return s + f" ({t.calls}×)"


def render_report(log: UsageLog, days: int = 7) -> str:
    since = log.since(days)
    span = "heute" if days == 1 else f"letzte {days} Tage"
    msgs, no_cloud = log.message_count(since)
    prov = log.by_provider(since)
    lines = [f"**Verbrauch – {span}**", ""]
    if not prov and not msgs:
        lines.append("Noch keine Nachrichten in diesem Zeitraum protokolliert.")
        return "\n".join(lines)

    lines += ["| Modell | Aufrufe | Eingabe (davon Cache) | Ausgabe (davon Denken) | Limit-Treffer |",
              "|---|---:|---:|---:|---:|"]
    for name in sorted(prov, key=lambda p: (p != "codex", p)):
        t = prov[name]
        cache = f" ({_k(t.cached_input_tokens)})" if t.cached_input_tokens else ""
        think = f" ({_k(t.reasoning_tokens)})" if t.reasoning_tokens else ""
        lines.append(f"| {PROVIDER_NAMES.get(name, name)} | {t.calls} | {_k(t.input_tokens)}{cache} | "
                     f"{_k(t.output_tokens)}{think} | {t.rate_limited or '–'} |")
    if msgs:
        lines += ["", f"{msgs} Nachrichten, davon {no_cloud} ohne Cloud-Aufruf (Lernkarte, Fachwechsel, Termine …)."]

    cmds = [c for c in log.by_command(since) if c[2]]
    if cmds:
        providers = sorted({p for _, _, per in cmds for p in per}, key=lambda p: (p != "codex", p))
        lines += ["", "**Pro Befehl** (Tokens Eingabe → Ausgabe)", "",
                  "| Befehl | Nachrichten | " + " | ".join(PROVIDER_NAMES.get(p, p).split(" ")[0] for p in providers)
                  + " | Ø pro Nachricht |", "|---|---:|" + "---:|" * (len(providers) + 1)]
        for cmd, n, per in cmds:
            cells = [(_provider_cell(per[p]) if p in per else "–") for p in providers]
            total = sum(t.input_tokens + t.output_tokens for t in per.values())
            avg = _k(total / n) if n else "–"
            lines.append(f"| {cmd} | {n or '–'} | " + " | ".join(cells) + f" | {avg} |")

    if days > 1:
        days_rows = log.by_day(since)
        if days_rows:
            lines += ["", "**Pro Tag** (Tokens gesamt, Aufrufe)", ""]
            per_day: dict[str, list[str]] = {}
            for d, p, t in days_rows:
                per_day.setdefault(d, []).append(
                    f"{p} {_k(t.input_tokens + t.output_tokens)} ({t.calls}×)")
            for d, parts in per_day.items():
                lines.append(f"- {d}: " + " · ".join(parts))

    claude = prov.get("claude")
    if claude and claude.cost_usd:
        lines += ["", f"*Claude-Aufrufe entsprächen zu API-Preisen ≈ {claude.cost_usd:.2f} USD; im Pro-Abo bezahlst "
                      "du das nicht extra, es zählt aber gegen dieselben Limits wie Chat und Claude Code.*"]
    lines += ["", "*Cache-Tokens zählen bei beiden Anbietern deutlich weniger gegen das Limit. "
                  "`/verbrauch heute`, `/verbrauch 30` für andere Zeiträume.*"]
    return "\n".join(lines)


def parse_days(arg: str) -> int:
    a = (arg or "").strip().lower()
    if a in {"heute", "today", "tag"}:
        return 1
    if a in {"woche", "week"}:
        return 7
    if a in {"monat", "month"}:
        return 30
    digits = "".join(ch for ch in a if ch.isdigit())
    return max(1, min(int(digits), 365)) if digits else 7
