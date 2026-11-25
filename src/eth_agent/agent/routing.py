"""Regelbasiertes Routing (v5: kein lokales Chat-Modell auf dem Server).

Regeln von oben nach unten, die erste passende gewinnt. Alles im Code prüfbar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SKILLS = {"repeat", "check", "practice", "diagnose", "learn", "rewrite", "help", "loesung", "lösung", "lernkarte", "mindmap", "karte", "weiter",
          "stop", "abbrechen", "ende", "plan", "lernen",
          "termin", "termine", "lernplan", "uebungsplan", "übungsplan"}
_NAMED = re.compile(r"^\s*@(codex|gpt|chatgpt|claude)\b[:,]?\s*", re.I)
_SKILL = re.compile(r"^\s*[/!]([a-zäöü]+)\b[ \t]*", re.I)
_HARD = re.compile(
    r"\b(aufgabe|serie|prüfung|pruefung|beweis|beweise|herleit\w*|zeige|berechne\w*|bestimme\w*|löse|loese|"
    r"exam|exercise|problem|derive|prove|show that|calculate|compute|solve|determine)\b", re.I)
_CODE = re.compile(r"```|Traceback \(most recent call last\)|\berror\b.*line \d+|^\s*(def|class|module|entity)\b",
                   re.I | re.M)


@dataclass
class Route:
    rule: str
    provider: str             # "codex" | "claude" | "skill"
    question: str             # Frage ohne Präfix
    skill: str | None = None
    tool_verify: bool = True
    note: str | None = None


def est_tokens(text: str) -> int:
    return len(text) // 4


def route(message: str) -> Route:
    m = _NAMED.match(message)
    if m:
        name = m.group(1).lower()
        return Route("R1", "claude" if name == "claude" else "codex", message[m.end():].strip())
    m = _SKILL.match(message)
    if m and m.group(1).lower() in SKILLS:
        return Route("R2", "skill", message[m.end():].strip(), skill=m.group(1).lower())
    if est_tokens(message) > 15_000:
        return Route("R5", "claude", message, note="langer Text -> Claude")
    if _HARD.search(message):
        return Route("R4", "codex", message)
    if _CODE.search(message):
        return Route("R6", "codex", message)
    return Route("R8", "codex", message, note="Standard")
