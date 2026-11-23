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
    input_tokens: int | None = None
    output_tokens: int | None = None
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

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def complete(self, prompt: str, timeout: int = 300, effort: str | None = None,
                 images: list[str] | None = None) -> LLMResult:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="eth-agent-codex-") as tmp:
            out = Path(tmp) / "answer.md"
            cmd = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-last-message", str(out)]
            if self.model:
                cmd += ["-m", self.model]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            for img in images or []:
                cmd += ["-i", str(Path(img).resolve())]
            cmd += self.extra_args + [prompt]
            try:
                p = _run(cmd, timeout, cwd=tmp)
            except subprocess.TimeoutExpired:
                return LLMResult(self.name, "", False, time.monotonic() - t0, error=f"Timeout nach {timeout}s")
            except FileNotFoundError:
                return LLMResult(self.name, "", False, 0.0, error="codex nicht installiert")
            text = out.read_text(encoding="utf-8").strip() if out.exists() else ""
            dur = time.monotonic() - t0
            log = (p.stderr or "") + (p.stdout or "")
            tokens = _codex_tokens(log)
            if p.returncode != 0 or not text:
                return LLMResult(self.name, text, False, dur, rate_limited=bool(_RATE_LIMIT.search(log)),
                                 error=log.strip()[-800:] or f"Exit-Code {p.returncode}")
            return LLMResult(self.name, text, True, dur, input_tokens=tokens, raw={"log_tail": log[-2000:]})


def _codex_tokens(log: str) -> int | None:
    m = re.findall(r"tokens used[:\s]*([\d,']+)", log, re.I)
    return int(re.sub(r"\D", "", m[-1])) if m else None


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
                p = _run(cmd, timeout, cwd=tmp, stdin=prompt)
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

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, timeout: int = 300, effort: str | None = None,
                 images: list[str] | None = None) -> LLMResult:
        self.prompts.append(prompt)
        self.efforts.append(effort)
        if self.fail:
            return LLMResult(self.name, "", False, 0.0, rate_limited=self.rate_limited, error="fake failure")
        text = self.reply(prompt) if callable(self.reply) else str(self.reply)
        return LLMResult(self.name, text, True, 0.01, input_tokens=len(prompt) // 4, output_tokens=len(text) // 4)
