"""Prüfcode ausführen: eigener Prozess, temporärer Ordner, Zeit- und Speicherlimit, möglichst ohne Netz.

Kein vollwertiger Sicherheits-Sandbox (der Code stammt von Codex/Claude, nicht von Fremden),
aber genug, damit ein fehlerhafter Prüfcode nichts kaputt macht oder hängen bleibt.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

_CHECK_BLOCK = re.compile(r"```python[ \t]+check[ \t]*\n(.*?)```", re.S | re.I)


@dataclass
class CheckResult:
    status: str            # "pass" | "fail" | "error" | "none"
    output: str
    code: str | None = None

    @property
    def badge(self) -> str:
        return {"pass": "Tool ✓", "fail": "Tool ✗", "error": "Prüfcode lief nicht – Antwort ungeprüft",
                "none": "ungeprüft"}[self.status]


def extract_check_block(answer: str) -> tuple[str, str | None]:
    """Trennt den Prüfcode-Block von der Antwort. Gibt (Antwort ohne Block, Code) zurück."""
    m = list(_CHECK_BLOCK.finditer(answer))
    if not m:
        return answer, None
    last = m[-1]
    cleaned = (answer[:last.start()] + answer[last.end():]).rstrip()
    return cleaned, last.group(1)


@functools.lru_cache(maxsize=1)
def _netns_prefix() -> tuple[str, ...]:
    """'unshare -rn' (eigener Netz-Namespace) verwenden, falls auf dem System erlaubt."""
    if not shutil.which("unshare"):
        return ()
    try:
        ok = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return ("unshare", "-rn") if ok else ()


def _limits():  # pragma: no cover - läuft im Kindprozess
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024**2, 10 * 1024**2))


def run_python(code: str, timeout: int = 60) -> CheckResult:
    with tempfile.TemporaryDirectory(prefix="eth-agent-check-") as tmp:
        path = os.path.join(tmp, "check.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp,
               "MPLBACKEND": "Agg", "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "2"}
        cmd = list(_netns_prefix()) + [sys.executable, "-I", path]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=tmp, env=env,
                               preexec_fn=_limits if os.name == "posix" else None)
        except subprocess.TimeoutExpired:
            return CheckResult("error", f"Zeitlimit ({timeout}s) überschritten", code)
    out = (p.stdout + ("\n" + p.stderr if p.stderr else "")).strip()
    if p.returncode == 0:
        return CheckResult("pass", out, code)
    if "AssertionError" in p.stderr:
        return CheckResult("fail", out[-2000:], code)
    return CheckResult("error", out[-2000:], code)


def check_answer(answer: str, timeout: int = 60) -> tuple[str, CheckResult]:
    cleaned, code = extract_check_block(answer)
    if not code:
        return answer, CheckResult("none", "")
    return cleaned, run_python(code, timeout)
