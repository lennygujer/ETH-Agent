"""Protokoll jedes Cloud-Aufrufs (für Kontingent-Überblick und spätere Auswertung)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .providers import LLMResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    ts TEXT DEFAULT (datetime('now')),
    provider TEXT, skill TEXT, route_rule TEXT,
    prompt_tokens_est INTEGER, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    duration_s REAL, ok INTEGER, rate_limited INTEGER, error TEXT
);
"""


class UsageLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # /check ruft Codex und Claude parallel auf -> Zugriff aus mehreren Threads
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)

    def record(self, r: LLMResult, *, skill: str, rule: str, prompt_tokens_est: int) -> None:
        with self.lock:
            self._record(r, skill, rule, prompt_tokens_est)

    def _record(self, r: LLMResult, skill: str, rule: str, prompt_tokens_est: int) -> None:
        self.db.execute(
            """INSERT INTO calls (provider, skill, route_rule, prompt_tokens_est, input_tokens,
               output_tokens, cost_usd, duration_s, ok, rate_limited, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (r.provider, skill, rule, prompt_tokens_est, r.input_tokens, r.output_tokens, r.cost_usd,
             round(r.duration_s, 2), int(r.ok), int(r.rate_limited), (r.error or "")[:500] or None))
        self.db.commit()

    def summary(self, hours: int = 5) -> list[tuple]:
        return list(self.db.execute(
            """SELECT provider, count(*), sum(ok), sum(rate_limited), sum(coalesce(input_tokens, prompt_tokens_est)),
                      sum(output_tokens), round(sum(coalesce(cost_usd,0)),3)
               FROM calls WHERE ts >= datetime('now', ?) GROUP BY provider""", (f"-{hours} hours",)))
