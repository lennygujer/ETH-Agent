"""Cloud-Modelle über ihre offiziellen Kommandozeilen-Programme (Abo-Login, keine API-Kosten).

- Codex CLI (ChatGPT Plus):  codex exec --skip-git-repo-check --sandbox read-only ...
- Claude Code (Claude Pro):  claude -p ... --output-format json

Beide laufen in einem leeren temporären Ordner, damit sie keine Dateien sehen oder ändern.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_RATE_LIMIT = re.compile(r"rate.?limit|usage.?limit|too many requests|\b429\b|quota|limit reached", re.I)


@dataclass
class LLMResult:
    provider: str
    text: str
    ok: bool
    duration_s: float
    input_tokens: int | None = None          # alle Eingabe-Tokens inkl. Cache
    output_tokens: int | None = None
    cached_input_tokens: int | None = None   # davon aus dem Cache
    reasoning_tokens: int | None = None      # davon Denk-Tokens (in output_tokens enthalten)
    cost_usd: float | None = None
    rate_limited: bool = False
    error: str | None = None
    raw: dict = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...
    def complete(self, prompt: str, timeout: int = 300, effort: str | None = None,
                 images: list[str] | None = None) -> LLMResult: ...


def _run(cmd: list[str], timeout: int, cwd: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                          cwd=cwd, env=env)


class CodexProvider:
    name = "codex"
    supports_images = True

    def __init__(self, binary: str = "codex", model: str | None = None, extra_args: list[str] | None = None):
        self.binary = binary
        self.model = model
        self.extra_args = extra_args or []
        self.json_events = True    # --json liefert genaue Token-Zahlen; ältere Versionen kennen es nicht

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def complete(self, prompt: str, timeout: int = 300, effort: str | None = None,
                 images: list[str] | None = None) -> LLMResult:
        res = self._complete(prompt, timeout, effort, images)
        if (not res.ok and self.json_events and res.error
                and re.search(r"unexpected argument '--json'|unrecognized.*--json|unknown.*--json", res.error, re.I)):
            self.json_events = False
            res = self._complete(prompt, timeout, effort, images)
        return res

    def _complete(self, prompt: str, timeout: int = 300, effort: str | None = None,
                 images: list[str] | None = None) -> LLMResult:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="eth-agent-codex-") as tmp:
            out = Path(tmp) / "answer.md"
            cmd = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-last-message", str(out)]
            if self.json_events:
                cmd += ["--json"]
            if self.model:
                cmd += ["-m", self.model]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            # Prompt immer über stdin ("-"): keine Längengrenze der Kommandozeile, nicht in der Prozessliste.
            # "-" steht vor -i, damit es nicht als weiterer Bildpfad gelesen wird.
            cmd += self.extra_args + ["-"]
            for img in images or []:
                cmd += ["-i", str(Path(img).resolve())]
            try:
                p = _run(cmd, timeout, cwd=tmp, stdin=clean_prompt(prompt))
            except subprocess.TimeoutExpired:
                return LLMResult(self.name, "", False, time.monotonic() - t0, error=f"Timeout nach {timeout}s")
            except FileNotFoundError:
                return LLMResult(self.name, "", False, 0.0, error="codex nicht installiert")
            text = out.read_text(encoding="utf-8").strip() if out.exists() else ""
            dur = time.monotonic() - t0
            log = (p.stderr or "") + (p.stdout or "")
            u = codex_usage(p.stdout or "")
            if not u:
                total = _codex_tokens(log)       # ohne --json nur eine Gesamtzahl ("tokens used")
                u = {"input_tokens": total} if total is not None else {}
            if p.returncode != 0 or not text:
                return LLMResult(self.name, text, False, dur, rate_limited=bool(_RATE_LIMIT.search(log)),
                                 error=_codex_error(p.stdout or "", log) or f"Exit-Code {p.returncode}", **u)
            return LLMResult(self.name, text, True, dur, raw={"log_tail": log[-2000:]}, **u)


def _codex_tokens(log: str) -> int | None:
    m = re.findall(r"tokens used[:\s]*([\d,']+)", log, re.I)
    return int(re.sub(r"\D", "", m[-1])) if m else None


_USAGE_KEYS = {"input_tokens": "input_tokens", "cached_input_tokens": "cached_input_tokens",
               "output_tokens": "output_tokens", "reasoning_output_tokens": "reasoning_tokens",
               "reasoning_tokens": "reasoning_tokens"}


def _pick_usage(d: dict) -> dict[str, int]:
    return {dst: int(d[src]) for src, dst in _USAGE_KEYS.items() if isinstance(d.get(src), (int, float))}


def codex_usage(stdout: str) -> dict[str, int]:
    """Token-Zahlen aus den JSON-Ereignissen von `codex exec --json`.

    Neuere Versionen: {"type":"turn.completed","usage":{"input_tokens":…,"cached_input_tokens":…,
    "output_tokens":…}} – pro Turn, wird summiert. Ältere: {"msg":{"type":"token_count", …}} mit
    laufender Summe (info.total_token_usage) – der letzte Stand gilt.
    """
    turns: dict[str, int] = {}
    running: dict[str, int] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            for k, v in _pick_usage(ev["usage"]).items():
                turns[k] = turns.get(k, 0) + v
            continue
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else ev
        if msg.get("type") == "token_count":
            info = msg.get("info") if isinstance(msg.get("info"), dict) else None
            total = info.get("total_token_usage") if info else None
            u = _pick_usage(total if isinstance(total, dict) else msg)
            if u:
                running = u
    return turns or running


def _codex_error(stdout: str, log: str) -> str:
    """Fehlermeldung aus den JSON-Ereignissen, sonst Ende des Protokolls."""
    for line in reversed(stdout.splitlines()):
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict) and ev.get("type") in {"error", "turn.failed"}:
            err = ev.get("error") if isinstance(ev.get("error"), dict) else ev
            m = err.get("message") or ev.get("message")
            if m:
                return str(m)[:800]
    return log.strip()[-800:]


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_prompt(prompt: str) -> str:
    """Steuerzeichen aus PDF-Text entfernen (ein Null-Byte bricht jeden Prozessaufruf ab)."""
    return _CONTROL.sub("", prompt)


class ClaudeProvider:
    name = "claude"
    supports_images = False   # claude -p ohne Werkzeuge kann keine Bilddateien lesen

    def __init__(self, binary: str = "claude", model: str | None = None, extra_args: list[str] | None = None):
        self.binary = binary
        self.model = model
        self.extra_args = extra_args or []

    def available(self) -> bool:
        return shutil.which(self.binary) is not None or Path("~/.local/bin/claude").expanduser().exists()

    def _bin(self) -> str:
        if shutil.which(self.binary):
            return self.binary
        local = Path("~/.local/bin/claude").expanduser()
        return str(local) if local.exists() else self.binary

    def complete(self, prompt: str, timeout: int = 300, effort: str | None = None,
                 images: list[str] | None = None) -> LLMResult:
        t0 = time.monotonic()
        cmd = [self._bin(), "-p", "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args
        with tempfile.TemporaryDirectory(prefix="eth-agent-claude-") as tmp:
            try:
                # Prompt über stdin: kein Längenproblem mit der Kommandozeile.
                p = _run(cmd, timeout, cwd=tmp, stdin=clean_prompt(prompt))
            except subprocess.TimeoutExpired:
                return LLMResult(self.name, "", False, time.monotonic() - t0, error=f"Timeout nach {timeout}s")
            except FileNotFoundError:
                return LLMResult(self.name, "", False, 0.0, error="claude nicht installiert")
        dur = time.monotonic() - t0
        log = (p.stdout or "") + (p.stderr or "")
        try:
            data = json.loads(p.stdout)
        except (json.JSONDecodeError, TypeError):
            return LLMResult(self.name, "", False, dur, rate_limited=bool(_RATE_LIMIT.search(log)),
                             error=log.strip()[-800:] or f"Exit-Code {p.returncode}")
        text = (data.get("result") or "").strip()
        usage = data.get("usage") or {}
        is_error = bool(data.get("is_error")) or p.returncode != 0 or not text
        return LLMResult(
            self.name, text, not is_error, dur,
            input_tokens=(usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0) or None,
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cache_read_input_tokens"),
            cost_usd=data.get("total_cost_usd"),
            rate_limited=is_error and bool(_RATE_LIMIT.search(text or log)),
            error=(text or log.strip()[-800:]) if is_error else None,
            raw={k: v for k, v in data.items() if k != "result"},
        )


class FakeProvider:
    """Für Tests und Trockenläufe: gibt eine feste oder berechnete Antwort zurück."""

    supports_images = True

    def __init__(self, name: str = "fake", reply=None, fail: bool = False, rate_limited: bool = False):
        self.name = name
        self.reply = reply or (lambda prompt: "Testantwort.")
        self.fail = fail
        self.rate_limited = rate_limited
        self.prompts: list[str] = []
        self.efforts: list[str | None] = []
        self.images: list[list[str] | None] = []

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, timeout: int = 300, effort: str | None = None,
                 images: list[str] | None = None) -> LLMResult:
        self.prompts.append(prompt)
        self.efforts.append(effort)
        self.images.append(images)
        if self.fail:
            return LLMResult(self.name, "", False, 0.0, rate_limited=self.rate_limited, error="fake failure")
        text = self.reply(prompt) if callable(self.reply) else str(self.reply)
        return LLMResult(self.name, text, True, 0.01, input_tokens=len(prompt) // 4, output_tokens=len(text) // 4)
